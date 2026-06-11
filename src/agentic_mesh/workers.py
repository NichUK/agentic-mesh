from __future__ import annotations

import json
import os
import queue
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agentic_mesh.models import (
    AgentRunResult,
    AuthMethod,
    DocumentUpdate,
    FlowState,
    Handoff,
    Message,
    MeshConfig,
    ProjectConfig,
    RouteRequest,
    RoleInstanceConfig,
    utc_now_iso,
)
from agentic_mesh.prompt_builder import render_worker_prompt
from agentic_mesh.prompt_audit import document_library_root_for
from agentic_mesh.prompt_audit import write_work_item_prompt_audit
from agentic_mesh.problem_status import ProblemStatus
from agentic_mesh.problem_status import worker_problem_status
from agentic_mesh.safe_outputs import load_safe_output_records
from agentic_mesh.safe_outputs import result_from_safe_output_records
from agentic_mesh.safe_outputs import write_safe_output_audit
from agentic_mesh.worker_runs import FileWorkerRunStore
from agentic_mesh.worker_runs import WorkerRun
from agentic_mesh.worker_runs import WorkerRunObserver
from agentic_mesh.worker_runs import provider_recovery_class_for_failure
from agentic_mesh.worker_runs import resolve_worker_run_timeout_policy
from agentic_mesh.worker_runs import worker_run_condition_for_failure
from agentic_mesh.worker_runs import worker_run_status_for_failure


@dataclass(frozen=True)
class MonitoredCompletedProcess:
    returncode: int
    stdout: str
    stderr: str
    started_at: float
    progress_observed_at: str
    timed_out: bool = False
    completed_from_probe: bool = False


class WorkerAdapter:
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult | "WorkerRunOutcome":
        raise NotImplementedError


@dataclass(frozen=True)
class WorkerRunOutcome:
    role_result: AgentRunResult | None = None
    problem_status: ProblemStatus | None = None
    progress_observed_at: str | None = None

    def __post_init__(self) -> None:
        if (self.role_result is None) == (self.problem_status is None):
            raise ValueError("worker outcome must contain exactly one result type")


def worker_failure_outcome(
    *,
    instance: RoleInstanceConfig,
    message: Message,
    flow_state: FlowState,
    failure_class: str,
    recovery_action: str,
    retryable: bool,
    reason: str,
    timeout_seconds: int | None = None,
    progress_observed_at: str | None = None,
    artifact_paths: list[str] | None = None,
) -> WorkerRunOutcome:
    return WorkerRunOutcome(
        problem_status=worker_problem_status(
            failure_class=failure_class,
            reason=reason,
            recovery_action=recovery_action,
            retryable=retryable,
            role_id=instance.role_id,
            role_instance_id=instance.instance_id,
            message_payload=message.payload,
            source_message_id=message.message_id,
            correlation_id=message.correlation_id,
            lifecycle_state=flow_state.state_id,
            worker_adapter=instance.override.worker.adapter,
            worker_model=instance.override.worker.model,
            timeout_seconds=timeout_seconds,
            progress_observed_at=progress_observed_at,
            artifact_paths=artifact_paths,
        )
    )


