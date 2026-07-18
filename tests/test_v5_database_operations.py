from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
from threading import Event
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5 import cli as v5_cli
from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import load_migrations
from agentic_mesh_v5.database_operations import DatabaseBackupService
from agentic_mesh_v5.database_operations import DatabaseOperationsError
from agentic_mesh_v5.database_operations import MaintenanceStore
from agentic_mesh_v5.database_operations import PostgresNativeTools
from agentic_mesh_v5.events import EventStore
from agentic_mesh_v5.health import HealthReporter
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.telemetry import Telemetry


@pytest.fixture
def database_factory():
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for Postgres tests")
    databases: list[str] = []

    def create() -> str:
        database_name = f"mesh_v5_{uuid.uuid4().hex}"
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )
        databases.append(database_name)
        return urlunsplit(urlsplit(base_url)._replace(path=f"/{database_name}"))

    try:
        yield create
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            for database_name in databases:
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


def _migrated(database_factory) -> str:
    database_url = database_factory()
    MigrationRunner(database_url).migrate()
    return database_url


def _native_tools() -> PostgresNativeTools:
    container = os.environ.get("AGENTIC_MESH_TEST_POSTGRES_CONTAINER")
    if not container:
        pytest.skip("AGENTIC_MESH_TEST_POSTGRES_CONTAINER is required for native backup tests")
    prefix = ("docker", "exec", "-i", "-u", "postgres", container)
    return PostgresNativeTools(
        pg_dump=(*prefix, "pg_dump"),
        pg_restore=(*prefix, "pg_restore"),
    )


