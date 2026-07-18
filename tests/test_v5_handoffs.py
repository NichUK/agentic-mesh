from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.handoffs import HandoffAuthorizationError
from agentic_mesh_v5.handoffs import HandoffConflict
from agentic_mesh_v5.handoffs import HandoffError
from agentic_mesh_v5.handoffs import HandoffOffer
from agentic_mesh_v5.handoffs import HandoffStore
from agentic_mesh_v5.queues import QueueConflict
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.routing import Router


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
def handoff_database(postgres_database: str):
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
                   ('bravo', 'qa', 'qa')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('alpha', 'eng-1', 'engineering', 'running'),
                   ('alpha', 'qa-1', 'qa', 'running'),
                   ('alpha', 'qa-2', 'qa', 'running'),
                   ('bravo', 'qa-1', 'qa', 'running')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items(project_id, work_item_id, title)
            VALUES ('alpha', 'work-1', 'Work 1'), ('bravo', 'work-1', 'Work 1')
            """
        )
    queues = RoleQueueStore(postgres_database)
    queues.create_queue(
        project_id="alpha", queue_id="engineering", role_id="engineering"
    )
    queues.create_queue(project_id="alpha", queue_id="qa", role_id="qa")
    queues.create_queue(project_id="bravo", queue_id="qa", role_id="qa")
    queues.enqueue(
        project_id="alpha",
        queue_id="engineering",
        queue_item_id="source-item",
        work_item_id="work-1",
        idempotency_key="source-item",
        payload={},
    )
    source = queues.claim(
        project_id="alpha",
        queue_id="engineering",
        owner_instance_id="eng-1",
        lease_seconds=3600,
    )
    return postgres_database, queues, source, HandoffStore(postgres_database)


def _offer(source, **changes) -> HandoffOffer:
    values = {
        "project_id": "alpha",
        "source_lease_id": source.lease_id,
        "source_lease_token": source.lease_token,
        "target_role_id": "qa",
        "idempotency_key": "handoff-1",
        "summary": "Verify the implementation evidence.",
        "payload": {"evidence": ["test://focused"]},
        "priority": 10,
    }
    values.update(changes)
    return HandoffOffer(**values)


def test_offer_is_atomic_idempotent_and_concurrent(handoff_database) -> None:
    database_url, _queues, source, store = handoff_database
    draft = _offer(source)

    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(lambda _index: store.offer(draft), range(12)))

    assert len({item.handoff_id for item in records}) == 1
    assert len({item.queue_item_id for item in records}) == 1
    assert records[0].status == "offered"
    assert records[0].delivery_target_met is True
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.handoffs"
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE queue_id = 'qa'
            """
        ).fetchone()[0] == 1
    with pytest.raises(HandoffConflict, match="different work"):
        store.offer(_offer(source, summary="A different request."))
    with pytest.raises(HandoffAuthorizationError, match="source lease token"):
        store.offer(
            _offer(
                source,
                idempotency_key="wrong-source-token",
                source_lease_token="wrong-token",
            )
        )


