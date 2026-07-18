from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA


def _required(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


@dataclass(frozen=True)
class OutboundDraft:
    topic: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "topic", _required(self.topic, "topic"))


@dataclass(frozen=True)
class EventDraft:
    project_id: str
    work_item_id: str
    actor_id: str
    correlation_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: Mapping[str, Any]
    causation_id: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "project_id",
            "work_item_id",
            "actor_id",
            "correlation_id",
            "aggregate_type",
            "aggregate_id",
            "event_type",
        ):
            object.__setattr__(self, field, _required(getattr(self, field), field))
        if self.causation_id is not None:
            object.__setattr__(
                self, "causation_id", _required(self.causation_id, "causation_id")
            )


@dataclass(frozen=True)
class StoredEvent:
    event_id: int
    project_id: str
    work_item_id: str
    actor_id: str
    correlation_id: str
    causation_id: str | None
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: Mapping[str, Any]
    occurred_at: str


@dataclass(frozen=True)
class OutboxMessage:
    outbox_id: int
    event_id: int
    project_id: str
    work_item_id: str
    actor_id: str
    correlation_id: str
    topic: str
    payload: Mapping[str, Any]
    idempotency_key: str
    attempt: int


@dataclass(frozen=True)
class DispatchResult:
    status: Literal["empty", "delivered", "failed"]
    outbox_id: int | None = None
    idempotency_key: str | None = None
    error: str | None = None


class EventUnitOfWork:
    def __init__(self, connection: psycopg.Connection[object]) -> None:
        self._connection = connection

    def execute(
        self, statement: str, parameters: Sequence[object] = ()
    ) -> psycopg.Cursor[object]:
        return self._connection.execute(statement, parameters)

    def append(
        self,
        event: EventDraft,
        outbound: Sequence[OutboundDraft] = (),
    ) -> StoredEvent:
        topics = [item.topic for item in outbound]
        if len(topics) != len(set(topics)):
            raise ValueError("outbound topics must be unique within one event")
        row = self._connection.execute(
            f"""
            INSERT INTO {SCHEMA}.events
                (project_id, work_item_id, actor_id, correlation_id, causation_id,
                 aggregate_type, aggregate_id, event_type, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING event_id, occurred_at::text
            """,
            (
                event.project_id,
                event.work_item_id,
                event.actor_id,
                event.correlation_id,
                event.causation_id,
                event.aggregate_type,
                event.aggregate_id,
                event.event_type,
                Jsonb(dict(event.payload)),
            ),
        ).fetchone()
        event_id, occurred_at = row
        for message in outbound:
            digest = hashlib.sha256(message.topic.encode("utf-8")).hexdigest()[:16]
            idempotency_key = f"{event.project_id}:{event_id}:{digest}"
            self._connection.execute(
                f"""
                INSERT INTO {SCHEMA}.outbox
                    (project_id, event_id, topic, payload, idempotency_key)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    event.project_id,
                    event_id,
                    message.topic,
                    Jsonb(dict(message.payload)),
                    idempotency_key,
                ),
            )
        return StoredEvent(
            event_id=event_id,
            project_id=event.project_id,
            work_item_id=event.work_item_id,
            actor_id=event.actor_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=dict(event.payload),
            occurred_at=occurred_at,
        )


class EventStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    @contextmanager
    def transaction(self) -> Iterator[EventUnitOfWork]:
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    yield EventUnitOfWork(connection)
        except (ValueError, DatabaseError):
            raise
        except Exception as exc:
            raise DatabaseError("event transaction failed") from exc

    def read(
        self, project_id: str, *, after_event_id: int = 0, limit: int = 100
    ) -> tuple[StoredEvent, ...]:
        project_id = _required(project_id, "project_id")
        if after_event_id < 0:
            raise ValueError("after_event_id cannot be negative")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                rows = connection.execute(
                    f"""
                    SELECT event_id, project_id, work_item_id, actor_id,
                           correlation_id, causation_id, aggregate_type,
                           aggregate_id, event_type, payload, occurred_at::text
                    FROM {SCHEMA}.events
                    WHERE project_id = %s AND event_id > %s
                    ORDER BY event_id
                    LIMIT %s
                    """,
                    (project_id, after_event_id, limit),
                ).fetchall()
        except Exception as exc:
            raise DatabaseError("event journal read failed") from exc
        return tuple(StoredEvent(*row) for row in rows)


class OutboxDispatcher:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def dispatch_one(
        self,
        deliver: Callable[[OutboxMessage], object],
        *,
        project_id: str | None = None,
    ) -> DispatchResult:
        if project_id is not None:
            project_id = _required(project_id, "project_id")
        project_filter = "" if project_id is None else "AND o.project_id = %s"
        parameters: tuple[object, ...] = () if project_id is None else (project_id,)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    row = connection.execute(
                        f"""
                        SELECT o.outbox_id, o.event_id, o.project_id,
                               e.work_item_id, e.actor_id, e.correlation_id,
                               o.topic, o.payload, o.idempotency_key,
                               o.attempt_count
                        FROM {SCHEMA}.outbox o
                        JOIN {SCHEMA}.events e
                          ON e.project_id = o.project_id
                         AND e.event_id = o.event_id
                        WHERE o.dispatched_at IS NULL
                          AND o.available_at <= clock_timestamp()
                          {project_filter}
                        ORDER BY o.project_id, o.available_at, o.outbox_id
                        FOR UPDATE OF o SKIP LOCKED
                        LIMIT 1
                        """,
                        parameters,
                    ).fetchone()
                    if row is None:
                        return DispatchResult(status="empty")
                    message = OutboxMessage(*row[:-1], attempt=row[-1] + 1)
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.outbox
                        SET attempt_count = attempt_count + 1, last_error = NULL
                        WHERE project_id = %s AND outbox_id = %s
                        """,
                        (message.project_id, message.outbox_id),
                    )
                    try:
                        deliver(message)
                    except Exception as exc:
                        error = f"delivery failed: {type(exc).__name__}"
                        connection.execute(
                            f"""
                            UPDATE {SCHEMA}.outbox
                            SET last_error = %s
                            WHERE project_id = %s AND outbox_id = %s
                            """,
                            (error, message.project_id, message.outbox_id),
                        )
                        return DispatchResult(
                            status="failed",
                            outbox_id=message.outbox_id,
                            idempotency_key=message.idempotency_key,
                            error=error,
                        )
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.outbox
                        SET dispatched_at = clock_timestamp(), last_error = NULL
                        WHERE project_id = %s AND outbox_id = %s
                        """,
                        (message.project_id, message.outbox_id),
                    )
                    return DispatchResult(
                        status="delivered",
                        outbox_id=message.outbox_id,
                        idempotency_key=message.idempotency_key,
                    )
        except (ValueError, DatabaseError):
            raise
        except Exception as exc:
            raise DatabaseError("outbox dispatch failed") from exc
