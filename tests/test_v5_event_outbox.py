from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import MigrationError
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import load_migrations
from agentic_mesh_v5.events import EventDraft
from agentic_mesh_v5.events import EventStore
from agentic_mesh_v5.events import DeliveryReceipt
from agentic_mesh_v5.events import OutboundDraft
from agentic_mesh_v5.events import OutboxDispatcher


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
def event_database(postgres_database: str) -> str:
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
            INSERT INTO agentic_mesh_v5.work_items
                (project_id, work_item_id, title)
            VALUES ('alpha', 'work-a', 'Alpha work'),
                   ('bravo', 'work-b', 'Bravo work')
            """
        )
    return postgres_database


def _event(
    *,
    project_id: str = "alpha",
    work_item_id: str = "work-a",
    event_type: str = "work.started",
    correlation_id: str = "corr-1",
) -> EventDraft:
    return EventDraft(
        project_id=project_id,
        work_item_id=work_item_id,
        actor_id="project-manager",
        correlation_id=correlation_id,
        aggregate_type="work-item",
        aggregate_id=work_item_id,
        event_type=event_type,
        payload={"status": event_type},
    )


def _append_outbound(database_url: str, *, project_id: str = "alpha") -> str:
    work_item_id = "work-a" if project_id == "alpha" else "work-b"
    with EventStore(database_url).transaction() as transaction:
        stored = transaction.append(
            _event(project_id=project_id, work_item_id=work_item_id),
            (OutboundDraft(topic="work.events", payload={"kind": "started"}),),
        )
    with psycopg.connect(database_url) as connection:
        return connection.execute(
            """
            SELECT idempotency_key FROM agentic_mesh_v5.outbox
            WHERE event_id = %s
            """,
            (stored.event_id,),
        ).fetchone()[0]


class RecordingDelivery:
    def __init__(self, action=None, *, failure: Exception | None = None) -> None:
        self.action = action
        self.failure = failure
        self.actions = []
        self.receipts = {}
        self.after_receipt = None

    def deliver(self, message):
        existing = self.receipts.get(message.idempotency_key)
        if existing is not None:
            return existing
        if self.failure is not None:
            raise self.failure
        self.actions.append(message)
        if self.action is not None:
            self.action(message)
        receipt = DeliveryReceipt(message.idempotency_key)
        self.receipts[message.idempotency_key] = receipt
        if self.after_receipt is not None:
            self.after_receipt(message)
        return receipt


def test_event_identifiers_and_outbound_topics_are_required() -> None:
    with pytest.raises(ValueError, match="project_id must be a string"):
        EventDraft(
            project_id=None,  # type: ignore[arg-type]
            work_item_id="work-a",
            actor_id="project-manager",
            correlation_id="corr-1",
            aggregate_type="work-item",
            aggregate_id="work-a",
            event_type="work.started",
            payload={},
        )
    with pytest.raises(ValueError, match="actor_id is required"):
        EventDraft(
            project_id="alpha",
            work_item_id="work-a",
            actor_id=" ",
            correlation_id="corr-1",
            aggregate_type="work-item",
            aggregate_id="work-a",
            event_type="work.started",
            payload={},
        )
    with pytest.raises(ValueError, match="topic is required"):
        OutboundDraft(topic=" ", payload={})


def test_v2_upgrade_rejects_unsupported_pre_writer_events(
    postgres_database: str,
) -> None:
    migrations = load_migrations()
    MigrationRunner(postgres_database, migrations=migrations[:1]).migrate()
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('legacy', 'Legacy')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.events
                (project_id, aggregate_type, aggregate_id, event_type, payload)
            VALUES ('legacy', 'manual', 'row-1', 'manual.created', '{}'::jsonb)
            """
        )

    with pytest.raises(MigrationError, match=r"migration 2 .* failed"):
        MigrationRunner(postgres_database, migrations=migrations).migrate()

    status = MigrationRunner(postgres_database, migrations=migrations).status()
    assert status.current_version == 1
    with psycopg.connect(postgres_database) as connection:
        actor_column = connection.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'agentic_mesh_v5'
              AND table_name = 'events'
              AND column_name = 'actor_id'
            """
        ).fetchone()
    assert actor_column is None


def test_source_event_and_outbox_commit_together(event_database: str) -> None:
    store = EventStore(event_database)

    with store.transaction() as transaction:
        transaction.execute(
            """
            UPDATE agentic_mesh_v5.work_items SET status = 'active'
            WHERE project_id = 'alpha' AND work_item_id = 'work-a'
            """
        )
        stored = transaction.append(
            _event(),
            (
                OutboundDraft(topic="work.events", payload={"audience": "runtime"}),
                OutboundDraft(topic="audit.events", payload={"audience": "audit"}),
            ),
        )

    with psycopg.connect(event_database) as connection:
        status = connection.execute(
            """
            SELECT status FROM agentic_mesh_v5.work_items
            WHERE project_id = 'alpha' AND work_item_id = 'work-a'
            """
        ).fetchone()[0]
        outbox = connection.execute(
            """
            SELECT topic, idempotency_key FROM agentic_mesh_v5.outbox
            WHERE project_id = 'alpha' AND event_id = %s ORDER BY topic
            """,
            (stored.event_id,),
        ).fetchall()
    assert status == "active"
    assert [row[0] for row in outbox] == ["audit.events", "work.events"]
    assert len({row[1] for row in outbox}) == 2


def test_duplicate_outbound_topic_rolls_back_event(event_database: str) -> None:
    with pytest.raises(ValueError, match="topics must be unique"):
        with EventStore(event_database).transaction() as transaction:
            transaction.append(
                _event(),
                (
                    OutboundDraft(topic="work.events", payload={"copy": 1}),
                    OutboundDraft(topic="work.events", payload={"copy": 2}),
                ),
            )
    with psycopg.connect(event_database) as connection:
        count = connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.events"
        ).fetchone()[0]
    assert count == 0


def test_uncommitted_source_change_emits_nothing(event_database: str) -> None:
    store = EventStore(event_database)

    with pytest.raises(DatabaseError, match="event transaction failed"):
        with store.transaction() as transaction:
            transaction.execute(
                """
                UPDATE agentic_mesh_v5.work_items SET status = 'active'
                WHERE project_id = 'alpha' AND work_item_id = 'work-a'
                """
            )
            transaction.append(
                _event(),
                (OutboundDraft(topic="work.events", payload={"secret": False}),),
            )
            raise RuntimeError("injected failure")

    with psycopg.connect(event_database) as connection:
        status = connection.execute(
            """
            SELECT status FROM agentic_mesh_v5.work_items
            WHERE project_id = 'alpha' AND work_item_id = 'work-a'
            """
        ).fetchone()[0]
        counts = connection.execute(
            """
            SELECT (SELECT count(*) FROM agentic_mesh_v5.events),
                   (SELECT count(*) FROM agentic_mesh_v5.outbox)
            """
        ).fetchone()
    assert status == "new"
    assert counts == (0, 0)


def test_journal_is_ordered_correlated_and_project_isolated(event_database: str) -> None:
    store = EventStore(event_database)
    with store.transaction() as transaction:
        first = transaction.append(_event(event_type="work.started"))
        second = transaction.append(
            _event(event_type="work.progressed", correlation_id="corr-1")
        )
        transaction.append(
            _event(
                project_id="bravo",
                work_item_id="work-b",
                correlation_id="corr-bravo",
            )
        )

    alpha = store.read("alpha")
    assert [item.event_id for item in alpha] == [first.event_id, second.event_id]
    assert [item.event_type for item in alpha] == ["work.started", "work.progressed"]
    assert all(item.project_id == "alpha" for item in alpha)
    assert all(item.work_item_id == "work-a" for item in alpha)
    assert all(item.actor_id == "project-manager" for item in alpha)
    assert all(item.correlation_id == "corr-1" for item in alpha)
    assert store.read("alpha", after_event_id=first.event_id) == (alpha[1],)
    assert [item.project_id for item in store.read("bravo")] == ["bravo"]


def test_database_rejects_event_update_and_delete(event_database: str) -> None:
    with EventStore(event_database).transaction() as transaction:
        stored = transaction.append(_event())

    with psycopg.connect(event_database, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(
                "UPDATE agentic_mesh_v5.events SET actor_id = 'other' WHERE event_id = %s",
                (stored.event_id,),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "DELETE FROM agentic_mesh_v5.projects WHERE project_id = 'alpha'"
            )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(
                "DELETE FROM agentic_mesh_v5.events WHERE event_id = %s",
                (stored.event_id,),
            )


def test_successful_delivery_is_not_repeated(event_database: str) -> None:
    expected_key = _append_outbound(event_database)
    adapter = RecordingDelivery()
    dispatcher = OutboxDispatcher(event_database)

    result = dispatcher.dispatch_one(adapter)

    assert result.status == "delivered"
    assert result.idempotency_key == expected_key
    assert adapter.actions[0].idempotency_key == expected_key
    assert adapter.actions[0].attempt == 1
    assert dispatcher.dispatch_one(adapter).status == "empty"
    assert len(adapter.actions) == 1


def test_dispatch_requires_adapter_and_rejects_mismatched_receipt(
    event_database: str,
) -> None:
    _append_outbound(event_database)
    dispatcher = OutboxDispatcher(event_database)

    with pytest.raises(ValueError, match="idempotent delivery adapter"):
        dispatcher.dispatch_one(lambda _message: None)  # type: ignore[arg-type]

    class WrongReceipt:
        def deliver(self, _message):
            return DeliveryReceipt("different-key")

    result = dispatcher.dispatch_one(WrongReceipt())

    assert result.status == "failed"
    assert result.error == "delivery failed: ValueError"


def test_delivery_failure_records_redacted_error_then_retries(event_database: str) -> None:
    expected_key = _append_outbound(event_database)
    dispatcher = OutboxDispatcher(event_database)
    adapter = RecordingDelivery(failure=RuntimeError("password=do-not-store"))

    failed = dispatcher.dispatch_one(adapter)

    assert failed.status == "failed"
    assert failed.error == "delivery failed: RuntimeError"
    assert "do-not-store" not in failed.error
    with psycopg.connect(event_database) as connection:
        failed_state = connection.execute(
            """
            SELECT attempt_count, dispatched_at IS NULL, last_error,
                   available_at > clock_timestamp()
            FROM agentic_mesh_v5.outbox
            WHERE idempotency_key = %s
            """,
            (expected_key,),
        ).fetchone()
        connection.execute(
            """
            UPDATE agentic_mesh_v5.outbox
            SET available_at = clock_timestamp()
            WHERE idempotency_key = %s
            """,
            (expected_key,),
        )
    assert failed_state == (1, True, "delivery failed: RuntimeError", True)
    adapter.failure = None
    retried = dispatcher.dispatch_one(adapter)
    assert retried.status == "delivered"
    assert retried.idempotency_key == expected_key
    assert adapter.actions[0].attempt == 2
    with psycopg.connect(event_database) as connection:
        row = connection.execute(
            """
            SELECT attempt_count, dispatched_at IS NOT NULL, last_error
            FROM agentic_mesh_v5.outbox
            WHERE idempotency_key = %s
            """,
            (expected_key,),
        ).fetchone()
    assert row == (2, True, None)


def test_failed_message_backoff_allows_later_work_to_progress(
    event_database: str,
) -> None:
    first_key = _append_outbound(event_database)
    with EventStore(event_database).transaction() as transaction:
        transaction.append(
            _event(event_type="work.progressed", correlation_id="corr-2"),
            (OutboundDraft(topic="work.progress", payload={"step": 2}),),
        )
    dispatcher = OutboxDispatcher(event_database)

    poison = RecordingDelivery(failure=RuntimeError("poison"))
    assert dispatcher.dispatch_one(poison).idempotency_key == first_key
    adapter = RecordingDelivery()
    result = dispatcher.dispatch_one(adapter)

    assert result.status == "delivered"
    assert adapter.actions[0].idempotency_key != first_key


def test_crash_after_delivery_reuses_same_idempotency_key(event_database: str) -> None:
    class SimulatedCrash(BaseException):
        pass

    expected_key = _append_outbound(event_database)
    dispatcher = OutboxDispatcher(event_database)
    adapter = RecordingDelivery()
    adapter.after_receipt = lambda _message: (_ for _ in ()).throw(SimulatedCrash)

    with pytest.raises(SimulatedCrash):
        dispatcher.dispatch_one(adapter)
    adapter.after_receipt = None
    result = dispatcher.dispatch_one(adapter)

    assert result.status == "delivered"
    assert [message.idempotency_key for message in adapter.actions] == [expected_key]
    with psycopg.connect(event_database) as connection:
        attempts = connection.execute(
            """
            SELECT attempt_count FROM agentic_mesh_v5.outbox
            WHERE idempotency_key = %s
            """,
            (expected_key,),
        ).fetchone()[0]
    assert attempts == 1


def test_concurrent_dispatcher_skips_locked_message(event_database: str) -> None:
    _append_outbound(event_database)
    dispatcher = OutboxDispatcher(event_database)
    entered = threading.Event()
    release = threading.Event()

    def hold(_message) -> None:
        entered.set()
        assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(dispatcher.dispatch_one, RecordingDelivery(hold))
        assert entered.wait(timeout=5)
        second = pool.submit(dispatcher.dispatch_one, RecordingDelivery())
        assert second.result(timeout=5).status == "empty"
        release.set()
        assert first.result(timeout=5).status == "delivered"


def test_dispatch_project_filter_never_selects_foreign_work(event_database: str) -> None:
    _append_outbound(event_database, project_id="alpha")
    bravo_key = _append_outbound(event_database, project_id="bravo")
    dispatcher = OutboxDispatcher(event_database)
    adapter = RecordingDelivery()

    result = dispatcher.dispatch_one(adapter, project_id="bravo")

    assert result.idempotency_key == bravo_key
    assert adapter.actions[0].project_id == "bravo"
    assert dispatcher.dispatch_one(adapter, project_id="bravo").status == "empty"
