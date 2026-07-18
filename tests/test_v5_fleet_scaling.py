from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import os
from threading import Barrier, Lock
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest
from fastapi.testclient import TestClient

from agentic_mesh_v5.api import create_app
from agentic_mesh_v5.api_auth import TokenAuthorizer
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.fleet import FleetAction
from agentic_mesh_v5.fleet import FleetConflict
from agentic_mesh_v5.fleet import FleetScaler
from agentic_mesh_v5.fleet import ScalingPolicy
from agentic_mesh_v5.queues import RoleQueueStore


class FakeSupervisor:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.attempts: list[FleetAction] = []
        self.applied: dict[str, FleetAction] = {}
        self._lock = Lock()

    def apply(self, action: FleetAction) -> None:
        with self._lock:
            self.attempts.append(action)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("private supervisor failure")
            self.applied.setdefault(action.action_id, action)


class ConcurrentSupervisor(FakeSupervisor):
    def __init__(self) -> None:
        super().__init__()
        self._barrier = Barrier(2)

    def apply(self, action: FleetAction) -> None:
        self._barrier.wait(timeout=5)
        super().apply(action)


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
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


@pytest.fixture
def fleet_database(postgres_database: str) -> str:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('alpha', 'Alpha')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering'),
                   ('alpha', 'project-manager', 'project-manager')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status, started_at,
                 heartbeat_at)
            VALUES
                ('alpha', 'eng-1', 'engineering', 'running',
                 clock_timestamp(), clock_timestamp()),
                ('alpha', 'eng-2', 'engineering', 'hibernated',
                 clock_timestamp(), clock_timestamp()),
                ('alpha', 'eng-3', 'engineering', 'hibernated',
                 clock_timestamp(), clock_timestamp()),
                ('alpha', 'pm-1', 'project-manager', 'running',
                 clock_timestamp(), clock_timestamp())
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items
                (project_id, work_item_id, assigned_role_id, title, status)
            SELECT 'alpha', 'work-' || value, 'engineering',
                   'Work ' || value, 'active'
            FROM generate_series(1, 10) AS value
            """
        )
    queues = RoleQueueStore(postgres_database)
    queues.create_queue(
        project_id="alpha", queue_id="engineering", role_id="engineering"
    )
    return postgres_database


def _policy(**changes) -> ScalingPolicy:
    values = {
        "project_id": "alpha",
        "role_id": "engineering",
        "min_warm_instances": 0,
        "max_instances": 3,
        "scale_after_seconds": 60,
        "idle_grace_seconds": 300,
        "hibernation_enabled": True,
    }
    values.update(changes)
    return ScalingPolicy(**values)


def _enqueue(
    database_url: str,
    *,
    item: int,
    age_seconds: int = 0,
) -> None:
    RoleQueueStore(database_url).enqueue(
        project_id="alpha",
        queue_id="engineering",
        queue_item_id=f"queue-{item}",
        work_item_id=f"work-{item}",
        idempotency_key=f"queue-{item}",
        payload={},
        available_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


def test_scale_to_zero_wakes_one_stable_instance_without_consuming_work(
    fleet_database: str,
) -> None:
    with psycopg.connect(fleet_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_instances
            SET status = 'hibernated', hibernated_at = clock_timestamp()
            WHERE role_id = 'engineering'
            """
        )
    _enqueue(fleet_database, item=1)
    supervisor = FakeSupervisor()
    scaler = FleetScaler(fleet_database, supervisor)
    scaler.configure(_policy())

    result = scaler.reconcile("alpha")

    assert len(result.actions) == 1
    assert result.actions[0].action.action == "wake"
    assert result.actions[0].action.instance_id == "eng-1"
    assert result.actions[0].status == "completed"
    with psycopg.connect(fleet_database) as connection:
        assert connection.execute(
            """
            SELECT status FROM agentic_mesh_v5.role_instances
            WHERE project_id = 'alpha' AND instance_id = 'eng-1'
            """
        ).fetchone()[0] == "running"
        assert connection.execute(
            """
            SELECT status FROM agentic_mesh_v5.queue_items
            WHERE queue_item_id = 'queue-1'
            """
        ).fetchone()[0] == "ready"


