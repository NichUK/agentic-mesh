from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from agentic_mesh_v5.continuation import ContinuationConflict
from agentic_mesh_v5.continuation import ContinuationAuthorizationError
from agentic_mesh_v5.continuation import ContinuationError
from agentic_mesh_v5.continuation import ContinuationMonitor
from agentic_mesh_v5.continuation import LOGICAL_PM_ID
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.queues import RoleQueueStore


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
def continuation_database(postgres_database: str):
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('alpha', 'Alpha'), ('bravo', 'Bravo'), ('charlie', 'Charlie')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_sponsors(project_id, sponsor_id)
            VALUES ('alpha', 'alpha-sponsor'), ('bravo', 'bravo-sponsor')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'project-manager', 'project-manager'),
                   ('alpha', 'engineering', 'engineering'),
                   ('bravo', 'project-manager', 'project-manager'),
                   ('bravo', 'engineering', 'engineering'),
                   ('charlie', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('alpha', 'eng-1', 'engineering', 'running')
            """
        )
        items = (
            ("alpha", "orphan", "active", {}),
            ("alpha", "progressing", "active", {}),
            ("alpha", "expired", "active", {}),
            ("alpha", "recovering", "active", {}),
            ("alpha", "ambiguous", "active", {
                "material_ambiguity": True,
                "sponsor_question": "Should this project prefer option A or B?",
            }),
            ("alpha", "gate-alpha", "active", {}),
            ("bravo", "gate-bravo", "active", {}),
            ("charlie", "blocked", "active", {}),
            ("charlie", "sponsorless-ambiguity", "active", {
                "material_ambiguity": True,
                "sponsor_question": "Which option should this project use?",
            }),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO agentic_mesh_v5.work_items
                    (project_id, work_item_id, assigned_role_id, title, status, payload)
                VALUES (%s, %s, 'engineering', %s, %s, %s)
                """,
                [(p, w, w, s, Jsonb(payload)) for p, w, s, payload in items],
            )
    queues = RoleQueueStore(postgres_database)
    queues.create_queue(
        project_id="alpha", queue_id="project-manager", role_id="project-manager"
    )
    queues.create_queue(
        project_id="bravo", queue_id="project-manager", role_id="project-manager"
    )
    queues.create_queue(
        project_id="alpha", queue_id="engineering", role_id="engineering"
    )
    queues.enqueue(
        project_id="alpha", queue_id="engineering",
        queue_item_id="progressing-item", work_item_id="progressing",
        idempotency_key="progressing-item", payload={},
    )
    queues.enqueue(
        project_id="alpha", queue_id="engineering",
        queue_item_id="expired-item", work_item_id="expired",
        idempotency_key="expired-item", payload={},
    )
    expired = queues.claim(
        project_id="alpha", queue_id="engineering",
        owner_instance_id="eng-1", lease_seconds=300,
    )
    assert expired.queue_item.work_item_id == "progressing"
    queues.complete(
        project_id="alpha", lease_id=expired.lease_id,
        lease_token=expired.lease_token,
    )
    expired = queues.claim(
        project_id="alpha", queue_id="engineering",
        owner_instance_id="eng-1", lease_seconds=300,
    )
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.leases
            SET acquired_at = clock_timestamp() - interval '2 seconds',
                heartbeat_at = clock_timestamp() - interval '2 seconds',
                expires_at = clock_timestamp() - interval '1 second'
            WHERE project_id = 'alpha' AND lease_id = %s
            """,
            (expired.lease_id,),
        )
        connection.execute(
            """
            UPDATE agentic_mesh_v5.queue_items SET status = 'ready'
            WHERE project_id = 'alpha' AND queue_item_id = 'progressing-item'
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.failure_incidents
                (project_id, incident_id, work_item_id, owner_role_id,
                 idempotency_key, request_fingerprint, failure_category,
                 safe_summary, source_ref, next_stage, next_attempt_number,
                 started_by)
            VALUES ('alpha', 'recovering-incident', 'recovering', 'engineering',
                    'recovering-incident', repeat('a', 64), 'execution',
                    'Mesh repair is required', 'evidence://recovering',
                    'recovery', 1, 'project-manager')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.recovery_requests
                (project_id, recovery_request_id, incident_id, work_item_id,
                 exact_goal)
            VALUES ('alpha', 'recovery-request', 'recovering-incident',
                    'recovering', 'Restore the Mesh and record evidence')
            """
        )
    lifecycle = LifecycleStore(postgres_database)
    lifecycle.open_gate(
        project_id="alpha", work_item_id="gate-alpha", gate_id="gate-alpha",
        gate_type="sponsor", requested_by="project-manager",
        sponsor_ids=("alpha-sponsor",), correlation_id="gate-alpha",
        expected_version=1,
    )
    lifecycle.open_gate(
        project_id="bravo", work_item_id="gate-bravo", gate_id="gate-bravo",
        gate_type="sponsor", requested_by="project-manager",
        sponsor_ids=("bravo-sponsor",), correlation_id="gate-bravo",
        expected_version=1,
    )
    return postgres_database, ContinuationMonitor(postgres_database)


def test_one_global_sweep_routes_orphans_and_respects_gates(
    continuation_database,
) -> None:
    database_url, monitor = continuation_database
    claim = monitor.claim(owner_id="pm-process-1", lease_seconds=60)

    result = monitor.sweep(
        owner_id=claim.owner_id, lease_token=claim.lease_token
    )
    observed = {
        (item.project_id, item.work_item_id): item for item in result.observations
    }

    assert result.logical_pm_id == LOGICAL_PM_ID
    assert observed[("alpha", "orphan")].disposition == "pm_routed"
    assert observed[("alpha", "progressing")].disposition == "progressing"
    assert observed[("alpha", "recovering")].disposition == "progressing"
    assert observed[("alpha", "expired")].disposition == "pm_routed"
    assert observed[("alpha", "gate-alpha")].disposition == "waiting_sponsor"
    assert observed[("bravo", "gate-bravo")].disposition == "waiting_sponsor"
    assert observed[("alpha", "ambiguous")].disposition == "sponsor_question"
    assert "option A or B" in observed[("alpha", "ambiguous")].detail
    assert observed[("charlie", "blocked")].disposition == "routing_blocked"
    sponsorless = observed[("charlie", "sponsorless-ambiguity")]
    assert sponsorless.disposition == "routing_blocked"
    assert "no configured sponsor" in sponsorless.detail
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.gates"
        ).fetchone()[0] == 3
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE queue_id = 'project-manager'
            """
        ).fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.audit_records
            WHERE action = 'continuation.routing_blocked'
            """
        ).fetchone()[0] == 2

    repeated = monitor.sweep(
        owner_id=claim.owner_id, lease_token=claim.lease_token
    )
    assert repeated.sweep_count == 2
    repeated_ambiguous = next(
        item for item in repeated.observations
        if item.project_id == "alpha" and item.work_item_id == "ambiguous"
    )
    assert repeated_ambiguous.disposition == "sponsor_question"
    assert "option A or B" in repeated_ambiguous.detail
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.gates"
        ).fetchone()[0] == 3
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE queue_id = 'project-manager'
            """
        ).fetchone()[0] == 2


