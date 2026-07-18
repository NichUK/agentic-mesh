from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.lifecycle import LifecycleConflict
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.reliability import ReliabilityConflict
from agentic_mesh_v5.reliability import ReliabilityStore


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
def reliability_database(postgres_database: str) -> tuple[str, LifecycleStore]:
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
    queues.create_queue(
        project_id="alpha", queue_id="engineering", role_id="engineering"
    )
    queues.create_queue(
        project_id="alpha", queue_id="project-manager", role_id="project-manager"
    )
    return postgres_database, lifecycle


def _active_work(lifecycle: LifecycleStore, work_item_id: str) -> None:
    lifecycle.create_work_item(
        project_id="alpha",
        work_item_id=work_item_id,
        title=f"Work {work_item_id}",
        owner_role_id="engineering",
        actor_id="project-manager",
        correlation_id=f"corr-{work_item_id}",
    )
    lifecycle.transition_work_item(
        project_id="alpha",
        work_item_id=work_item_id,
        target_status="active",
        actor_id="project-manager",
        correlation_id=f"corr-{work_item_id}",
        expected_version=1,
    )


def _start(store: ReliabilityStore, work_item_id: str):
    return store.start(
        project_id="alpha",
        work_item_id=work_item_id,
        idempotency_key=f"failure-{work_item_id}",
        failure_category="execution",
        safe_summary="The verified build remains unhealthy",
        source_ref=f"evidence://{work_item_id}",
        actor_id="engineering",
    )


def _attempt(
    store: ReliabilityStore,
    work_item_id: str,
    incident_id: str,
    stage: str,
    number: int,
    outcome: str,
):
    correction = (
        f"Correction {work_item_id} number {number}: change the bounded repair approach"
        if stage == "pm_correction"
        else None
    )
    return store.record_attempt(
        project_id="alpha",
        work_item_id=work_item_id,
        incident_id=incident_id,
        attempt_id=f"{work_item_id}-{stage}-{number}",
        stage=stage,
        attempt_number=number,
        outcome=outcome,
        correction_instruction=correction,
        evidence={"source": f"evidence://{work_item_id}/{stage}/{number}"},
        actor_id="project-manager" if stage == "pm_correction" else "engineering",
    )


