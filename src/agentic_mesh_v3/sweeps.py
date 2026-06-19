from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.reporting import work_item_url


WATCH_STATES = {"blocked", "waiting_human", "waiting_agent", "waiting_external", "recovering"}
TERMINAL_STATES = {"closed", "canceled", "superseded", "failed_terminal"}


@dataclass(frozen=True)
class SweepFinding:
    work_item_id: str
    title: str
    state: str
    owner_role: str
    reason: str
    next_action: str
    updated_at: str
    artifact_count: int = 0
    work_item_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProjectSweepService:
    """Read-only project health sweep for Project Manager agents."""

    def __init__(self, db: V3Database) -> None:
        self.db = db

    def sweep(
        self,
        *,
        stale_after_seconds: int = 3600,
        now: datetime | None = None,
    ) -> tuple[SweepFinding, ...]:
        current_time = now or datetime.now(timezone.utc)
        findings: list[SweepFinding] = []
        for row in self.db.connection.execute(
            """
            SELECT wi.work_item_id, wi.title, wi.state, wi.owner_role, wi.next_action, wi.updated_at,
                   COUNT(a.artifact_id) AS artifact_count
            FROM work_items wi
            LEFT JOIN artifacts a ON a.work_item_id = wi.work_item_id
            WHERE wi.state NOT IN ('closed', 'canceled', 'superseded', 'failed_terminal')
            GROUP BY wi.work_item_id
            ORDER BY wi.updated_at ASC, wi.work_item_id ASC
            """
        ):
            reason = _reason_for(
                row=dict(row),
                governance_reason=_governance_reason(self.db, work_item_id=row["work_item_id"]),
                stale_after_seconds=stale_after_seconds,
                now=current_time,
            )
            if reason is None:
                continue
            findings.append(
                SweepFinding(
                    work_item_id=row["work_item_id"],
                    title=row["title"],
                    state=row["state"],
                    owner_role=row["owner_role"],
                    reason=reason,
                    next_action=row["next_action"],
                    updated_at=row["updated_at"],
                    artifact_count=int(row["artifact_count"] or 0),
                    work_item_url=work_item_url(row["work_item_id"]),
                )
            )
        return tuple(findings)

    def publish_findings(
        self,
        broker: BrokerAdapter,
        *,
        stream: str,
        findings: tuple[SweepFinding, ...],
        project_manager_role_id: str = "project-manager",
    ) -> tuple[str, ...]:
        broker.ensure_stream(stream, [f"agent.{project_manager_role_id}"])
        message_ids: list[str] = []
        for finding in findings:
            signature = _finding_signature(finding)
            if self._already_published(finding.work_item_id, signature=signature):
                continue
            message = broker.publish(
                stream,
                f"agent.{project_manager_role_id}",
                {
                    "message_type": "project_sweep.finding",
                    "work_item_id": finding.work_item_id,
                    "title": finding.title,
                    "state": finding.state,
                    "owner_role": finding.owner_role,
                    "reason": finding.reason,
                    "next_action": finding.next_action,
                    "updated_at": finding.updated_at,
                    "artifact_count": finding.artifact_count,
                    "work_item_url": finding.work_item_url,
                    "required_action": "Review the finding and use normal tools to chase, unblock, rescope, or close the work.",
                },
            )
            self._record_published_finding(finding, message_id=message.message_id, signature=signature)
            message_ids.append(message.message_id)
        return tuple(message_ids)

    def _already_published(self, work_item_id: str, *, signature: str) -> bool:
        row = self.db.connection.execute(
            """
            SELECT 1
            FROM events
            WHERE event_type='project_sweep.finding_published'
              AND aggregate_type='work_item'
              AND aggregate_id=?
              AND json_extract(payload_json, '$.signature')=?
            LIMIT 1
            """,
            (work_item_id, signature),
        ).fetchone()
        return row is not None

    def _record_published_finding(self, finding: SweepFinding, *, message_id: str, signature: str) -> None:
        with self.db.connection:
            self.db.record_event(
                "project_sweep.finding_published",
                "work_item",
                finding.work_item_id,
                {
                    "message_id": message_id,
                    "signature": signature,
                    "state": finding.state,
                    "owner_role": finding.owner_role,
                    "reason": finding.reason,
                    "next_action": finding.next_action,
                    "updated_at": finding.updated_at,
                    "artifact_count": finding.artifact_count,
                },
            )


def _reason_for(
    *,
    row: dict[str, Any],
    governance_reason: str | None = None,
    stale_after_seconds: int,
    now: datetime,
) -> str | None:
    state = str(row["state"])
    if state in WATCH_STATES:
        return f"work item is in {state}"
    if governance_reason:
        return governance_reason
    updated_at = _parse_sqlite_timestamp(str(row["updated_at"]))
    if updated_at is None:
        return "work item updated_at timestamp is unreadable"
    age = now - updated_at
    if age >= timedelta(seconds=stale_after_seconds):
        return f"work item has not changed for {int(age.total_seconds())} seconds"
    return None


def _finding_signature(finding: SweepFinding) -> str:
    payload = {
        "work_item_id": finding.work_item_id,
        "state": finding.state,
        "owner_role": finding.owner_role,
        "reason": finding.reason,
        "next_action": finding.next_action,
        "updated_at": finding.updated_at,
        "artifact_count": finding.artifact_count,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _parse_sqlite_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _governance_reason(db: V3Database, *, work_item_id: str) -> str | None:
    checklist = db.work_item_governance_checklist(work_item_id)
    if checklist is None or checklist.is_satisfied:
        return None
    parts: list[str] = []
    if checklist.missing_consultations:
        parts.append(f"consultations={', '.join(checklist.missing_consultations)}")
    if checklist.missing_informed_updates:
        parts.append(f"informed_updates={', '.join(checklist.missing_informed_updates)}")
    if checklist.pending_sponsor_decisions:
        parts.append(f"sponsor_decisions={', '.join(checklist.pending_sponsor_decisions)}")
    if not parts:
        return None
    return "governance checklist has unresolved items: " + "; ".join(parts)
