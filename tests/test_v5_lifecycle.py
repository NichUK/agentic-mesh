from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import load_migrations
from agentic_mesh_v5.events import EventStore
from agentic_mesh_v5.lifecycle import LifecycleAuthorizationError
from agentic_mesh_v5.lifecycle import LifecycleConflict
from agentic_mesh_v5.lifecycle import LifecycleNotFound
from agentic_mesh_v5.lifecycle import LifecycleStore


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
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
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
def lifecycle_database(postgres_database: str) -> tuple[str, LifecycleStore]:
    MigrationRunner(postgres_database).migrate()
    store = LifecycleStore(postgres_database)
    store.create_project(
        project_id="alpha",
        display_name="Alpha",
        sponsor_ids=("sponsor-1", "sponsor-2"),
    )
    store.create_project(
        project_id="bravo",
        display_name="Bravo",
        sponsor_ids=("bravo-sponsor",),
    )
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering'),
                   ('bravo', 'engineering', 'engineering')
            """
        )
    return postgres_database, store


def _create_work(
    store: LifecycleStore,
    *,
    project_id: str = "alpha",
    work_item_id: str = "work-1",
):
    return store.create_work_item(
        project_id=project_id,
        work_item_id=work_item_id,
        title=f"Work {work_item_id}",
        owner_role_id="engineering",
        actor_id="project-manager",
        correlation_id=f"corr-{work_item_id}",
    )


def _activate(store: LifecycleStore, work_item_id: str = "work-1"):
    return store.transition_work_item(
        project_id="alpha",
        work_item_id=work_item_id,
        target_status="active",
        actor_id="project-manager",
        correlation_id=f"corr-{work_item_id}",
        expected_version=1,
    )


def _open_gate(store: LifecycleStore, work_item_id: str = "work-1"):
    return store.open_gate(
        project_id="alpha",
        work_item_id=work_item_id,
        gate_id=f"gate-{work_item_id}",
        gate_type="sponsor",
        requested_by="project-manager",
        sponsor_ids=("sponsor-1", "sponsor-2"),
        correlation_id=f"corr-{work_item_id}",
        expected_version=2,
        evidence={"proposal": "doc://proposal"},
    )


def test_project_requires_unique_sponsors(postgres_database: str) -> None:
    MigrationRunner(postgres_database).migrate()
    store = LifecycleStore(postgres_database)

    with pytest.raises(ValueError, match="at least one sponsor"):
        store.create_project(project_id="alpha", display_name="Alpha", sponsor_ids=())
    with pytest.raises(ValueError, match="must be unique"):
        store.create_project(
            project_id="alpha",
            display_name="Alpha",
            sponsor_ids=("sponsor", "sponsor"),
        )


def test_v3_migration_backfills_existing_gate_correlation(
    postgres_database: str,
) -> None:
    migrations = load_migrations()
    MigrationRunner(postgres_database, migrations=migrations[:2]).migrate()
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('legacy', 'Legacy')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items(project_id, work_item_id, title)
            VALUES ('legacy', 'work-legacy', 'Legacy work')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.gates
                (project_id, gate_id, work_item_id, gate_type, requested_by)
            VALUES ('legacy', 'gate-legacy', 'work-legacy', 'sponsor', 'pm')
            """
        )

    MigrationRunner(postgres_database, migrations=migrations).migrate()

    with psycopg.connect(postgres_database) as connection:
        correlation = connection.execute(
            """
            SELECT correlation_id FROM agentic_mesh_v5.gates
            WHERE project_id = 'legacy' AND gate_id = 'gate-legacy'
            """
        ).fetchone()[0]
    assert correlation == "legacy-gate:gate-legacy"


def test_create_and_activate_work_are_atomic_and_correlated(lifecycle_database) -> None:
    database_url, store = lifecycle_database
    created = _create_work(store)
    active = _activate(store)

    assert created.status == "new"
    assert created.owner_role_id == "engineering"
    assert created.version == 1
    assert active.status == "active"
    assert active.version == 2
    events = EventStore(database_url).read("alpha")
    assert [item.event_type for item in events] == ["work.created", "work.active"]
    assert all(item.correlation_id == "corr-work-1" for item in events)
    with psycopg.connect(database_url) as connection:
        outbox_count = connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.outbox WHERE project_id = 'alpha'"
        ).fetchone()[0]
    assert outbox_count == 2


def test_invalid_and_stale_transitions_mutate_nothing(lifecycle_database) -> None:
    database_url, store = lifecycle_database
    _create_work(store)

    with pytest.raises(LifecycleConflict, match="invalid work item transition"):
        store.transition_work_item(
            project_id="alpha",
            work_item_id="work-1",
            target_status="completed",
            actor_id="project-manager",
            correlation_id="corr-work-1",
            expected_version=1,
            reason="not active",
        )
    _activate(store)
    with pytest.raises(LifecycleConflict, match="version is stale"):
        store.transition_work_item(
            project_id="alpha",
            work_item_id="work-1",
            target_status="completed",
            actor_id="project-manager",
            correlation_id="corr-work-1",
            expected_version=1,
            reason="stale",
        )

    assert store.get_work_item("alpha", "work-1").status == "active"
    assert len(EventStore(database_url).read("alpha")) == 2


@pytest.mark.parametrize("terminal", ["completed", "error"])
def test_terminal_outcomes_are_distinct_and_immutable(
    lifecycle_database, terminal: str
) -> None:
    _database_url, store = lifecycle_database
    _create_work(store)
    _activate(store)

    terminal_item = store.transition_work_item(
        project_id="alpha",
        work_item_id="work-1",
        target_status=terminal,
        actor_id="project-manager",
        correlation_id="corr-work-1",
        expected_version=2,
        reason=f"terminal {terminal}",
        evidence={"artifact": "doc://acceptance"},
    )

    assert terminal_item.status == terminal
    assert terminal_item.terminal_reason == f"terminal {terminal}"
    assert terminal_item.terminal_evidence == {"artifact": "doc://acceptance"}
    with pytest.raises(LifecycleConflict, match="invalid work item transition"):
        store.transition_work_item(
            project_id="alpha",
            work_item_id="work-1",
            target_status="active",
            actor_id="project-manager",
            correlation_id="corr-work-1",
            expected_version=3,
        )


