from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.queues import QueueItemRecord


_STORE_ERROR = "routing operation failed"
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ITEM_COLUMNS = """
    item.project_id, item.queue_item_id, item.queue_id, item.work_item_id,
    item.status, item.priority, item.attempt_count, item.available_at::text,
    item.payload, item.idempotency_key
"""


class RoutingError(DatabaseError):
    pass


class RoutingNotFound(RoutingError):
    pass


class RoutingConflict(RoutingError):
    pass


@dataclass(frozen=True, slots=True)
class RouteDraft:
    project_id: str
    work_item_id: str
    target_role_id: str
    idempotency_key: str
    payload: dict[str, object]
    capability: str | None = None
    priority: int = 0
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "project_id",
            "work_item_id",
            "target_role_id",
            "idempotency_key",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if self.capability is not None:
            object.__setattr__(
                self, "capability", _identifier(self.capability, "capability")
            )
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")
        object.__setattr__(self, "payload", dict(self.payload))
        if (
            type(self.priority) is not int
            or self.priority < -2_147_483_648
            or self.priority > 2_147_483_647
        ):
            raise ValueError("priority is invalid")
        if self.available_at is not None:
            if (
                not isinstance(self.available_at, datetime)
                or self.available_at.tzinfo is None
                or self.available_at.utcoffset() is None
            ):
                raise ValueError("available_at must be a timezone-aware datetime")
            object.__setattr__(
                self, "available_at", self.available_at.astimezone(timezone.utc)
            )


class Router:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def route(self, draft: RouteDraft) -> QueueItemRecord:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                with connection.transaction():
                    return self.route_in_transaction(connection, draft)
        except RoutingError:
            raise
        except psycopg.errors.ForeignKeyViolation:
            raise RoutingNotFound("route target not found in project") from None
        except psycopg.errors.UniqueViolation:
            raise RoutingConflict("route idempotency conflict") from None
        except Exception:
            raise RoutingError(_STORE_ERROR) from None

    def route_in_transaction(
        self,
        connection: psycopg.Connection[object],
        draft: RouteDraft,
    ) -> QueueItemRecord:
        """Route using the caller's transaction for atomic composition."""
        if not isinstance(draft, RouteDraft):
            raise ValueError("route draft is invalid")
        lock_key = f"{draft.project_id}\x1f{draft.idempotency_key}"
        fingerprint = _fingerprint(draft)
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_key,),
        )
        existing = self._existing(connection, draft, fingerprint)
        if existing is not None:
            return existing
        if connection.execute(
            f"""
            SELECT 1 FROM {SCHEMA}.work_items
            WHERE project_id = %s AND work_item_id = %s
            """,
            (draft.project_id, draft.work_item_id),
        ).fetchone() is None:
            raise RoutingNotFound("project work item not found")
        target = connection.execute(
            f"""
            SELECT queue.queue_id
            FROM {SCHEMA}.role_queues AS queue
            JOIN {SCHEMA}.roles AS role
              ON role.project_id = queue.project_id
             AND role.role_id = queue.role_id
            WHERE queue.project_id = %s AND queue.role_id = %s
              AND queue.capability IS NOT DISTINCT FROM %s
              AND NOT queue.paused AND role.status = 'active'
            """,
            (draft.project_id, draft.target_role_id, draft.capability),
        ).fetchone()
        if target is None:
            raise RoutingNotFound("active role capability target not found")
        queue_item_id = _route_item_id(draft.project_id, draft.idempotency_key)
        row = connection.execute(
            f"""
            INSERT INTO {SCHEMA}.queue_items
                (project_id, queue_item_id, queue_id, work_item_id,
                 priority, available_at, payload, idempotency_key,
                 route_fingerprint)
            VALUES (%s, %s, %s, %s, %s,
                    COALESCE(%s, clock_timestamp()), %s, %s, %s)
            RETURNING project_id, queue_item_id, queue_id,
                      work_item_id, status, priority, attempt_count,
                      available_at::text, payload, idempotency_key
            """,
            (
                draft.project_id,
                queue_item_id,
                target[0],
                draft.work_item_id,
                draft.priority,
                draft.available_at,
                Jsonb(draft.payload),
                draft.idempotency_key,
                fingerprint,
            ),
        ).fetchone()
        return _record(row)

    @staticmethod
    def _existing(
        connection: psycopg.Connection[object],
        draft: RouteDraft,
        fingerprint: str,
    ) -> QueueItemRecord | None:
        row = connection.execute(
            f"""
            SELECT {_ITEM_COLUMNS}, item.route_fingerprint
            FROM {SCHEMA}.queue_items AS item
            JOIN {SCHEMA}.role_queues AS queue
              ON queue.project_id = item.project_id
             AND queue.queue_id = item.queue_id
            WHERE item.project_id = %s AND item.idempotency_key = %s
            """,
            (draft.project_id, draft.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row[10] != fingerprint:
            raise RoutingConflict("idempotency key is already used by different work")
        return _record(row[:10])


def _record(row: object) -> QueueItemRecord:
    if not isinstance(row, (tuple, list)) or len(row) != 10:
        raise RoutingError(_STORE_ERROR)
    return QueueItemRecord(*row)


def _route_item_id(project_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"{project_id}\x00{idempotency_key}".encode("utf-8")
    ).hexdigest()[:32]
    return f"route-{digest}"


def _fingerprint(draft: RouteDraft) -> str:
    try:
        canonical = json.dumps(
            {
                "work_item_id": draft.work_item_id,
                "target_role_id": draft.target_role_id,
                "capability": draft.capability,
                "priority": draft.priority,
                "available_at": (
                    None if draft.available_at is None else draft.available_at.isoformat()
                ),
                "payload": draft.payload,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise ValueError("route payload must contain JSON values") from None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    selected = value.strip()
    if _ID.fullmatch(selected) is None:
        raise ValueError(f"{field_name} is invalid")
    return selected
