from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from agentic_mesh_v3.db import V3Database


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
            SELECT work_item_id, title, state, owner_role, next_action, updated_at
            FROM work_items
            WHERE state NOT IN ('closed', 'canceled', 'superseded', 'failed_terminal')
            ORDER BY updated_at ASC, work_item_id ASC
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
                )
            )
        return tuple(findings)


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
