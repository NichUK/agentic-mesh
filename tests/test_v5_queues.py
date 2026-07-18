from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.queues import LeaseExpired
from agentic_mesh_v5.queues import QueueAuthorizationError
from agentic_mesh_v5.queues import QueueConflict
from agentic_mesh_v5.queues import QueueNotFound
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.routing import RouteDraft
from agentic_mesh_v5.routing import Router
from agentic_mesh_v5.routing import RoutingConflict
from agentic_mesh_v5.routing import RoutingNotFound


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
def queue_database(postgres_database: str) -> tuple[str, RoleQueueStore]:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
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
                   ('alpha', 'qa', 'qa'),
                   ('bravo', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('alpha', 'eng-1', 'engineering', 'running'),
                   ('alpha', 'eng-2', 'engineering', 'running'),
                   ('alpha', 'qa-1', 'qa', 'running'),
                   ('bravo', 'bravo-eng', 'engineering', 'running')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items(project_id, work_item_id, title)
            VALUES ('alpha', 'work-1', 'Work 1'),
                   ('alpha', 'work-2', 'Work 2'),
                   ('alpha', 'work-3', 'Work 3'),
                   ('bravo', 'bravo-work', 'Bravo work')
            """
        )
    store = RoleQueueStore(postgres_database)
    store.create_queue(project_id="alpha", queue_id="engineering", role_id="engineering")
    store.create_queue(project_id="bravo", queue_id="engineering", role_id="engineering")
    return postgres_database, store


def _enqueue(
    store: RoleQueueStore,
    item_id: str,
    work_id: str,
    *,
    priority: int = 0,
    available_at: datetime | None = None,
):
    return store.enqueue(
        project_id="alpha",
        queue_id="engineering",
        queue_item_id=item_id,
        work_item_id=work_id,
        idempotency_key=f"idem-{item_id}",
        payload={"work_item_id": work_id},
        priority=priority,
        available_at=available_at,
    )


def test_enqueue_rejects_naive_ready_time_before_database_access() -> None:
    store = RoleQueueStore("postgresql://not-opened/mesh")

    with pytest.raises(ValueError, match="timezone-aware"):
        store.enqueue(
            project_id="alpha",
            queue_id="engineering",
            queue_item_id="item-1",
            work_item_id="work-1",
            idempotency_key="idem-1",
            payload={},
            available_at=datetime.now(),
        )


def test_priority_and_ready_time_control_claim_order(queue_database) -> None:
    _database_url, store = queue_database
    _enqueue(store, "item-low", "work-1", priority=1)
    _enqueue(store, "item-high", "work-2", priority=10)
    _enqueue(
        store,
        "item-delayed",
        "work-3",
        priority=100,
        available_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    first = store.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=60,
    )
    store.complete(
        project_id="alpha", lease_id=first.lease_id, lease_token=first.lease_token
    )
    second = store.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=60,
    )
    store.complete(
        project_id="alpha", lease_id=second.lease_id, lease_token=second.lease_token
    )

    assert first.queue_item.queue_item_id == "item-high"
    assert second.queue_item.queue_item_id == "item-low"
    assert (
        store.claim(
            project_id="alpha",
            queue_id="engineering",
            owner_instance_id="eng-1",
            lease_seconds=60,
        )
        is None
    )


def test_concurrent_claimers_create_one_owner_lease(queue_database) -> None:
    database_url, store = queue_database
    _enqueue(store, "item-1", "work-1")

    def claim(instance_id):
        return store.claim(
            project_id="alpha",
            queue_id="engineering",
            owner_instance_id=instance_id,
            lease_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("eng-1", "eng-2")))

    assert sum(item is not None for item in claims) == 1
    with psycopg.connect(database_url) as connection:
        active = connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.leases
            WHERE project_id = 'alpha' AND released_at IS NULL
            """
        ).fetchone()[0]
    assert active == 1


def test_heartbeat_completion_and_duplicate_token_use(queue_database) -> None:
    _database_url, store = queue_database
    _enqueue(store, "item-1", "work-1")
    claim = store.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=10,
    )

    extended = store.heartbeat(
        project_id="alpha",
        lease_id=claim.lease_id,
        lease_token=claim.lease_token,
        lease_seconds=120,
    )
    completed = store.complete(
        project_id="alpha",
        lease_id=claim.lease_id,
        lease_token=claim.lease_token,
    )

    assert extended > claim.expires_at
    assert completed.status == "completed"
    with pytest.raises(QueueAuthorizationError, match="token is invalid"):
        store.complete(
            project_id="alpha",
            lease_id=claim.lease_id,
            lease_token=claim.lease_token,
        )


def test_release_requeues_and_increments_attempt(queue_database) -> None:
    _database_url, store = queue_database
    _enqueue(store, "item-1", "work-1")
    first = store.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=60,
    )

    released = store.release(
        project_id="alpha",
        lease_id=first.lease_id,
        lease_token=first.lease_token,
    )
    second = store.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-2",
        lease_seconds=60,
    )

    assert released.status == "ready"
    assert second.queue_item.queue_item_id == "item-1"
    assert second.queue_item.attempt_count == 2


