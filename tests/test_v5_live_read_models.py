from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.api import API_PREFIX
from agentic_mesh_v5.api import create_app
from agentic_mesh_v5.api_auth import TokenAuthorizer
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import load_migrations
import agentic_mesh_v5.read_models as read_model_module
from agentic_mesh_v5.read_models import DOMAINS
from agentic_mesh_v5.read_models import ReadModelStore


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


def _seed_project(connection, project_id: str) -> None:
    connection.execute(
        """
        INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
        VALUES (%s, %s)
        """,
        (project_id, project_id.title()),
    )
    connection.execute(
        """
        INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
        VALUES (%s, 'engineering', 'engineering')
        """,
        (project_id,),
    )
    connection.execute(
        """
        INSERT INTO agentic_mesh_v5.role_instances
            (project_id, instance_id, role_id, status, metadata)
        VALUES (%s, 'eng-1', 'engineering', 'running', '{"private":"never-project"}')
        """,
        (project_id,),
    )


def _insert_work(connection, project_id: str, work_item_id: str) -> None:
    connection.execute(
        """
        INSERT INTO agentic_mesh_v5.work_items
            (project_id, work_item_id, assigned_role_id, title)
        VALUES (%s, %s, 'engineering', %s)
        """,
        (project_id, work_item_id, f"Work {work_item_id}"),
    )