def _populate(database_url: str):
    lifecycle = LifecycleStore(database_url)
    queues = RoleQueueStore(database_url)
    lifecycle.create_project(
        project_id="alpha", display_name="Alpha", sponsor_ids=("sponsor-1",)
    )
    with psycopg.connect(database_url) as connection:
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
            VALUES ('alpha', 'eng-1', 'engineering', 'running')
            """
        )
    lifecycle.create_work_item(
        project_id="alpha",
        work_item_id="work-1",
        title="Durable work",
        owner_role_id="engineering",
        actor_id="sponsor-1",
        correlation_id="corr-1",
    )
    lifecycle.transition_work_item(
        project_id="alpha",
        work_item_id="work-1",
        target_status="active",
        actor_id="sponsor-1",
        correlation_id="corr-1",
        expected_version=1,
    )
    queues.create_queue(
        project_id="alpha", queue_id="engineering", role_id="engineering"
    )
    queues.enqueue(
        project_id="alpha",
        queue_id="engineering",
        queue_item_id="queue-1",
        work_item_id="work-1",
        idempotency_key="idem-1",
        payload={"safe": "payload"},
    )
    claim = queues.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=3600,
    )
    assert claim is not None
    return claim


def test_native_tool_credentials_use_environment_not_arguments(
    tmp_path: Path, monkeypatch
) -> None:
    calls = []
    create_modes = []
    original_open = os.open

    def secure_open(path, flags, mode=0o777, *, dir_fd=None):
        create_modes.append(mode)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def completed(command, **kwargs):
        calls.append((command, kwargs["env"]))
        output = kwargs.get("stdout")
        if hasattr(output, "write"):
            output.write(b"custom archive")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", completed)
    monkeypatch.setattr(os, "open", secure_open)
    archive = tmp_path / "safe.dump"
    tools = PostgresNativeTools()
    database_url = (
        "postgresql://mesh-user:super-secret@db.internal:5432/mesh"
        "?sslmode=require&channel_binding=require"
    )

    tools.dump(database_url, archive)
    tools.restore(database_url, archive)

    assert len(calls) == 2
    assert create_modes == [0o600]
    for command, environment in calls:
        rendered = " ".join(command)
        assert "super-secret" not in rendered
        assert "postgresql://" not in rendered
        assert environment["PGPASSWORD"] == "super-secret"
        assert environment["PGHOST"] == "db.internal"
        assert environment["PGSSLMODE"] == "require"
        assert environment["PGCHANNELBINDING"] == "require"
    assert "mesh" in calls[0][0]
    assert "mesh" in calls[1][0]

    with pytest.raises(
        DatabaseConfigurationError, match="cannot preserve: fallback_application_name"
    ):
        tools.dump(
            "postgresql://mesh-user@db.internal/mesh"
            "?fallback_application_name=unsafe-to-drop",
            tmp_path / "rejected.dump",
        )


def test_backup_reserves_output_and_validates_tools_before_pausing(
    database_factory, tmp_path: Path
) -> None:
    database_url = _migrated(database_factory)
    archive = tmp_path / "exclusive.dump"
    started = Event()
    release = Event()

    class CoordinatedTools:
        def validate(self, _server_major):
            return None

        def dump(self, _database_url, output):
            started.set()
            assert release.wait(timeout=10)
            output.write_bytes(b"synthetic archive")

        def verify(self, _archive):
            return None

    service = DatabaseBackupService(database_url, tools=CoordinatedTools())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            service.backup,
            archive,
            actor="operator",
            reason="exclusive path test",
        )
        assert started.wait(timeout=10)
        with pytest.raises(DatabaseOperationsError, match="already exists"):
            service.backup(
                archive,
                actor="other-operator",
                reason="competing backup",
            )
        release.set()
        result = first.result(timeout=10)
    assert Path(result.archive) == archive

    rejected = tmp_path / "old-client.dump"

    class RejectedTools:
        def validate(self, _server_major):
            raise DatabaseOperationsError("pg_dump major version is older")

    with pytest.raises(DatabaseOperationsError, match="older"):
        DatabaseBackupService(database_url, tools=RejectedTools()).backup(
            rejected,
            actor="operator",
            reason="client validation",
        )
    assert not rejected.exists()
    assert MaintenanceStore(database_url).status().status == "active"


def test_pause_waits_for_active_writer_and_guards_all_mutations(database_factory) -> None:
    database_url = _migrated(database_factory)
    maintenance = MaintenanceStore(database_url)
    writer = psycopg.connect(database_url)
    writer.execute(
        """
        INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
        VALUES ('acknowledged', 'Acknowledged')
        """
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            maintenance.pause, actor="operator", reason="consistent backup"
        )
        time.sleep(0.25)
        assert not future.done()
        writer.execute(
            """
            INSERT INTO agentic_mesh_v5.project_sponsors(project_id, sponsor_id)
            VALUES ('acknowledged', 'sponsor-after-pause-request')
            """
        )
        writer.commit()
        paused = future.result(timeout=10)
    writer.close()

    assert paused.status == "paused"
    assert paused.changed is True
    assert maintenance.pause(
        actor="other", reason="already paused"
    ).changed is False
    with psycopg.connect(database_url, autocommit=True) as connection:
        assert connection.execute(
            "SELECT display_name FROM agentic_mesh_v5.projects WHERE project_id = 'acknowledged'"
        ).fetchone()[0] == "Acknowledged"
        assert connection.execute(
            """
            SELECT sponsor_id FROM agentic_mesh_v5.project_sponsors
            WHERE project_id = 'acknowledged'
            """
        ).fetchone()[0] == "sponsor-after-pause-request"
        with pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState,
            match="maintenance pause",
        ):
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
                VALUES ('rejected', 'Rejected')
                """
            )
        with pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState,
            match="maintenance pause",
        ):
            connection.execute(
                "TRUNCATE agentic_mesh_v5.database_maintenance_history"
            )

    resumed = maintenance.resume(actor="operator", reason="backup complete")
    assert resumed.status == "active"
    assert resumed.changed is True
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('resumed', 'Resumed')
            """
        )
        history = connection.execute(
            "SELECT status FROM agentic_mesh_v5.database_maintenance_history ORDER BY transition_id"
        ).fetchall()
    assert history == [("active",), ("paused",), ("active",)]


def test_native_backup_restore_reproduces_acknowledged_state(
    database_factory, tmp_path: Path
) -> None:
    source_url = _migrated(database_factory)
    target_url = database_factory()
    claim = _populate(source_url)
    tools = _native_tools()
    archive = tmp_path / "mesh.dump"

    backup = DatabaseBackupService(source_url, tools=tools).backup(
        archive, actor="backup-operator", reason="scheduled backup"
    )

    assert Path(backup.archive) == archive
    assert Path(backup.manifest).is_file()
    assert backup.size_bytes > 0
    assert backup.table_counts["events"] >= 2
    assert "thread_affinities" in backup.table_counts
    assert "thread_reseeds" in backup.table_counts
    assert MaintenanceStore(source_url).status().status == "active"

    restored = DatabaseBackupService(target_url, tools=tools).restore(archive)

    assert restored.database_schema_version == load_migrations()[-1].version
    assert restored.table_counts == backup.table_counts
    assert restored.maintenance_status == "paused"
    assert [item.event_type for item in EventStore(target_url).read("alpha")] == [
        "work.created",
        "work.active",
    ]
    restored_item = RoleQueueStore(target_url).get_item("alpha", "queue-1")
    assert restored_item.status == "leased"
    with psycopg.connect(target_url, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "UPDATE agentic_mesh_v5.work_items SET title = 'unsafe' WHERE project_id = 'alpha'"
            )
    MaintenanceStore(target_url).resume(actor="restore-operator", reason="restore verified")
    LifecycleStore(target_url).open_gate(
        project_id="alpha",
        work_item_id="work-1",
        gate_id="gate-after-restore",
        gate_type="sponsor",
        requested_by="sponsor-1",
        sponsor_ids=("sponsor-1",),
        correlation_id="corr-1",
        expected_version=2,
    )
    completed = RoleQueueStore(target_url).complete(
        project_id="alpha",
        lease_id=claim.lease_id,
        lease_token=claim.lease_token,
    )
    assert completed.status == "completed"
    event_ids = [item.event_id for item in EventStore(target_url).read("alpha")]
    assert len(event_ids) == 3
    assert event_ids == sorted(set(event_ids))


def test_tamper_nonempty_target_and_failed_backup_fail_closed(
    database_factory, tmp_path: Path
) -> None:
    source_url = _migrated(database_factory)
    _populate(source_url)
    tools = _native_tools()
    archive = tmp_path / "mesh.dump"
    service = DatabaseBackupService(source_url, tools=tools)
    service.backup(archive, actor="operator", reason="test backup")

    archive.write_bytes(archive.read_bytes() + b"tampered")
    empty_target = database_factory()
    with pytest.raises(DatabaseOperationsError, match="checksum"):
        DatabaseBackupService(empty_target, tools=tools).restore(archive)

    clean_archive = tmp_path / "mesh-clean.dump"
    service.backup(clean_archive, actor="operator", reason="second test backup")
    nonempty_target = database_factory()
    with psycopg.connect(nonempty_target) as connection:
        connection.execute("CREATE TABLE public.existing(value integer)")
    with pytest.raises(DatabaseOperationsError, match="not empty"):
        DatabaseBackupService(nonempty_target, tools=tools).restore(clean_archive)

    object_target = database_factory()
    with psycopg.connect(object_target) as connection:
        connection.execute("CREATE SEQUENCE public.existing_sequence")
    with pytest.raises(DatabaseOperationsError, match="not empty"):
        DatabaseBackupService(object_target, tools=tools).restore(clean_archive)

    class FailingTools:
        def validate(self, _server_major):
            return None

        def dump(self, _database_url, _archive):
            raise DatabaseOperationsError("synthetic pg_dump failure")

        def verify(self, _archive):
            raise AssertionError("verification should not run")

    failed_archive = tmp_path / "failed.dump"
    with pytest.raises(DatabaseOperationsError, match="synthetic"):
        DatabaseBackupService(source_url, tools=FailingTools()).backup(
            failed_archive, actor="operator", reason="failure drill"
        )
    assert MaintenanceStore(source_url).status().status == "active"
    assert not failed_archive.exists()
    assert not Path(f"{failed_archive}.manifest.json").exists()


def test_connection_interruption_rolls_back_unacknowledged_work(database_factory) -> None:
    base_url = os.environ["AGENTIC_MESH_TEST_DATABASE_URL"]
    database_url = _migrated(database_factory)
    LifecycleStore(database_url).create_project(
        project_id="acknowledged", display_name="Acknowledged", sponsor_ids=("sponsor",)
    )
    interrupted = psycopg.connect(database_url)
    interrupted.execute(
        """
        INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
        VALUES ('unacknowledged', 'Unacknowledged')
        """
    )
    backend_pid = interrupted.execute("SELECT pg_backend_pid()").fetchone()[0]
    with psycopg.connect(base_url, autocommit=True) as admin:
        assert admin.execute("SELECT pg_terminate_backend(%s)", (backend_pid,)).fetchone()[0]
    with pytest.raises(psycopg.Error):
        interrupted.commit()
    interrupted.close()

    with psycopg.connect(database_url, autocommit=True) as connection:
        projects = connection.execute(
            "SELECT project_id FROM agentic_mesh_v5.projects ORDER BY project_id"
        ).fetchall()
    assert projects == [("acknowledged",)]


def test_database_outage_pauses_operations_and_recovers_without_loss(
    database_factory,
) -> None:
    container = os.environ.get("AGENTIC_MESH_TEST_POSTGRES_CONTAINER")
    if not container:
        pytest.skip("AGENTIC_MESH_TEST_POSTGRES_CONTAINER is required for outage tests")
    database_url = _migrated(database_factory)
    LifecycleStore(database_url).create_project(
        project_id="durable", display_name="Durable", sponsor_ids=("sponsor",)
    )
    outage_url = f"{database_url}?connect_timeout=2"

    subprocess.run(["docker", "pause", container], check=True, capture_output=True)
    try:
        report = HealthReporter(outage_url, Telemetry()).readiness()
        assert report.status == "degraded"
        assert report.dependencies[0].reason == "connection_failed"
        with pytest.raises(DatabaseOperationsError):
            MaintenanceStore(outage_url).status()
    finally:
        subprocess.run(
            ["docker", "unpause", container], check=True, capture_output=True
        )

    with psycopg.connect(database_url, autocommit=True) as connection:
        assert connection.execute(
            "SELECT display_name FROM agentic_mesh_v5.projects WHERE project_id = 'durable'"
        ).fetchone()[0] == "Durable"
    assert HealthReporter(database_url, Telemetry()).readiness().status == "ok"


def test_maintenance_cli_is_idempotent_and_machine_readable(
    database_factory, monkeypatch, capsys
) -> None:
    database_url = _migrated(database_factory)
    monkeypatch.setenv("AGENTIC_MESH_V5_DATABASE_URL", database_url)

    assert v5_cli.main(["--json", "database-maintenance-status"]) == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial["status"] == "database-active"
    assert v5_cli.main(
        [
            "--json",
            "database-pause",
            "--actor",
            "operator",
            "--reason",
            "CLI drill",
        ]
    ) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "database-paused"
    assert paused["maintenance_status"] == "paused"
    assert paused["changed"] is True
    assert v5_cli.main(
        [
            "--json",
            "database-resume",
            "--actor",
            "operator",
            "--reason",
            "CLI drill complete",
        ]
    ) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "database-active"
    assert resumed["maintenance_status"] == "active"
    assert resumed["changed"] is True
