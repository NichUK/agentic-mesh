from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_mesh import telemetry
from agentic_mesh.models import utc_now_iso


class EventJournal:
    def __init__(self, state_root: Path, project_id: str) -> None:
        self.path = state_root / "projects" / project_id / "journal" / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, **fields: Any) -> dict[str, Any]:
        event = {
            "timestamp": utc_now_iso(),
            "event_type": event_type,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        telemetry.record_journal_event(event)
        return event

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
