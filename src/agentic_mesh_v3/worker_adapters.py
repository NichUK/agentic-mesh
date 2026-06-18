from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

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
        completed = _run_worker_command(
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
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(f"worker subprocess failed: {detail}")
        return _tool_calls_from_stdout(completed.stdout)


@dataclass(frozen=True)
class CodexCliWorker:
    """Run Codex CLI with the V3 safe-output tool contract.

    Codex must perform durable effects through safe-output CLI/MCP calls. Stdout
    is only a small operational envelope listing the calls made so the role
    service can acknowledge the inbox message.
    """

    command: tuple[str, ...] = ("codex", "exec")
    timeout_seconds: int = 14400
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox_mode: str | None = None

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        completed = _run_worker_command(
            _codex_command_with_options(
                self.command,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                sandbox_mode=self.sandbox_mode,
            ),
            input=_codex_prompt_text(prompt, message),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(f"codex-cli worker failed: {detail}")
        return _tool_calls_from_stdout(completed.stdout)


def build_worker_adapter(
    *,
    adapter: str,
    command: tuple[str, ...] | None = None,
    timeout_seconds: int | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    sandbox_mode: str | None = None,
) -> AgentWorker:
    if adapter == "safe-output-subprocess":
        if not command:
            raise ValueError("safe-output-subprocess worker requires command")
        return SafeOutputSubprocessWorker(
            command=_validated_command(command, adapter_name=adapter),
            timeout_seconds=timeout_seconds or 14400,
        )
    if adapter == "codex-cli":
        return CodexCliWorker(
            command=_validated_command(command or ("codex", "exec"), adapter_name=adapter),
            timeout_seconds=timeout_seconds or 14400,
            model=_non_empty_optional("model", model),
            reasoning_effort=_reasoning_effort(reasoning_effort),
            sandbox_mode=_non_empty_optional("sandbox_mode", sandbox_mode),
        )
    raise ValueError(f"unsupported V3 worker adapter: {adapter}")


def _codex_command_with_options(
    command: tuple[str, ...],
    *,
    model: str | None,
    reasoning_effort: str | None,
    sandbox_mode: str | None,
) -> list[str]:
    if not command:
        raise ValueError("codex-cli worker requires command")
    if not _is_codex_exec_command(command):
        return list(command)
    result = list(command)
    if model:
        result.extend(["--model", model])
    if sandbox_mode:
        result.extend(["--sandbox", sandbox_mode])
    if reasoning_effort:
        result.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    return result


def _is_codex_exec_command(command: tuple[str, ...]) -> bool:
    return len(command) >= 2 and Path(command[0]).name == "codex" and command[1] == "exec"


def _codex_prompt_text(prompt: str, message: AgentMessage) -> str:
    return "\n\n".join(
        [
            prompt,
            "SAFE-OUTPUT TOOL CONTRACT\n"
            "You MUST do durable work by calling approved Agentic Mesh safe-output tools before finishing. "
            "For conversational work, call status.reply with a text_markdown Markdown payload. For durable project work, call the "
            "appropriate work, handoff, document, governance, approval, release, or memory tools. If no "
            "action is appropriate, call noop with a reason payload. Use status.complete with a summary "
            "or report.incomplete with a reason. Do not rely on final prose as the result.\n"
            "At least one successful call must be a terminal safe-output tool: status.reply, status.complete, "
            "noop, or report.incomplete.\n"
            "After the safe-output tool calls have succeeded, write only this operational JSON envelope to stdout, "
            "including each tool_name and whether it was terminal:\n"
            '{"tool_calls":[{"tool_name":"status.reply","call_id":"call-...","terminal":true}]}\n'
            "If there was an error, exit non-zero and put the error detail on stderr.",
            "MESSAGE CONTEXT\n"
            + json.dumps(
                {
                    "message_id": message.message_id,
                    "subject": message.subject,
                    "payload": message.payload,
                },
                indent=2,
                sort_keys=True,
            ),
        ]
    )


def _non_empty_optional(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip():
        raise ValueError(f"codex-cli {name} must be a non-empty string")
    return value


def _reasoning_effort(value: str | None) -> str | None:
    value = _non_empty_optional("reasoning_effort", value)
    if value is not None and value not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("codex-cli reasoning_effort must be one of none, minimal, low, medium, high, xhigh")
    return value


def _validated_command(command: tuple[str, ...], *, adapter_name: str) -> tuple[str, ...]:
    for index, item in enumerate(command):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{adapter_name} command item {index} must be a non-empty string")
    return command


def _run_worker_command(
    command: tuple[str, ...] | list[str],
    *,
    input: str,
    capture_output: bool,
    text: bool,
    timeout: int,
) -> CompletedProcess[str]:
    """Run a worker command and tear down its process tree on timeout."""

    if not capture_output or not text:
        raise ValueError("worker commands must capture text output")
    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(list(command), **popen_kwargs)  # noqa: S603 - command is validated at config load.
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        timeout_detail = f"worker subprocess timed out after {timeout} seconds"
        stderr = "\n".join(part for part in (stderr, timeout_detail) if part)
        raise subprocess.TimeoutExpired(
            cmd=exc.cmd,
            timeout=exc.timeout,
            output=stdout,
            stderr=stderr,
        ) from exc
    return CompletedProcess(list(command), process.returncode, stdout, stderr)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(  # noqa: S603,S607 - best-effort local worker cleanup.
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


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
            value = item.get("tool_name") or item.get("call_id")
            if value:
                text = str(value)
                calls.append(f"terminal:{text}" if item.get("terminal") is True else text)
        else:
            raise ValueError("worker subprocess tool call entries must be strings or objects")
    return calls
