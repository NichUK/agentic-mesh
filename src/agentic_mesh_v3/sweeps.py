from __future__ import annotations

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
            reason = _reason_for(row=dict(row), stale_after_seconds=stale_after_seconds, now=current_time)
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
            message_ids.append(message.message_id)
        return tuple(message_ids)


def _reason_for(*, row: dict[str, Any], stale_after_seconds: int, now: datetime) -> str | None:
    state = str(row["state"])
    if state in WATCH_STATES:
        return f"work item is in {state}"
    updated_at = _parse_sqlite_timestamp(str(row["updated_at"]))
    if updated_at is None:
        return "work item updated_at timestamp is unreadable"
    age = now - updated_at
    if age >= timedelta(seconds=stale_after_seconds):
        return f"work item has not changed for {int(age.total_seconds())} seconds"
    return None


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