def test_complete_failure_chain_is_required_before_terminal_error(
    reliability_database,
) -> None:
    database_url, lifecycle = reliability_database
    _active_work(lifecycle, "terminal-work")
    store = ReliabilityStore(database_url)
    started = _start(store, "terminal-work")
    incident_id = started.incident.incident_id

    assert (started.incident.next_stage, started.incident.next_attempt_number) == (
        "technical",
        1,
    )
    with pytest.raises(LifecycleConflict, match="exhausted retry"):
        lifecycle.transition_work_item(
            project_id="alpha",
            work_item_id="terminal-work",
            target_status="error",
            actor_id="project-manager",
            correlation_id="terminal-early",
            expected_version=2,
            reason="too early",
        )
    with pytest.raises(LifecycleConflict, match="failure incident"):
        lifecycle.transition_work_item(
            project_id="alpha",
            work_item_id="terminal-work",
            target_status="completed",
            actor_id="project-manager",
            correlation_id="completed-too-early",
            expected_version=2,
            reason="incident still active",
        )

    for number in range(1, 4):
        status = _attempt(
            store, "terminal-work", incident_id, "technical", number, "failed"
        )
    assert (status.incident.next_stage, status.incident.next_attempt_number) == (
        "pm_correction",
        1,
    )
    with psycopg.connect(database_url) as connection:
        pm_instruction = connection.execute(
            """
            SELECT payload #>> '{reliability,required_action}'
            FROM agentic_mesh_v5.queue_items
            WHERE project_id = 'alpha' AND work_item_id = 'terminal-work'
              AND queue_id = 'project-manager'
            ORDER BY created_at LIMIT 1
            """
        ).fetchone()[0]
    assert "distinct, minimal" in pm_instruction
    assert "over-engineer" in pm_instruction
    for number in range(1, 4):
        status = _attempt(
            store, "terminal-work", incident_id, "pm_correction", number, "failed"
        )
    assert status.recovery_request is not None
    assert status.recovery_request.status == "pending"
    assert "Leave the Mesh in a verified working state" in status.recovery_request.exact_goal
    assert [item.ordinal for item in status.attempts] == list(range(1, 7))

    with pytest.raises(LifecycleConflict, match="exhausted retry"):
        lifecycle.transition_work_item(
            project_id="alpha",
            work_item_id="terminal-work",
            target_status="error",
            actor_id="project-manager",
            correlation_id="terminal-before-recovery",
            expected_version=2,
            reason="still too early",
        )

    status = _attempt(
        store, "terminal-work", incident_id, "recovery", 1, "failed"
    )
    assert status.incident.status == "terminal_eligible"
    assert status.recovery_request.status == "failed"
    assert [item.ordinal for item in status.attempts] == list(range(1, 8))

    def finish_terminal(index: int):
        try:
            return lifecycle.transition_work_item(
                project_id="alpha",
                work_item_id="terminal-work",
                target_status="error",
                actor_id="project-manager@example.com",
                correlation_id=f"terminal-authorized-{index}",
                expected_version=2,
                reason="recovery exhausted",
                evidence={"acceptance": "evidence://terminal"},
            )
        except LifecycleConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        terminal_results = list(pool.map(finish_terminal, range(2)))
    completed = [item for item in terminal_results if item is not None]
    assert len(completed) == 1
    terminal = completed[0]
    assert terminal.status == "error"
    assert terminal.terminal_evidence == {
        "acceptance": "evidence://terminal",
        "failure_incident_id": incident_id,
    }
    assert store.status("alpha", "terminal-work", incident_id).incident.status == "terminal"
    with pytest.raises(LifecycleConflict, match="invalid work item transition"):
        lifecycle.transition_work_item(
            project_id="alpha",
            work_item_id="terminal-work",
            target_status="active",
            actor_id="project-manager",
            correlation_id="terminal-cannot-resume",
            expected_version=3,
        )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.events
            WHERE project_id = 'alpha' AND aggregate_id = %s
              AND event_type LIKE 'reliability.%%'
            """,
            (incident_id,),
        ).fetchone()[0] == 8
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.outbox
            WHERE project_id = 'alpha' AND topic = 'reliability.changed'
            """
        ).fetchone()[0] == 8
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.events
            WHERE project_id = 'alpha' AND work_item_id = 'terminal-work'
              AND event_type = 'work.error'
            """
        ).fetchone()[0] == 1

    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """
                UPDATE agentic_mesh_v5.failure_attempts SET actor_id = 'changed'
                WHERE project_id = 'alpha' AND incident_id = %s
                """,
                (incident_id,),
            )
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """
                DELETE FROM agentic_mesh_v5.failure_attempts
                WHERE project_id = 'alpha' AND incident_id = %s
                """,
                (incident_id,),
            )


def test_success_at_every_stage_recovers_and_recovery_resumes_once(
    reliability_database,
) -> None:
    database_url, lifecycle = reliability_database
    store = ReliabilityStore(database_url)
    stages = [
        ("technical", 1),
        ("technical", 2),
        ("technical", 3),
        ("pm_correction", 1),
        ("pm_correction", 2),
        ("pm_correction", 3),
        ("recovery", 1),
    ]
    for target_ordinal, (target_stage, target_number) in enumerate(stages, start=1):
        work_item_id = f"success-{target_ordinal}"
        _active_work(lifecycle, work_item_id)
        incident_id = _start(store, work_item_id).incident.incident_id
        for stage, number in stages[:target_ordinal]:
            status = _attempt(
                store,
                work_item_id,
                incident_id,
                stage,
                number,
                "succeeded" if (stage, number) == (target_stage, target_number) else "failed",
            )
        assert status.incident.status == "recovered"
        assert lifecycle.get_work_item("alpha", work_item_id).status == "active"
        restarted = ReliabilityStore(database_url).status(
            "alpha", work_item_id, incident_id
        )
        assert restarted == status
        if target_ordinal == 1:
            completed = lifecycle.transition_work_item(
                project_id="alpha",
                work_item_id=work_item_id,
                target_status="completed",
                actor_id="project-manager",
                correlation_id=f"complete-{work_item_id}",
                expected_version=2,
                reason="retry succeeded",
            )
            assert completed.status == "completed"
            assert _start(store, work_item_id) == status
        if target_stage == "recovery":
            assert status.recovery_request.status == "succeeded"
            with psycopg.connect(database_url) as connection:
                assert connection.execute(
                    """
                    SELECT count(*) FROM agentic_mesh_v5.queue_items
                    WHERE project_id = 'alpha' AND work_item_id = %s
                      AND idempotency_key = %s
                    """,
                    (work_item_id, f"reliability:{incident_id}:resume"),
                ).fetchone()[0] == 1


def test_idempotency_distinct_corrections_concurrency_and_route_rollback(
    reliability_database,
) -> None:
    database_url, lifecycle = reliability_database
    _active_work(lifecycle, "concurrent-work")
    store = ReliabilityStore(database_url)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: _start(store, "concurrent-work"), range(2)))
    incident_id = results[0].incident.incident_id
    assert results[0] == results[1]
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.failure_incidents
            WHERE project_id = 'alpha' AND work_item_id = 'concurrent-work'
            """
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE project_id = 'alpha' AND work_item_id = 'concurrent-work'
            """
        ).fetchone()[0] == 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempt_results = list(
            pool.map(
                lambda _value: _attempt(
                    store, "concurrent-work", incident_id, "technical", 1, "failed"
                ),
                range(2),
            )
        )
    first, repeated = attempt_results
    assert first == repeated
    with pytest.raises(ReliabilityConflict, match="different result"):
        store.record_attempt(
            project_id="alpha",
            work_item_id="concurrent-work",
            incident_id=incident_id,
            attempt_id="concurrent-work-technical-1",
            stage="technical",
            attempt_number=1,
            outcome="succeeded",
            actor_id="engineering",
        )
    _attempt(store, "concurrent-work", incident_id, "technical", 2, "failed")
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_queues SET paused = true
            WHERE project_id = 'alpha' AND role_id = 'project-manager'
            """
        )
    with pytest.raises(ReliabilityConflict, match="route is unavailable"):
        _attempt(store, "concurrent-work", incident_id, "technical", 3, "failed")
    rolled_back = store.status("alpha", "concurrent-work", incident_id)
    assert len(rolled_back.attempts) == 2
    assert (rolled_back.incident.next_stage, rolled_back.incident.next_attempt_number) == (
        "technical",
        3,
    )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.events
            WHERE project_id = 'alpha' AND aggregate_id = %s
              AND event_type LIKE 'reliability.%%'
            """,
            (incident_id,),
        ).fetchone()[0] == 3
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_queues SET paused = false
            WHERE project_id = 'alpha' AND role_id = 'project-manager'
            """
        )
    pm = _attempt(store, "concurrent-work", incident_id, "technical", 3, "failed")
    correction = _attempt(
        store, "concurrent-work", incident_id, "pm_correction", 1, "failed"
    )
    assert pm.incident.next_stage == "pm_correction"
    with pytest.raises(ReliabilityConflict, match="must be distinct"):
        store.record_attempt(
            project_id="alpha",
            work_item_id="concurrent-work",
            incident_id=incident_id,
            attempt_id="duplicate-correction",
            stage="pm_correction",
            attempt_number=2,
            outcome="failed",
            correction_instruction=correction.attempts[-1].correction_instruction,
            actor_id="project-manager",
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """
                UPDATE agentic_mesh_v5.failure_incidents
                SET next_stage = 'recovery', next_attempt_number = 2
                WHERE project_id = 'alpha' AND incident_id = %s
                """,
                (incident_id,),
            )