def test_gate_preserves_owner_and_retains_approval_evidence(lifecycle_database) -> None:
    _database_url, store = lifecycle_database
    _create_work(store)
    _activate(store)
    gate = _open_gate(store)
    gated = store.get_work_item("alpha", "work-1")

    assert gate.status == "pending"
    assert gate.evidence == {"proposal": "doc://proposal"}
    assert gated.status == "gated"
    assert gated.owner_role_id == "engineering"
    assert gated.version == 3

    approval = store.decide_gate(
        project_id="alpha",
        gate_id="gate-work-1",
        sponsor_id="sponsor-1",
        decision="approved",
        rationale="Proceed",
        evidence={"card": "teams://approval/1"},
    )
    resumed = store.get_work_item("alpha", "work-1")

    assert approval.approver_id == "sponsor-1"
    assert approval.decision == "approved"
    assert approval.rationale == "Proceed"
    assert approval.evidence == {"card": "teams://approval/1"}
    assert approval.decided_at is not None
    assert resumed.status == "active"
    assert resumed.owner_role_id == "engineering"
    assert resumed.version == 4


def test_rejection_resumes_same_owner_for_rework(lifecycle_database) -> None:
    _database_url, store = lifecycle_database
    _create_work(store)
    _activate(store)
    _open_gate(store)

    store.decide_gate(
        project_id="alpha",
        gate_id="gate-work-1",
        sponsor_id="sponsor-2",
        decision="rejected",
        rationale="Revise scope",
    )

    assert store.get_gate("alpha", "gate-work-1").status == "rejected"
    resumed = store.get_work_item("alpha", "work-1")
    assert (resumed.status, resumed.owner_role_id) == ("active", "engineering")


def test_duplicate_gate_id_is_conflict_and_rolls_back(lifecycle_database) -> None:
    _database_url, store = lifecycle_database
    _create_work(store, work_item_id="work-1")
    _activate(store, work_item_id="work-1")
    _open_gate(store, work_item_id="work-1")
    _create_work(store, work_item_id="work-2")
    _activate(store, work_item_id="work-2")

    with pytest.raises(LifecycleConflict, match="gate already exists"):
        store.open_gate(
            project_id="alpha",
            work_item_id="work-2",
            gate_id="gate-work-1",
            gate_type="sponsor",
            requested_by="project-manager",
            sponsor_ids=("sponsor-1",),
            correlation_id="corr-work-2",
            expected_version=2,
        )

    work = store.get_work_item("alpha", "work-2")
    assert (work.status, work.version, work.owner_role_id) == (
        "active",
        2,
        "engineering",
    )


def test_unauthorized_and_cross_project_decisions_mutate_nothing(
    lifecycle_database,
) -> None:
    _database_url, store = lifecycle_database
    _create_work(store)
    _activate(store)
    _open_gate(store)

    with pytest.raises(LifecycleAuthorizationError, match="not authorized"):
        store.decide_gate(
            project_id="alpha",
            gate_id="gate-work-1",
            sponsor_id="bravo-sponsor",
            decision="approved",
            rationale="foreign",
        )
    with pytest.raises(LifecycleNotFound, match="gate not found"):
        store.decide_gate(
            project_id="bravo",
            gate_id="gate-work-1",
            sponsor_id="bravo-sponsor",
            decision="approved",
            rationale="cross-project",
        )

    assert store.get_gate("alpha", "gate-work-1").status == "pending"
    assert store.get_work_item("alpha", "work-1").status == "gated"


def test_concurrent_sponsor_decisions_resolve_gate_once(lifecycle_database) -> None:
    database_url, store = lifecycle_database
    _create_work(store)
    _activate(store)
    _open_gate(store)

    def decide(sponsor: str, decision: str) -> str:
        try:
            return store.decide_gate(
                project_id="alpha",
                gate_id="gate-work-1",
                sponsor_id=sponsor,
                decision=decision,
                rationale=f"{decision} concurrently",
            ).decision or "none"
        except LifecycleConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda args: decide(*args),
                (("sponsor-1", "approved"), ("sponsor-2", "rejected")),
            )
        )

    assert outcomes.count("conflict") == 1
    assert len(set(outcomes) & {"approved", "rejected"}) == 1
    assert store.get_work_item("alpha", "work-1").version == 4
    with psycopg.connect(database_url) as connection:
        decided = connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.approvals
            WHERE project_id = 'alpha' AND gate_id = 'gate-work-1'
              AND decision IS NOT NULL
            """
        ).fetchone()[0]
    assert decided == 1


def test_event_failure_rolls_back_terminal_transition(lifecycle_database) -> None:
    database_url, store = lifecycle_database
    _create_work(store)
    _activate(store)

    with pytest.raises(DatabaseError, match="event transaction failed"):
        store.transition_work_item(
            project_id="alpha",
            work_item_id="work-1",
            target_status="completed",
            actor_id="project-manager",
            correlation_id="corr-work-1",
            expected_version=2,
            reason="bad evidence",
            evidence={"not-json": object()},
        )

    current = store.get_work_item("alpha", "work-1")
    assert (current.status, current.version) == ("active", 2)
    assert [item.event_type for item in EventStore(database_url).read("alpha")] == [
        "work.created",
        "work.active",
    ]
