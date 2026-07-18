from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import load_migrations
from agentic_mesh_v5.database_operations import MaintenanceStore
from agentic_mesh_v5.progress import ProgressConflict
from agentic_mesh_v5.progress import ProgressDraft
from agentic_mesh_v5.progress import ProgressError
from agentic_mesh_v5.progress import ProgressNotFound
from agentic_mesh_v5.progress import ProgressSensitiveContent
from agentic_mesh_v5.progress import ProgressStore
from agentic_mesh_v5.read_models import ReadModelStore


SYNTHETIC_KEY = "sk-" + "a" * 32


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required")
    database_name = f"mesh_v5_progress_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
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


def _seed(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('alpha', 'Alpha'), ('bravo', 'Bravo')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering'),
                   ('bravo', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('alpha', 'eng-1', 'engineering', 'running'),
                   ('alpha', 'eng-2', 'engineering', 'running'),
                   ('bravo', 'bravo-eng-1', 'engineering', 'running')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items
                (project_id, work_item_id, assigned_role_id, title)
            VALUES ('alpha', 'work-1', 'engineering', 'First'),
                   ('alpha', 'work-2', 'engineering', 'Second'),
                   ('bravo', 'work-1', 'engineering', 'Foreign')
            """
        )


@pytest.fixture
def progress_database(postgres_database: str) -> tuple[str, ProgressStore]:
    MigrationRunner(postgres_database).migrate()
    _seed(postgres_database)
    return postgres_database, ProgressStore(postgres_database)


def _draft(**changes: object) -> ProgressDraft:
    values: dict[str, object] = {
        "project_id": "alpha",
        "work_item_id": "work-1",
        "role_instance_id": "eng-1",
        "checkpoint_id": "checkpoint-1",
        "expected_previous_sequence": 0,
        "status": "working",
        "goal": "Deliver the bounded progress slice",
        "step": "Implement the durable checkpoint",
        "completed_action": "Defined the contract",
        "activity": "Writing focused tests",
        "blocker": None,
        "next_action": "Run the Postgres suite",
        "safe_summary": "Checkpoint storage is being implemented.",
    }
    values.update(changes)
    return ProgressDraft(**values)  # type: ignore[arg-type]


def test_complete_checkpoint_is_stored_and_projected_without_rewriting(
    progress_database: tuple[str, ProgressStore],
) -> None:
    database_url, store = progress_database

    record = store.record(_draft())

    assert record.sequence == 1
    assert record.safe_summary == "Checkpoint storage is being implemented."
    assert record.completed_action == "Defined the contract"
    assert record.activity == "Writing focused tests"
    assert record.blocker is None
    assert store.read("alpha", "checkpoint-1") == record
    with psycopg.connect(database_url) as connection:
        event = connection.execute(
            """
            SELECT payload FROM agentic_mesh_v5.read_model_events
            WHERE project_id = 'alpha' AND domain = 'progress'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()[0]
    assert event["goal"] == record.goal
    assert event["step"] == record.step
    assert event["next_action"] == record.next_action
    assert event["safe_summary"] == record.safe_summary
    live = ReadModelStore(database_url).snapshot("alpha")["domains"]["progress"]
    assert live[0]["safe_summary"] == record.safe_summary
    assert live[0]["activity"] == record.activity


def test_idempotent_retry_survives_newer_progress_and_conflicts_fail_closed(
    progress_database: tuple[str, ProgressStore],
) -> None:
    database_url, store = progress_database
    first_draft = _draft()
    first = store.record(first_draft)
    second = store.record(
        _draft(
            checkpoint_id="checkpoint-2",
            expected_previous_sequence=1,
            step="Verify the durable checkpoint",
        )
    )

    assert store.record(first_draft) == first
    assert second.sequence == 2
    with pytest.raises(ProgressConflict, match="different progress"):
        store.record(_draft(goal="Changed replay payload"))
    with pytest.raises(ProgressConflict, match="stale"):
        store.record(_draft(checkpoint_id="checkpoint-3"))
    with psycopg.connect(database_url) as connection:
        count = connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.progress"
        ).fetchone()[0]
        event_count = connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.read_model_events
            WHERE domain = 'progress'
            """
        ).fetchone()[0]
    assert count == 2
    assert event_count == 2


def test_concurrent_same_sequence_has_one_winner_without_gap(
    progress_database: tuple[str, ProgressStore],
) -> None:
    database_url, store = progress_database
    drafts = (
        _draft(checkpoint_id="checkpoint-a"),
        _draft(checkpoint_id="checkpoint-b"),
    )

    def record(draft: ProgressDraft) -> str:
        try:
            return f"recorded:{store.record(draft).sequence}"
        except ProgressConflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(record, drafts))

    assert sorted(results) == ["recorded:1", "stale"]
    with psycopg.connect(database_url) as connection:
        sequences = connection.execute(
            "SELECT sequence FROM agentic_mesh_v5.progress ORDER BY sequence"
        ).fetchall()
    assert sequences == [(1,)]


@pytest.mark.parametrize(
    "changes",
    [
        {"role_instance_id": "missing"},
        {"work_item_id": "missing"},
        {"role_instance_id": "eng-1", "project_id": "bravo", "work_item_id": "missing"},
        {"project_id": "alpha", "role_instance_id": "bravo-eng-1"},
    ],
)
def test_unknown_or_cross_project_resources_are_rejected(
    progress_database: tuple[str, ProgressStore], changes: dict[str, object]
) -> None:
    database_url, store = progress_database
    with pytest.raises(ProgressNotFound):
        store.record(_draft(**changes))
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.progress"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "field_name",
    [
        "project_id",
        "work_item_id",
        "role_instance_id",
        "checkpoint_id",
        "status",
        "goal",
        "step",
        "completed_action",
        "activity",
        "blocker",
        "next_action",
        "safe_summary",
    ],
)
def test_sensitive_material_is_rejected_in_every_text_field(
    field_name: str,
) -> None:
    with pytest.raises(ProgressSensitiveContent) as captured:
        _draft(**{field_name: SYNTHETIC_KEY})
    assert "sk-" not in str(captured.value)


@pytest.mark.parametrize(
    "value",
    [
        "-----BEGIN PRIVATE KEY-----",
        "Bearer " + "b" * 32,
        "client_" + "secret=" + "c" * 24,
        '"access_' + 'token": "' + "d" * 32 + '"',
        "api_" + "key=" + "e" * 32,
        "ghp_" + "f" * 32,
        "AKIA" + "G" * 16,
        "postgresql://mesh:super-secret@database/mesh",
        "<analysis>private reasoning</analysis>",
    ],
)
def test_restricted_content_categories_are_rejected(value: str) -> None:
    with pytest.raises(ProgressSensitiveContent):
        _draft(safe_summary=value)


@pytest.mark.parametrize(
    "changes",
    [
        {"goal": " "},
        {"activity": " "},
        {"safe_summary": "x" * 1001},
        {"status": "Not Machine Readable"},
        {"expected_previous_sequence": -1},
        {"expected_previous_sequence": True},
        {"checkpoint_id": "contains spaces"},
    ],
)
def test_invalid_structure_is_rejected_before_store(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _draft(**changes)


def test_maintenance_and_store_failures_are_redacted(
    progress_database: tuple[str, ProgressStore],
) -> None:
    database_url, store = progress_database
    MaintenanceStore(database_url).pause(
        actor="operator", reason="progress acceptance"
    )
    with pytest.raises(ProgressError) as captured:
        store.record(_draft())
    assert str(captured.value) == "progress checkpoint operation failed"


def test_unavailable_store_error_does_not_expose_connection_details() -> None:
    store = ProgressStore("postgresql://127.0.0.1:1/private-database")
    with pytest.raises(ProgressError) as captured:
        store.record(_draft())
    assert str(captured.value) == "progress checkpoint operation failed"
    assert "private-database" not in str(captured.value)


def test_upgrade_assigns_deterministic_ids_to_legacy_progress(
    postgres_database: str,
) -> None:
    migrations = load_migrations()
    MigrationRunner(postgres_database, migrations=migrations[:8]).migrate()
    _seed(postgres_database)
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.progress
                (project_id, work_item_id, role_instance_id, sequence, status,
                 goal, step, next_action, safe_summary)
            VALUES ('alpha', 'work-1', 'eng-1', 1, 'working',
                    'Goal', 'Step', 'Next', 'Safe')
            """
        )

    MigrationRunner(postgres_database).migrate()

    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            "SELECT checkpoint_id FROM agentic_mesh_v5.progress"
        ).fetchone()[0] == "legacy-1"


def test_progress_implementation_has_no_provider_or_summarizer_dependency() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agentic_mesh_v5"
        / "progress.py"
    ).read_text(encoding="utf-8")
    assert "codex_provider" not in source
    assert "worker_provider" not in source
    assert "start_turn" not in source
    assert "openai" not in source.lower()
