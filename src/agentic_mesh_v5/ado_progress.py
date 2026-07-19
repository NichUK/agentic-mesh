from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agentic_mesh_v5.ado_adapter import AdoComment
from agentic_mesh_v5.ado_adapter import AdoConflict
from agentic_mesh_v5.ado_adapter import AdoWorkItem
from agentic_mesh_v5.ado_adapter import ProjectAdoAdapter
from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA


_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_KINDS = frozenset(
    {
        "start",
        "handoff",
        "blocker",
        "recovery",
        "pull-request",
        "deployment",
        "acceptance",
    }
)
_EVIDENCE_KINDS = frozenset(
    {
        "source",
        "implementation",
        "automated-test",
        "handoff",
        "blocker",
        "recovery",
        "pull-request",
        "deployment",
        "acceptance",
        "owner-review",
    }
)
_LABELS = {
    "start": "Started",
    "handoff": "Material handoff",
    "blocker": "Blocked",
    "recovery": "Recovered",
    "pull-request": "Pull request",
    "deployment": "Deployment",
    "acceptance": "Accepted",
}
_REQUIRED_EVIDENCE = {
    "start": "source",
    "handoff": "handoff",
    "blocker": "blocker",
    "recovery": "recovery",
    "pull-request": "pull-request",
    "deployment": "deployment",
    "acceptance": "acceptance",
}
_PREDECESSOR = {"Active": "New", "Resolved": "Active", "Closed": "Resolved"}


class AdoProgressError(DatabaseError):
    pass


class AdoProgressConflict(AdoProgressError):
    pass


class AdoProgressBlocked(AdoProgressError):
    pass


@dataclass(frozen=True, slots=True)
class MilestoneEvidence:
    kind: str
    reference: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MilestoneRequest:
    project_id: str
    work_item_id: str
    milestone_id: str
    sequence: int
    kind: str
    summary: str
    evidence: tuple[MilestoneEvidence, ...]
    next_action: str
    actor_id: str


@dataclass(frozen=True, slots=True)
class AdoMilestone:
    project_id: str
    milestone_id: str
    work_item_id: str
    sequence: int
    milestone_kind: str
    fingerprint: str
    summary: str
    evidence: tuple[Mapping[str, str], ...]
    next_action: str
    target_state: str | None
    status: str
    state_disposition: str | None
    comment_id: int | None
    external_revision: int | None
    external_state: str | None
    suppression_reason: str | None
    started_by: str
    created_at: str
    published_at: str | None
    version: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class AdoProgressClient(Protocol):
    def read(self, *, project_id: str, work_item_id: str) -> AdoWorkItem: ...

    def comment_once(
        self,
        *,
        project_id: str,
        work_item_id: str,
        marker: str,
        text: str,
    ) -> AdoComment: ...

    def update_state(
        self,
        *,
        project_id: str,
        work_item_id: str,
        operation_id: str,
        expected_state: str,
        target_state: str,
        actor_id: str,
    ) -> int: ...


class AdapterAdoProgressClient:
    def __init__(self, adapter: ProjectAdoAdapter) -> None:
        self._adapter = adapter

    def read(self, *, project_id: str, work_item_id: str) -> AdoWorkItem:
        return self._adapter.read(project_id=project_id, work_item_id=work_item_id)

    def comment_once(
        self,
        *,
        project_id: str,
        work_item_id: str,
        marker: str,
        text: str,
    ) -> AdoComment:
        return self._adapter.comment_once(
            project_id=project_id,
            work_item_id=work_item_id,
            marker=marker,
            text=text,
        )

    def update_state(
        self,
        *,
        project_id: str,
        work_item_id: str,
        operation_id: str,
        expected_state: str,
        target_state: str,
        actor_id: str,
    ) -> int:
        result = self._adapter.update(
            project_id=project_id,
            work_item_id=work_item_id,
            operation_id=operation_id,
            fields={"System.State": target_state},
            expected_fields={"System.State": expected_state},
            actor_id=actor_id,
        )
        if result.external_revision is None:
            raise AdoProgressError("ADO state update has no external revision")
        return result.external_revision


