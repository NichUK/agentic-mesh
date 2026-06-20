from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from json import JSONDecoder
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
            env=_message_context_environment(message),
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
            env=_message_context_environment(message),
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(f"codex-cli worker failed: {detail}")
        return _tool_calls_from_stdout(completed.stdout)


@dataclass(frozen=True)
class PersistentSessionWorker:
    """Persistent-session worker surface backed by Codex resumable sessions.

    This adapter is the V3 contract boundary for long-lived provider sessions:
    central DB memory remains canonical, while the provider session is an
    accelerator for conversational continuity. Codex CLI currently exposes a
    persisted resume path, so this adapter resumes the latest role-local Codex
    session after the first successful fresh run.
    """

    codex_worker: CodexCliWorker
    provider: str = "codex-cli"
    session_mode: str = "codex-exec-resume"
    session_status: str = "active"
    _fresh_session_started: bool = field(default=False, init=False, repr=False, compare=False)

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        prompt_text = _codex_prompt_text(prompt, message)
        if self._fresh_session_started and _is_codex_exec_command(self.codex_worker.command):
            try:
                return self._run_resume(prompt_text, message)
            except RuntimeError as exc:
                if not _is_missing_resume_session_error(str(exc)):
                    raise
        calls = self.codex_worker.run(prompt, message)
        object.__setattr__(self, "_fresh_session_started", True)
        return calls

    def _run_resume(self, prompt_text: str, message: AgentMessage) -> list[str]:
        completed = _run_worker_command(
            _codex_resume_command_with_options(
                self.codex_worker.command,
                model=self.codex_worker.model,
                reasoning_effort=self.codex_worker.reasoning_effort,
            ),
            input=prompt_text,
            capture_output=True,
            text=True,
            timeout=self.codex_worker.timeout_seconds,
            env=_message_context_environment(message),
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(f"codex-cli resume worker failed: {detail}")
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
    if adapter == "persistent-session":
        validated_command = _validated_command(command or ("codex", "exec"), adapter_name=adapter)
        resumable = _is_codex_exec_command(validated_command)
        return PersistentSessionWorker(
            codex_worker=CodexCliWorker(
                command=validated_command,
                timeout_seconds=timeout_seconds or 14400,
                model=_non_empty_optional("model", model),
                reasoning_effort=_reasoning_effort(reasoning_effort),
                sandbox_mode=_non_empty_optional("sandbox_mode", sandbox_mode),
            ),
            session_mode="codex-exec-resume" if resumable else "resume-backed-degraded",
            session_status="active" if resumable else "degraded",
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
    result.extend(["--output-schema", str(_codex_output_schema_path())])
    result.extend(
        [
            "--config",
            'approval_policy="never"',
            "--config",
            'shell_environment_policy.inherit="all"',
        ]
    )
    if reasoning_effort:
        result.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    return result


def _codex_resume_command_with_options(
    command: tuple[str, ...],
    *,
    model: str | None,
    reasoning_effort: str | None,
) -> list[str]:
    if not _is_codex_exec_command(command):
        return list(command)
    result = [command[0], command[1], "resume", "--last"]
    if model:
        result.extend(["--model", model])
    result.extend(["--output-schema", str(_codex_output_schema_path())])
    result.extend(
        [
            "--config",
            'approval_policy="never"',
            "--config",
            'shell_environment_policy.inherit="all"',
        ]
    )
    if reasoning_effort:
        result.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    result.append("-")
    return result


def _is_codex_exec_command(command: tuple[str, ...]) -> bool:
    return len(command) >= 2 and Path(command[0]).name == "codex" and command[1] == "exec"


def _is_missing_resume_session_error(detail: str) -> bool:
    normalized = detail.casefold()
    return "no session" in normalized or "no previous session" in normalized or "not found" in normalized


def _codex_output_schema_path() -> Path:
    prompt_root = os.environ.get("AGENTIC_MESH_PROMPT_CONFIG_ROOT")
    candidates = []
    if prompt_root:
        candidates.append(Path(prompt_root) / "worker" / "tool-envelope.schema.json")
    candidates.extend(
        [
            Path.cwd() / "config" / "prompts" / "worker" / "tool-envelope.schema.json",
            Path("/mesh/system/config/prompts/worker/tool-envelope.schema.json"),
            Path(__file__).resolve().parents[2] / "config" / "prompts" / "worker" / "tool-envelope.schema.json",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"missing Codex safe-output envelope schema; searched: {searched}")


def _codex_prompt_text(prompt: str, message: AgentMessage) -> str:
    return "\n\n".join(
        [
            prompt,
            _codex_tool_contract_text(),
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
            _final_tool_execution_checklist(),
        ]
    )


def _codex_tool_contract_text() -> str:
    prompt_root = os.environ.get("AGENTIC_MESH_PROMPT_CONFIG_ROOT")
    candidates = []
    if prompt_root:
        candidates.append(Path(prompt_root) / "worker" / "codex-tool-contract.md")
    candidates.extend(
        [
            Path.cwd() / "config" / "prompts" / "worker" / "codex-tool-contract.md",
            Path("/mesh/system/config/prompts/worker/codex-tool-contract.md"),
            Path(__file__).resolve().parents[2] / "config" / "prompts" / "worker" / "codex-tool-contract.md",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"missing Codex tool contract prompt file; searched: {searched}")


def _final_tool_execution_checklist() -> str:
    return """FINAL EXECUTION CHECKLIST
Before finishing this run:
1. Call at least one allowed DO safe-output tool for the current assignment.
2. Call at least one allowed REPLY safe-output tool for the result or next step.
3. If work is non-terminal, create the required handoff, delegation, stakeholder question, approval request, blocker, or informed update before replying.
4. After the tool calls succeed, write only the operational JSON envelope with real call IDs to stdout.
5. If you cannot perform the requested action, call report.incomplete or blocker.raise instead of ending with prose."""


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
    env: dict[str, str] | None = None,
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
    if env:
        popen_kwargs["env"] = {**os.environ, **env}
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


def _message_context_environment(message: AgentMessage) -> dict[str, str]:
    env = {
        "AGENTIC_MESH_MESSAGE_ID": message.message_id,
        "AGENTIC_MESH_CORRELATION_ID": str(message.payload.get("correlation_id") or f"corr-{message.message_id}"),
    }
    for payload_key, env_key in (
        ("project_id", "AGENTIC_MESH_PROJECT_ID"),
        ("role_id", "AGENTIC_MESH_ROLE_ID"),
        ("role_instance_id", "AGENTIC_MESH_ROLE_INSTANCE_ID"),
        ("connector", "AGENTIC_MESH_CONNECTOR"),
        ("conversation_ref", "AGENTIC_MESH_CONVERSATION_REF"),
        ("reply_target_ref", "AGENTIC_MESH_REPLY_TARGET_REF"),
        ("reply_thread_ref", "AGENTIC_MESH_REPLY_THREAD_REF"),
        ("source_type", "AGENTIC_MESH_SOURCE_TYPE"),
        ("sender_ref", "AGENTIC_MESH_SENDER_REF"),
        ("work_item_id", "AGENTIC_MESH_WORK_ITEM_ID"),
        ("queue_item_id", "AGENTIC_MESH_QUEUE_ITEM_ID"),
    ):
        value = message.payload.get(payload_key)
        if value is not None and str(value) != "":
            env[env_key] = str(value)
    return env


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
        payload = _json_object_from_stdout(stdout)
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


def _json_object_from_stdout(stdout: str) -> dict[str, object]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        raise ValueError("worker subprocess stdout must be a JSON object")
    return payload


def _extract_json_object(text: str) -> object:
    decoder = JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            payload, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        suffix = text[index + end :].strip()
        if suffix and suffix[0] not in {"`", "\n"}:
            continue
        return payload
    raise json.JSONDecodeError("no JSON object found", text, 0)