def test_busy_role_scales_after_threshold_and_stops_at_limit(
    fleet_database: str,
) -> None:
    queues = RoleQueueStore(fleet_database)
    _enqueue(fleet_database, item=1)
    first = queues.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=300,
    )
    assert first is not None
    _enqueue(fleet_database, item=2, age_seconds=59)
    supervisor = FakeSupervisor()
    scaler = FleetScaler(fleet_database, supervisor)
    scaler.configure(_policy())

    assert scaler.reconcile("alpha").actions == ()
    with psycopg.connect(fleet_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.queue_items
            SET available_at = clock_timestamp() - interval '61 seconds'
            WHERE queue_item_id = 'queue-2'
            """
        )
    second_wake = scaler.reconcile("alpha")
    assert [item.action.instance_id for item in second_wake.actions] == ["eng-2"]
    second = queues.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-2",
        lease_seconds=300,
    )
    assert second is not None
    _enqueue(fleet_database, item=3, age_seconds=61)
    third_wake = scaler.reconcile("alpha")
    assert [item.action.instance_id for item in third_wake.actions] == ["eng-3"]
    assert scaler.reconcile("alpha").actions == ()
    with psycopg.connect(fleet_database) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.role_instances
            WHERE project_id = 'alpha' AND role_id = 'engineering'
              AND status = 'running'
            """
        ).fetchone()[0] == 3
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.leases
            WHERE released_at IS NULL
            """
        ).fetchone()[0] == 2


def test_hibernation_fails_closed_until_every_safe_point_blocker_clears(
    fleet_database: str,
) -> None:
    with psycopg.connect(fleet_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_instances
            SET status = 'running', idle_since = clock_timestamp() - interval '301 seconds'
            WHERE role_id = 'engineering'
            """
        )
    queues = RoleQueueStore(fleet_database)
    _enqueue(fleet_database, item=1)
    lease = queues.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=300,
    )
    assert lease is not None
    with psycopg.connect(fleet_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.thread_affinities
                (project_id, work_item_id, role_id, conversation_id,
                 provider_id, thread_id, last_instance_id, prompt_digest,
                 active_operation_id, active_instance_id, active_started_at)
            VALUES ('alpha', 'work-2', 'engineering', 'conversation-2',
                    'fake', 'thread-2', 'eng-2', %s,
                    'operation-2', 'eng-2', clock_timestamp())
            """,
            ("a" * 64,),
        )
        event_id = connection.execute(
            """
            INSERT INTO agentic_mesh_v5.events
                (project_id, aggregate_type, aggregate_id, event_type, payload,
                 work_item_id, actor_id, correlation_id)
            VALUES ('alpha', 'role-instance', 'eng-3', 'test.pending', '{}',
                    'work-3', 'test', 'pending-eng-3')
            RETURNING event_id
            """
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.outbox
                (project_id, event_id, topic, payload, idempotency_key)
            VALUES ('alpha', %s, 'test', %s, 'pending-eng-3')
            """,
            (event_id, Jsonb({"role_instance_id": "eng-3"})),
        )
    supervisor = FakeSupervisor()
    scaler = FleetScaler(fleet_database, supervisor)
    scaler.configure(_policy())

    assert scaler.reconcile("alpha").actions == ()
    queues.complete(
        project_id="alpha",
        lease_id=lease.lease_id,
        lease_token=lease.lease_token,
    )
    with psycopg.connect(fleet_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.thread_affinities
            SET active_operation_id = NULL, active_instance_id = NULL,
                active_started_at = NULL
            WHERE project_id = 'alpha' AND work_item_id = 'work-2'
            """
        )
        connection.execute(
            """
            UPDATE agentic_mesh_v5.outbox SET dispatched_at = clock_timestamp()
            WHERE idempotency_key = 'pending-eng-3'
            """
        )
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_instances
            SET idle_since = clock_timestamp() - interval '301 seconds'
            WHERE role_id = 'engineering'
            """
        )

    hibernated = scaler.reconcile("alpha")

    assert {item.action.instance_id for item in hibernated.actions} == {
        "eng-1", "eng-2", "eng-3"
    }
    assert {item.status for item in hibernated.actions} == {"completed"}
    with psycopg.connect(fleet_database) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.role_instances
            WHERE role_id = 'engineering' AND status = 'hibernated'
            """
        ).fetchone()[0] == 3
        affinity = connection.execute(
            """
            SELECT provider_id, thread_id, prompt_digest, last_instance_id
            FROM agentic_mesh_v5.thread_affinities
            WHERE project_id = 'alpha' AND work_item_id = 'work-2'
            """
        ).fetchone()
        assert affinity == ("fake", "thread-2", "a" * 64, "eng-2")
        assert connection.execute(
            """
            SELECT status FROM agentic_mesh_v5.queue_items
            WHERE queue_item_id = 'queue-1'
            """
        ).fetchone()[0] == "completed"


def test_minimum_warm_and_project_manager_guardrails_are_enforced(
    fleet_database: str,
) -> None:
    supervisor = FakeSupervisor()
    scaler = FleetScaler(fleet_database, supervisor)
    with pytest.raises(ValueError, match="project-manager requires"):
        scaler.configure(
            _policy(
                role_id="project-manager",
                min_warm_instances=0,
                max_instances=1,
            )
        )
    pm = scaler.configure(
        _policy(
            role_id="project-manager",
            min_warm_instances=1,
            max_instances=1,
            hibernation_enabled=False,
        )
    )
    assert pm.min_warm_instances == 1
    with pytest.raises(FleetConflict, match="configured instance pool"):
        scaler.configure(_policy(max_instances=4))
    scaler.configure(_policy(min_warm_instances=1))
    with psycopg.connect(fleet_database) as connection:
        assert connection.execute(
            """
            SELECT object_type FROM agentic_mesh_v5.audit_records
            WHERE action = 'fleet.policy_configured'
              AND object_id = 'project-manager'
            ORDER BY audit_id DESC LIMIT 1
            """
        ).fetchone() == ("role-scaling-policy",)
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_instances
            SET status = 'running', idle_since = clock_timestamp() - interval '301 seconds'
            WHERE role_id = 'engineering'
            """
        )
    result = scaler.reconcile("alpha")
    assert len(result.actions) == 2
    with psycopg.connect(fleet_database) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.role_instances
            WHERE role_id = 'engineering' AND status = 'running'
            """
        ).fetchone()[0] == 1


def test_failed_and_concurrent_reconciliation_reuses_one_action_id(
    fleet_database: str, monkeypatch,
) -> None:
    with psycopg.connect(fleet_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_instances SET status = 'hibernated'
            WHERE role_id = 'engineering'
            """
        )
    _enqueue(fleet_database, item=1)
    failing = FakeSupervisor(fail_once=True)
    scaler = FleetScaler(fleet_database, failing)
    scaler.configure(_policy())

    record_failure = scaler._record_failure
    monkeypatch.setattr(
        scaler,
        "_record_failure",
        lambda _action: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    first = scaler.reconcile("alpha")
    monkeypatch.setattr(scaler, "_record_failure", record_failure)
    second = scaler.reconcile("alpha")

    assert first.actions[0].status == "failed"
    assert second.actions[0].status == "completed"
    assert first.actions[0].action.action_id == second.actions[0].action.action_id
    assert "private" not in first.actions[0].detail
    assert "evidence unavailable" in first.actions[0].detail
    with psycopg.connect(fleet_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_instances
            SET status = 'hibernated', lifecycle_action_id = NULL,
                lifecycle_reason = 'reset for concurrency test'
            WHERE role_id = 'engineering'
            """
        )
        connection.execute(
            """
            DELETE FROM agentic_mesh_v5.outbox;
            DELETE FROM agentic_mesh_v5.events;
            """
        )
    concurrent = ConcurrentSupervisor()
    scalers = (
        FleetScaler(fleet_database, concurrent),
        FleetScaler(fleet_database, concurrent),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: item.reconcile("alpha"), scalers))

    action_ids = {
        result.actions[0].action.action_id for result in results
    }
    assert len(action_ids) == 1
    assert {result.actions[0].status for result in results} == {
        "completed", "superseded"
    }
    assert len(concurrent.applied) == 1


def test_api_lifespan_automatically_reconciles_when_supervisor_is_installed(
    fleet_database: str,
) -> None:
    with psycopg.connect(fleet_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_instances SET status = 'hibernated'
            WHERE role_id = 'engineering'
            """
        )
    _enqueue(fleet_database, item=1)
    supervisor = FakeSupervisor()
    FleetScaler(fleet_database, supervisor).configure(_policy())
    token = "fleet-loop-test-token"
    authorizer = TokenAuthorizer(
        [
            {
                "subject": "operator",
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "projects": ["*"],
                "scopes": ["read", "write"],
            }
        ]
    )
    app = create_app(
        fleet_database,
        authorizer=authorizer,
        fleet_supervisor=supervisor,
        fleet_reconcile_interval_seconds=0.05,
    )

    with TestClient(app):
        deadline = time.monotonic() + 2
        status = "hibernated"
        while time.monotonic() < deadline and status != "running":
            with psycopg.connect(fleet_database) as connection:
                status = connection.execute(
                    """
                    SELECT status FROM agentic_mesh_v5.role_instances
                    WHERE project_id = 'alpha' AND instance_id = 'eng-1'
                    """
                ).fetchone()[0]
            if status != "running":
                time.sleep(0.02)

    assert status == "running"
    assert [action.instance_id for action in supervisor.applied.values()] == ["eng-1"]