class ConfiguredWorkerAdapter(WorkerAdapter):
    """Dispatches role work to the adapter declared by project configuration."""

    def __init__(
        self,
        *,
        mesh_config: MeshConfig | None = None,
        project: ProjectConfig,
        auth_methods: dict[str, AuthMethod],
        workspace_root: Path,
        state_root: Path,
    ) -> None:
        self.mesh_config = mesh_config
        self.project = project
        self.auth_methods = auth_methods
        self.workspace_root = workspace_root
        self.state_root = state_root
        self.codex = CodexCliWorkerAdapter(
            mesh_config=mesh_config,
            project=project,
            auth_methods=auth_methods,
            workspace_root=workspace_root,
            state_root=state_root,
        )

    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult | WorkerRunOutcome:
        store = FileWorkerRunStore(self.state_root, self.project.project_id)
        timeout_policy = resolve_worker_run_timeout_policy(instance)
        run = WorkerRun.from_claim(
            instance=instance,
            message=message,
            lifecycle_state=flow_state.state_id,
            timeout_policy=timeout_policy,
            status_url=_work_item_status_url(message.payload.get("work_item_id")),
        )
        try:
            store.start_run(run)
            observer = WorkerRunObserver(store=store, run=run)
            observer.mark_running()
        except Exception:
            return worker_failure_outcome(
                instance=instance,
                message=message,
                flow_state=flow_state,
                failure_class="publication_failed",
                recovery_action="operator_review",
                retryable=True,
                reason="Runtime could not persist trusted WorkerRun start evidence.",
            )
        adapter = instance.override.worker.adapter
        if adapter == "codex-cli":
            output = self.codex.run(
                instance,
                message,
                flow_state,
                worker_run_observer=observer,
            )
            _mark_worker_run_terminal(observer, output)
            return output
        output = self._problem(
            instance=instance,
            message=message,
            flow_state=flow_state,
            failure_class="unsupported_adapter",
            recovery_action="repair_configuration",
            retryable=False,
            reason="Configured worker adapter is not implemented in this runtime.",
        )
        _mark_worker_run_terminal(observer, output)
        return output

    @staticmethod
    def _problem(
        *,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
        failure_class: str,
        recovery_action: str,
        retryable: bool,
        reason: str,
        timeout_seconds: int | None = None,
        progress_observed_at: str | None = None,
        artifact_paths: list[str] | None = None,
    ) -> WorkerRunOutcome:
        return WorkerRunOutcome(
            problem_status=worker_problem_status(
                failure_class=failure_class,
                reason=reason,
                recovery_action=recovery_action,
                retryable=retryable,
                role_id=instance.role_id,
                role_instance_id=instance.instance_id,
                message_payload=message.payload,
                source_message_id=message.message_id,
                correlation_id=message.correlation_id,
                lifecycle_state=flow_state.state_id,
                worker_adapter=instance.override.worker.adapter,
                worker_model=instance.override.worker.model,
                timeout_seconds=timeout_seconds,
                progress_observed_at=progress_observed_at,
                artifact_paths=artifact_paths,
            )
        )


