from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.role_service import Worker
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
            if assignment.safe_output_transport is not None:
                return []
            raise ValueError("subprocess worker emitted no stdout safe-output JSON")
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"subprocess worker emitted invalid JSON: {exc.msg}") from exc
        return parse_safe_output_calls(raw, assignment=assignment, source="subprocess worker")


class CodexCliWorker:
    """Run a Codex-compatible command that emits safe-output JSON."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: int = 14400,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_mode: str | None = None,
        auth: dict[str, Any] | None = None,
    ) -> None:
        if not command:
            raise ValueError("codex-cli worker command is required")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        if model is not None and not model.strip():
            raise ValueError("codex-cli model must be a non-empty string")
        if reasoning_effort is not None and reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("codex-cli reasoning_effort must be one of none, minimal, low, medium, high, xhigh")
        if sandbox_mode is not None and not sandbox_mode.strip():
            raise ValueError("codex-cli sandbox_mode must be a non-empty string")
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.sandbox_mode = sandbox_mode
        self.auth = dict(auth or {})

    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        prompt_payload = _codex_prompt_payload(assignment, worker=self)
        input_text = (
            _codex_prompt_text(prompt_payload)
            if _is_codex_exec_command(self.command)
            else json.dumps(prompt_payload, sort_keys=True)
        )
        command = _codex_command_with_options(
            self.command,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            sandbox_mode=self.sandbox_mode,
        )
        try:
            result = subprocess.run(
                list(command),
                input=input_text,
                capture_output=True,
                check=False,
                encoding="utf-8",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"codex-cli worker timed out after {self.timeout_seconds} seconds") from exc
        if result.returncode != 0:
            stderr = result.stderr.strip()
            detail = f": {stderr}" if stderr else ""
            raise RuntimeError(f"codex-cli worker exited with code {result.returncode}{detail}")
        if not result.stdout.strip():
            if assignment.safe_output_transport is not None:
                return []
            raise ValueError("codex-cli worker emitted no stdout safe-output JSON")
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            if assignment.safe_output_transport is not None:
                return []
            raise ValueError(f"codex-cli worker emitted invalid JSON: {exc.msg}") from exc
        return parse_safe_output_calls(raw, assignment=assignment, source="codex-cli worker")


def _codex_prompt_payload(assignment: RoleAssignment, *, worker: CodexCliWorker) -> dict[str, Any]:
    return {
        "prompt": assignment.generated_prompt,
        "safe_outputs": assignment.safe_output_transport,
        "assignment": _assignment_payload(assignment),
        "worker": {
            "adapter": "codex-cli",
            "model": worker.model,
            "reasoning_effort": worker.reasoning_effort,
            "sandbox_mode": worker.sandbox_mode,
            "auth": worker.auth,
        },
    }


def _codex_prompt_text(payload: dict[str, Any]) -> str:
    assignment = payload["assignment"]
    safe_outputs = payload.get("safe_outputs") if isinstance(payload.get("safe_outputs"), dict) else {}
    record_command = safe_outputs.get("record_command") if isinstance(safe_outputs, dict) else None
    record_command_text = " ".join(_shell_quote(str(item)) for item in record_command) if isinstance(record_command, list) else ""
    destination_hint = ""
    assignment_payload = assignment.get("payload") if isinstance(assignment.get("payload"), dict) else {}
    if assignment_payload:
        conversation_id = assignment_payload.get("conversation_id")
        destination_ref = assignment_payload.get("destination_ref")
        destination_type = assignment_payload.get("destination_type")
        if conversation_id and destination_ref:
            destination_hint = (
                "\nFor status.reply to the source conversation, include these payload fields exactly: "
                f"conversation_id={conversation_id!r}, destination_ref={destination_ref!r}, "
                f"destination_type={destination_type or 'dm'!r}."
            )
    return "\n\n".join(
        part
        for part in [
            str(payload.get("prompt") or ""),
            "SAFE-OUTPUT TOOL CONTRACT\n"
            "You MUST perform durable output by running the safe-output CLI command before finishing. "
            "Do not rely on a final prose answer. If this is a simple conversational request, call "
            "`status.reply` with a concise Markdown message and no work item. If there is genuinely "
            "nothing to do, call `noop` with a reason."
            f"{destination_hint}\n"
            "Use this command pattern:\n"
            f"{record_command_text} --tool-name status.reply --payload-json '<json payload>'\n"
            "The command must exit successfully before you finish.",
            "ASSIGNMENT JSON\n" + json.dumps(assignment, indent=2, sort_keys=True),
        ]
        if part
    )


def _is_codex_exec_command(command: tuple[str, ...]) -> bool:
    return len(command) >= 2 and Path(command[0]).name == "codex" and command[1] == "exec"


def _codex_command_with_options(
    command: tuple[str, ...],
    *,
    model: str | None,
    reasoning_effort: str | None,
    sandbox_mode: str | None,
) -> tuple[str, ...]:
    if not _is_codex_exec_command(command):
        return command
    result = list(command)
    if model and model != "codex":
        result.extend(["--model", model])
    if sandbox_mode:
        result.extend(["--sandbox", sandbox_mode])
    if reasoning_effort:
        result.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    return tuple(result)


def _shell_quote(value: str) -> str:
    if not value:
        return "''"
    if all(ch.isalnum() or ch in "@%_+=:,./-" for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


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


def build_worker_adapter(config: dict[str, Any]) -> Worker:
    adapter = config.get("adapter")
    if adapter == "safe-output-file":
        path = config.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("safe-output-file worker config requires non-empty path")
        return SafeOutputFileWorker(Path(path))
    if adapter == "safe-output-subprocess":
        command = config.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError("safe-output-subprocess worker config requires non-empty command list")
        parsed_command = _validated_command(command, adapter_name="safe-output-subprocess")
        timeout_seconds = _validated_timeout(config, adapter_name="safe-output-subprocess", default=300)
        return SafeOutputSubprocessWorker(tuple(parsed_command), timeout_seconds=timeout_seconds)
    if adapter == "codex-cli":
        command = config.get("command")
        if command is None:
            executable = config.get("executable", "codex")
            args = config.get("args", ["exec"])
            if not isinstance(executable, str) or not executable.strip():
                raise ValueError("codex-cli executable must be a non-empty string")
            if not isinstance(args, list):
                raise ValueError("codex-cli args must be a list")
            command = [executable, *args]
        if not isinstance(command, list) or not command:
            raise ValueError("codex-cli worker config requires non-empty command list")
        parsed_command = _validated_command(command, adapter_name="codex-cli")
        timeout_seconds = _validated_timeout(config, adapter_name="codex-cli", default=14400)
        model = config.get("model")
        reasoning_effort = config.get("reasoning_effort")
        sandbox_mode = config.get("sandbox_mode")
        auth = config.get("auth")
        if model is not None and not isinstance(model, str):
            raise ValueError("codex-cli model must be a string")
        if reasoning_effort is not None and not isinstance(reasoning_effort, str):
            raise ValueError("codex-cli reasoning_effort must be a string")
        if sandbox_mode is not None and not isinstance(sandbox_mode, str):
            raise ValueError("codex-cli sandbox_mode must be a string")
        if auth is not None and not isinstance(auth, dict):
            raise ValueError("codex-cli auth must be a mapping")
        return CodexCliWorker(
            tuple(parsed_command),
            timeout_seconds=timeout_seconds,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_mode=sandbox_mode,
            auth=auth,
        )
    raise ValueError(f"unsupported worker adapter `{adapter}`")


def _validated_command(command: list[Any], *, adapter_name: str) -> list[str]:
    parsed_command: list[str] = []
    for index, item in enumerate(command):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{adapter_name} command item {index} must be a non-empty string")
        parsed_command.append(item)
    return parsed_command


def _validated_timeout(config: dict[str, Any], *, adapter_name: str, default: int) -> int:
    timeout_seconds = config.get("timeout_seconds", default)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise ValueError(f"{adapter_name} timeout_seconds must be an integer")
    return timeout_seconds
