from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.api_auth import Principal
from agentic_mesh_v5.cli import main
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.recovery_supervisor import CommandRecoveryLauncher
from agentic_mesh_v5.recovery_supervisor import RecoveryAuthorizationError
from agentic_mesh_v5.recovery_supervisor import RecoveryConflict
from agentic_mesh_v5.recovery_supervisor import RecoveryExecutionError
from agentic_mesh_v5.recovery_supervisor import RecoveryJob
from agentic_mesh_v5.recovery_supervisor import RecoveryResult
from agentic_mesh_v5.recovery_supervisor import RecoverySupervisor
from agentic_mesh_v5.recovery_supervisor import RecoverySupervisorStore
from agentic_mesh_v5.reliability import ReliabilityConflict
from agentic_mesh_v5.reliability import ReliabilityStore
from agentic_mesh_v5.tool_profiles import ToolProfileRegistry


RECOVERY_TOKEN = "recovery-supervisor-test-token"


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for Postgres tests")
    database_name = f"mesh_v5_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    parsed = urlsplit(base_url)
    database_url = urlunsplit(parsed._replace(path=f"/{database_name}"))
    try:
        yield database_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
            )


@pytest.fixture
def recovery_database(postgres_database: str) -> str:
    MigrationRunner(postgres_database).migrate()
    lifecycle = LifecycleStore(postgres_database)
    lifecycle.create_project(
        project_id="alpha", display_name="Alpha", sponsor_ids=("sponsor",)
    )
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering'),
                   ('alpha', 'project-manager', 'project-manager')
            """
        )
    queues = RoleQueueStore(postgres_database)
    queues.create_queue(project_id="alpha", queue_id="engineering", role_id="engineering")
    queues.create_queue(
        project_id="alpha", queue_id="project-manager", role_id="project-manager"
    )
    return postgres_database


def _principal(*, permitted: bool = True) -> Principal:
    return Principal(
        subject="recovery-supervisor",
        projects=frozenset({"alpha"}),
        scopes=frozenset({"recovery:execute"} if permitted else {"read"}),
    )


def _profile_repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "configuration"
    schema = root / "schemas" / "package.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    package = root / "packages" / "tool-profile" / "recovery" / "0.1.0"
    package.mkdir(parents=True)
    profile = {
        "tool_profile": {
            "schema_version": 1,
            "profile_id": "recovery",
            "image": {
                "repository": "agentic-mesh/worker-recovery",
                "tag": "0.1.0",
                "platform": "linux/amd64",
            },
            "capabilities": [
                {"id": "diagnostics.system", "required": True},
                {"id": "structured-output", "required": True},
            ],
            "mounts": [
                {
                    "id": "project-source",
                    "source": "project-source-reference",
                    "target": "/workspace",
                    "access": "read-write",
                    "required": True,
                }
            ],
            "credentials": [
                {
                    "id": "codex-auth",
                    "kind": "oauth-cache",
                    "delivery": "mount",
                    "target": "/mesh/credentials/codex",
                    "required": True,
                }
            ],
            "health": {
                "command": ["agentic-mesh-worker-healthcheck"],
                "interval_seconds": 30,
                "timeout_seconds": 5,
                "failure_threshold": 3,
            },
            "resources": {
                "cpu_millis": 2000,
                "memory_mb": 4096,
                "ephemeral_storage_mb": 8192,
            },
            "launch_policy": {
                "normal_routing": False,
                "allowed_launchers": ["recovery-supervisor"],
            },
        }
    }
    (package / "tool-profile.json").write_text(
        json.dumps(profile, sort_keys=True) + "\n", encoding="utf-8"
    )
    (package / "package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "recovery",
                "kind": "tool-profile",
                "version": "0.1.0",
                "content": ["tool-profile.json"],
                "dependencies": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return root, "tool-profile/recovery@0.1.0"


def _pending_recovery(database_url: str, work_item_id: str = "broken-work"):
    lifecycle = LifecycleStore(database_url)
    lifecycle.create_work_item(
        project_id="alpha",
        work_item_id=work_item_id,
        title="Broken work",
        owner_role_id="engineering",
        actor_id="project-manager",
        correlation_id=f"create-{work_item_id}",
    )
    lifecycle.transition_work_item(
        project_id="alpha",
        work_item_id=work_item_id,
        target_status="active",
        actor_id="project-manager",
        correlation_id=f"start-{work_item_id}",
        expected_version=1,
    )
    reliability = ReliabilityStore(database_url)
    status = reliability.start(
        project_id="alpha",
        work_item_id=work_item_id,
        idempotency_key=f"failure-{work_item_id}",
        failure_category="execution",
        safe_summary="The verified Mesh health check remains unhealthy",
        source_ref=f"evidence://{work_item_id}",
        actor_id="engineering",
    )
    for stage, maximum in (("technical", 3), ("pm_correction", 3)):
        for number in range(1, maximum + 1):
            status = reliability.record_attempt(
                project_id="alpha",
                work_item_id=work_item_id,
                incident_id=status.incident.incident_id,
                attempt_id=f"{work_item_id}-{stage}-{number}",
                stage=stage,
                attempt_number=number,
                outcome="failed",
                actor_id="project-manager" if stage == "pm_correction" else "engineering",
                correction_instruction=(
                    f"Bounded correction {number} for {work_item_id}"
                    if stage == "pm_correction"
                    else None
                ),
                evidence={"source": f"evidence://{work_item_id}/{stage}/{number}"},
            )
    assert status.recovery_request is not None
    return status


@dataclass
class _FakeLauncher:
    result: RecoveryResult
    job: RecoveryJob | None = None
    delay_seconds: float = 0

    def launch(self, job: RecoveryJob) -> RecoveryResult:
        self.job = job
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return self.result


def test_supervisor_runs_exact_goal_with_default_limits_and_resumes_owner(
    recovery_database: str, tmp_path: Path
) -> None:
    pending = _pending_recovery(recovery_database)
    root, reference = _profile_repository(tmp_path)
    launcher = _FakeLauncher(
        RecoveryResult("succeeded", "Mesh health is restored.", "evidence://repair/1", 321)
    )
    supervisor = RecoverySupervisor(
        database_url=recovery_database,
        principal=_principal(),
        owner_id="recovery-1",
        registry=ToolProfileRegistry(root),
        tool_profile_reference=reference,
        launcher=launcher,
    )

    execution = supervisor.execute_once()

    assert execution.status == "completed"
    assert execution.run is not None
    assert execution.run.time_limit_minutes == 120
    assert execution.run.usage_limit == 200_000
    assert launcher.job is not None
    assert launcher.job.exact_goal == pending.recovery_request.exact_goal
    assert launcher.job.project_id == "alpha"
    assert launcher.job.credential_references == ("codex-auth",)
    assert "test-token" not in json.dumps(launcher.job.to_dict())
    final = ReliabilityStore(recovery_database).status(
        "alpha", "broken-work", pending.incident.incident_id
    )
    assert final.incident.status == "recovered"
    assert final.recovery_request.status == "succeeded"
    with psycopg.connect(recovery_database) as connection:
        resumed = connection.execute(
            """
            SELECT COUNT(*) FROM agentic_mesh_v5.queue_items
            WHERE project_id = 'alpha'
              AND idempotency_key = %s
            """,
            (f"reliability:{pending.incident.incident_id}:resume",),
        ).fetchone()[0]
    assert resumed == 1


def test_supervisor_renews_the_smallest_allowed_lease_during_execution(
    recovery_database: str, tmp_path: Path
) -> None:
    pending = _pending_recovery(recovery_database, "heartbeat-work")
    root, reference = _profile_repository(tmp_path)
    launcher = _FakeLauncher(
        RecoveryResult("succeeded", "Repair verified.", "evidence://heartbeat", 10),
        delay_seconds=1.2,
    )
    supervisor = RecoverySupervisor(
        database_url=recovery_database,
        principal=_principal(),
        owner_id="recovery-1",
        registry=ToolProfileRegistry(root),
        tool_profile_reference=reference,
        launcher=launcher,
        lease_seconds=1,
    )

    execution = supervisor.execute_once()

    assert execution.status == "completed"
    final = ReliabilityStore(recovery_database).status(
        "alpha", "heartbeat-work", pending.incident.incident_id
    )
    assert final.incident.status == "recovered"


def test_claim_requires_recovery_scope_and_reclaims_same_run(
    recovery_database: str, tmp_path: Path
) -> None:
    pending = _pending_recovery(recovery_database, "reclaim-work")
    root, reference = _profile_repository(tmp_path)
    profile = ToolProfileRegistry(root).load_for_launch(
        reference, launcher="recovery-supervisor", via_normal_routing=False
    )
    store = RecoverySupervisorStore(recovery_database)

    with pytest.raises(RecoveryAuthorizationError):
        store.claim(principal=_principal(permitted=False), owner_id="bad", profile=profile)
    first = store.claim(
        principal=_principal(), owner_id="recovery-1", profile=profile, lease_seconds=1
    )
    assert first is not None
    assert store.claim(
        principal=_principal(), owner_id="recovery-2", profile=profile
    ) is None
    with psycopg.connect(recovery_database) as connection:
        with pytest.raises(psycopg.Error, match="immutable"):
            connection.execute(
                """
                UPDATE agentic_mesh_v5.recovery_requests
                SET exact_goal = 'changed'
                WHERE project_id = 'alpha' AND recovery_request_id = %s
                """,
                (pending.recovery_request.recovery_request_id,),
            )
        connection.rollback()
        connection.execute(
            """
            UPDATE agentic_mesh_v5.recovery_supervisor_runs
            SET lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE project_id = 'alpha' AND recovery_request_id = %s
            """,
            (pending.recovery_request.recovery_request_id,),
        )
    second = store.claim(
        principal=_principal(), owner_id="recovery-2", profile=profile, lease_seconds=30
    )
    assert second is not None
    assert second.run_id == first.run_id
    assert second.claim_count == 2
    with pytest.raises(RecoveryConflict, match="lease"):
        store.heartbeat(principal=_principal(), run=first)


def test_overall_deadline_is_a_verified_failed_recovery(
    recovery_database: str, tmp_path: Path
) -> None:
    pending = _pending_recovery(recovery_database, "deadline-work")
    root, reference = _profile_repository(tmp_path)
    profile = ToolProfileRegistry(root).load_for_launch(
        reference, launcher="recovery-supervisor", via_normal_routing=False
    )
    store = RecoverySupervisorStore(recovery_database)
    run = store.claim(
        principal=_principal(), owner_id="recovery-1", profile=profile
    )
    assert run is not None
    with psycopg.connect(recovery_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.recovery_supervisor_runs
            SET lease_expires_at = clock_timestamp() - interval '2 seconds',
                deadline_at = clock_timestamp() - interval '1 second'
            WHERE project_id = 'alpha' AND run_id = %s
            """,
            (run.run_id,),
        )

    assert store.reconcile(_principal()) == 1

    late_result = store.report(
        principal=_principal(),
        run=run,
        result=RecoveryResult(
            "succeeded", "Late launcher result.", "evidence://late", 17
        ),
    )
    assert late_result.outcome == "failed"
    assert late_result.usage_used == 0

    final = ReliabilityStore(recovery_database).status(
        "alpha", "deadline-work", pending.incident.incident_id
    )
    assert final.incident.status == "terminal_eligible"
    assert final.recovery_request.evidence["verification_ref"] == (
        f"supervisor://time-limit/{run.run_id}"
    )


