from __future__ import annotations

import json
import subprocess
from pathlib import Path
from dataclasses import asdict
from typing import Any

from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.safe_outputs import SafeOutputCall


class SafeOutputFileWorker:
    """Deterministic worker adapter for local/operator role-service tests."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return parse_safe_output_calls(raw, assignment=assignment, source="safe-output file")


class SafeOutputSubprocessWorker:
    """Run an external command that reads assignment JSON and emits safe-output JSON."""

    def __init__(self, command: tuple[str, ...], *, timeout_seconds: int = 300) -> None:
        if not command:
            raise ValueError("subprocess worker command is required")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        self.command = command
        self.timeout_seconds = timeout_seconds

    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        assignment_json = json.dumps(_assignment_payload(assignment), sort_keys=True)
        try:
            result = subprocess.run(
                list(self.command),
                input=assignment_json,
                capture_output=True,
                check=False,
                encoding="utf-8",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"subprocess worker timed out after {self.timeout_seconds} seconds") from exc
        if result.returncode != 0:
            stderr = result.stderr.strip()
            detail = f": {stderr}" if stderr else ""
            raise RuntimeError(f"subprocess worker exited with code {result.returncode}{detail}")
        if not result.stdout.strip():
            raise ValueError("subprocess worker emitted no stdout safe-output JSON")
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"subprocess worker emitted invalid JSON: {exc.msg}") from exc
        return parse_safe_output_calls(raw, assignment=assignment, source="subprocess worker")


def parse_safe_output_calls(raw: Any, *, assignment: RoleAssignment, source: str) -> list[SafeOutputCall]:
    items = raw.get("calls") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError(f"{source} must contain a list or an object with a calls list")

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


def _assignment_payload(assignment: RoleAssignment) -> dict[str, Any]:
    payload = asdict(assignment)
    payload["conversation_context"] = list(assignment.conversation_context)
    payload["memory_context"] = list(assignment.memory_context)
    return payload


def safe_output_file_payload(calls: list[dict[str, Any]]) -> str:
    return json.dumps({"calls": calls}, indent=2, sort_keys=True)
