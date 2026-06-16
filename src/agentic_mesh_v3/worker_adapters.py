from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from agentic_mesh_v3.agent import AgentMessage
from agentic_mesh_v3.agent import AgentWorker


@dataclass(frozen=True)
class SafeOutputSubprocessWorker:
    """Run an external worker that records durable effects through safe-output tools.

    The subprocess receives the generated prompt and message metadata on stdin.
    Its stdout is an operational status envelope, not the source of durable
    state. Durable effects must already have been recorded through CLI/MCP tools.
    """

    command: tuple[str, ...]
    timeout_seconds: int = 14400

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        completed = subprocess.run(
            self.command,
            input=json.dumps(
                {
                    "prompt": prompt,
                    "message": {
                        "message_id": message.message_id,
                        "subject": message.subject,
                        "payload": message.payload,
                    },
                }
            ),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(f"worker subprocess failed: {detail}")
        return _tool_calls_from_stdout(completed.stdout)


def build_worker_adapter(
    *,
    adapter: str,
    command: tuple[str, ...] | None = None,
    timeout_seconds: int | None = None,
) -> AgentWorker:
    if adapter == "safe-output-subprocess":
        if not command:
            raise ValueError("safe-output-subprocess worker requires command")
        return SafeOutputSubprocessWorker(command=command, timeout_seconds=timeout_seconds or 14400)
    raise ValueError(f"unsupported V3 worker adapter: {adapter}")


def _tool_calls_from_stdout(stdout: str) -> list[str]:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("worker subprocess stdout must be JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("worker subprocess stdout must be a JSON object")
    raw_calls = payload.get("tool_calls") or payload.get("calls") or []
    if not isinstance(raw_calls, list):
        raise ValueError("worker subprocess `tool_calls` must be a list")
    calls: list[str] = []
    for item in raw_calls:
        if isinstance(item, str):
            calls.append(item)
        elif isinstance(item, dict):
            value = item.get("call_id") or item.get("tool_name")
            if value:
                calls.append(str(value))
        else:
            raise ValueError("worker subprocess tool call entries must be strings or objects")
    return calls
