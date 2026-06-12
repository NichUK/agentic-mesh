from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.safe_outputs import SafeOutputCall


class SafeOutputFileWorker:
    """Deterministic worker adapter for local/operator role-service tests."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        items = raw.get("calls") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError("safe-output file must contain a list or an object with a calls list")

        calls: list[SafeOutputCall] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"safe-output call at index {index} must be an object")
            tool_name = item.get("tool_name")
            payload = item.get("payload")
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise ValueError(f"safe-output call at index {index} requires tool_name")
            if not isinstance(payload, dict):
                raise ValueError(f"safe-output call at index {index} requires object payload")
            role_id = item.get("role_id", assignment.role_id)
            if role_id != assignment.role_id:
                raise ValueError(
                    f"safe-output call at index {index} role_id {role_id!r} "
                    f"does not match assignment role {assignment.role_id!r}"
                )
            calls.append(
                SafeOutputCall(
                    role_id=assignment.role_id,
                    tool_name=tool_name,
                    payload=dict(payload),
                    terminal=bool(item.get("terminal", False)),
                )
            )
        return calls


def safe_output_file_payload(calls: list[dict[str, Any]]) -> str:
    return json.dumps({"calls": calls}, indent=2, sort_keys=True)