class AdoMilestonePublisher:
    def __init__(
        self, database_url: str, *, client: AdoProgressClient
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._client = client

    def publish(self, request: MilestoneRequest) -> AdoMilestone:
        request, fingerprint, target_state = _request(request)
        lock_key = f"ado-progress:{request.project_id}:{request.work_item_id}"
        with psycopg.connect(self._database_url, autocommit=True) as lock:
            lock.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,)
            )
            try:
                milestone = self._start(request, fingerprint, target_state)
                if milestone.status != "pending":
                    return milestone
                return self._publish(milestone, request.actor_id)
            finally:
                lock.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (lock_key,),
                )

    def get(self, *, project_id: str, milestone_id: str) -> AdoMilestone | None:
        project_id = _project_id(project_id)
        milestone_id = _external_id(milestone_id, "milestone_id")
        with psycopg.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            return self._read(connection, project_id, milestone_id)

    def _start(
        self,
        request: MilestoneRequest,
        fingerprint: str,
        target_state: str | None,
    ) -> AdoMilestone:
        evidence = [item.to_dict() for item in request.evidence]
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    existing = self._read(
                        connection, request.project_id, request.milestone_id
                    )
                    if existing is not None:
                        if existing.fingerprint != fingerprint:
                            raise AdoProgressConflict(
                                "milestone_id belongs to another payload"
                            )
                        return existing
                    highest = connection.execute(
                        f"""
                        SELECT max(sequence) AS highest_sequence
                        FROM {SCHEMA}.work_item_ado_milestones
                        WHERE project_id = %s AND work_item_id = %s
                        """,
                        (request.project_id, request.work_item_id),
                    ).fetchone()["highest_sequence"]
                    if highest is not None and request.sequence <= highest:
                        return self._insert(
                            connection,
                            request,
                            fingerprint,
                            target_state,
                            evidence,
                            status="suppressed",
                            suppression_reason="stale milestone sequence",
                        )
                    pending = connection.execute(
                        f"""
                        SELECT milestone_id FROM {SCHEMA}.work_item_ado_milestones
                        WHERE project_id = %s AND work_item_id = %s
                          AND status = 'pending' AND sequence < %s
                        ORDER BY sequence LIMIT 1
                        """,
                        (
                            request.project_id,
                            request.work_item_id,
                            request.sequence,
                        ),
                    ).fetchone()
                    if pending is not None:
                        raise AdoProgressBlocked(
                            "earlier ADO milestone is pending: "
                            f"{pending['milestone_id']}"
                        )
                    return self._insert(
                        connection,
                        request,
                        fingerprint,
                        target_state,
                        evidence,
                        status="pending",
                        suppression_reason=None,
                    )
        except AdoProgressError:
            raise
        except psycopg.errors.UniqueViolation as exc:
            raise AdoProgressConflict("milestone sequence is already used") from exc
        except Exception as exc:
            raise AdoProgressError("ADO milestone persistence failed") from exc

    def _insert(
        self,
        connection,
        request: MilestoneRequest,
        fingerprint: str,
        target_state: str | None,
        evidence: Sequence[Mapping[str, str]],
        *,
        status: str,
        suppression_reason: str | None,
    ) -> AdoMilestone:
        suppressed = status == "suppressed"
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.work_item_ado_milestones
                (project_id, milestone_id, work_item_id, sequence,
                 milestone_kind, fingerprint, summary, evidence, next_action,
                 target_state, status, state_disposition, suppression_reason,
                 started_by, published_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, CASE WHEN %s THEN clock_timestamp() ELSE NULL END)
            """,
            (
                request.project_id,
                request.milestone_id,
                request.work_item_id,
                request.sequence,
                request.kind,
                fingerprint,
                request.summary,
                Jsonb(list(evidence)),
                request.next_action,
                target_state,
                status,
                "suppressed" if suppressed else None,
                suppression_reason,
                request.actor_id,
                suppressed,
            ),
        )
        return self._required_read(
            connection, request.project_id, request.milestone_id
        )

    def _publish(self, milestone: AdoMilestone, actor_id: str) -> AdoMilestone:
        remote = self._client.read(
            project_id=milestone.project_id,
            work_item_id=milestone.work_item_id,
        )
        current_state = remote.fields.get("System.State")
        if not isinstance(current_state, str):
            raise AdoProgressError("ADO work item has no state")
        comment = self._client.comment_once(
            project_id=milestone.project_id,
            work_item_id=milestone.work_item_id,
            marker=f"milestone.{milestone.milestone_id}.{milestone.fingerprint[:16]}",
            text=_comment_text(milestone),
        )
        disposition = "not-requested"
        revision = remote.revision
        final_state = current_state
        target = milestone.target_state
        if target is not None:
            predecessor = _PREDECESSOR[target]
            if current_state == target:
                disposition = "already-current"
            elif current_state != predecessor:
                disposition = "preserved-manual"
            else:
                try:
                    revision = self._client.update_state(
                        project_id=milestone.project_id,
                        work_item_id=milestone.work_item_id,
                        operation_id=f"milestone-state:{milestone.milestone_id}",
                        expected_state=predecessor,
                        target_state=target,
                        actor_id=actor_id,
                    )
                    disposition = "updated"
                    final_state = target
                except AdoConflict:
                    latest = self._client.read(
                        project_id=milestone.project_id,
                        work_item_id=milestone.work_item_id,
                    )
                    latest_state = latest.fields.get("System.State")
                    if not isinstance(latest_state, str):
                        raise AdoProgressError("ADO work item has no state")
                    revision = latest.revision
                    final_state = latest_state
                    disposition = (
                        "already-current"
                        if latest_state == target
                        else "preserved-manual"
                    )
        return self._finish(
            milestone,
            comment_id=comment.comment_id,
            disposition=disposition,
            external_revision=revision,
            external_state=final_state,
        )

    def _finish(
        self,
        milestone: AdoMilestone,
        *,
        comment_id: int,
        disposition: str,
        external_revision: int,
        external_state: str,
    ) -> AdoMilestone:
        with psycopg.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            with connection.transaction():
                connection.execute(
                    f"""
                    UPDATE {SCHEMA}.work_item_ado_milestones
                    SET status = 'published', state_disposition = %s,
                        comment_id = %s, external_revision = %s,
                        external_state = %s, published_at = clock_timestamp(),
                        version = version + 1
                    WHERE project_id = %s AND milestone_id = %s
                      AND status = 'pending'
                    """,
                    (
                        disposition,
                        comment_id,
                        external_revision,
                        external_state,
                        milestone.project_id,
                        milestone.milestone_id,
                    ),
                )
                return self._required_read(
                    connection, milestone.project_id, milestone.milestone_id
                )

    @staticmethod
    def _read(connection, project_id: str, milestone_id: str) -> AdoMilestone | None:
        row = connection.execute(
            f"""
            SELECT project_id, milestone_id, work_item_id, sequence,
                   milestone_kind, fingerprint, summary, evidence, next_action,
                   target_state, status, state_disposition, comment_id,
                   external_revision, external_state, suppression_reason,
                   started_by, created_at::text, published_at::text, version
            FROM {SCHEMA}.work_item_ado_milestones
            WHERE project_id = %s AND milestone_id = %s
            """,
            (project_id, milestone_id),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["evidence"] = tuple(values["evidence"])
        return AdoMilestone(**values)

    @classmethod
    def _required_read(
        cls, connection, project_id: str, milestone_id: str
    ) -> AdoMilestone:
        milestone = cls._read(connection, project_id, milestone_id)
        if milestone is None:
            raise AdoProgressConflict("ADO milestone is missing")
        return milestone


def _request(
    value: MilestoneRequest,
) -> tuple[MilestoneRequest, str, str | None]:
    if not isinstance(value, MilestoneRequest):
        raise ValueError("MilestoneRequest is required")
    if isinstance(value.sequence, bool) or not isinstance(value.sequence, int):
        raise ValueError("sequence must be an integer")
    if value.sequence < 1:
        raise ValueError("sequence must be positive")
    kind = value.kind if value.kind in _KINDS else None
    if kind is None:
        raise ValueError("milestone kind is invalid")
    evidence = tuple(
        sorted(
            (_evidence(item) for item in value.evidence),
            key=lambda item: (item.kind, item.reference),
        )
    )
    if not evidence or len(evidence) != len(set(evidence)):
        raise ValueError("milestone evidence must be non-empty and unique")
    evidence_kinds = {item.kind for item in evidence}
    required_evidence = _REQUIRED_EVIDENCE[kind]
    if required_evidence not in evidence_kinds:
        raise ValueError(f"{kind} milestone requires {required_evidence} evidence")
    target = milestone_target_state(kind, evidence)
    normalized = MilestoneRequest(
        project_id=_project_id(value.project_id),
        work_item_id=_external_id(value.work_item_id, "work_item_id"),
        milestone_id=_external_id(value.milestone_id, "milestone_id"),
        sequence=value.sequence,
        kind=kind,
        summary=_text(value.summary, "summary", 1000),
        evidence=evidence,
        next_action=_text(value.next_action, "next_action", 1000),
        actor_id=_external_id(value.actor_id, "actor_id"),
    )
    payload = {
        "project_id": normalized.project_id,
        "work_item_id": normalized.work_item_id,
        "milestone_id": normalized.milestone_id,
        "sequence": normalized.sequence,
        "kind": normalized.kind,
        "summary": normalized.summary,
        "evidence": [item.to_dict() for item in normalized.evidence],
        "next_action": normalized.next_action,
        "target_state": target,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return normalized, fingerprint, target


def milestone_target_state(
    kind: str, evidence: Sequence[MilestoneEvidence]
) -> str | None:
    evidence_kinds = {item.kind for item in evidence}
    if kind == "start":
        return "Active"
    if kind in {"pull-request", "deployment"}:
        if {"implementation", "automated-test"} <= evidence_kinds:
            return "Resolved"
        return None
    if kind == "acceptance":
        if not {"acceptance", "owner-review"} <= evidence_kinds:
            raise ValueError(
                "acceptance milestone requires acceptance and owner-review evidence"
            )
        return "Closed"
    return None


def _comment_text(milestone: AdoMilestone) -> str:
    lines = [
        f"Status: {_LABELS[milestone.milestone_kind]}",
        milestone.summary,
        "",
        "Evidence:",
    ]
    lines.extend(
        f"- {item['kind']}: {item['reference']}" for item in milestone.evidence
    )
    lines.extend(("", f"Next action: {milestone.next_action}"))
    return "\n".join(lines)


def _evidence(value: object) -> MilestoneEvidence:
    if not isinstance(value, MilestoneEvidence) or value.kind not in _EVIDENCE_KINDS:
        raise ValueError("milestone evidence kind is invalid")
    return MilestoneEvidence(
        value.kind, _text(value.reference, "evidence reference", 500)
    )


def _text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError(f"{field} is invalid")
    return value.strip()


def _project_id(value: object) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise ValueError("project_id is invalid")
    return value


def _external_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _EXTERNAL_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value