class CodexCliWorkerAdapter(WorkerAdapter):
    """Runs Codex CLI and interprets its safe-output tool calls."""

    def __init__(
        self,
        *,
        mesh_config: MeshConfig | None = None,
        project: ProjectConfig,
        auth_methods: dict[str, AuthMethod],
        workspace_root: Path,
        state_root: Path,
    ) -> None:
        self.mesh_config = mesh_config
        self.project = project
        self.auth_methods = auth_methods
        self.workspace_root = workspace_root
        self.state_root = state_root
        self.secret_root = state_root / "secrets"
        self.document_library_root = document_library_root_for(project, workspace_root)

    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
        worker_run_observer: WorkerRunObserver | None = None,
    ) -> AgentRunResult | WorkerRunOutcome:
        codex_bin = shutil.which("codex")
        if not codex_bin:
            return worker_failure_outcome(
                instance=instance,
                message=message,
                flow_state=flow_state,
                failure_class="missing_executable",
                recovery_action="repair_configuration",
                retryable=False,
                reason="Codex CLI is not installed in this worker container.",
            )

        auth_env_or_error = self._auth_environment(instance, message, flow_state)
        if isinstance(auth_env_or_error, WorkerRunOutcome):
            return auth_env_or_error

        with tempfile.TemporaryDirectory(prefix="agentic-mesh-worker-") as temp_dir:
            temp_path = Path(temp_dir)
            safe_output_path = temp_path / "safe-outputs.jsonl"
            command = [
                codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                instance.override.worker.sandbox_mode,
                "-C",
                str(self.workspace_root),
            ]
            model = instance.override.worker.model
            if model and model != "codex":
                command.extend(["--model", model])
            reasoning_effort = instance.override.worker.reasoning_effort
            if reasoning_effort:
                command.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])
            command.append("-")

            env = os.environ.copy()
            env.update(auth_env_or_error)
            env.update(
                {
                    "AGENTIC_MESH_SAFE_OUTPUT_FILE": str(safe_output_path),
                    "AGENTIC_MESH_PROJECT_ID": instance.project_id,
                    "AGENTIC_MESH_ROLE_ID": instance.role_id,
                    "AGENTIC_MESH_ROLE_INSTANCE_ID": instance.instance_id,
                    "AGENTIC_MESH_WORK_ITEM_ID": str(
                        message.payload.get("work_item_id") or ""
                    ),
                    "AGENTIC_MESH_WORK_ITEM_TYPE": str(
                        message.payload.get("work_item_type") or ""
                    ),
                    "AGENTIC_MESH_LIFECYCLE_STATE": flow_state.state_id,
                    "AGENTIC_MESH_MESSAGE_ID": message.message_id,
                    "AGENTIC_MESH_CORRELATION_ID": message.correlation_id,
                }
            )
            timeout_policy = resolve_worker_timeout_policy(instance)
            prompt = self._prompt(instance, message, flow_state)
            try:
                write_work_item_prompt_audit(
                    document_library_root=self.document_library_root,
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    prompt=prompt,
                    command=command,
                    workspace_root=self.workspace_root,
                )
            except Exception as exc:
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="publication_failed",
                    recovery_action="operator_review",
                    retryable=True,
                    reason=f"Runtime could not persist full worker prompt audit evidence: {exc.__class__.__name__}.",
                )
            session_probe_started_at = time.time()
            codex_home = env.get("CODEX_HOME")
            session_markers = [
                str(marker)
                for marker in [
                    message.message_id,
                    message.payload.get("work_item_id"),
                    instance.role_id,
                ]
                if marker
            ]
            completed = run_progress_aware_command(
                command,
                input_text=prompt,
                cwd=self.workspace_root,
                env=env,
                timeout_seconds=timeout_policy["max_timeout_seconds"],
                progress_window_seconds=timeout_policy["progress_window_seconds"],
                progress_paths=[safe_output_path],
                worker_run_observer=worker_run_observer,
                completion_probe=(
                    (
                        lambda: codex_session_completed_result(
                            Path(codex_home),
                            started_at=session_probe_started_at,
                            required_markers=session_markers,
                        )
                    )
                    if codex_home
                    else None
                ),
            )
            result: AgentRunResult | None = None
            validation_error: str | None = None
            try:
                records = load_safe_output_records(safe_output_path)
                result = result_from_safe_output_records(
                    records=records,
                    message=message,
                    flow_state=flow_state,
                )
                write_safe_output_audit(
                    document_library_root=self.document_library_root,
                    instance=instance,
                    message=message,
                    records=records,
                    result=result,
                )
            except Exception as exc:
                records = []
                try:
                    records = load_safe_output_records(safe_output_path)
                except Exception:
                    pass
                validation_error = f"{exc.__class__.__name__}: {exc}"
                try:
                    write_safe_output_audit(
                        document_library_root=self.document_library_root,
                        instance=instance,
                        message=message,
                        records=records,
                        validation_error=validation_error,
                    )
                except Exception:
                    pass
            if completed.timed_out:
                if result is not None:
                    return result
                partial_artifacts = self._partial_artifacts(
                    work_item_id=message.payload.get("work_item_id"),
                    started_at=completed.started_at,
                )
                recovery_action = (
                    "reconcile_partial_artifacts"
                    if partial_artifacts
                    else "retry_same_state"
                )
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="timeout",
                    recovery_action=recovery_action,
                    retryable=True,
                    reason="Worker execution exceeded the configured timeout or progress window before a valid role result was available.",
                    timeout_seconds=timeout_policy["timeout_seconds"],
                    progress_observed_at=completed.progress_observed_at,
                    artifact_paths=partial_artifacts,
                )

            process_output = "\n".join(
                part for part in (completed.stdout, completed.stderr) if part
            )
            if result is None and completed.returncode != 0:
                auth_reason = codex_auth_failure_reason(process_output)
                if auth_reason is not None:
                    return worker_failure_outcome(
                        instance=instance,
                        message=message,
                        flow_state=flow_state,
                        failure_class="auth_failed",
                        recovery_action="repair_auth",
                        retryable=False,
                        reason=auth_reason,
                        progress_observed_at=completed.progress_observed_at,
                    )
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="process_failed",
                    recovery_action="operator_review",
                    retryable=True,
                    reason=(
                        "Codex CLI exited before returning valid terminal "
                        "safe-output records."
                    ),
                    progress_observed_at=completed.progress_observed_at,
                )

            if result is None:
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="invalid_result",
                    recovery_action="retry_safe_output_contract",
                    retryable=True,
                    reason=(
                        "Codex CLI finished without valid terminal safe-output "
                        f"records: {validation_error or 'no safe-output records'}"
                    ),
                    progress_observed_at=completed.progress_observed_at,
                )

            if completed.returncode != 0 and result.status not in {
                "blocked",
                "needs_clarification",
                "failed",
            }:
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="process_failed",
                    recovery_action="operator_review",
                    retryable=True,
                    reason="Codex CLI exited before returning a valid role result.",
                )
            return result

    def _auth_environment(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> dict[str, str] | WorkerRunOutcome:
        binding = instance.override.worker.auth
        if binding is None:
            return {}

        method = self.auth_methods[binding.method]
        env: dict[str, str] = {}
        if method.requires_secret_ref:
            if not binding.secret_ref:
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="auth_missing",
                    recovery_action="repair_auth",
                    retryable=False,
                    reason=f"Worker auth method category `{method.category}` requires a configured secret.",
                )
            secret_path = self.secret_root / binding.secret_ref
            if not secret_path.exists():
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="auth_missing",
                    recovery_action="repair_auth",
                    retryable=False,
                    reason=f"Worker auth method category `{method.category}` has no available secret value.",
                )
            value = secret_path.read_text(encoding="utf-8").strip()
            for env_var in method.env_vars:
                env[env_var] = value

        if method.requires_mount_ref:
            if not binding.mount_ref:
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="auth_missing",
                    recovery_action="repair_auth",
                    retryable=False,
                    reason=self._auth_setup_message(
                        binding,
                        method.method_id,
                        f"Worker auth method category `{method.category}` requires a configured credential mount.",
                    ),
                )
            mount_path = self.state_root / "worker_mounts" / binding.mount_ref
            if not mount_path.exists():
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="auth_missing",
                    recovery_action="repair_auth",
                    retryable=False,
                    reason=self._auth_setup_message(
                        binding,
                        method.method_id,
                        "Worker credential mount is not available.",
                    ),
                )
            for env_var in method.env_vars:
                env[env_var] = str(mount_path)

        env.update(binding.env)
        return env

    def _auth_setup_message(
        self,
        binding,
        method_id: str,
        fallback: str,
    ) -> str:
        if method_id != "codex_oauth_cache":
            return fallback
        setup_url = os.environ.get(
            "AGENTIC_MESH_AUTH_ADMIN_URL",
            "http://127.0.0.1:8100/auth/credentials",
        )
        return (
            "Codex OAuth login is required before this agent can work. "
            f"Open the auth admin status surface at {setup_url} and sign in with OpenAI."
        )

    def _partial_artifacts(self, *, work_item_id: Any, started_at: float) -> list[str]:
        if not isinstance(work_item_id, str) or not work_item_id:
            return []
        root = self.workspace_root.resolve()
        work_item_root = root / "work-items" / work_item_id
        if not work_item_root.exists():
            return []
        artifacts: list[str] = []
        for path in work_item_root.rglob("*"):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not path.is_file() or root not in resolved.parents:
                continue
            try:
                if path.stat().st_mtime < started_at:
                    continue
            except OSError:
                continue
            artifacts.append(str(resolved.relative_to(root)).replace("\\", "/"))
        return artifacts

    def _prompt(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> str:
        return render_worker_prompt(
            project=self.project,
            mesh_config=self.mesh_config,
            instance=instance,
            message=message,
            flow_state=flow_state,
            workspace_root=self.workspace_root,
            state_root=self.state_root,
        )


class StubCodexWorkerAdapter(WorkerAdapter):
    """Deterministic test double for runtime unit tests only."""

    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        title = message.payload.get("title", "Untitled work item")
        summary = message.payload.get("summary", message.payload.get("text", ""))
        work_item_id = message.payload.get("work_item_id", message.message_id)
        work_item_type = message.payload.get("work_item_type", "slice")
        consult_lines = [
            (
                f"  - `{consult_id}` -> `{consult.target_role}` "
                f"at `{consult.target_state}`: {consult.purpose}"
            )
            for consult_id, consult in flow_state.consults.items()
        ]
        consult_summary = "\n".join(consult_lines) if consult_lines else "  - none"
        runtime_instructions = message.payload.get("runtime_instructions") or {}
        handoff_lines = [
            (
                f"  - `{option.get('status')}` -> `{option.get('target_role')}` "
                f"at `{option.get('target_state')}`"
            )
            for option in runtime_instructions.get("available_handoffs", [])
        ]
        handoff_summary = "\n".join(handoff_lines) if handoff_lines else "  - none"

        content = (
            f"\n## {title}\n\n"
            f"- Work item: `{work_item_id}`\n"
            f"- Work item type: `{work_item_type}`\n"
            f"- Lifecycle state: `{flow_state.state_id}`\n"
            f"- State purpose: {flow_state.purpose}\n"
            f"- Owner role: `{instance.role_id}`\n"
            f"- Claimed by: `{instance.instance_id}`\n"
            f"- Source message: `{message.message_id}`\n"
            f"- Correlation id: `{message.correlation_id}`\n"
            f"- Summary: {summary}\n"
            f"- Runtime scope: {runtime_instructions.get('scope', 'not provided')}\n"
            f"- Handoff guidance: {runtime_instructions.get('handoff_guidance', 'not provided')}\n"
            f"- Available handoff routes:\n{handoff_summary}\n"
            f"- Error guidance: {runtime_instructions.get('error_guidance', 'not provided')}\n"
            f"- Allowed consult routes:\n{consult_summary}\n"
        )
        handoffs: list[Handoff] = []
        transition = (
            flow_state.handoffs.get("completed")
            if message.payload.get("auto_handoff") is True
            else None
        )
        if transition:
            handoffs.append(
                Handoff(
                    target_role=transition.target_role,
                    message_type=transition.message_type,
                    payload={
                        "title": title,
                        "summary": summary,
                        "work_item_id": work_item_id,
                        "work_item_type": work_item_type,
                        "previous_lifecycle_state": flow_state.state_id,
                        "lifecycle_state": transition.target_state,
                        "source_message_id": message.message_id,
                        "auto_handoff": True,
                    },
                )
            )

        return AgentRunResult(
            status="completed",
            message=f"Completed {flow_state.state_id} for {work_item_id}",
            document_updates=[
                DocumentUpdate(path=flow_state.artifact_path, content=content),
            ],
            handoffs=handoffs,
        )


def blocked_result(flow_state: FlowState, reason: str) -> AgentRunResult:
    del flow_state
    return AgentRunResult(
        status="blocked",
        message=reason,
        document_updates=[],
        handoffs=[],
    )


def summarize_worker_failure(detail: str, max_length: int = 2000) -> str:
    text = detail.strip()
    if len(text) <= max_length:
        return text
    return f"...\n{text[-max_length:].lstrip()}"


def codex_auth_failure_reason(detail: str) -> str | None:
    text = " ".join(str(detail).split())
    lowered = text.lower()
    auth_markers = (
        "token_invalidated",
        "access token could not be refreshed",
        "authentication token has been invalidated",
        "your session has ended",
        "please log in again",
        "401 unauthorized",
    )
    if not any(marker in lowered for marker in auth_markers):
        return None
    summary = summarize_worker_failure(text, max_length=500)
    return (
        "Codex OAuth authentication failed before the agent could call any "
        "safe-output tool. Re-authenticate the configured Codex credential, "
        f"then retry the message. Provider detail: {summary}"
    )


def resolve_worker_timeout_policy(instance: RoleInstanceConfig) -> dict[str, int | None]:
    worker = instance.override.worker
    role_timeout = 3600 if instance.role_id == "engineering" else 1800
    role_progress = 600 if instance.role_id == "engineering" else 300
    timeout = worker.timeout_seconds or role_timeout
    progress_window = worker.progress_window_seconds or role_progress
    max_timeout = worker.max_timeout_seconds

    env_timeout = os.environ.get("AGENTIC_MESH_WORKER_TIMEOUT_SECONDS")
    if env_timeout:
        timeout = int(env_timeout)
        max_timeout = timeout
    env_progress = os.environ.get("AGENTIC_MESH_WORKER_PROGRESS_WINDOW_SECONDS")
    if env_progress:
        progress_window = int(env_progress)
    if max_timeout is not None:
        timeout = min(timeout, max_timeout)
    if timeout < 60 or progress_window < 60:
        raise ValueError("worker timeout and progress window must be at least 60 seconds")
    return {
        "timeout_seconds": timeout,
        "progress_window_seconds": progress_window,
        "max_timeout_seconds": max_timeout,
    }


def codex_session_completed_result(
    codex_home: Path,
    *,
    started_at: float,
    required_markers: list[str] | None = None,
) -> str | None:
    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        return None
    try:
        session_paths = sorted(
            (
                path
                for path in sessions_root.rglob("*.jsonl")
                if path.is_file() and path.stat().st_mtime >= started_at - 30
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in session_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        markers = [marker for marker in required_markers or [] if marker]
        if markers and not all(marker in text for marker in markers):
            continue
        lines = text.splitlines()
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "task_complete":
                continue
            message = payload.get("last_agent_message")
            if isinstance(message, str) and message.strip():
                return message
    return None


def run_progress_aware_command(
    command: list[str],
    *,
    input_text: str,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int | None,
    progress_window_seconds: int,
    progress_paths: list[Path],
    worker_run_observer: WorkerRunObserver | None = None,
    completion_probe: Callable[[], str | None] | None = None,
) -> MonitoredCompletedProcess:
    started_at = time.time()
    progress_observed_at = utc_now_iso()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
    )
    if process.stdin is not None:
        process.stdin.write(input_text)
        process.stdin.close()

    if os.name == "nt":
        return _run_progress_aware_command_threaded(
            process=process,
            started_at=started_at,
            progress_observed_at=progress_observed_at,
            timeout_seconds=timeout_seconds,
            progress_window_seconds=progress_window_seconds,
            progress_paths=progress_paths,
            worker_run_observer=worker_run_observer,
            completion_probe=completion_probe,
        )

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    selector = selectors.DefaultSelector()
    if process.stdout is not None:
        selector.register(process.stdout, selectors.EVENT_READ, stdout_parts)
    if process.stderr is not None:
        selector.register(process.stderr, selectors.EVENT_READ, stderr_parts)

    last_progress = started_at
    last_completion_probe = 0.0
    while process.poll() is None:
        now = time.time()
        if completion_probe is not None and now - last_completion_probe >= 5:
            last_completion_probe = now
            probed_result = completion_probe()
            if probed_result:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                return MonitoredCompletedProcess(
                    returncode=0,
                    stdout=probed_result,
                    stderr="".join(stderr_parts),
                    started_at=started_at,
                    progress_observed_at=utc_now_iso(),
                    completed_from_probe=True,
                )
        absolute_timeout = (
            timeout_seconds is not None and now - started_at >= timeout_seconds
        )
        progress_timeout = now - last_progress >= progress_window_seconds
        if absolute_timeout or progress_timeout:
            process.kill()
            process.wait(timeout=5)
            return MonitoredCompletedProcess(
                returncode=-9,
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
                started_at=started_at,
                progress_observed_at=progress_observed_at,
                timed_out=True,
            )
        for key, _ in selector.select(timeout=0.2):
            line = key.fileobj.readline()
            if not line:
                try:
                    selector.unregister(key.fileobj)
                except Exception:
                    pass
                continue
            key.data.append(line)
            last_progress = time.time()
            progress_observed_at = utc_now_iso()
            if worker_run_observer is not None:
                stream_name = "stdout" if key.data is stdout_parts else "stderr"
                worker_run_observer.observe_output(stream_name, line)
        for path in progress_paths:
            try:
                if path.exists() and path.stat().st_mtime >= last_progress:
                    last_progress = path.stat().st_mtime
                    progress_observed_at = utc_now_iso()
                    if worker_run_observer is not None:
                        worker_run_observer.observe_progress("result_file")
            except OSError:
                continue

    for stream, parts in [
        (process.stdout, stdout_parts),
        (process.stderr, stderr_parts),
    ]:
        if stream is None:
            continue
        try:
            remainder = stream.read()
            if remainder:
                parts.append(remainder)
                if worker_run_observer is not None:
                    stream_name = "stdout" if parts is stdout_parts else "stderr"
                    worker_run_observer.observe_output(stream_name, remainder)
        except Exception:
            pass
    return MonitoredCompletedProcess(
        returncode=process.returncode or 0,
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
        started_at=started_at,
        progress_observed_at=progress_observed_at,
    )


def _run_progress_aware_command_threaded(
    *,
    process: subprocess.Popen[str],
    started_at: float,
    progress_observed_at: str,
    timeout_seconds: int | None,
    progress_window_seconds: int,
    progress_paths: list[Path],
    worker_run_observer: WorkerRunObserver | None,
    completion_probe: Callable[[], str | None] | None,
) -> MonitoredCompletedProcess:
    output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def reader(stream, stream_name: str) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                output_queue.put((stream_name, line))
        except Exception:
            return

    for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
        threading.Thread(target=reader, args=(stream, name), daemon=True).start()

    last_progress = started_at
    last_completion_probe = 0.0
    while process.poll() is None:
        now = time.time()
        if completion_probe is not None and now - last_completion_probe >= 5:
            last_completion_probe = now
            probed_result = completion_probe()
            if probed_result:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                return MonitoredCompletedProcess(
                    returncode=0,
                    stdout=probed_result,
                    stderr="".join(stderr_parts),
                    started_at=started_at,
                    progress_observed_at=utc_now_iso(),
                    completed_from_probe=True,
                )
        absolute_timeout = (
            timeout_seconds is not None and now - started_at >= timeout_seconds
        )
        progress_timeout = now - last_progress >= progress_window_seconds
        if absolute_timeout or progress_timeout:
            process.kill()
            process.wait(timeout=5)
            return MonitoredCompletedProcess(
                returncode=-9,
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
                started_at=started_at,
                progress_observed_at=progress_observed_at,
                timed_out=True,
            )
        try:
            stream_name, line = output_queue.get(timeout=0.2)
        except queue.Empty:
            stream_name = ""
            line = ""
        if line:
            if stream_name == "stdout":
                stdout_parts.append(line)
            else:
                stderr_parts.append(line)
            last_progress = time.time()
            progress_observed_at = utc_now_iso()
            if worker_run_observer is not None:
                worker_run_observer.observe_output(stream_name, line)
        for path in progress_paths:
            try:
                if path.exists() and path.stat().st_mtime >= last_progress:
                    last_progress = path.stat().st_mtime
                    progress_observed_at = utc_now_iso()
                    if worker_run_observer is not None:
                        worker_run_observer.observe_progress("result_file")
            except OSError:
                continue

    while True:
        try:
            stream_name, line = output_queue.get_nowait()
        except queue.Empty:
            break
        if stream_name == "stdout":
            stdout_parts.append(line)
        else:
            stderr_parts.append(line)
        if worker_run_observer is not None:
            worker_run_observer.observe_output(stream_name, line)
    return MonitoredCompletedProcess(
        returncode=process.returncode or 0,
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
        started_at=started_at,
        progress_observed_at=progress_observed_at,
    )


def _mark_worker_run_terminal(
    observer: WorkerRunObserver,
    output: AgentRunResult | WorkerRunOutcome,
) -> None:
    if isinstance(output, WorkerRunOutcome):
        if output.problem_status is None:
            if output.role_result is not None:
                observer.mark_terminal(
                    run_status="completed",
                    run_condition="completed_valid_result",
                )
            return
        problem = output.problem_status
        observer.mark_terminal(
            run_status=worker_run_status_for_failure(problem.failure_class),
            run_condition=worker_run_condition_for_failure(problem.failure_class),
            failure_class=problem.failure_class,
            provider_recovery_class=provider_recovery_class_for_failure(
                problem.failure_class,
            ),
            retryable=problem.retryable,
            recovery_action=problem.recovery_action,
            next_action=problem.next_action,
            action_owner=problem.action_owner,
            problem_status_ref=problem.work_item_id,
        )
        return
    observer.mark_terminal(
        run_status="completed",
        run_condition="completed_valid_result",
    )


def _work_item_status_url(work_item_id: Any) -> str | None:
    if not isinstance(work_item_id, str) or not work_item_id.strip():
        return None
    if "/" in work_item_id or "\\" in work_item_id or ".." in work_item_id:
        return None
    return f"/work-items/{work_item_id}"
