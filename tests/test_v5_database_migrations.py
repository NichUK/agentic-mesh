from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5 import cli
from agentic_mesh_v5.database import DATABASE_URL_ENV
from agentic_mesh_v5.database import Migration
from agentic_mesh_v5.database import MigrationError
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import load_migrations
from agentic_mesh_v5.database_operations import MaintenanceStore


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


def _migration(version: int, name: str, statement: str) -> Migration:
    return Migration.from_text(version=version, name=name, sql=statement)


def test_packaged_migrations_are_contiguous_and_v5_only() -> None:
    migrations = load_migrations()

    assert [item.version for item in migrations] == list(
        range(1, len(migrations) + 1)
    )
    assert migrations[0].name == "initial_control_plane"
    assert "agentic_mesh_v5.projects" in migrations[0].sql
    assert all("agentic_mesh_v4" not in item.sql for item in migrations)


def test_loader_rejects_missing_and_malformed_versions(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0003_third.sql").write_text("SELECT 3;", encoding="utf-8")

    with pytest.raises(MigrationError, match="contiguous"):
        load_migrations(tmp_path)

    (tmp_path / "0003_third.sql").unlink()
    (tmp_path / "bad-name.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(MigrationError, match="invalid migration filename"):
        load_migrations(tmp_path)


def test_runner_rejects_an_explicit_empty_migration_set() -> None:
    with pytest.raises(MigrationError, match="at least one migration"):
        MigrationRunner("postgresql://not-opened/mesh", migrations=())


def test_clean_database_migrates_and_repeat_is_noop(postgres_database: str) -> None:
    runner = MigrationRunner(postgres_database)
    available_version = load_migrations()[-1].version

    assert runner.status().to_dict() == {
        "current_version": 0,
        "available_version": available_version,
        "pending_versions": list(range(1, available_version + 1)),
        "applied": [],
    }
    first = runner.migrate()
    second = runner.migrate()

    assert first.current_version == second.current_version == available_version
    assert second.pending_versions == ()
    assert len(second.applied) == available_version
    with psycopg.connect(postgres_database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'agentic_mesh_v5'
                """
            )
        }
        outbox_index = connection.execute(
            """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'agentic_mesh_v5'
              AND indexname = 'outbox_pending_idx'
            """
        ).fetchone()[0]
        recovery_work_index = connection.execute(
            """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'agentic_mesh_v5'
              AND indexname = 'recovery_requests_pending_work_idx'
            """
        ).fetchone()[0]
    assert {
        "projects",
        "roles",
        "role_instances",
        "work_items",
        "role_queues",
        "queue_items",
        "leases",
        "events",
        "outbox",
        "handoffs",
        "gates",
        "approvals",
        "memories",
        "packages",
        "progress",
        "audit_records",
        "thread_affinities",
        "thread_reseeds",
        "role_scaling_policies",
        "failure_incidents",
        "failure_attempts",
        "recovery_requests",
        "recovery_supervisor_runs",
        "schema_migrations",
    }.issubset(tables)
    assert "(project_id, available_at, outbox_id)" in outbox_index
    assert "(project_id, work_item_id)" in recovery_work_index
    assert "WHERE (status = 'pending'::text)" in recovery_work_index


def test_ordered_upgrade_and_checksum_drift(postgres_database: str) -> None:
    first = _migration(
        1,
        "first",
        "CREATE TABLE agentic_mesh_v5.first_record (id integer PRIMARY KEY);",
    )
    second = _migration(
        2,
        "second",
        "CREATE TABLE agentic_mesh_v5.second_record (id integer PRIMARY KEY);",
    )
    assert MigrationRunner(postgres_database, migrations=(first,)).migrate().current_version == 1
    assert (
        MigrationRunner(postgres_database, migrations=(first, second))
        .migrate()
        .current_version
        == 2
    )

    changed = _migration(1, "first", "SELECT 1;")
    with pytest.raises(MigrationError, match="does not match packaged content"):
        MigrationRunner(postgres_database, migrations=(changed, second)).migrate()

    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            "DELETE FROM agentic_mesh_v5.schema_migrations WHERE version = 1"
        )
    with pytest.raises(MigrationError, match="history is not contiguous"):
        MigrationRunner(postgres_database, migrations=(first, second)).status()


def test_handoff_acceptance_migration_preserves_legacy_offer(
    postgres_database: str,
) -> None:
    packaged = load_migrations()
    acceptance = next(
        item for item in packaged if item.name == "handoff_acceptance"
    )
    before_acceptance = tuple(
        item for item in packaged if item.version < acceptance.version
    )
    MigrationRunner(
        postgres_database, migrations=before_acceptance
    ).migrate()
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
            VALUES ('alpha', 'engineering', 'engineering'), ('alpha', 'qa', 'qa')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items(project_id, work_item_id, title)
            VALUES ('alpha', 'work-1', 'Work 1')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.handoffs
                (project_id, handoff_id, work_item_id, source_role_id,
                 target_role_id, summary, idempotency_key)
            VALUES ('alpha', 'legacy', 'work-1', 'engineering', 'qa',
                    'Legacy offer', 'legacy')
            """
        )

    MigrationRunner(postgres_database, migrations=packaged).migrate()

    with psycopg.connect(postgres_database) as connection:
        row = connection.execute(
            """
            SELECT status, queued_at = offered_at, queue_item_id,
                   request_fingerprint
            FROM agentic_mesh_v5.handoffs
            WHERE project_id = 'alpha' AND handoff_id = 'legacy'
            """
        ).fetchone()
    assert row == ("offered", True, None, None)


def test_fleet_migration_normalizes_legacy_transitional_instances(
    postgres_database: str,
) -> None:
    packaged = load_migrations()
    fleet = next(item for item in packaged if item.name == "fleet_scaling")
    MigrationRunner(
        postgres_database,
        migrations=tuple(item for item in packaged if item.version < fleet.version),
    ).migrate()
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
            VALUES ('alpha', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('alpha', 'eng-1', 'engineering', 'starting')
            """
        )

    MigrationRunner(postgres_database, migrations=packaged).migrate()

    with psycopg.connect(postgres_database) as connection:
        row = connection.execute(
            """
            SELECT status, lifecycle_action_id, lifecycle_reason
            FROM agentic_mesh_v5.role_instances
            WHERE project_id = 'alpha' AND instance_id = 'eng-1'
            """
        ).fetchone()
    assert row == (
        "stopped",
        None,
        "normalized legacy transition during fleet migration",
    )


def test_failed_run_rolls_back_history_and_schema(postgres_database: str) -> None:
    migrations = (
        _migration(
            1,
            "first",
            "CREATE TABLE agentic_mesh_v5.rollback_probe (id integer PRIMARY KEY);",
        ),
        _migration(2, "broken", "CREATE TABLE this is not valid SQL;"),
    )

    with pytest.raises(MigrationError, match=r"migration 2 \(broken\) failed"):
        MigrationRunner(postgres_database, migrations=migrations).migrate()

    with psycopg.connect(postgres_database) as connection:
        schema = connection.execute(
            "SELECT to_regnamespace('agentic_mesh_v5')"
        ).fetchone()[0]
    assert schema is None


def test_concurrent_runners_apply_once(postgres_database: str) -> None:
    available_version = load_migrations()[-1].version

    def migrate() -> int:
        return MigrationRunner(postgres_database).migrate().current_version

    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = list(pool.map(lambda _: migrate(), range(2)))

    assert versions == [available_version, available_version]
    with psycopg.connect(postgres_database) as connection:
        count = connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.schema_migrations"
        ).fetchone()[0]
    assert count == available_version


def test_later_migration_tables_receive_maintenance_guards(
    postgres_database: str,
) -> None:
    packaged = load_migrations()
    future = _migration(
        packaged[-1].version + 1,
        "future_guard_probe",
        """
        CREATE TABLE agentic_mesh_v5.future_guard_probe (
            id integer PRIMARY KEY
        );
        """,
    )
    MigrationRunner(postgres_database, migrations=(*packaged, future)).migrate()
    MaintenanceStore(postgres_database).pause(
        actor="operator", reason="future table guard test"
    )

    with psycopg.connect(postgres_database, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "INSERT INTO agentic_mesh_v5.future_guard_probe(id) VALUES (1)"
            )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute("TRUNCATE agentic_mesh_v5.future_guard_probe")


def test_project_foreign_keys_and_scoped_ownership_fail_closed(
    postgres_database: str,
) -> None:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database, autocommit=True) as connection:
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
                   ('bravo', 'foreign-role', 'engineering')
            """
        )

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.work_items
                    (project_id, work_item_id, assigned_role_id, title)
                VALUES ('alpha', 'work-1', 'foreign-role', 'Cross-project probe')
                """
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.packages
                    (scope, package_type, package_name, package_version, digest, source_ref)
                VALUES ('project', 'role', 'engineering', '1.0.0', 'digest', 'git:ref')
                """
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.memories(scope, role_id, content)
                VALUES ('project_role', 'engineering', 'Missing project')
                """
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.audit_records
                    (scope, actor_id, action, object_type, object_id)
                VALUES ('project', 'operator', 'create', 'probe', '1')
                """
            )


def test_cli_migrates_and_reports_status_with_configured_database(
    postgres_database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, postgres_database)

    assert cli.main(["--json", "database-status"]) == 0
    pending = json.loads(capsys.readouterr().out)
    assert pending["status"] == "database-pending"
    assert pending["pending_versions"]
    assert postgres_database not in json.dumps(pending)

    assert cli.main(["--json", "database-migrate"]) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["status"] == "database-current"
    assert migrated["current_version"] == load_migrations()[-1].version
    assert postgres_database not in json.dumps(migrated)

    assert cli.main(["--json", "database-status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["pending_versions"] == []
    assert postgres_database not in json.dumps(status)


def test_cli_rejects_missing_database_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)

    assert cli.main(["--json", "database-status"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": f"{DATABASE_URL_ENV} is required",
        "runtime": "agentic-mesh-v5",
        "status": "rejected",
    }