def test_monitor_claim_is_singleton_and_restart_is_idempotent(
    continuation_database,
) -> None:
    database_url, monitor = continuation_database

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(
            pool.map(
                lambda owner: _try_claim(monitor, owner),
                ("pm-process-1", "pm-process-2"),
            )
        )
    claims = [item for item in attempts if item is not None]
    assert len(claims) == 1
    first = claims[0]
    assert monitor.claim(owner_id=first.owner_id, lease_seconds=60) == first
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.pm_monitor_lease
            SET acquired_at = clock_timestamp() - interval '2 seconds',
                heartbeat_at = clock_timestamp() - interval '2 seconds',
                expires_at = clock_timestamp() - interval '1 second'
            WHERE monitor_id = %s
            """,
            (LOGICAL_PM_ID,),
        )
    same_owner = monitor.claim(owner_id=first.owner_id, lease_seconds=60)
    monitor.sweep(owner_id=same_owner.owner_id, lease_token=same_owner.lease_token)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.pm_monitor_lease
            SET acquired_at = clock_timestamp() - interval '2 seconds',
                heartbeat_at = clock_timestamp() - interval '2 seconds',
                expires_at = clock_timestamp() - interval '1 second'
            WHERE monitor_id = %s
            """,
            (LOGICAL_PM_ID,),
        )
    restarted = monitor.claim(owner_id="pm-process-restarted", lease_seconds=60)
    assert restarted.logical_pm_id == first.logical_pm_id
    with pytest.raises(ContinuationAuthorizationError, match="lease is invalid"):
        monitor.heartbeat(owner_id=first.owner_id, lease_token=first.lease_token)
    monitor.sweep(owner_id=restarted.owner_id, lease_token=restarted.lease_token)
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE queue_id = 'project-manager'
            """
        ).fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.audit_records
            WHERE action = 'pm-monitor.taken-over'
            """
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.audit_records
            WHERE action = 'pm-monitor.claimed'
            """
        ).fetchone()[0] == 2


def test_sweep_rolls_back_pm_route_when_observation_fails(
    continuation_database, monkeypatch
) -> None:
    database_url, monitor = continuation_database
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.work_items SET status = 'completed'
            WHERE work_item_id <> 'orphan'
            """
        )
    claim = monitor.claim(owner_id="pm-process-1", lease_seconds=60)

    def fail_observation(*_args, **_kwargs):
        raise RuntimeError("crash after route")

    monkeypatch.setattr(monitor, "_record_observation", fail_observation)
    with pytest.raises(ContinuationError, match="monitor operation failed"):
        monitor.sweep(owner_id=claim.owner_id, lease_token=claim.lease_token)
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE queue_id = 'project-manager'
            """
        ).fetchone()[0] == 0
        assert monitor.status().sweep_count == 0


def _try_claim(monitor: ContinuationMonitor, owner: str):
    try:
        return monitor.claim(owner_id=owner, lease_seconds=60)
    except ContinuationConflict:
        return None