def test_expired_lease_reclaims_and_rejects_old_token(queue_database) -> None:
    database_url, store = queue_database
    _enqueue(store, "item-1", "work-1")
    first = store.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=60,
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.leases
            SET acquired_at = clock_timestamp() - interval '2 minutes',
                heartbeat_at = clock_timestamp() - interval '2 minutes',
                expires_at = clock_timestamp() - interval '1 second'
            WHERE project_id = 'alpha' AND lease_id = %s
            """,
            (first.lease_id,),
        )

    with pytest.raises(LeaseExpired):
        store.heartbeat(
            project_id="alpha",
            lease_id=first.lease_id,
            lease_token=first.lease_token,
            lease_seconds=60,
        )
    second = store.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-2",
        lease_seconds=60,
    )

    assert second.queue_item.queue_item_id == "item-1"
    assert second.queue_item.attempt_count == 2
    with pytest.raises(QueueAuthorizationError):
        store.release(
            project_id="alpha",
            lease_id=first.lease_id,
            lease_token=first.lease_token,
        )


def test_role_and_project_boundaries_fail_closed(queue_database) -> None:
    _database_url, store = queue_database
    _enqueue(store, "item-1", "work-1")

    with pytest.raises(QueueAuthorizationError, match="cannot claim"):
        store.claim(
            project_id="alpha",
            queue_id="engineering",
            owner_instance_id="qa-1",
            lease_seconds=60,
        )
    with pytest.raises(QueueNotFound):
        store.claim(
            project_id="alpha",
            queue_id="engineering",
            owner_instance_id="bravo-eng",
            lease_seconds=60,
        )
    with pytest.raises(QueueNotFound):
        store.get_item("bravo", "item-1")


def test_metrics_report_depth_age_delay_lease_and_attempts(queue_database) -> None:
    database_url, store = queue_database
    _enqueue(store, "item-ready", "work-1")
    _enqueue(
        store,
        "item-delayed",
        "work-2",
        available_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.queue_items
            SET available_at = clock_timestamp() - interval '10 seconds'
            WHERE project_id = 'alpha' AND queue_item_id = 'item-ready'
            """
        )
    before = store.metrics(project_id="alpha", queue_id="engineering")
    assert (before.ready, before.delayed, before.leased) == (1, 1, 0)
    assert before.oldest_ready_age_seconds is not None
    assert before.oldest_ready_age_seconds >= 9

    store.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=60,
    )
    metrics = store.metrics(project_id="alpha", queue_id="engineering")

    assert (metrics.depth, metrics.ready, metrics.delayed, metrics.leased) == (2, 0, 1, 1)
    assert metrics.oldest_ready_age_seconds is None
    assert metrics.total_attempts == 1


def test_duplicate_enqueue_is_conflict(queue_database) -> None:
    _database_url, store = queue_database
    _enqueue(store, "item-1", "work-1")

    with pytest.raises(QueueConflict, match="idempotency key"):
        store.enqueue(
            project_id="alpha",
            queue_id="engineering",
            queue_item_id="item-2",
            work_item_id="work-2",
            idempotency_key="idem-item-1",
            payload={},
        )


def test_router_resolves_role_capability_and_preserves_priority(queue_database) -> None:
    database_url, store = queue_database
    store.create_queue(
        project_id="alpha",
        queue_id="engineering-browser",
        role_id="engineering",
        capability="browser",
    )
    router = Router(database_url)
    low = router.route(
        RouteDraft(
            "alpha", "work-1", "engineering", "route-low", {"kind": "general"},
            priority=1,
        )
    )
    high = router.route(
        RouteDraft(
            "alpha", "work-2", "engineering", "route-high", {"kind": "general"},
            priority=10,
        )
    )
    browser = router.route(
        RouteDraft(
            "alpha", "work-3", "engineering", "route-browser", {},
            capability="browser", priority=100,
        )
    )

    claim = store.claim(
        project_id="alpha", queue_id="engineering", owner_instance_id="eng-1",
        lease_seconds=60,
    )

    assert (low.queue_id, high.queue_id) == ("engineering", "engineering")
    assert browser.queue_id == "engineering-browser"
    assert claim.queue_item.queue_item_id == high.queue_item_id


def test_concurrent_duplicate_routes_return_one_item_and_conflicts_fail(
    queue_database,
) -> None:
    database_url, _store = queue_database
    router = Router(database_url)
    draft = RouteDraft(
        "alpha", "work-1", "engineering", "same-route", {"kind": "build"},
        priority=7,
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda _index: router.route(draft), range(16)))

    assert len({record.queue_item_id for record in records}) == 1
    with psycopg.connect(database_url) as connection:
        count = connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE project_id = 'alpha' AND idempotency_key = 'same-route'
            """
        ).fetchone()[0]
    assert count == 1
    with pytest.raises(RoutingConflict, match="already used"):
        router.route(
            RouteDraft(
                "alpha", "work-1", "engineering", "same-route",
                {"kind": "different"}, priority=7,
            )
        )


def test_router_rejects_invalid_paused_and_foreign_targets(queue_database) -> None:
    database_url, store = queue_database
    router = Router(database_url)
    store.create_queue(
        project_id="alpha", queue_id="qa", role_id="qa"
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE agentic_mesh_v5.role_queues SET paused = true "
            "WHERE project_id = 'alpha' AND queue_id = 'qa'"
        )

    invalid = (
        RouteDraft("alpha", "work-1", "engineering", "missing-cap", {}, capability="gpu"),
        RouteDraft("alpha", "work-1", "finance", "foreign-role", {}),
        RouteDraft("alpha", "work-1", "qa", "paused", {}),
    )
    for draft in invalid:
        with pytest.raises(RoutingNotFound, match="target not found"):
            router.route(draft)

    with psycopg.connect(database_url) as connection:
        routed = connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE project_id = 'alpha'
            """
        ).fetchone()[0]
    assert routed == 0