def test_report_is_durable_before_policy_and_usage_limit_is_verified_failure(
    recovery_database: str, tmp_path: Path
) -> None:
    pending = _pending_recovery(recovery_database, "limit-work")
    with pytest.raises(ReliabilityConflict, match="verified supervisor"):
        ReliabilityStore(recovery_database).record_attempt(
            project_id="alpha",
            work_item_id="limit-work",
            incident_id=pending.incident.incident_id,
            attempt_id="direct-recovery-bypass",
            stage="recovery",
            attempt_number=1,
            outcome="failed",
            actor_id="project-manager",
            evidence={"source": "evidence://unverified"},
        )
    root, reference = _profile_repository(tmp_path)
    profile = ToolProfileRegistry(root).load_for_launch(
        reference, launcher="recovery-supervisor", via_normal_routing=False
    )
    store = RecoverySupervisorStore(recovery_database)
    run = store.claim(
        principal=_principal(),
        owner_id="recovery-1",
        profile=profile,
        usage_limit=10,
    )
    assert run is not None

    reported = store.report(
        principal=_principal(),
        run=run,
        result=RecoveryResult("succeeded", "Repair appears healthy.", "evidence://repair", 11),
    )

    assert reported.status == "reported"
    assert reported.outcome == "failed"
    assert reported.verification_ref == f"supervisor://usage-limit/{run.run_id}"
    repeated = store.report(
        principal=_principal(),
        run=run,
        result=RecoveryResult(
            "succeeded", "Repair appears healthy.", "evidence://repair", 11
        ),
    )
    assert repeated.reported_at == reported.reported_at
    with pytest.raises(RecoveryConflict, match="different result"):
        store.report(
            principal=_principal(),
            run=run,
            result=RecoveryResult("failed", "Different result.", "evidence://other", 1),
        )
    with psycopg.connect(recovery_database) as connection:
        with pytest.raises(psycopg.Error, match="verified supervisor"):
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.failure_attempts
                    (project_id, attempt_id, incident_id, ordinal, stage,
                     stage_attempt, outcome, actor_id, evidence,
                     request_fingerprint)
                VALUES ('alpha', %s, %s, 7, 'recovery', 1, 'failed',
                        'direct-database-writer', '{"fake": true}'::jsonb,
                        repeat('c', 64))
                """,
                (run.run_id, pending.incident.incident_id),
            )
    before = ReliabilityStore(recovery_database).status(
        "alpha", "limit-work", pending.incident.incident_id
    )
    assert before.incident.status == "active"
    assert before.recovery_request.status == "pending"
    assert store.reconcile(_principal()) == 1
    after = ReliabilityStore(recovery_database).status(
        "alpha", "limit-work", pending.incident.incident_id
    )
    assert after.incident.status == "terminal_eligible"
    assert after.recovery_request.status == "failed"
    assert store.reconcile(_principal()) == 0
    with pytest.raises(ValueError, match="verification_ref"):
        store.report(
            principal=_principal(),
            run=reported,
            result=RecoveryResult("failed", "No repair.", "", 1),
        )
    with pytest.raises(ValueError, match="restricted content"):
        store.report(
            principal=_principal(),
            run=reported,
            result=RecoveryResult(
                "failed",
                "password=not-a-safe-secret",
                "evidence://unsafe",
                1,
            ),
        )


def test_standalone_cli_completes_recovery_without_control_api(
    recovery_database: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    pending = _pending_recovery(recovery_database, "cli-work")
    root, reference = _profile_repository(tmp_path)
    launcher_script = tmp_path / "launcher.py"
    launcher_script.write_text(
        """
