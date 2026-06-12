from __future__ import annotations

import json
from typing import Any

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputService


def parse_safe_output_payload_json(payload_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--payload-json must be valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("--payload-json must be a JSON object")
    return payload


def record_safe_output_for_run(
    db: V2Database,
    *,
    run_id: object,
    role_id: object,
    tool_name: object,
    payload: dict[str, Any],
    terminal: bool = False,
    service: SafeOutputService | None = None,
) -> dict[str, object]:
    run_id = _required_text(run_id, "run_id")
    role_id = _required_text(role_id, "role_id")
    tool_name = _required_text(tool_name, "tool_name")
    run = db.get_agent_run(run_id)
    if run is None:
        raise ValueError(f"agent run `{run_id}` was not found")
    if run.get("status") != "running":
        raise ValueError(f"agent run `{run_id}` is not running")
    if run.get("role_id") != role_id:
        raise ValueError(
            f"agent run `{run_id}` belongs to role `{run.get('role_id')}`, not `{role_id}`"
        )
    if not isinstance(payload, dict):
        raise ValueError("safe-output payload must be a JSON object")

    safe_outputs = service or SafeOutputService(db)
    call = SafeOutputCall(
        role_id=role_id,
        tool_name=tool_name,
        payload=payload,
        terminal=bool(terminal),
    )
    call_id = safe_outputs.record(run_id=run_id, call=call)
    recorded = next(
        (row for row in db.list_safe_output_calls_for_run(run_id) if row.get("call_id") == call_id),
        None,
    )
    return {
        "status": "ok",
        "run_id": run_id,
        "role_id": role_id,
        "tool_name": tool_name,
        "call_id": call_id,
        "terminal": bool(recorded.get("terminal")) if recorded is not None else bool(terminal),
    }


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{name}` must be a non-empty string")
    return value.strip()