def test_migration_backfill_rebuilds_without_changing_authoritative_rows(
    postgres_database: str,
) -> None:
    MigrationRunner(postgres_database, migrations=load_migrations()[:4]).migrate()
    with psycopg.connect(postgres_database) as connection:
        _seed_project(connection, "alpha")
        _insert_work(connection, "alpha", "work-1")
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_queues(project_id, queue_id, role_id)
            VALUES ('alpha', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.queue_items
                (project_id, queue_item_id, queue_id, work_item_id, idempotency_key)
            VALUES ('alpha', 'item-1', 'engineering', 'work-1', 'idem-item-1')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.progress
                (project_id, work_item_id, role_instance_id, sequence, status,
                 goal, step, next_action, safe_summary)
            VALUES ('alpha', 'work-1', 'eng-1', 1, 'active',
                    'Deliver', 'Implement', 'Test', 'Safe progress')
            """
        )
    with psycopg.connect(postgres_database) as connection:
        before = {
            table: connection.execute(
                sql.SQL("SELECT to_jsonb(item) FROM {}.{} AS item ORDER BY 1::text").format(
                    sql.Identifier("agentic_mesh_v5"), sql.Identifier(table)
                )
            ).fetchall()
            for table in (
                "projects", "roles", "role_instances", "work_items",
                "role_queues", "queue_items", "progress",
            )
        }

    MigrationRunner(postgres_database).migrate()
    store = ReadModelStore(postgres_database)
    assert store.rebuild("alpha") == 7
    snapshot = store.snapshot("alpha")

    with psycopg.connect(postgres_database) as connection:
        after = {
            table: connection.execute(
                sql.SQL("SELECT to_jsonb(item) FROM {}.{} AS item ORDER BY 1::text").format(
                    sql.Identifier("agentic_mesh_v5"), sql.Identifier(table)
                )
            ).fetchall()
            for table in before
        }
    for table in before:
        if table != "progress":
            assert after[table] == before[table]
    migrated_progress = dict(after["progress"][0][0])
    assert migrated_progress.pop("checkpoint_id") == "legacy-1"
    assert migrated_progress == before["progress"][0][0]
    assert set(snapshot["domains"]) == set(DOMAINS)
    assert all(len(snapshot["domains"][domain]) == 1 for domain in DOMAINS)
    assert "metadata" not in snapshot["domains"]["instance"][0]
    assert snapshot["domains"]["queue"][0]["depth"] == 1
    assert snapshot["domains"]["queue"][0]["ready"] == 1
    assert snapshot["domains"]["progress"][0]["safe_summary"] == "Safe progress"


def test_projection_events_share_source_transaction_and_are_append_only(
    postgres_database: str,
) -> None:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        _seed_project(connection, "alpha")
    with pytest.raises(RuntimeError):
        with psycopg.connect(postgres_database) as connection:
            _insert_work(connection, "alpha", "rolled-back")
            raise RuntimeError("roll back")

    with psycopg.connect(postgres_database) as connection:
        rolled_back = connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.read_model_events
            WHERE project_id = 'alpha' AND entity_id = 'rolled-back'
            """
        ).fetchone()[0]
        event_id = connection.execute(
            """
            SELECT min(event_id) FROM agentic_mesh_v5.read_model_events
            WHERE project_id = 'alpha'
            """
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(
                """
                UPDATE agentic_mesh_v5.read_model_events SET payload = '{}'
                WHERE event_id = %s
                """,
                (event_id,),
            )
    assert rolled_back == 0


def test_project_delete_cascades_all_projection_state(postgres_database: str) -> None:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        _seed_project(connection, "alpha")
        _insert_work(connection, "alpha", "work-1")
    store = ReadModelStore(postgres_database)
    assert store.snapshot("alpha")["domains"]["work"]

    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            "DELETE FROM agentic_mesh_v5.projects WHERE project_id = 'alpha'"
        )
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM agentic_mesh_v5.read_model_events),
                (SELECT count(*) FROM agentic_mesh_v5.read_model_entities),
                (SELECT count(*) FROM agentic_mesh_v5.read_model_cursors)
            """
        ).fetchone()

    assert counts == (0, 0, 0)


def test_replay_delete_and_concurrent_advance_converge(postgres_database: str) -> None:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        _seed_project(connection, "alpha")
        _insert_work(connection, "alpha", "temporary")
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_queues(project_id, queue_id, role_id)
            VALUES ('alpha', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.queue_items
                (project_id, queue_item_id, queue_id, work_item_id, idempotency_key)
            VALUES ('alpha', 'item-1', 'engineering', 'temporary', 'idem-item-1')
            """
        )
    store = ReadModelStore(postgres_database)
    assert [item["work_item_id"] for item in store.snapshot("alpha")["domains"]["work"]] == [
        "temporary"
    ]
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            DELETE FROM agentic_mesh_v5.work_items
            WHERE project_id = 'alpha' AND work_item_id = 'temporary'
            """
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(lambda _item: store.snapshot("alpha"), range(2)))

    assert snapshots[0] == snapshots[1]
    assert snapshots[0]["domains"]["work"] == []
    assert snapshots[0]["domains"]["queue"][0]["depth"] == 0
    assert store.rebuild("alpha") > 0
    assert store.snapshot("alpha") == snapshots[0]


def test_event_batches_resume_in_order_without_holding_database_state(
    postgres_database: str,
) -> None:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        _seed_project(connection, "alpha")
        _seed_project(connection, "bravo")
        _insert_work(connection, "alpha", "alpha-1")
        _insert_work(connection, "bravo", "bravo-1")
    store = ReadModelStore(postgres_database)

    first = store.events("alpha", limit=1)
    time.sleep(0.01)
    with psycopg.connect(postgres_database) as connection:
        _insert_work(connection, "bravo", "bravo-2")
        _insert_work(connection, "alpha", "alpha-2")
    remaining = store.events("alpha", after_event_id=first[-1].event_id)

    combined = first + remaining
    assert [item.event_id for item in combined] == sorted(
        item.event_id for item in combined
    )
    assert all(item.project_id == "alpha" for item in combined)
    assert "bravo-2" not in {item.entity_id for item in combined}
    assert "alpha-2" in {item.entity_id for item in combined}


def test_snapshot_bounds_catch_up_and_reports_projection_lag(
    postgres_database: str, monkeypatch
) -> None:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        _seed_project(connection, "alpha")
        for index in range(5):
            _insert_work(connection, "alpha", f"work-{index}")
    monkeypatch.setattr(read_model_module, "SNAPSHOT_BATCH_SIZE", 2)
    monkeypatch.setattr(read_model_module, "SNAPSHOT_MAX_BATCHES", 1)

    snapshot = ReadModelStore(postgres_database).snapshot("alpha")

    assert snapshot["last_event_id"] < snapshot["latest_event_id"]
    assert snapshot["caught_up"] is False


def test_snapshot_recomputes_clock_dependent_queue_readiness(
    postgres_database: str,
) -> None:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        _seed_project(connection, "alpha")
        _insert_work(connection, "alpha", "work-1")
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_queues(project_id, queue_id, role_id)
            VALUES ('alpha', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.queue_items
                (project_id, queue_item_id, queue_id, work_item_id,
                 available_at, idempotency_key)
            VALUES ('alpha', 'delayed', 'engineering', 'work-1', %s, 'idem-delayed')
            """,
            (datetime.now(timezone.utc) + timedelta(seconds=5),),
        )
    store = ReadModelStore(postgres_database)
    delayed = store.snapshot("alpha")
    time.sleep(5.2)
    ready = store.snapshot("alpha")

    assert delayed["domains"]["queue"][0]["delayed"] == 1
    assert delayed["domains"]["queue"][0]["ready"] == 0
    assert ready["domains"]["queue"][0]["delayed"] == 0
    assert ready["domains"]["queue"][0]["ready"] == 1
    assert ready["latest_event_id"] == delayed["latest_event_id"]


