from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any
import uuid

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.routing import RouteDraft
from agentic_mesh_v5.routing import Router
from agentic_mesh_v5.routing import RoutingNotFound


LOGICAL_PM_ID = "global-project-manager"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_STORE_ERROR = "PM continuation monitor operation failed"


class ContinuationError(DatabaseError):
    pass


class ContinuationConflict(ContinuationError):
    pass


class ContinuationAuthorizationError(ContinuationError):
    pass


@dataclass(frozen=True, slots=True)
class MonitorClaim:
    logical_pm_id: str
    owner_id: str
    lease_token: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class MonitorStatus:
    logical_pm_id: str
    owner_id: str
    heartbeat_at: str
    expires_at: str
    active: bool
    sweep_count: int
    last_sweep_at: str | None


@dataclass(frozen=True, slots=True)
class ContinuationObservation:
    project_id: str
    work_item_id: str
    work_version: int
    disposition: str
    action_id: str | None
    detail: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class SweepResult:
    logical_pm_id: str
    owner_id: str
    sweep_count: int
    observations: tuple[ContinuationObservation, ...]


class ContinuationMonitor:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._router = Router(database_url)
        self._lifecycle = LifecycleStore(database_url)

    def claim(self, *, owner_id: str, lease_seconds: int) -> MonitorClaim:
        owner_id = _identifier(owner_id, "owner_id")
        _lease_seconds(lease_seconds)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (LOGICAL_PM_ID,),
                    )
                    row = connection.execute(
                        f"""
                        SELECT owner_id, lease_token,
                               acquired_at::text, heartbeat_at::text,
                               expires_at::text,
                               expires_at > clock_timestamp()
                        FROM {SCHEMA}.pm_monitor_lease
                        WHERE monitor_id = %s FOR UPDATE
                        """,
                        (LOGICAL_PM_ID,),
                    ).fetchone()
                    if row is not None and row[5]:
                        if row[0] != owner_id:
                            raise ContinuationConflict(
                                "global PM monitor already has an active owner"
                            )
                        return MonitorClaim(LOGICAL_PM_ID, owner_id, *row[1:5])
                    token = uuid.uuid4().hex
                    action = (
                        "pm-monitor.claimed"
                        if row is None or row[0] == owner_id
                        else "pm-monitor.taken-over"
                    )
                    result = connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.pm_monitor_lease
                            (monitor_id, owner_id, lease_token, lease_seconds,
                             expires_at)
                        VALUES (%s, %s, %s, %s,
                                clock_timestamp() + make_interval(secs => %s))
                        ON CONFLICT (monitor_id) DO UPDATE
                        SET owner_id = EXCLUDED.owner_id,
                            lease_token = EXCLUDED.lease_token,
                            lease_seconds = EXCLUDED.lease_seconds,
                            acquired_at = clock_timestamp(),
                            heartbeat_at = clock_timestamp(),
                            expires_at = clock_timestamp()
                                + make_interval(secs => EXCLUDED.lease_seconds)
                        RETURNING acquired_at::text, heartbeat_at::text,
                                  expires_at::text
                        """,
                        (LOGICAL_PM_ID, owner_id, token, lease_seconds, lease_seconds),
                    ).fetchone()
                    self._audit(
                        connection, None, owner_id, action, LOGICAL_PM_ID,
                        {"previous_owner_id": None if row is None else row[0]},
                    )
                    return MonitorClaim(LOGICAL_PM_ID, owner_id, token, *result)
        except ContinuationError:
            raise
        except Exception:
            raise ContinuationError(_STORE_ERROR) from None

    def heartbeat(self, *, owner_id: str, lease_token: str) -> MonitorStatus:
        owner_id = _identifier(owner_id, "owner_id")
        lease_token = _required(lease_token, "lease_token")
        try:
            with psycopg.connect(self._database_url) as connection:
                row = connection.execute(
                    f"""
                    UPDATE {SCHEMA}.pm_monitor_lease
                    SET heartbeat_at = clock_timestamp(),
                        expires_at = clock_timestamp()
                            + make_interval(secs => lease_seconds)
                    WHERE monitor_id = %s AND owner_id = %s
                      AND lease_token = %s
                      AND expires_at > clock_timestamp()
                    RETURNING owner_id, heartbeat_at::text, expires_at::text,
                              sweep_count, last_sweep_at::text
                    """,
                    (LOGICAL_PM_ID, owner_id, lease_token),
                ).fetchone()
            if row is None:
                raise ContinuationAuthorizationError(
                    "active global PM monitor lease is invalid"
                )
            return MonitorStatus(LOGICAL_PM_ID, row[0], row[1], row[2], True, row[3], row[4])
        except ContinuationError:
            raise
        except Exception:
            raise ContinuationError(_STORE_ERROR) from None

    def status(self) -> MonitorStatus:
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                row = connection.execute(
                    f"""
                    SELECT owner_id, heartbeat_at::text, expires_at::text,
                           expires_at > clock_timestamp(), sweep_count,
                           last_sweep_at::text
                    FROM {SCHEMA}.pm_monitor_lease
                    WHERE monitor_id = %s
                    """,
                    (LOGICAL_PM_ID,),
                ).fetchone()
            if row is None:
                raise ContinuationConflict("global PM monitor has not been claimed")
            return MonitorStatus(LOGICAL_PM_ID, *row)
        except ContinuationError:
            raise
        except Exception:
            raise ContinuationError(_STORE_ERROR) from None

    def sweep(self, *, owner_id: str, lease_token: str) -> SweepResult:
        owner_id = _identifier(owner_id, "owner_id")
        lease_token = _required(lease_token, "lease_token")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    lease = connection.execute(
                        f"""
                        SELECT owner_id FROM {SCHEMA}.pm_monitor_lease
                        WHERE monitor_id = %s AND owner_id = %s
                          AND lease_token = %s
                          AND expires_at > clock_timestamp()
                        FOR UPDATE
                        """,
                        (LOGICAL_PM_ID, owner_id, lease_token),
                    ).fetchone()
                    if lease is None:
                        raise ContinuationAuthorizationError(
                            "active global PM monitor lease is invalid"
                        )
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.pm_monitor_lease
                        SET heartbeat_at = clock_timestamp(),
                            expires_at = clock_timestamp()
                                + make_interval(secs => lease_seconds)
                        WHERE monitor_id = %s
                        """,
                        (LOGICAL_PM_ID,),
                    )
                    items = connection.execute(
                        f"""
                        SELECT item.project_id, item.work_item_id, item.status,
                               item.assigned_role_id, item.version, item.payload
                        FROM {SCHEMA}.work_items AS item
                        JOIN {SCHEMA}.projects AS project
                          ON project.project_id = item.project_id
                        WHERE project.status = 'active'
                          AND item.status IN ('new', 'active', 'gated')
                        ORDER BY item.project_id, item.work_item_id
                        """
                    ).fetchall()
                    observations = tuple(
                        self._observe(connection, item, owner_id) for item in items
                    )
                    sweep_count = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.pm_monitor_lease
                        SET sweep_count = sweep_count + 1,
                            last_sweep_at = clock_timestamp()
                        WHERE monitor_id = %s
                        RETURNING sweep_count
                        """,
                        (LOGICAL_PM_ID,),
                    ).fetchone()[0]
                    return SweepResult(
                        LOGICAL_PM_ID, owner_id, sweep_count, observations
                    )
        except ContinuationError:
            raise
        except Exception:
            raise ContinuationError(_STORE_ERROR) from None

    def _observe(self, connection, item, actor_id: str) -> ContinuationObservation:
        project_id, work_item_id, status, owner_role_id, version, payload = item
        pending_gate = connection.execute(
            f"""
            SELECT gate_id FROM {SCHEMA}.gates
            WHERE project_id = %s AND work_item_id = %s AND status = 'pending'
            ORDER BY requested_at, gate_id LIMIT 1
            """,
            (project_id, work_item_id),
        ).fetchone()
        if status == "gated" and pending_gate is not None:
            values = ("waiting_sponsor", pending_gate[0], "pending sponsor gate")
        elif status == "active" and _material_ambiguity(payload):
            question = _sponsor_question(payload)
            if question is not None:
                sponsors = tuple(
                    row[0] for row in connection.execute(
                        f"""
                        SELECT sponsor_id FROM {SCHEMA}.project_sponsors
                        WHERE project_id = %s ORDER BY sponsor_id
                        """,
                        (project_id,),
                    ).fetchall()
                )
                gate_id = _action_id("ambiguity", project_id, work_item_id, version)
                self._lifecycle.open_gate(
                    project_id=project_id,
                    work_item_id=work_item_id,
                    gate_id=gate_id,
                    gate_type="sponsor-clarification",
                    requested_by=LOGICAL_PM_ID,
                    sponsor_ids=sponsors,
                    correlation_id=gate_id,
                    expected_version=version,
                    evidence={"question": question, "source": "pm-continuation-monitor"},
                )
                values = ("sponsor_question", gate_id, question)
            else:
                values = self._route_pm(
                    connection, project_id, work_item_id, owner_role_id, version,
                    "material ambiguity needs a concrete sponsor question",
                )
        elif self._has_continuation(connection, project_id, work_item_id):
            values = ("progressing", None, "durable continuation exists")
        else:
            reason = (
                "gated work has no pending sponsor decision"
                if status == "gated"
                else "nonterminal work has no durable continuation"
            )
            values = self._route_pm(
                connection, project_id, work_item_id, owner_role_id, version, reason
            )
        return self._record_observation(
            connection, project_id, work_item_id, version, *values, actor_id
        )

    def _route_pm(
        self,
        connection,
        project_id: str,
        work_item_id: str,
        owner_role_id: str,
        version: int,
        reason: str,
    ) -> tuple[str, str | None, str]:
        key = _action_id("pm-continuation", project_id, work_item_id, version)
        try:
            routed = self._router.route_in_transaction(
                connection,
                RouteDraft(
                    project_id=project_id,
                    work_item_id=work_item_id,
                    target_role_id="project-manager",
                    idempotency_key=key,
                    payload={
                        "pm_continuation": {
                            "work_version": version,
                            "current_owner_role_id": owner_role_id,
                            "reason": reason,
                        }
                    },
                    priority=100,
                )
            )
            return "pm_routed", routed.queue_item_id, reason
        except RoutingNotFound:
            return "routing_blocked", None, f"{reason}; project-manager route unavailable"

    @staticmethod
    def _has_continuation(connection, project_id: str, work_item_id: str) -> bool:
        return connection.execute(
            f"""
            SELECT 1 FROM {SCHEMA}.queue_items AS item
            WHERE item.project_id = %s AND item.work_item_id = %s
              AND (
                item.status = 'ready'
                OR (
                    item.status = 'leased' AND EXISTS (
                        SELECT 1 FROM {SCHEMA}.leases AS lease
                        WHERE lease.project_id = item.project_id
                          AND lease.queue_item_id = item.queue_item_id
                          AND lease.released_at IS NULL
                          AND lease.expires_at > clock_timestamp()
                    )
                )
              )
            LIMIT 1
            """,
            (project_id, work_item_id),
        ).fetchone() is not None

    def _record_observation(
        self, connection, project_id, work_item_id, version,
        disposition, action_id, detail, actor_id,
    ) -> ContinuationObservation:
        previous = connection.execute(
            f"""
            SELECT work_version, disposition, action_id, detail
            FROM {SCHEMA}.continuation_status
            WHERE project_id = %s AND work_item_id = %s
            """,
            (project_id, work_item_id),
        ).fetchone()
        row = connection.execute(
            f"""
            INSERT INTO {SCHEMA}.continuation_status
                (project_id, work_item_id, work_version, disposition,
                 action_id, detail)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, work_item_id) DO UPDATE
            SET work_version = EXCLUDED.work_version,
                disposition = EXCLUDED.disposition,
                action_id = EXCLUDED.action_id,
                detail = EXCLUDED.detail,
                observed_at = clock_timestamp()
            RETURNING project_id, work_item_id, work_version, disposition,
                      action_id, detail, observed_at::text
            """,
            (project_id, work_item_id, version, disposition, action_id, detail),
        ).fetchone()
        current = (version, disposition, action_id, detail)
        if previous != current:
            self._audit(
                connection, project_id, actor_id,
                f"continuation.{disposition}", work_item_id,
                {"work_version": version, "action_id": action_id, "detail": detail},
            )
        return ContinuationObservation(*row)

    @staticmethod
    def _audit(connection, project_id, actor_id, action, object_id, details) -> None:
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.audit_records
                (scope, project_id, actor_id, action, object_type,
                 object_id, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "organization" if project_id is None else "project",
                project_id, actor_id, action, "continuation-monitor",
                object_id, Jsonb(details),
            ),
        )


def _action_id(prefix: str, project_id: str, work_item_id: str, version: int) -> str:
    digest = hashlib.sha256(
        f"{prefix}\x00{project_id}\x00{work_item_id}\x00{version}".encode()
    ).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _material_ambiguity(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("material_ambiguity") is True


def _sponsor_question(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("sponsor_question"), str):
        return None
    value = payload["sponsor_question"].strip()
    return value if value and len(value) <= 2000 else None


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    selected = value.strip()
    if _IDENTIFIER.fullmatch(selected) is None:
        raise ValueError(f"{field_name} is invalid")
    return selected


def _required(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is invalid")
    return value.strip()


def _lease_seconds(value: object) -> int:
    if type(value) is not int or value < 1 or value > 300:
        raise ValueError("lease_seconds must be between 1 and 300")
    return value