import json, sys
job = json.load(sys.stdin)
assert job['exact_goal']
print(json.dumps({
    'outcome': 'succeeded',
    'safe_summary': 'Standalone repair verified.',
    'usage_used': 12,
    'verification_ref': 'evidence://standalone-repair'
}))
""".lstrip(),
        encoding="utf-8",
    )
    command_file = tmp_path / "launcher-command.json"
    command_file.write_text(
        json.dumps({"argv": [sys.executable, str(launcher_script)]}), encoding="utf-8"
    )
    token_file = tmp_path / "recovery-token"
    token_file.write_text(RECOVERY_TOKEN, encoding="utf-8")
    principals_file = tmp_path / "principals.json"
    principals_file.write_text(
        json.dumps(
            {
                "principals": [
                    {
                        "subject": "recovery-supervisor",
                        "token_sha256": hashlib.sha256(RECOVERY_TOKEN.encode()).hexdigest(),
                        "projects": ["alpha"],
                        "scopes": ["recovery:execute"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTIC_MESH_V5_DATABASE_URL", recovery_database)
    monkeypatch.setenv("AGENTIC_MESH_V5_API_PRINCIPALS_FILE", str(principals_file))
    monkeypatch.setenv("AGENTIC_MESH_V5_API_URL", "http://127.0.0.1:1")

    exit_code = main(
        [
            "--json",
            "recovery-run-once",
            "--config-root",
            str(root),
            "--tool-profile",
            reference,
            "--launcher-command-file",
            str(command_file),
            "--token-file",
            str(token_file),
            "--owner",
            "standalone-1",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "completed"
    assert "lease_token" not in json.dumps(output)
    final = ReliabilityStore(recovery_database).status(
        "alpha", "cli-work", pending.incident.incident_id
    )
    assert final.incident.status == "recovered"


def test_command_launcher_normalizes_postgres_time_and_reports_invalid_utf8(
    tmp_path: Path
) -> None:
    launcher_script = tmp_path / "invalid-output.py"
    launcher_script.write_text(
        "import sys\nsys.stdout.buffer.write(b'\\xff')\n", encoding="utf-8"
    )
    job = RecoveryJob(
        project_id="alpha",
        recovery_request_id="request-1",
        incident_id="incident-1",
        work_item_id="work-1",
        run_id="run-1",
        exact_goal="Restore the verified fixture.",
        deadline_at="2099-01-01 00:00:00+00",
        usage_limit=100,
        tool_profile_reference="tool-profile/recovery@0.1.0",
        tool_profile_digest="a" * 64,
        image="agentic-mesh/worker-recovery:0.1.0",
        mount_references=(),
        credential_references=(),
    )

    with pytest.raises(RecoveryExecutionError, match="invalid UTF-8 output"):
        CommandRecoveryLauncher([sys.executable, str(launcher_script)]).launch(job)
