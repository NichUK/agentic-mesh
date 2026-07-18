from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

import psycopg

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.routing import RouteDraft
from agentic_mesh_v5.routing import Router
from agentic_mesh_v5.routing import RoutingConflict
from agentic_mesh_v5.routing import RoutingNotFound


_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_STORE_ERROR = "handoff operation failed"


class HandoffError(DatabaseError):
    pass


class HandoffNotFound(HandoffError):
    pass


class HandoffConflict(HandoffError):
    pass


class HandoffAuthorizationError(HandoffError):
    pass


@dataclass(frozen=True, slots=True)
class HandoffOffer:
    project_id: str
    source_lease_id: str
    source_lease_token: str
    target_role_id: str
    idempotency_key: str
    summary: str
    payload: dict[str, Any]
    capability: str | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "project_id",
            "source_lease_id",
            "target_role_id",
            "idempotency_key",
        ):
            object.__setattr__(
                self, field_name, _identifier(getattr(self, field_name), field_name)
            )
        if self.capability is not None:
            object.__setattr__(
                self, "capability", _identifier(self.capability, "capability")
            )
        object.__setattr__(
            self,
            "source_lease_token",
            _required(self.source_lease_token, "source_lease_token", 512),
        )
        object.__setattr__(self, "summary", _required(self.summary, "summary", 4000))
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")
        object.__setattr__(self, "payload", dict(self.payload))
        if (
            type(self.priority) is not int
            or self.priority < -2_147_483_648
            or self.priority > 2_147_483_647
        ):
            raise ValueError("priority is invalid")


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    project_id: str
    handoff_id: str
    work_item_id: str
    source_role_id: str
    source_instance_id: str | None
    source_queue_item_id: str | None
    source_lease_id: str | None
    target_role_id: str
    target_instance_id: str | None
    queue_item_id: str | None
    target_lease_id: str | None
    status: str
    summary: str
    idempotency_key: str
    offered_at: str
    queued_at: str
    claimed_at: str | None
    accepted_at: str | None
    delivery_latency_seconds: float
    claim_latency_seconds: float | None
    acceptance_latency_seconds: float | None
    delivery_target_met: bool
    claim_target_met: bool | None
    acceptance_target_met: bool | None
    claim_overdue: bool
    acceptance_overdue: bool


_RECORD_COLUMNS = f"""
    handoff.project_id, handoff.handoff_id, handoff.work_item_id,
    handoff.source_role_id, handoff.source_instance_id,
    handoff.source_queue_item_id, handoff.source_lease_id,
    handoff.target_role_id, handoff.target_instance_id,
    handoff.queue_item_id, handoff.target_lease_id, handoff.status,
    handoff.summary, handoff.idempotency_key, handoff.offered_at::text,
    handoff.queued_at::text, handoff.claimed_at::text,
    handoff.accepted_at::text,
    EXTRACT(epoch FROM handoff.queued_at - handoff.offered_at)::float8,
    CASE WHEN handoff.claimed_at IS NULL THEN NULL ELSE
        EXTRACT(epoch FROM handoff.claimed_at - handoff.queued_at)::float8 END,
    CASE WHEN handoff.accepted_at IS NULL THEN NULL ELSE
        EXTRACT(epoch FROM handoff.accepted_at - handoff.claimed_at)::float8 END,
    handoff.queued_at <= handoff.offered_at + interval '10 seconds',
    CASE WHEN handoff.claimed_at IS NULL THEN NULL ELSE
        handoff.claimed_at <= handoff.queued_at + interval '90 seconds' END,
    CASE WHEN handoff.accepted_at IS NULL THEN NULL ELSE
        handoff.accepted_at <= handoff.claimed_at + interval '120 seconds' END,
    handoff.claimed_at IS NULL
        AND clock_timestamp() > handoff.queued_at + interval '90 seconds',
    handoff.claimed_at IS NOT NULL AND handoff.accepted_at IS NULL
        AND clock_timestamp() > handoff.claimed_at + interval '120 seconds'
"""


class HandoffStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._router = Router(database_url)

    def offer(self, draft: HandoffOffer) -> HandoffRecord:
        if not isinstance(draft, HandoffOffer):
            raise ValueError("handoff offer is invalid")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                with connection.transaction():
                    source = self._source_lease(connection, draft)
                    fingerprint = _fingerprint(draft, source)
                    existing = self._existing(
                        connection, draft.project_id, draft.idempotency_key
                    )
                    if existing is not None:
                        if existing[1] != fingerprint:
                            raise HandoffConflict(
                                "idempotency key is already used by different work"
                            )
                        return self._get(connection, draft.project_id, existing[0])
                    if (
                        source[4] != "leased"
                        or source[5] != "running"
                        or source[6]
                        or source[7]
                    ):
                        raise HandoffAuthorizationError(
                            "source lease is not active"
                        )
                    offered_at = connection.execute(
                        "SELECT clock_timestamp()"
                    ).fetchone()[0]
                    handoff_id = _handoff_id(
                        draft.project_id, draft.idempotency_key
                    )
                    target_payload = {
                        "handoff": {
                            "handoff_id": handoff_id,
                            "source_role_id": source[3],
                            "source_instance_id": source[2],
                            "summary": draft.summary,
                        },
                        "payload": draft.payload,
                    }
                    routed = self._router.route_in_transaction(
                        connection,
                        RouteDraft(
                            project_id=draft.project_id,
                            work_item_id=source[1],
                            target_role_id=draft.target_role_id,
                            capability=draft.capability,
                            idempotency_key=draft.idempotency_key,
                            payload=target_payload,
                            priority=draft.priority,
                        ),
                    )
                    queued_at = connection.execute(
                        f"""
                        SELECT created_at FROM {SCHEMA}.queue_items
                        WHERE project_id = %s AND queue_item_id = %s
                        """,
                        (draft.project_id, routed.queue_item_id),
                    ).fetchone()[0]
                    if queued_at < offered_at:
                        raise HandoffConflict(
                            "idempotency key is already used by routed work "
                            "without a handoff"
                        )
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.handoffs
                            (project_id, handoff_id, work_item_id,
                             source_role_id, source_instance_id,
                             source_queue_item_id, source_lease_id,
                             target_role_id, queue_item_id, status, summary,
                             idempotency_key, offered_at, queued_at,
                             request_fingerprint)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                'offered', %s, %s, %s, %s, %s)
                        """,
                        (
                            draft.project_id,
                            handoff_id,
                            source[1],
                            source[3],
                            source[2],
                            source[0],
                            draft.source_lease_id,
                            draft.target_role_id,
                            routed.queue_item_id,
                            draft.summary,
                            draft.idempotency_key,
                            offered_at,
                            queued_at,
                            fingerprint,
                        ),
                    )
                    return self._get(connection, draft.project_id, handoff_id)
        except HandoffError:
            raise
        except RoutingNotFound as exc:
            raise HandoffNotFound(str(exc)) from None
        except RoutingConflict as exc:
            raise HandoffConflict(str(exc)) from None
        except psycopg.errors.ForeignKeyViolation:
            raise HandoffNotFound("handoff resource not found in project") from None
        except psycopg.errors.UniqueViolation:
            raise HandoffConflict("handoff already exists") from None
        except Exception:
            raise HandoffError(_STORE_ERROR) from None

    def claim(
        self,
        *,
        project_id: str,
        handoff_id: str,
        lease_id: str,
        lease_token: str,
    ) -> HandoffRecord:
        identifiers = self._transition_identifiers(
            project_id, handoff_id, lease_id, lease_token
        )
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    row = self._lock(connection, identifiers[0], identifiers[1])
                    if row[0] == "accepted":
                        self._historical_target_lease(connection, row, identifiers)
                        return self._get(connection, identifiers[0], identifiers[1])
                    target = self._active_target_lease(connection, row, identifiers)
                    if row[0] == "claimed" and row[2] != identifiers[2]:
                        previous_active = connection.execute(
                            f"""
                            SELECT 1 FROM {SCHEMA}.leases
                            WHERE project_id = %s AND lease_id = %s
                              AND released_at IS NULL
                              AND expires_at > clock_timestamp()
                            """,
                            (identifiers[0], row[2]),
                        ).fetchone()
                        if previous_active is not None:
                            raise HandoffConflict(
                                "handoff is held by another active lease"
                            )
                    if row[0] == "offered" or row[2] != identifiers[2]:
                        connection.execute(
                            f"""
                            UPDATE {SCHEMA}.handoffs
                            SET status = 'claimed', target_instance_id = %s,
                                target_lease_id = %s,
                                claimed_at = clock_timestamp(), accepted_at = NULL
                            WHERE project_id = %s AND handoff_id = %s
                            """,
                            (target, identifiers[2], identifiers[0], identifiers[1]),
                        )
                    return self._get(connection, identifiers[0], identifiers[1])
        except HandoffError:
            raise
        except Exception:
            raise HandoffError(_STORE_ERROR) from None

    def accept(
        self,
        *,
        project_id: str,
        handoff_id: str,
        lease_id: str,
        lease_token: str,
    ) -> HandoffRecord:
        identifiers = self._transition_identifiers(
            project_id, handoff_id, lease_id, lease_token
        )
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    row = self._lock(connection, identifiers[0], identifiers[1])
                    if row[0] == "offered":
                        raise HandoffConflict("handoff must be claimed before acceptance")
                    if row[2] != identifiers[2]:
                        raise HandoffAuthorizationError(
                            "target lease does not own this handoff"
                        )
                    if row[0] == "accepted":
                        self._historical_target_lease(connection, row, identifiers)
                        return self._get(connection, identifiers[0], identifiers[1])
                    self._active_target_lease(connection, row, identifiers)
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.handoffs
                        SET status = 'accepted', accepted_at = clock_timestamp()
                        WHERE project_id = %s AND handoff_id = %s
                        """,
                        (identifiers[0], identifiers[1]),
                    )
                    return self._get(connection, identifiers[0], identifiers[1])
        except HandoffError:
            raise
        except Exception:
            raise HandoffError(_STORE_ERROR) from None

    def get(self, project_id: str, handoff_id: str) -> HandoffRecord:
        project_id = _identifier(project_id, "project_id")
        handoff_id = _identifier(handoff_id, "handoff_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                return self._get(connection, project_id, handoff_id)
        except HandoffError:
            raise
        except Exception:
            raise HandoffError(_STORE_ERROR) from None

    @staticmethod
    def _source_lease(connection, draft: HandoffOffer) -> tuple[Any, ...]:
        row = connection.execute(
            f"""
            SELECT lease.queue_item_id, item.work_item_id,
                   lease.owner_instance_id, instance.role_id,
                   item.status, instance.status,
                   lease.released_at IS NOT NULL,
                   lease.expires_at <= clock_timestamp()
            FROM {SCHEMA}.leases AS lease
            JOIN {SCHEMA}.queue_items AS item
              ON item.project_id = lease.project_id
             AND item.queue_item_id = lease.queue_item_id
            JOIN {SCHEMA}.role_instances AS instance
              ON instance.project_id = lease.project_id
             AND instance.instance_id = lease.owner_instance_id
            WHERE lease.project_id = %s AND lease.lease_id = %s
              AND lease.lease_token = %s
            FOR UPDATE OF lease
            """,
            (draft.project_id, draft.source_lease_id, draft.source_lease_token),
        ).fetchone()
        if row is None:
            raise HandoffAuthorizationError("source lease token is invalid")
        return row

    @staticmethod
    def _existing(connection, project_id: str, idempotency_key: str):
        return connection.execute(
            f"""
            SELECT handoff_id, request_fingerprint
            FROM {SCHEMA}.handoffs
            WHERE project_id = %s AND idempotency_key = %s
            """,
            (project_id, idempotency_key),
        ).fetchone()

    @staticmethod
    def _lock(connection, project_id: str, handoff_id: str):
        row = connection.execute(
            f"""
            SELECT status, queue_item_id, target_lease_id, target_role_id
            FROM {SCHEMA}.handoffs
            WHERE project_id = %s AND handoff_id = %s
            FOR UPDATE
            """,
            (project_id, handoff_id),
        ).fetchone()
        if row is None:
            raise HandoffNotFound("handoff not found")
        return row

    @staticmethod
    def _active_target_lease(connection, handoff, identifiers) -> str:
        row = connection.execute(
            f"""
            SELECT lease.owner_instance_id
            FROM {SCHEMA}.leases AS lease
            JOIN {SCHEMA}.role_instances AS instance
              ON instance.project_id = lease.project_id
             AND instance.instance_id = lease.owner_instance_id
            WHERE lease.project_id = %s AND lease.lease_id = %s
              AND lease.lease_token = %s AND lease.queue_item_id = %s
              AND lease.released_at IS NULL
              AND lease.expires_at > clock_timestamp()
              AND instance.role_id = %s AND instance.status = 'running'
            """,
            (
                identifiers[0], identifiers[2], identifiers[3],
                handoff[1], handoff[3],
            ),
        ).fetchone()
        if row is None:
            raise HandoffAuthorizationError(
                "active target lease token is invalid for this handoff"
            )
        return row[0]

    @staticmethod
    def _historical_target_lease(connection, handoff, identifiers) -> None:
        if handoff[2] != identifiers[2]:
            raise HandoffAuthorizationError("target lease does not own this handoff")
        row = connection.execute(
            f"""
            SELECT 1 FROM {SCHEMA}.leases
            WHERE project_id = %s AND lease_id = %s AND lease_token = %s
              AND queue_item_id = %s
            """,
            (identifiers[0], identifiers[2], identifiers[3], handoff[1]),
        ).fetchone()
        if row is None:
            raise HandoffAuthorizationError("target lease token is invalid")

    @staticmethod
    def _get(connection, project_id: str, handoff_id: str) -> HandoffRecord:
        row = connection.execute(
            f"""
            SELECT {_RECORD_COLUMNS}
            FROM {SCHEMA}.handoffs AS handoff
            WHERE handoff.project_id = %s AND handoff.handoff_id = %s
            """,
            (project_id, handoff_id),
        ).fetchone()
        if row is None:
            raise HandoffNotFound("handoff not found")
        return HandoffRecord(*row)

    @staticmethod
    def _transition_identifiers(project_id, handoff_id, lease_id, lease_token):
        return (
            _identifier(project_id, "project_id"),
            _identifier(handoff_id, "handoff_id"),
            _identifier(lease_id, "lease_id"),
            _required(lease_token, "lease_token", 512),
        )


def _handoff_id(project_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"{project_id}\x00{idempotency_key}".encode("utf-8")
    ).hexdigest()[:32]
    return f"handoff-{digest}"


def _fingerprint(draft: HandoffOffer, source: tuple[Any, ...]) -> str:
    try:
        encoded = json.dumps(
            {
                "source_queue_item_id": source[0],
                "source_lease_id": draft.source_lease_id,
                "source_instance_id": source[2],
                "source_role_id": source[3],
                "target_role_id": draft.target_role_id,
                "capability": draft.capability,
                "summary": draft.summary,
                "payload": draft.payload,
                "priority": draft.priority,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise ValueError("handoff payload must contain JSON values") from None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    selected = value.strip()
    if _IDENTIFIER.fullmatch(selected) is None:
        raise ValueError(f"{field_name} is invalid")
    return selected


def _required(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    selected = value.strip()
    if not selected or len(selected) > maximum:
        raise ValueError(f"{field_name} is invalid")
    return selected