def test_offer_rolls_back_route_when_handoff_cannot_commit(
    handoff_database, monkeypatch
) -> None:
    database_url, _queues, source, store = handoff_database
    original = Router.route_in_transaction

    def route_then_fail(router, connection, draft):
        original(router, connection, draft)
        raise RuntimeError("crash after route")

    monkeypatch.setattr(Router, "route_in_transaction", route_then_fail)

    with pytest.raises(HandoffError, match="handoff operation failed"):
        store.offer(_offer(source))
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.handoffs"
        ).fetchone()[0] == 0
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE queue_id = 'qa'
            """
        ).fetchone()[0] == 0


def test_claim_accept_and_source_completion_gate(handoff_database) -> None:
    _database_url, queues, source, store = handoff_database
    offered = store.offer(_offer(source))
    with pytest.raises(QueueConflict, match="before handoff acceptance"):
        queues.complete(
            project_id="alpha",
            lease_id=source.lease_id,
            lease_token=source.lease_token,
        )

    target = queues.claim(
        project_id="alpha", queue_id="qa", owner_instance_id="qa-1",
        lease_seconds=3600,
    )
    with pytest.raises(HandoffAuthorizationError, match="target lease"):
        store.claim(
            project_id="alpha", handoff_id=offered.handoff_id,
            lease_id=source.lease_id, lease_token=source.lease_token,
        )
    claimed = store.claim(
        project_id="alpha", handoff_id=offered.handoff_id,
        lease_id=target.lease_id, lease_token=target.lease_token,
    )
    repeated_claim = store.claim(
        project_id="alpha", handoff_id=offered.handoff_id,
        lease_id=target.lease_id, lease_token=target.lease_token,
    )
    assert claimed == repeated_claim
    assert claimed.status == "claimed"
    assert claimed.target_instance_id == "qa-1"

    accepted = store.accept(
        project_id="alpha", handoff_id=offered.handoff_id,
        lease_id=target.lease_id, lease_token=target.lease_token,
    )
    assert accepted.status == "accepted"
    assert queues.complete(
        project_id="alpha", lease_id=source.lease_id,
        lease_token=source.lease_token,
    ).status == "completed"
    queues.complete(
        project_id="alpha", lease_id=target.lease_id,
        lease_token=target.lease_token,
    )
    assert store.accept(
        project_id="alpha", handoff_id=offered.handoff_id,
        lease_id=target.lease_id, lease_token=target.lease_token,
    ) == accepted
    assert store.offer(_offer(source)) == accepted


def test_expired_target_lease_can_be_reclaimed(handoff_database) -> None:
    database_url, queues, source, store = handoff_database
    offered = store.offer(_offer(source))
    first = queues.claim(
        project_id="alpha", queue_id="qa", owner_instance_id="qa-1",
        lease_seconds=3600,
    )
    store.claim(
        project_id="alpha", handoff_id=offered.handoff_id,
        lease_id=first.lease_id, lease_token=first.lease_token,
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.leases
            SET acquired_at = clock_timestamp() - interval '2 seconds',
                heartbeat_at = clock_timestamp() - interval '2 seconds',
                expires_at = clock_timestamp() - interval '1 second'
            WHERE project_id = 'alpha' AND lease_id = %s
            """,
            (first.lease_id,),
        )
    second = queues.claim(
        project_id="alpha", queue_id="qa", owner_instance_id="qa-2",
        lease_seconds=3600,
    )
    reclaimed = store.claim(
        project_id="alpha", handoff_id=offered.handoff_id,
        lease_id=second.lease_id, lease_token=second.lease_token,
    )
    assert reclaimed.target_instance_id == "qa-2"
    assert reclaimed.target_lease_id == second.lease_id
    with pytest.raises(HandoffAuthorizationError, match="does not own"):
        store.accept(
            project_id="alpha", handoff_id=offered.handoff_id,
            lease_id=first.lease_id, lease_token=first.lease_token,
        )
    assert store.accept(
        project_id="alpha", handoff_id=offered.handoff_id,
        lease_id=second.lease_id, lease_token=second.lease_token,
    ).status == "accepted"


def test_service_target_boundaries_and_overdue_state(handoff_database) -> None:
    database_url, queues, source, store = handoff_database
    offered = store.offer(_offer(source))
    target = queues.claim(
        project_id="alpha", queue_id="qa", owner_instance_id="qa-1",
        lease_seconds=3600,
    )
    store.claim(
        project_id="alpha", handoff_id=offered.handoff_id,
        lease_id=target.lease_id, lease_token=target.lease_token,
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.handoffs
            SET offered_at = clock_timestamp() - interval '223 seconds',
                queued_at = clock_timestamp() - interval '212 seconds',
                claimed_at = clock_timestamp() - interval '121 seconds'
            WHERE project_id = 'alpha' AND handoff_id = %s
            """,
            (offered.handoff_id,),
        )
    overdue = store.get("alpha", offered.handoff_id)
    assert overdue.delivery_target_met is False
    assert overdue.claim_target_met is False
    assert overdue.acceptance_target_met is None
    assert overdue.acceptance_overdue is True
    missed = store.accept(
        project_id="alpha", handoff_id=offered.handoff_id,
        lease_id=target.lease_id, lease_token=target.lease_token,
    )
    assert missed.acceptance_target_met is False

    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            WITH observed AS (SELECT clock_timestamp() AS now)
            UPDATE agentic_mesh_v5.handoffs AS handoff
            SET offered_at = observed.now - interval '220 seconds',
                queued_at = observed.now - interval '210 seconds',
                claimed_at = observed.now - interval '120 seconds',
                accepted_at = observed.now
            FROM observed
            WHERE project_id = 'alpha' AND handoff_id = %s
            """,
            (offered.handoff_id,),
        )
    boundary = store.get("alpha", offered.handoff_id)
    assert boundary.delivery_target_met is True
    assert boundary.claim_target_met is True
    assert boundary.acceptance_target_met is True
