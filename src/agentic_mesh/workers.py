from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agentic_mesh.capabilities import capability_prompt_context
from agentic_mesh.document_library import document_library_context
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
from agentic_mesh.problem_status import ProblemStatus
from agentic_mesh.problem_status import worker_problem_status
from agentic_mesh.worker_runs import FileWorkerRunStore
from agentic_mesh.worker_runs import WorkerRun
from agentic_mesh.worker_runs import WorkerRunObserver
from agentic_mesh.worker_runs import provider_recovery_class_for_failure
from agentic_mesh.worker_runs import resolve_worker_run_timeout_policy
from agentic_mesh.worker_runs import worker_run_condition_for_failure
from agentic_mesh.worker_runs import worker_run_status_for_failure


AGENT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "message", "document_updates", "handoffs", "routes"],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "blocked", "needs_clarification", "failed"],
        },
        "message": {"type": "string", "minLength": 1},
        "document_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "path",
                    "content",
                    "purpose",
                    "review_status",
                    "index_summary",
                    "maintain_work_item_index",
                ],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "purpose": {"type": ["string", "null"]},
                    "review_status": {"type": ["string", "null"]},
                    "index_summary": {"type": ["string", "null"]},
                    "maintain_work_item_index": {"type": ["boolean", "null"]},
                },
            },
        },
        "handoffs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target_role", "message_type", "payload"],
                "properties": {
                    "target_role": {"type": "string"},
                    "message_type": {"type": "string"},
                    "payload": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "title",
                            "summary",
                            "work_item_id",
                            "work_item_type",
                            "previous_lifecycle_state",
                            "lifecycle_state",
                            "source_message_id",
                            "out_of_flow",
                            "out_of_flow_reason",
                        ],
                        "properties": {
                            "title": {"type": ["string", "null"]},
                            "summary": {"type": ["string", "null"]},
                            "work_item_id": {"type": ["string", "null"]},
                            "work_item_type": {"type": ["string", "null"]},
                            "previous_lifecycle_state": {"type": ["string", "null"]},
                            "lifecycle_state": {"type": ["string", "null"]},
                            "source_message_id": {"type": ["string", "null"]},
                            "out_of_flow": {"type": ["boolean", "null"]},
                            "out_of_flow_reason": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        },
        "routes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target_role", "message_type", "payload"],
                "properties": {
                    "target_role": {"type": "string"},
                    "message_type": {"type": "string"},
                    "payload": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "title",
                            "summary",
                            "work_item_id",
                            "work_item_type",
                            "previous_lifecycle_state",
                            "lifecycle_state",
                            "source_message_id",
                            "out_of_flow",
                            "out_of_flow_reason",
                            "review_status",
                            "correction_status",
                            "defect_id",
                            "gate_id",
                            "review_artifact_path",
                            "required_change",
                            "evidence_required",
                        ],
                        "properties": {
                            "title": {"type": ["string", "null"]},
                            "summary": {"type": ["string", "null"]},
                            "work_item_id": {"type": ["string", "null"]},
                            "work_item_type": {"type": ["string", "null"]},
                            "previous_lifecycle_state": {"type": ["string", "null"]},
                            "lifecycle_state": {"type": ["string", "null"]},
                            "source_message_id": {"type": ["string", "null"]},
                            "out_of_flow": {"type": ["boolean", "null"]},
                            "out_of_flow_reason": {"type": ["string", "null"]},
                            "review_status": {"type": ["string", "null"]},
                            "correction_status": {"type": ["string", "null"]},
                            "defect_id": {"type": ["string", "null"]},
                            "gate_id": {"type": ["string", "null"]},
                            "review_artifact_path": {"type": ["string", "null"]},
                            "required_change": {"type": ["string", "null"]},
                            "evidence_required": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        },
    },
}


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
    """Runs a real Codex CLI agent and converts its JSON result to runtime output."""

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
            schema_path = temp_path / "agent-result.schema.json"
            output_path = temp_path / "agent-result.json"
            schema_path.write_text(
                json.dumps(AGENT_RESULT_SCHEMA, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            command = [
                codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                instance.override.worker.sandbox_mode,
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
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
            timeout_policy = resolve_worker_timeout_policy(instance)
            prompt = self._prompt(instance, message, flow_state)
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
                progress_paths=[output_path],
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
            if completed.timed_out:
                if output_path.exists():
                    try:
                        payload = parse_agent_result(output_path.read_text(encoding="utf-8"))
                        return result_from_payload(payload, flow_state)
                    except ValueError:
                        pass
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

            if completed.returncode != 0:
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class="process_failed",
                    recovery_action="operator_review",
                    retryable=True,
                    reason="Codex CLI exited before returning a valid role result.",
                )

            raw_result = (
                output_path.read_text(encoding="utf-8")
                if output_path.exists()
                else completed.stdout
            )
            try:
                payload = parse_agent_result(raw_result)
                return result_from_payload(payload, flow_state)
            except ValueError as exc:
                failure_class = "schema_failed" if "unsupported status" in str(exc) else "invalid_result"
                return worker_failure_outcome(
                    instance=instance,
                    message=message,
                    flow_state=flow_state,
                    failure_class=failure_class,
                    recovery_action="operator_review",
                    retryable=True,
                    reason=f"Codex CLI returned an invalid role result: {exc}",
                )

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
        runtime_instructions = message.payload.get("runtime_instructions") or {}
        available_handoffs = runtime_instructions.get("available_handoffs", [])
        available_consults = runtime_instructions.get("available_consults", [])
        repositories = {
            repository_id: {
                "type": repository.type,
                "path": repository.path,
                "absolute_path": str(
                    Path(repository.path)
                    if Path(repository.path).is_absolute()
                    else (self.workspace_root / repository.path).resolve()
                ),
                "default_branch": repository.default_branch,
            }
            for repository_id, repository in self.project.workspace.repositories.items()
        }
        if self.mesh_config is not None:
            capability_context = capability_prompt_context(
                mesh_config=self.mesh_config,
                instance=instance,
                state_root=self.state_root,
            )
        else:
            capability_context = {
                "schema_version": "capability-prompt-context-v0",
                "availability_statement": "Capability availability has not been validated for this role instance.",
                "configured_required": [
                    {
                        "capability_id": tool_id,
                        "category": "compatibility_default_tool",
                        "display_name": tool_id.replace(".", " ").title(),
                        "availability": "not_validated",
                    }
                    for tool_id in instance.template.default_tools
                ],
                "available": [],
                "missing_required": [],
                "fallbacks": [],
                "waivers": [],
                "redaction_applied": True,
            }
        return f"""
You are the `{instance.role_id}` role agent for Agentic Mesh project `{instance.project_id}`.

Role purpose:
{instance.template.purpose}

Project goal:
{json.dumps({
            "description": self.project.goal.description,
            "success_measures": self.project.goal.success_measures,
            "constraints": self.project.goal.constraints,
            "guidance": self.project.goal.guidance,
        }, indent=2)}

Project workspace:
{json.dumps({
            "current_working_directory": str(self.workspace_root),
            "workspace_root": self.project.workspace.root,
            "default_repository": self.project.workspace.default_repository,
            "repositories": repositories,
        }, indent=2)}

Document library and role memory:
{json.dumps(document_library_context(self.workspace_root, self.project), indent=2)}

Standing role instructions:
{json.dumps(instance.template.standing_instructions, indent=2)}

Role charter:
{json.dumps({
            "role_profile": instance.template.role_profile,
            "accountabilities": instance.template.accountabilities,
            "decision_rights": instance.template.decision_rights,
            "boundaries": instance.template.boundaries,
            "collaboration_style": instance.template.collaboration_style,
            "quality_bar": instance.template.quality_bar,
            "memory_focus": instance.template.memory_focus,
            "core_workflows": instance.template.core_workflows,
            "standards_references": instance.template.standards_references,
            "anti_patterns": instance.template.anti_patterns,
        }, indent=2)}

Role capability context:
{json.dumps(capability_context, indent=2)}

Project role instructions:
{json.dumps(instance.override.instructions, indent=2)}

Allowed write paths:
{json.dumps(instance.override.write_paths, indent=2)}

Work item:
{json.dumps(message.payload, indent=2, sort_keys=True)}

Current flow state:
{json.dumps({
            "state_id": flow_state.state_id,
            "purpose": flow_state.purpose,
            "artifact_path": flow_state.artifact_path,
            "gates": [
                {
                    "gate_id": gate.gate_id,
                    "type": gate.type,
                    "required_documents": gate.required_documents,
                    "affected_roles": gate.affected_roles,
                    "review_outcomes": gate.review_outcomes,
                    "max_resolution_loops": gate.max_resolution_loops,
                }
                for gate in flow_state.gates
            ],
            "available_handoffs": available_handoffs,
            "available_consults": available_consults,
        }, indent=2)}

You must do the actual role work. Inspect the repository and project documents
from your role's perspective before answering. Do not produce generic template
output. If you cannot complete the work because credentials, tools, context, or
permissions are missing, return status `blocked` with a precise reason.
Keep every action aligned to the project goal. Ask necessary clarifying
questions, propose explicit assumptions when appropriate, and work with other
roles through the configured flow to advance the goal. If a task, handoff,
artifact, or recommendation does not advance the goal or reduce a meaningful
risk to it, say so and keep the work scoped.
Prefer configured lifecycle handoffs. If a genuinely warranted handoff needs to
go outside the configured route, include `lifecycle_state` for a state owned by
the target role and include `out_of_flow_reason` explaining why the exception is
needed. For handoff payload fields that runtime can derive, use `null` when you
do not need to set them yourself. Do not emit ambiguous handoffs.

Return only the final JSON object required by the provided schema. Put all
document changes in `document_updates`; do not rely on unreported filesystem
edits. For direct broadcast work, use the requested artifact path and do not
emit handoffs unless the prompt explicitly asks for one.

All real lifecycle work must produce enterprise-grade documentation. For slice
and subslice work, use the configured slice-scoped artifact path, normally under
`work-items/{{work_item_id}}/`, unless you are deliberately updating a durable
project standard, ADR, index, or evergreen reference. The document update should
include the objective, scope, assumptions, decisions, evidence, risks, review
log, and next handoff or closure criteria appropriate to your role and state.
Do not write vague generic notes into durable area documents as a substitute for
slice evidence.

Always include top-level `routes`; use an empty array when there are no consult
or correction routes. Use first-class `routes` for configured consult or correction routes. A QA
`changes_requested` correction to Engineering must use the configured consult
target role, target lifecycle state, and message type, and include the defect,
required change, and evidence fields when available. Keep `handoffs` for
forward lifecycle handoffs and legacy compatibility.

Use the document library as the canonical project memory. Use role memory only
as a concise, source-linked accelerator, and include provenance links when you
update it. For plan or document review gates, write visible `## Review Log`
entries with stable review ids, concrete required changes, dispositions, and
linked sub-slices where needed. Resolve disagreement through written review
loops first; request mediation only after the configured resolution loop limit.
""".strip()


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
                returncode=process.returncode or -9,
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


def parse_agent_result(raw_result: str) -> dict[str, Any]:
    text = raw_result.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"result is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("result must be a JSON object")
    return payload


def result_from_payload(payload: dict[str, Any], flow_state: FlowState) -> AgentRunResult:
    status = payload.get("status")
    if status not in {"completed", "blocked", "needs_clarification", "failed"}:
        raise ValueError(f"unsupported status `{status}`")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")

    updates: list[DocumentUpdate] = []
    for item in payload.get("document_updates", []):
        if not isinstance(item, dict):
            raise ValueError("document update must be an object")
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("document update path must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("document update content must be a non-empty string")
        purpose = item.get("purpose")
        review_status = item.get("review_status")
        index_summary = item.get("index_summary")
        maintain_work_item_index = item.get("maintain_work_item_index")
        for name, value in [
            ("purpose", purpose),
            ("review_status", review_status),
            ("index_summary", index_summary),
        ]:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"document update {name} must be a string or null")
        if maintain_work_item_index is not None and not isinstance(
            maintain_work_item_index,
            bool,
        ):
            raise ValueError(
                "document update maintain_work_item_index must be a boolean or null"
            )
        updates.append(
            DocumentUpdate(
                path=path,
                content=content,
                purpose=purpose,
                review_status=review_status,
                index_summary=index_summary,
                maintain_work_item_index=maintain_work_item_index,
            )
        )

    if (
        not updates
        and status == "completed"
        and not payload.get("handoffs")
        and not payload.get("routes")
    ):
        raise ValueError("completed results must include a document update, route, or handoff")

    routes: list[RouteRequest] = []
    for item in payload.get("routes", []):
        if not isinstance(item, dict):
            raise ValueError("route must be an object")
        target_role = item.get("target_role")
        message_type = item.get("message_type")
        route_payload = item.get("payload")
        if not isinstance(target_role, str) or not target_role.strip():
            raise ValueError("route target_role must be a non-empty string")
        if not isinstance(message_type, str) or not message_type.strip():
            raise ValueError("route message_type must be a non-empty string")
        if not isinstance(route_payload, dict):
            raise ValueError("route payload must be an object")
        routes.append(
            RouteRequest(
                target_role=target_role,
                message_type=message_type,
                payload=route_payload,
            )
        )

    handoffs: list[Handoff] = []
    for item in payload.get("handoffs", []):
        if not isinstance(item, dict):
            raise ValueError("handoff must be an object")
        target_role = item.get("target_role")
        message_type = item.get("message_type")
        handoff_payload = item.get("payload")
        if not isinstance(target_role, str) or not target_role.strip():
            raise ValueError("handoff target_role must be a non-empty string")
        if not isinstance(message_type, str) or not message_type.strip():
            raise ValueError("handoff message_type must be a non-empty string")
        if not isinstance(handoff_payload, dict):
            raise ValueError("handoff payload must be an object")
        handoffs.append(
            Handoff(
                target_role=target_role,
                message_type=message_type,
                payload=handoff_payload,
            )
        )

    return AgentRunResult(
        status=status,
        message=message,
        document_updates=updates,
        routes=routes,
        handoffs=handoffs,
    )