def _authorization() -> TokenAuthorizer:
    return TokenAuthorizer(
        [
            {
                "subject": "alpha-viewer",
                "token_sha256": hashlib.sha256(b"alpha-token").hexdigest(),
                "projects": ["alpha"],
                "scopes": ["read"],
            },
            {
                "subject": "bravo-viewer",
                "token_sha256": hashlib.sha256(b"bravo-token").hexdigest(),
                "projects": ["bravo"],
                "scopes": ["read"],
            },
        ]
    )


def _bearer(token: str, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **headers}


def _sse_payloads(response) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_authorized_snapshot_and_sse_resume_never_cross_projects(
    postgres_database: str,
) -> None:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        _seed_project(connection, "alpha")
        _seed_project(connection, "bravo")
        _insert_work(connection, "alpha", "alpha-1")
        _insert_work(connection, "bravo", "bravo-1")
    client = TestClient(
        create_app(postgres_database, authorizer=_authorization()),
        raise_server_exceptions=False,
    )

    snapshot = client.get(
        f"{API_PREFIX}/projects/alpha/read-model", headers=_bearer("alpha-token")
    )
    initial = client.get(
        f"{API_PREFIX}/projects/alpha/events?once=true",
        headers=_bearer("alpha-token"),
    )
    initial_payloads = _sse_payloads(initial)
    cursor = max(item["event_id"] for item in initial_payloads)
    with psycopg.connect(postgres_database) as connection:
        _insert_work(connection, "bravo", "bravo-later")
        _insert_work(connection, "alpha", "alpha-later")
    resumed = client.get(
        f"{API_PREFIX}/projects/alpha/events?once=true",
        headers=_bearer("alpha-token", **{"Last-Event-ID": str(cursor)}),
    )
    resumed_payloads = _sse_payloads(resumed)
    forbidden = client.get(
        f"{API_PREFIX}/projects/alpha/read-model", headers=_bearer("bravo-token")
    )
    invalid = client.get(
        f"{API_PREFIX}/projects/alpha/events?once=true",
        headers=_bearer("alpha-token", **{"Last-Event-ID": "not-an-id"}),
    )

    assert snapshot.status_code == 200
    assert snapshot.json()["caught_up"] is True
    assert "bravo-1" not in json.dumps(snapshot.json())
    assert initial.headers["content-type"].startswith("text/event-stream")
    assert resumed_payloads
    assert all(item["event_id"] > cursor for item in resumed_payloads)
    assert {item["entity_id"] for item in resumed_payloads} == {"alpha-later"}
    assert forbidden.status_code == 403
    assert invalid.status_code == 422
    assert invalid.headers["content-type"].startswith("application/problem+json")
