from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA


class QueueError(DatabaseError):
    pass


class QueueNotFound(QueueError):
    pass


class QueueConflict(QueueError):
    pass


class QueueAuthorizationError(QueueError):
    pass


class LeaseExpired(QueueConflict):
    pass


def _required(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


@dataclass(frozen=True)
class QueueItemRecord:
    project_id: str
    queue_item_id: str
    queue_id: str
    work_item_id: str
    status: str
    priority: int
    attempt_count: int
    available_at: str
    payload: dict[str, Any]
    idempotency_key: str


@dataclass(frozen=True)
class LeaseClaim:
    project_id: str
    lease_id: str
    lease_token: str
    queue_item: QueueItemRecord
    owner_instance_id: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str


@dataclass(frozen=True)
class QueueMetrics:
    project_id: str
    queue_id: str
    depth: int
    ready: int
    delayed: int
    leased: int
    oldest_ready_age_seconds: float | None
    total_attempts: int


class RoleQueueStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def create_queue(self, *, project_id: str, queue_id: str, role_id: str) -> None:
        project_id = _required(project_id, "project_id")
        queue_id = _required(queue_id, "queue_id")
        role_id = _required(role_id, "role_id")
        try:
            with psycopg.connect(self._database_url) as connection:
                connection.execute(
                    f"""
                    INSERT INTO {SCHEMA}.role_queues(project_id, queue_id, role_id)
                    VALUES (%s, %s, %s)
                    """,
                    (project_id, queue_id, role_id),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise QueueConflict("queue already exists") from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            raise QueueNotFound("project role not found") from exc

    def enqueue(
        self,
        *,
        project_id: str,
        queue_id: str,
        queue_item_id: str,
        work_item_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        priority: int = 0,
        available_at: datetime | None = None,
    ) -> QueueItemRecord:
        project_id = _required(project_id, "project_id")
        queue_id = _required(queue_id, "queue_id")
        queue_item_id = _required(queue_item_id, "queue_item_id")
        work_item_id = _required(work_item_id, "work_item_id")
        idempotency_key = _required(idempotency_key, "idempotency_key")
        if available_at is not None and (
            not isinstance(available_at, datetime)
            or available_at.tzinfo is None
            or available_at.utcoffset() is None
        ):
            raise ValueError("available_at must be a timezone-aware datetime")
        try:
            with psycopg.connect(self._database_url) as connection:
                connection.execute(
                    f"""
                    INSERT INTO {SCHEMA}.queue_items
                        (project_id, queue_item_id, queue_id, work_item_id,
                         priority, available_at, payload, idempotency_key)
                    VALUES (%s, %s, %s, %s, %s,
                            COALESCE(%s, clock_timestamp()), %s, %s)
                    """,
                    (
                        project_id,
                        queue_item_id,
                        queue_id,
                        work_item_id,
                        priority,
                        available_at,
                        Jsonb(payload),
                        idempotency_key,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise QueueConflict("queue item or idempotency key already exists") from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            raise QueueNotFound("queue or work item not found in project") from exc
        return self.get_item(project_id, queue_item_id)

    def claim(
        self,
        *,
        project_id: str,
        queue_id: str,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> LeaseClaim | None:
        project_id = _required(project_id, "project_id")
        queue_id = _required(queue_id, "queue_id")
        owner_instance_id = _required(owner_instance_id, "owner_instance_id")
        self._validate_lease_seconds(lease_seconds)
        lease_id = f"lease-{uuid.uuid4().hex}"
        lease_token = uuid.uuid4().hex
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            with connection.transaction():
                binding = connection.execute(
                    f"""
                    SELECT q.role_id, q.paused, i.role_id, i.status
                    FROM {SCHEMA}.role_queues q
                    JOIN {SCHEMA}.role_instances i
                      ON i.project_id = q.project_id
                     AND i.instance_id = %s
                    WHERE q.project_id = %s AND q.queue_id = %s
                    """,
                    (owner_instance_id, project_id, queue_id),
                ).fetchone()
                if binding is None:
                    raise QueueNotFound("queue or role instance not found")
                queue_role, paused, instance_role, instance_status = binding
                if paused:
                    raise QueueConflict("queue is paused")
                if queue_role != instance_role or instance_status != "running":
                    raise QueueAuthorizationError(
                        "role instance cannot claim this role queue"
                    )
                self._reclaim_expired(connection, project_id=project_id)
                row = connection.execute(
                    f"""
                    SELECT project_id, queue_item_id, queue_id, work_item_id,
                           status, priority, attempt_count, available_at::text,
                           payload, idempotency_key
                    FROM {SCHEMA}.queue_items
                    WHERE project_id = %s AND queue_id = %s
                      AND status = 'ready'
                      AND available_at <= clock_timestamp()
                    ORDER BY priority DESC, available_at, created_at, queue_item_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    (project_id, queue_id),
                ).fetchone()
                if row is None:
                    return None
                item = QueueItemRecord(*row)
                lease = connection.execute(
                    f"""
                    INSERT INTO {SCHEMA}.leases
                        (project_id, lease_id, queue_item_id, owner_instance_id,
                         lease_token, expires_at)
                    VALUES (%s, %s, %s, %s, %s,
                            clock_timestamp() + make_interval(secs => %s))
                    RETURNING acquired_at::text, heartbeat_at::text,
                              expires_at::text
                    """,
                    (
                        project_id,
                        lease_id,
                        item.queue_item_id,
                        owner_instance_id,
                        lease_token,
                        lease_seconds,
                    ),
                ).fetchone()
                attempt_count = connection.execute(
                    f"""
                    UPDATE {SCHEMA}.queue_items
                    SET status = 'leased', attempt_count = attempt_count + 1,
                        updated_at = clock_timestamp()
                    WHERE project_id = %s AND queue_item_id = %s
                    RETURNING attempt_count
                    """,
                    (project_id, item.queue_item_id),
                ).fetchone()[0]
                item = QueueItemRecord(
                    project_id=item.project_id,
                    queue_item_id=item.queue_item_id,
                    queue_id=item.queue_id,
                    work_item_id=item.work_item_id,
                    status="leased",
                    priority=item.priority,
                    attempt_count=attempt_count,
                    available_at=item.available_at,
                    payload=item.payload,
                    idempotency_key=item.idempotency_key,
                )
                return LeaseClaim(
                    project_id=project_id,
                    lease_id=lease_id,
                    lease_token=lease_token,
                    queue_item=item,
                    owner_instance_id=owner_instance_id,
                    acquired_at=lease[0],
                    heartbeat_at=lease[1],
                    expires_at=lease[2],
                )

    def heartbeat(
        self,
        *,
        project_id: str,
        lease_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> str:
        project_id, lease_id, lease_token = self._lease_identifiers(
            project_id, lease_id, lease_token
        )
        self._validate_lease_seconds(lease_seconds)
        with psycopg.connect(self._database_url) as connection:
            lease = self._lock_active_lease(connection, project_id, lease_id, lease_token)
            if lease[1]:
                raise LeaseExpired("lease has expired")
            return connection.execute(
                f"""
                UPDATE {SCHEMA}.leases
                SET heartbeat_at = clock_timestamp(),
                    expires_at = clock_timestamp() + make_interval(secs => %s)
                WHERE project_id = %s AND lease_id = %s
                RETURNING expires_at::text
                """,
                (lease_seconds, project_id, lease_id),
            ).fetchone()[0]

    def complete(
        self, *, project_id: str, lease_id: str, lease_token: str
    ) -> QueueItemRecord:
        return self._finish_lease(
            project_id=project_id,
            lease_id=lease_id,
            lease_token=lease_token,
            item_status="completed",
            reason="completed",
        )

    def release(
        self, *, project_id: str, lease_id: str, lease_token: str
    ) -> QueueItemRecord:
        return self._finish_lease(
            project_id=project_id,
            lease_id=lease_id,
            lease_token=lease_token,
            item_status="ready",
            reason="released",
        )

    def metrics(self, *, project_id: str, queue_id: str) -> QueueMetrics:
        project_id = _required(project_id, "project_id")
        queue_id = _required(queue_id, "queue_id")
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            exists = connection.execute(
                f"""
                SELECT 1 FROM {SCHEMA}.role_queues
                WHERE project_id = %s AND queue_id = %s
                """,
                (project_id, queue_id),
            ).fetchone()
            if exists is None:
                raise QueueNotFound("queue not found")
            row = connection.execute(
                f"""
                WITH observed AS (
                    SELECT clock_timestamp() AS now
                )
                SELECT
                    count(*) FILTER (WHERE status IN ('ready', 'leased')),
                    count(*) FILTER (
                        WHERE status = 'ready'
                          AND available_at <= (SELECT now FROM observed)
                    ),
                    count(*) FILTER (
                        WHERE status = 'ready'
                          AND available_at > (SELECT now FROM observed)
                    ),
                    count(*) FILTER (WHERE status = 'leased'),
                    EXTRACT(epoch FROM (SELECT now FROM observed) - min(available_at)
                        FILTER (WHERE status = 'ready'
                                      AND available_at <= (SELECT now FROM observed))),
                    COALESCE(sum(attempt_count), 0)
                FROM {SCHEMA}.queue_items
                WHERE project_id = %s AND queue_id = %s
                """,
                (project_id, queue_id),
            ).fetchone()
        return QueueMetrics(
            project_id=project_id,
            queue_id=queue_id,
            depth=row[0],
            ready=row[1],
            delayed=row[2],
            leased=row[3],
            oldest_ready_age_seconds=float(row[4]) if row[4] is not None else None,
            total_attempts=row[5],
        )

    def get_item(self, project_id: str, queue_item_id: str) -> QueueItemRecord:
        project_id = _required(project_id, "project_id")
        queue_item_id = _required(queue_item_id, "queue_item_id")
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            row = connection.execute(
                f"""
                SELECT project_id, queue_item_id, queue_id, work_item_id,
                       status, priority, attempt_count, available_at::text,
                       payload, idempotency_key
                FROM {SCHEMA}.queue_items
                WHERE project_id = %s AND queue_item_id = %s
                """,
                (project_id, queue_item_id),
            ).fetchone()
        if row is None:
            raise QueueNotFound("queue item not found")
        return QueueItemRecord(*row)

    def _finish_lease(
        self,
        *,
        project_id: str,
        lease_id: str,
        lease_token: str,
        item_status: str,
        reason: str,
    ) -> QueueItemRecord:
        project_id, lease_id, lease_token = self._lease_identifiers(
            project_id, lease_id, lease_token
        )
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            with connection.transaction():
                lease = self._lock_active_lease(
                    connection, project_id, lease_id, lease_token
                )
                queue_item_id, expired = lease
                if expired:
                    raise LeaseExpired("lease has expired")
                connection.execute(
                    f"""
                    UPDATE {SCHEMA}.leases
                    SET released_at = clock_timestamp(), released_reason = %s
                    WHERE project_id = %s AND lease_id = %s
                    """,
                    (reason, project_id, lease_id),
                )
                connection.execute(
                    f"""
                    UPDATE {SCHEMA}.queue_items
                    SET status = %s, updated_at = clock_timestamp()
                    WHERE project_id = %s AND queue_item_id = %s
                    """,
                    (item_status, project_id, queue_item_id),
                )
        return self.get_item(project_id, queue_item_id)

    @staticmethod
    def _lock_active_lease(connection, project_id, lease_id, lease_token):
        row = connection.execute(
            f"""
            SELECT queue_item_id, expires_at <= clock_timestamp()
            FROM {SCHEMA}.leases
            WHERE project_id = %s AND lease_id = %s
              AND lease_token = %s AND released_at IS NULL
            FOR UPDATE
            """,
            (project_id, lease_id, lease_token),
        ).fetchone()
        if row is None:
            raise QueueAuthorizationError("active lease token is invalid")
        return row

    @staticmethod
    def _reclaim_expired(connection, *, project_id: str) -> None:
        connection.execute(
            f"""
            WITH expired AS (
                UPDATE {SCHEMA}.leases
                SET released_at = clock_timestamp(), released_reason = 'expired'
                WHERE project_id = %s AND released_at IS NULL
                  AND expires_at <= clock_timestamp()
                RETURNING queue_item_id
            )
            UPDATE {SCHEMA}.queue_items AS item
            SET status = 'ready', updated_at = clock_timestamp()
            FROM expired
            WHERE item.project_id = %s
              AND item.queue_item_id = expired.queue_item_id
              AND item.status = 'leased'
            """,
            (project_id, project_id),
        )

    @staticmethod
    def _validate_lease_seconds(value: int) -> None:
        if not isinstance(value, int) or value < 1 or value > 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")

    @staticmethod
    def _lease_identifiers(project_id, lease_id, lease_token):
        return (
            _required(project_id, "project_id"),
            _required(lease_id, "lease_id"),
            _required(lease_token, "lease_token"),
        )
