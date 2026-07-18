from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Mapping, Sequence

from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError, SCHEMA
from agentic_mesh_v5.events import EventDraft, EventStore, OutboundDraft
from agentic_mesh_v5.lifecycle import ApprovalRecord
from agentic_mesh_v5.lifecycle import LifecycleAuthorizationError
from agentic_mesh_v5.lifecycle import LifecycleConflict, LifecycleNotFound
from agentic_mesh_v5.routing import RouteDraft, Router


_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SPONSOR_GATE_TYPES = {"human_response", "sponsor_approval"}


@dataclass(frozen=True, slots=True)
class SponsorGateRecord:
    project_id: str
    gate_id: str
    work_item_id: str
    gate_type: str
    status: str
    requested_by: str
    correlation_id: str
    evidence: Mapping[str, object]
    flow_state: str
    flow_entry_version: int
    flow_obligation_id: str
    expires_at: str
    timed_out_at: str | None


class SponsorApprovalCoordinator:
    """Atomically compose lifecycle approvals with one external-flow gate."""

    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._events = EventStore(database_url)
        self._router = Router(database_url)

    def open(
        self,
        *,
        project_id: str,
        work_item_id: str,
        gate_id: str,
        obligation_id: str,
        requested_by: str,
        sponsor_ids: Sequence[str],
        correlation_id: str,
        expected_version: int,
        expires_in_seconds: int = 172_800,
        evidence: Mapping[str, object] | None = None,
    ) -> SponsorGateRecord:
        project_id, work_item_id, gate_id, obligation_id = _identifiers(
            project_id, work_item_id, gate_id, obligation_id
        )
        requested_by, correlation_id = _identifiers(requested_by, correlation_id)
        sponsors = tuple(_identifier(item, "sponsor_id") for item in sponsor_ids)
        if not sponsors or len(sponsors) != len(set(sponsors)):
            raise ValueError("sponsor_ids must be non-empty and unique")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version is invalid")
        if type(expires_in_seconds) is not int or not 1 <= expires_in_seconds <= 604_800:
            raise ValueError("expires_in_seconds must be between 1 and 604800")
        evidence = _evidence(evidence)
        with self._events.transaction() as transaction:
            existing = self._gate_row(transaction, project_id, gate_id, lock=True)
            if existing is not None:
                if self._same_open(
                    transaction,
                    existing,
                    work_item_id=work_item_id,
                    obligation_id=obligation_id,
                    requested_by=requested_by,
                    sponsors=sponsors,
                    correlation_id=correlation_id,
                    evidence=evidence,
                ):
                    return _gate_record(existing)
                raise LifecycleConflict("gate already exists with different request data")
            work = transaction.execute(
                f"""
                SELECT status, assigned_role_id, version
                FROM {SCHEMA}.work_items
                WHERE project_id = %s AND work_item_id = %s FOR UPDATE
                """,
                (project_id, work_item_id),
            ).fetchone()
            concurrent = self._gate_row(transaction, project_id, gate_id, lock=True)
            if concurrent is not None:
                if self._same_open(
                    transaction,
                    concurrent,
                    work_item_id=work_item_id,
                    obligation_id=obligation_id,
                    requested_by=requested_by,
                    sponsors=sponsors,
                    correlation_id=correlation_id,
                    evidence=evidence,
                ):
                    return _gate_record(concurrent)
                raise LifecycleConflict("gate already exists with different request data")
            if work is None:
                raise LifecycleNotFound("work item not found")
            if work[0] != "active" or work[2] != expected_version:
                raise LifecycleConflict("work item is not active at expected version")
            flow = transaction.execute(
                f"""
                SELECT current_state, version, owner_role_id, status, flow_snapshot
                FROM {SCHEMA}.flow_runs
                WHERE project_id = %s AND work_item_id = %s FOR UPDATE
                """,
                (project_id, work_item_id),
            ).fetchone()
            if flow is None:
                raise LifecycleNotFound("flow run not found")
            if flow[3] != "active":
                raise LifecycleConflict("flow run is not active")
            if requested_by not in {flow[2], flow[4].get("leader_role")}:
                raise LifecycleAuthorizationError(
                    "sponsor gate requires the flow owner or leader"
                )
            obligation = transaction.execute(
                f"""
                SELECT accountable_role_id, payload, status
                FROM {SCHEMA}.flow_obligations
                WHERE project_id = %s AND work_item_id = %s AND state = %s
                  AND entry_version = %s AND obligation_kind = 'gate'
                  AND obligation_id = %s FOR UPDATE
                """,
                (project_id, work_item_id, flow[0], flow[1], obligation_id),
            ).fetchone()
            if obligation is None:
                raise LifecycleNotFound("current sponsor flow obligation not found")
            gate_type = obligation[1].get("type")
            if (
                gate_type not in _SPONSOR_GATE_TYPES
                or obligation[1].get("requested_from") != "sponsor"
                or obligation[2] != "pending"
            ):
                raise LifecycleConflict("flow obligation is not a pending sponsor gate")
            if self._artifact_version(
                transaction, project_id, work_item_id, flow[0], flow[1]
            ) is None:
                raise LifecycleConflict(
                    "sponsor gate requires a verified state artifact"
                )
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
                raise LifecycleAuthorizationError(
                    "gate approver is not a project sponsor"
                )
            expires_at = transaction.execute(
                "SELECT clock_timestamp() + (%s * interval '1 second')",
                (expires_in_seconds,),
            ).fetchone()[0]
            row = transaction.execute(
                f"""
                INSERT INTO {SCHEMA}.gates
                    (project_id, gate_id, work_item_id, gate_type, requested_by,
                     correlation_id, evidence, flow_state, flow_entry_version,
                     flow_obligation_kind, flow_obligation_id, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'gate', %s, %s)
                RETURNING project_id, gate_id, work_item_id, gate_type, status,
                          requested_by, correlation_id, evidence, flow_state,
                          flow_entry_version, flow_obligation_id,
                          expires_at::text, timed_out_at::text
                """,
                (
                    project_id, gate_id, work_item_id, gate_type, requested_by,
                    correlation_id, Jsonb(evidence), flow[0], flow[1],
                    obligation_id, expires_at,
                ),
            ).fetchone()
            for sponsor_id in sponsors:
                transaction.execute(
                    f"""
                    INSERT INTO {SCHEMA}.approvals
                        (project_id, approval_id, gate_id, approver_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (project_id, f"{gate_id}:{sponsor_id}", gate_id, sponsor_id),
                )
            transaction.execute(
                f"""
                UPDATE {SCHEMA}.work_items
                SET status = 'gated', version = version + 1,
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND work_item_id = %s
                """,
                (project_id, work_item_id),
            )
            self._append(
                transaction,
                project_id=project_id,
                work_item_id=work_item_id,
                actor_id=requested_by,
                correlation_id=correlation_id,
                event_type="sponsor_gate.opened",
                status="gated",
                payload={
                    "gate_id": gate_id,
                    "obligation_id": obligation_id,
                    "expires_at": row[11],
                    "sponsor_ids": list(sponsors),
                },
            )
            return _gate_record(row)

    def decide(
        self,
        *,
        project_id: str,
        gate_id: str,
        sponsor_id: str,
        decision: str,
        rationale: str,
        evidence: Mapping[str, object] | None,
        operation_id: str,
    ) -> ApprovalRecord:
        project_id, gate_id, sponsor_id, operation_id = _identifiers(
            project_id, gate_id, sponsor_id, operation_id
        )
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        rationale = _required(rationale, "rationale", 4000)
        evidence = _evidence(evidence)
        digest = _digest(
            "sponsor-decision", gate_id, sponsor_id, decision, rationale, evidence
        )
        approval_id = f"{gate_id}:{sponsor_id}"
        with self._events.transaction() as transaction:
            gate = self._gate_row(transaction, project_id, gate_id, lock=True)
            if gate is None or gate[9] is None:
                raise LifecycleNotFound("managed sponsor gate not found")
            approval = transaction.execute(
                f"""
                SELECT approval_id, decision, rationale, evidence, decided_at::text
                FROM {SCHEMA}.approvals
                WHERE project_id = %s AND gate_id = %s AND approver_id = %s
                FOR UPDATE
                """,
                (project_id, gate_id, sponsor_id),
            ).fetchone()
            if approval is None:
                raise LifecycleAuthorizationError(
                    "sponsor is not authorized for this gate"
                )
            if gate[4] != "pending":
                if (
                    gate[15] == digest
                    and approval[1] == decision
                    and approval[2] == rationale
                    and approval[3] == evidence
                ):
                    return ApprovalRecord(
                        project_id, approval[0], gate_id, sponsor_id,
                        approval[1], approval[2], approval[3], approval[4],
                    )
                raise LifecycleConflict("sponsor gate is already resolved")
            database_now = transaction.execute("SELECT clock_timestamp()").fetchone()[0]
            if gate[11] <= database_now:
                raise LifecycleConflict("sponsor gate has expired")
            work, flow, artifact = self._lock_current(transaction, gate)
            if approval[1] is not None:
                raise LifecycleConflict("approval is already decided")
            decided_at = transaction.execute(
                f"""
                UPDATE {SCHEMA}.approvals
                SET decision = %s, rationale = %s, evidence = %s,
                    decided_at = clock_timestamp()
                WHERE project_id = %s AND approval_id = %s
                RETURNING decided_at::text
                """,
                (decision, rationale, Jsonb(evidence), project_id, approval_id),
            ).fetchone()[0]
            transaction.execute(
                f"""
                UPDATE {SCHEMA}.gates
                SET status = %s, resolved_at = clock_timestamp(),
                    resolved_operation_id = %s, resolved_request_digest = %s
                WHERE project_id = %s AND gate_id = %s
                """,
                (decision, operation_id, digest, project_id, gate_id),
            )
            governance_record_id = f"sponsor:{gate_id}"
            governance_evidence = {
                "approval_id": approval_id,
                "gate_id": gate_id,
                "decision": decision,
                "reviewed_document": {
                    "path": artifact[0],
                    "etag": artifact[1],
                },
                **evidence,
            }
            transaction.execute(
                f"""
                INSERT INTO {SCHEMA}.governance_records
                    (project_id, work_item_id, record_id, request_digest, state,
                     entry_version, obligation_kind, obligation_id, decision,
                     actor_role_id, reason, evidence, document_path, document_etag)
                VALUES (%s, %s, %s, %s, %s, %s, 'gate', %s, %s, %s, %s, %s,
                        %s, %s)
                """,
                (
                    project_id, gate[2], governance_record_id, digest, gate[8],
                    gate[9], gate[10], decision, sponsor_id, rationale,
                    Jsonb(governance_evidence), artifact[0], artifact[1],
                ),
            )
            if decision == "approved":
                updated = transaction.execute(
                    f"""
                    UPDATE {SCHEMA}.flow_obligations
                    SET status = 'satisfied', evidence = %s, updated_by = %s,
                        updated_at = clock_timestamp()
                    WHERE project_id = %s AND work_item_id = %s AND state = %s
                      AND entry_version = %s AND obligation_kind = 'gate'
                      AND obligation_id = %s AND status = 'pending'
                    """,
                    (
                        Jsonb({
                            "governance_record_id": governance_record_id,
                            "approval_id": approval_id,
                        }),
                        sponsor_id, project_id, gate[2], gate[8], gate[9], gate[10],
                    ),
                ).rowcount
                if updated != 1:
                    raise LifecycleConflict("sponsor flow obligation is no longer pending")
                target_role = flow[2]
            else:
                target_role = "project-manager"
            transaction.execute(
                f"""
                UPDATE {SCHEMA}.work_items
                SET status = 'active', version = version + 1,
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND work_item_id = %s
                """,
                (project_id, gate[2]),
            )
            self._route(
                transaction,
                gate=gate,
                target_role=target_role,
                outcome=decision,
                actor_id=sponsor_id,
            )
            self._append(
                transaction,
                project_id=project_id,
                work_item_id=gate[2],
                actor_id=sponsor_id,
                correlation_id=operation_id,
                event_type=f"sponsor_gate.{decision}",
                status="active",
                payload=governance_evidence,
            )
        return ApprovalRecord(
            project_id, approval_id, gate_id, sponsor_id,
            decision, rationale, evidence, decided_at,
        )

    def timeout(
        self,
        *,
        project_id: str,
        gate_id: str,
        actor_id: str,
        operation_id: str,
        observed_at: datetime | None = None,
    ) -> SponsorGateRecord:
        project_id, gate_id, actor_id, operation_id = _identifiers(
            project_id, gate_id, actor_id, operation_id
        )
        if observed_at is not None:
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("observed_at must be timezone aware")
            observed_at = observed_at.astimezone(timezone.utc)
        digest = _digest("sponsor-timeout", gate_id)
        with self._events.transaction() as transaction:
            effective_at = observed_at or transaction.execute(
                "SELECT clock_timestamp()"
            ).fetchone()[0]
            gate = self._gate_row(transaction, project_id, gate_id, lock=True)
            if gate is None or gate[9] is None:
                raise LifecycleNotFound("managed sponsor gate not found")
            if gate[4] != "pending":
                if gate[4] == "timed_out" and gate[15] == digest:
                    return _gate_record(gate)
                raise LifecycleConflict("sponsor gate is already resolved")
            if gate[11] > effective_at:
                raise LifecycleConflict("sponsor gate has not expired")
            _work, flow, _artifact = self._lock_current(transaction, gate)
            if actor_id != flow[4].get("leader_role"):
                raise LifecycleAuthorizationError(
                    "sponsor timeout requires the flow leader"
                )
            row = transaction.execute(
                f"""
                UPDATE {SCHEMA}.gates
                SET status = 'timed_out', resolved_at = %s, timed_out_at = %s,
                    resolved_operation_id = %s, resolved_request_digest = %s
                WHERE project_id = %s AND gate_id = %s
                RETURNING project_id, gate_id, work_item_id, gate_type, status,
                          requested_by, correlation_id, evidence, flow_state,
                          flow_entry_version, flow_obligation_id,
                          expires_at::text, timed_out_at::text
                """,
                (
                    effective_at, effective_at, operation_id, digest,
                    project_id, gate_id,
                ),
            ).fetchone()
            transaction.execute(
                f"""
                UPDATE {SCHEMA}.work_items
                SET status = 'active', version = version + 1,
                    updated_at = clock_timestamp()
                WHERE project_id = %s AND work_item_id = %s
                """,
                (project_id, gate[2]),
            )
            self._route(
                transaction,
                gate=gate,
                target_role="project-manager",
                outcome="timed_out",
                actor_id=actor_id,
            )
            self._append(
                transaction,
                project_id=project_id,
                work_item_id=gate[2],
                actor_id=actor_id,
                correlation_id=operation_id,
                event_type="sponsor_gate.timed_out",
                status="active",
                payload={"gate_id": gate_id, "obligation_id": gate[10]},
            )
            return _gate_record(row)

    def get(self, project_id: str, gate_id: str) -> SponsorGateRecord:
        project_id, gate_id = _identifiers(project_id, gate_id)
        with self._events.transaction() as transaction:
            row = self._gate_row(transaction, project_id, gate_id, lock=False)
        if row is None or row[9] is None:
            raise LifecycleNotFound("managed sponsor gate not found")
        return _gate_record(row)

    def is_managed(self, project_id: str, gate_id: str) -> bool:
        project_id, gate_id = _identifiers(project_id, gate_id)
        with self._events.transaction() as transaction:
            row = transaction.execute(
                f"""
                SELECT flow_obligation_id FROM {SCHEMA}.gates
                WHERE project_id = %s AND gate_id = %s
                """,
                (project_id, gate_id),
            ).fetchone()
        return row is not None and row[0] is not None

    @staticmethod
    def _gate_row(transaction, project_id, gate_id, *, lock):
        return transaction.execute(
            f"""
            SELECT project_id, gate_id, work_item_id, gate_type, status,
                   requested_by, correlation_id, evidence, flow_state,
                   flow_entry_version, flow_obligation_id, expires_at,
                   timed_out_at::text, resolved_at::text,
                   resolved_operation_id, resolved_request_digest
            FROM {SCHEMA}.gates
            WHERE project_id = %s AND gate_id = %s
            {"FOR UPDATE" if lock else ""}
            """,
            (project_id, gate_id),
        ).fetchone()

    @staticmethod
    def _same_open(
        transaction,
        gate,
        *,
        work_item_id,
        obligation_id,
        requested_by,
        sponsors,
        correlation_id,
        evidence,
    ) -> bool:
        recorded_sponsors = tuple(
            row[0]
            for row in transaction.execute(
                f"""
                SELECT approver_id FROM {SCHEMA}.approvals
                WHERE project_id = %s AND gate_id = %s ORDER BY approver_id
                """,
                (gate[0], gate[1]),
            ).fetchall()
        )
        return (
            gate[2] == work_item_id
            and gate[5] == requested_by
            and gate[6] == correlation_id
            and gate[7] == evidence
            and gate[10] == obligation_id
            and recorded_sponsors == tuple(sorted(sponsors))
        )

    @staticmethod
    def _lock_current(transaction, gate):
        work = transaction.execute(
            f"""
            SELECT status, assigned_role_id, version
            FROM {SCHEMA}.work_items
            WHERE project_id = %s AND work_item_id = %s FOR UPDATE
            """,
            (gate[0], gate[2]),
        ).fetchone()
        if work is None or work[0] != "gated":
            raise LifecycleConflict("gated work item state is inconsistent")
        flow = transaction.execute(
            f"""
            SELECT current_state, version, owner_role_id, status, flow_snapshot
            FROM {SCHEMA}.flow_runs
            WHERE project_id = %s AND work_item_id = %s FOR UPDATE
            """,
            (gate[0], gate[2]),
        ).fetchone()
        if (
            flow is None
            or flow[0] != gate[8]
            or flow[1] != gate[9]
            or flow[3] != "active"
        ):
            raise LifecycleConflict("sponsor gate is not current for the flow")
        obligation = transaction.execute(
            f"""
            SELECT status FROM {SCHEMA}.flow_obligations
            WHERE project_id = %s AND work_item_id = %s AND state = %s
              AND entry_version = %s AND obligation_kind = 'gate'
              AND obligation_id = %s FOR UPDATE
            """,
            (gate[0], gate[2], gate[8], gate[9], gate[10]),
        ).fetchone()
        if obligation is None or obligation[0] != "pending":
            raise LifecycleConflict("sponsor flow obligation is no longer pending")
        artifact = SponsorApprovalCoordinator._artifact_version(
            transaction, gate[0], gate[2], gate[8], gate[9]
        )
        if artifact is None:
            raise LifecycleConflict("sponsor gate requires a verified state artifact")
        return work, flow, artifact

    @staticmethod
    def _artifact_version(transaction, project_id, work_item_id, state, version):
        return transaction.execute(
            f"""
            SELECT evidence.document_path, evidence.document_etag
            FROM {SCHEMA}.flow_obligations AS obligation
            JOIN {SCHEMA}.governance_records AS evidence
              ON evidence.project_id = obligation.project_id
             AND evidence.work_item_id = obligation.work_item_id
             AND evidence.state = obligation.state
             AND evidence.entry_version = obligation.entry_version
             AND evidence.obligation_kind = obligation.obligation_kind
             AND evidence.obligation_id = obligation.obligation_id
             AND evidence.decision = 'verified'
            WHERE obligation.project_id = %s AND obligation.work_item_id = %s
              AND obligation.state = %s AND obligation.entry_version = %s
              AND obligation.obligation_kind = 'artifact'
              AND obligation.status = 'satisfied'
            """,
            (project_id, work_item_id, state, version),
        ).fetchone()

    def _route(self, transaction, *, gate, target_role, outcome, actor_id) -> None:
        self._router.route_in_transaction(
            transaction,
            RouteDraft(
                project_id=gate[0],
                work_item_id=gate[2],
                target_role_id=target_role,
                idempotency_key=f"sponsor-gate:{gate[1]}:{outcome}",
                payload={
                    "kind": "sponsor-gate-continuation",
                    "gate_id": gate[1],
                    "obligation_id": gate[10],
                    "outcome": outcome,
                    "decided_by": actor_id,
                },
            ),
        )

    @staticmethod
    def _append(
        transaction,
        *,
        project_id,
        work_item_id,
        actor_id,
        correlation_id,
        event_type,
        status,
        payload,
    ) -> None:
        transaction.append(
            EventDraft(
                project_id=project_id,
                work_item_id=work_item_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                aggregate_type="sponsor-gate",
                aggregate_id=payload["gate_id"],
                event_type=event_type,
                payload=payload,
            ),
            (
                OutboundDraft(
                    topic="lifecycle.events",
                    payload={
                        "project_id": project_id,
                        "work_item_id": work_item_id,
                        "event_type": event_type,
                        "status": status,
                    },
                ),
            ),
        )

def _gate_record(row) -> SponsorGateRecord:
    return SponsorGateRecord(
        *row[:11], _timestamp(row[11]), _timestamp(row[12])
    )


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return datetime.fromisoformat(value).isoformat()
    raise ValueError("database timestamp is invalid")


def _identifiers(*values: object) -> tuple[str, ...]:
    return tuple(_identifier(value, "identifier") for value in values)


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value.strip()) is None:
        raise ValueError(f"{field} is invalid")
    return value.strip()


def _required(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} is invalid")
    return value.strip()


def _evidence(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("evidence must be an object")
    result = dict(value)
    try:
        encoded = json.dumps(
            result, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > 1_000_000:
        raise ValueError("evidence is too large")
    return result


def _digest(*values: object) -> str:
    encoded = json.dumps(
        values, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
