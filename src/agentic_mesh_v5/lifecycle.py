from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.events import EventDraft
from agentic_mesh_v5.events import EventStore
from agentic_mesh_v5.events import OutboundDraft


class LifecycleError(DatabaseError):
    """Base error for rejected lifecycle operations."""


class LifecycleNotFound(LifecycleError):
    pass


class LifecycleConflict(LifecycleError):
    pass


class LifecycleAuthorizationError(LifecycleError):
    pass


def _required(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


@dataclass(frozen=True)
class WorkItemRecord:
    project_id: str
    work_item_id: str
    title: str
    status: str
    owner_role_id: str
    version: int
    terminal_reason: str | None
    terminal_evidence: Mapping[str, Any]


@dataclass(frozen=True)
class GateRecord:
    project_id: str
    gate_id: str
    work_item_id: str
    gate_type: str
    status: str
    requested_by: str
    correlation_id: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class ApprovalRecord:
    project_id: str
    approval_id: str
    gate_id: str
    approver_id: str
    decision: str | None
    rationale: str | None
    evidence: Mapping[str, Any]
    decided_at: str | None


class LifecycleStore:
    _DIRECT_TRANSITIONS = {
        "new": frozenset({"active"}),
        "active": frozenset({"completed", "error"}),
        "gated": frozenset(),
        "completed": frozenset(),
        "error": frozenset(),
    }

    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._events = EventStore(database_url)

    def create_project(
        self,
        *,
        project_id: str,
        display_name: str,
        sponsor_ids: Sequence[str],
    ) -> None:
        project_id = _required(project_id, "project_id")
        display_name = _required(display_name, "display_name")
        sponsors = tuple(_required(item, "sponsor_id") for item in sponsor_ids)
        if not sponsors:
            raise ValueError("at least one sponsor_id is required")
        if len(sponsors) != len(set(sponsors)):
            raise ValueError("sponsor_ids must be unique")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.projects(project_id, display_name)
                        VALUES (%s, %s)
                        """,
                        (project_id, display_name),
                    )
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            f"""
                            INSERT INTO {SCHEMA}.project_sponsors
                                (project_id, sponsor_id)
                            VALUES (%s, %s)
                            """,
                            [(project_id, sponsor) for sponsor in sponsors],
                        )
        except psycopg.errors.UniqueViolation as exc:
            raise LifecycleConflict("project or sponsor already exists") from exc
        except Exception as exc:
            raise LifecycleError("project creation failed") from exc

    def create_work_item(
        self,
        *,
        project_id: str,
        work_item_id: str,
        title: str,
        owner_role_id: str,
        actor_id: str,
        correlation_id: str,
    ) -> WorkItemRecord:
        values = {
            "project_id": _required(project_id, "project_id"),
            "work_item_id": _required(work_item_id, "work_item_id"),
            "title": _required(title, "title"),
            "owner_role_id": _required(owner_role_id, "owner_role_id"),
            "actor_id": _required(actor_id, "actor_id"),
            "correlation_id": _required(correlation_id, "correlation_id"),
        }
        with self._events.transaction() as transaction:
            try:
                transaction.execute(
                    f"""
                    INSERT INTO {SCHEMA}.work_items
                        (project_id, work_item_id, assigned_role_id, title, status)
                    VALUES (%s, %s, %s, %s, 'new')
                    """,
                    (
                        values["project_id"],
                        values["work_item_id"],
                        values["owner_role_id"],
                        values["title"],
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise LifecycleConflict("work item already exists") from exc
            except psycopg.errors.ForeignKeyViolation as exc:
                raise LifecycleNotFound("project or owning role not found") from exc
            transaction.append(
                self._event(values, event_type="work.created", payload={"status": "new"}),
                (self._outbound(values, "work.created", "new"),),
            )
        return self.get_work_item(values["project_id"], values["work_item_id"])

    def transition_work_item(
        self,
        *,
        project_id: str,
        work_item_id: str,
        target_status: Literal["active", "completed", "error"],
        actor_id: str,
        correlation_id: str,
        expected_version: int,
        reason: str = "",
        evidence: Mapping[str, Any] | None = None,
    ) -> WorkItemRecord:
        project_id = _required(project_id, "project_id")
        work_item_id = _required(work_item_id, "work_item_id")
        target_status = _required(target_status, "target_status")
        actor_id = _required(actor_id, "actor_id")
        correlation_id = _required(correlation_id, "correlation_id")
        if expected_version < 1:
            raise ValueError("expected_version must be positive")
        terminal = target_status in {"completed", "error"}
        reason_value = _required(reason, "reason") if terminal else None
        evidence_value = dict(evidence or {})
        with self._events.transaction() as transaction:
            current = self._lock_work_item(transaction, project_id, work_item_id)
            if current.version != expected_version:
                raise LifecycleConflict("work item version is stale")
            allowed = self._DIRECT_TRANSITIONS.get(current.status, frozenset())
            if target_status not in allowed:
                raise LifecycleConflict(
                    f"invalid work item transition: {current.status} -> {target_status}"
                )
            row = transaction.execute(
                f"""
                UPDATE {SCHEMA}.work_items
                SET status = %s, version = version + 1,
                    terminal_reason = %s, terminal_evidence = %s,
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND work_item_id = %s
                RETURNING version
                """,
                (
                    target_status,
                    reason_value,
                    Jsonb(evidence_value),
                    project_id,
                    work_item_id,
                ),
            ).fetchone()
            values = {
                "project_id": project_id,
                "work_item_id": work_item_id,
                "owner_role_id": current.owner_role_id,
                "actor_id": actor_id,
                "correlation_id": correlation_id,
            }
            event_type = f"work.{target_status}"
            transaction.append(
                self._event(
                    values,
                    event_type=event_type,
                    payload={
                        "status": target_status,
                        "version": row[0],
                        "reason": reason_value,
                        "evidence": evidence_value,
                    },
                ),
                (self._outbound(values, event_type, target_status),),
            )
        return self.get_work_item(project_id, work_item_id)

    def open_gate(
        self,
        *,
        project_id: str,
        work_item_id: str,
        gate_id: str,
        gate_type: str,
        requested_by: str,
        sponsor_ids: Sequence[str],
        correlation_id: str,
        expected_version: int,
        evidence: Mapping[str, Any] | None = None,
    ) -> GateRecord:
        project_id = _required(project_id, "project_id")
        work_item_id = _required(work_item_id, "work_item_id")
        gate_id = _required(gate_id, "gate_id")
        gate_type = _required(gate_type, "gate_type")
        requested_by = _required(requested_by, "requested_by")
        correlation_id = _required(correlation_id, "correlation_id")
        sponsors = tuple(_required(item, "sponsor_id") for item in sponsor_ids)
        if not sponsors or len(sponsors) != len(set(sponsors)):
            raise ValueError("sponsor_ids must be non-empty and unique")
        evidence_value = dict(evidence or {})
        with self._events.transaction() as transaction:
            current = self._lock_work_item(transaction, project_id, work_item_id)
            if current.version != expected_version:
                raise LifecycleConflict("work item version is stale")
            if current.status != "active":
                raise LifecycleConflict("only active work can enter a gate")
            authorized = {
                row[0]
                for row in transaction.execute(
                    f"""
                    SELECT sponsor_id FROM {SCHEMA}.project_sponsors
                    WHERE project_id = %s AND sponsor_id = ANY(%s)
                    """,
                    (project_id, list(sponsors)),
                ).fetchall()
            }
            if authorized != set(sponsors):
                raise LifecycleAuthorizationError("gate approver is not a project sponsor")
            try:
                transaction.execute(
                    f"""
                    INSERT INTO {SCHEMA}.gates
                        (project_id, gate_id, work_item_id, gate_type, requested_by,
                         correlation_id, evidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        project_id,
                        gate_id,
                        work_item_id,
                        gate_type,
                        requested_by,
                        correlation_id,
                        Jsonb(evidence_value),
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise LifecycleConflict("gate already exists") from exc
            transaction.execute(
                f"""
                UPDATE {SCHEMA}.work_items
                SET status = 'gated', version = version + 1,
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND work_item_id = %s
                """,
                (project_id, work_item_id),
            )
            for sponsor in sponsors:
                transaction.execute(
                    f"""
                    INSERT INTO {SCHEMA}.approvals
                        (project_id, approval_id, gate_id, approver_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (project_id, f"{gate_id}:{sponsor}", gate_id, sponsor),
                )
            values = {
                "project_id": project_id,
                "work_item_id": work_item_id,
                "owner_role_id": current.owner_role_id,
                "actor_id": requested_by,
                "correlation_id": correlation_id,
            }
            transaction.append(
                self._event(
                    values,
                    event_type="gate.opened",
                    payload={"gate_id": gate_id, "gate_type": gate_type},
                ),
                (self._outbound(values, "gate.opened", "gated"),),
            )
        return self.get_gate(project_id, gate_id)

    def decide_gate(
        self,
        *,
        project_id: str,
        gate_id: str,
        sponsor_id: str,
        decision: Literal["approved", "rejected"],
        rationale: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> ApprovalRecord:
        project_id = _required(project_id, "project_id")
        gate_id = _required(gate_id, "gate_id")
        sponsor_id = _required(sponsor_id, "sponsor_id")
        decision = _required(decision, "decision")
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        rationale = _required(rationale, "rationale")
        evidence_value = dict(evidence or {})
        with self._events.transaction() as transaction:
            gate = transaction.execute(
                f"""
                SELECT work_item_id, status, requested_by, correlation_id
                FROM {SCHEMA}.gates
                WHERE project_id = %s AND gate_id = %s
                FOR UPDATE
                """,
                (project_id, gate_id),
            ).fetchone()
            if gate is None:
                raise LifecycleNotFound("gate not found")
            work_item_id, gate_status, _requested_by, correlation_id = gate
            if gate_status != "pending":
                raise LifecycleConflict("gate is already resolved")
            approval = transaction.execute(
                f"""
                SELECT approval_id, decision
                FROM {SCHEMA}.approvals
                WHERE project_id = %s AND gate_id = %s AND approver_id = %s
                FOR UPDATE
                """,
                (project_id, gate_id, sponsor_id),
            ).fetchone()
            if approval is None:
                raise LifecycleAuthorizationError("sponsor is not authorized for this gate")
            if approval[1] is not None:
                raise LifecycleConflict("approval is already decided")
            current = self._lock_work_item(transaction, project_id, work_item_id)
            if current.status != "gated":
                raise LifecycleConflict("gated work item state is inconsistent")
            transaction.execute(
                f"""
                UPDATE {SCHEMA}.approvals
                SET decision = %s, rationale = %s, evidence = %s,
                    decided_at = clock_timestamp()
                WHERE project_id = %s AND approval_id = %s
                """,
                (
                    decision,
                    rationale,
                    Jsonb(evidence_value),
                    project_id,
                    approval[0],
                ),
            )
            transaction.execute(
                f"""
                UPDATE {SCHEMA}.gates
                SET status = %s, resolved_at = clock_timestamp()
                WHERE project_id = %s AND gate_id = %s
                """,
                (decision, project_id, gate_id),
            )
            transaction.execute(
                f"""
                UPDATE {SCHEMA}.work_items
                SET status = 'active', version = version + 1,
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND work_item_id = %s
                """,
                (project_id, work_item_id),
            )
            values = {
                "project_id": project_id,
                "work_item_id": work_item_id,
                "owner_role_id": current.owner_role_id,
                "actor_id": sponsor_id,
                "correlation_id": correlation_id,
            }
            event_type = f"gate.{decision}"
            transaction.append(
                self._event(
                    values,
                    event_type=event_type,
                    payload={
                        "gate_id": gate_id,
                        "decision": decision,
                        "rationale": rationale,
                        "evidence": evidence_value,
                    },
                ),
                (self._outbound(values, event_type, "active"),),
            )
        return self.get_approval(project_id, approval[0])

    def get_work_item(self, project_id: str, work_item_id: str) -> WorkItemRecord:
        project_id = _required(project_id, "project_id")
        work_item_id = _required(work_item_id, "work_item_id")
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            row = connection.execute(
                f"""
                SELECT project_id, work_item_id, title, status, assigned_role_id,
                       version, terminal_reason, terminal_evidence
                FROM {SCHEMA}.work_items
                WHERE project_id = %s AND work_item_id = %s
                """,
                (project_id, work_item_id),
            ).fetchone()
        if row is None:
            raise LifecycleNotFound("work item not found")
        return WorkItemRecord(*row)

    def get_gate(self, project_id: str, gate_id: str) -> GateRecord:
        project_id = _required(project_id, "project_id")
        gate_id = _required(gate_id, "gate_id")
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            row = connection.execute(
                f"""
                SELECT project_id, gate_id, work_item_id, gate_type, status,
                       requested_by, correlation_id, evidence
                FROM {SCHEMA}.gates
                WHERE project_id = %s AND gate_id = %s
                """,
                (project_id, gate_id),
            ).fetchone()
        if row is None:
            raise LifecycleNotFound("gate not found")
        return GateRecord(*row)

    def get_approval(self, project_id: str, approval_id: str) -> ApprovalRecord:
        project_id = _required(project_id, "project_id")
        approval_id = _required(approval_id, "approval_id")
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            row = connection.execute(
                f"""
                SELECT project_id, approval_id, gate_id, approver_id, decision,
                       rationale, evidence, decided_at::text
                FROM {SCHEMA}.approvals
                WHERE project_id = %s AND approval_id = %s
                """,
                (project_id, approval_id),
            ).fetchone()
        if row is None:
            raise LifecycleNotFound("approval not found")
        return ApprovalRecord(*row)

    @staticmethod
    def _lock_work_item(transaction, project_id: str, work_item_id: str) -> WorkItemRecord:
        row = transaction.execute(
            f"""
            SELECT project_id, work_item_id, title, status, assigned_role_id,
                   version, terminal_reason, terminal_evidence
            FROM {SCHEMA}.work_items
            WHERE project_id = %s AND work_item_id = %s
            FOR UPDATE
            """,
            (project_id, work_item_id),
        ).fetchone()
        if row is None:
            raise LifecycleNotFound("work item not found")
        return WorkItemRecord(*row)

    @staticmethod
    def _event(values, *, event_type: str, payload: Mapping[str, Any]) -> EventDraft:
        return EventDraft(
            project_id=values["project_id"],
            work_item_id=values["work_item_id"],
            actor_id=values["actor_id"],
            correlation_id=values["correlation_id"],
            aggregate_type="work-item",
            aggregate_id=values["work_item_id"],
            event_type=event_type,
            payload=payload,
        )

    @staticmethod
    def _outbound(values, event_type: str, status: str) -> OutboundDraft:
        return OutboundDraft(
            topic="lifecycle.events",
            payload={
                "project_id": values["project_id"],
                "work_item_id": values["work_item_id"],
                "event_type": event_type,
                "status": status,
            },
        )
