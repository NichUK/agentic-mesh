from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from agentic_mesh.document_library import document_library_context
from agentic_mesh.models import (
    AgentRunResult,
    AuthMethod,
    DocumentUpdate,
    FlowState,
    Handoff,
    Message,
    ProjectConfig,
    RoleInstanceConfig,
)


AGENT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "message", "document_updates", "handoffs"],
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
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
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
                        "properties": {},
                    },
                },
            },
        },
    },
}


class WorkerAdapter:
    def run(
        self,
        instance: RoleInstanceConfig,
        message: Message,
        flow_state: FlowState,
    ) -> AgentRunResult:
        raise NotImplementedError


class ConfiguredWorkerAdapter(WorkerAdapter):
    """Dispatches role work to the adapter declared by project configuration."""

    def __init__(
        self,
        *,
        project: ProjectConfig,
        auth_methods: dict[str, AuthMethod],
        workspace_root: Path,
        state_root: Path,
    ) -> None:
        self.project = project
        self.auth_methods = auth_methods
        self.workspace_root = workspace_root
        self.state_root = state_root
        self.codex = CodexCliWorkerAdapter(
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
    ) -> AgentRunResult:
        adapter = instance.override.worker.adapter
        if adapter == "codex-cli":
            return self.codex.run(instance, message, flow_state)
        return blocked_result(
            flow_state,
            (
                f"Worker adapter `{adapter}` is configured for "
                f"`{instance.instance_id}`, but no real implementation is "
                "available in this runtime. Work was not completed."
            ),
        )


class CodexCliWorkerAdapter(WorkerAdapter):
    """Runs a real Codex CLI agent and converts its JSON result to runtime output."""

    def __init__(
        self,
        *,
        project: ProjectConfig,
        auth_methods: dict[str, AuthMethod],
        workspace_root: Path,
        state_root: Path,
    ) -> None:
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
    ) -> AgentRunResult:
        codex_bin = shutil.which("codex")
        if not codex_bin:
            return blocked_result(
                flow_state,
                "Codex CLI is not installed in this worker container.",
            )

        auth_env_or_error = self._auth_environment(instance, flow_state)
        if isinstance(auth_env_or_error, AgentRunResult):
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
            timeout = int(os.environ.get("AGENTIC_MESH_WORKER_TIMEOUT_SECONDS", "900"))
            prompt = self._prompt(instance, message, flow_state)
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    cwd=self.workspace_root,
                    env=env,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return blocked_result(
                    flow_state,
                    f"Codex CLI timed out after {timeout} seconds.",
                )

            if completed.returncode != 0:
                detail = "\n".join(
                    part.strip()
                    for part in [completed.stderr, completed.stdout]
                    if part and part.strip()
                )
                return blocked_result(
                    flow_state,
                    "Codex CLI failed before returning a valid agent result."
                    + (f"\n\n{summarize_worker_failure(detail)}" if detail else ""),
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
                return blocked_result(
                    flow_state,
                    f"Codex CLI returned an invalid agent result: {exc}",
                )

    def _auth_environment(
        self,
        instance: RoleInstanceConfig,
        flow_state: FlowState,
    ) -> dict[str, str] | AgentRunResult:
        binding = instance.override.worker.auth
        if binding is None:
            return {}

        method = self.auth_methods[binding.method]
        env: dict[str, str] = {}
        if method.requires_secret_ref:
            if not binding.secret_ref:
                return blocked_result(
                    flow_state,
                    f"Worker auth method `{method.method_id}` requires secret_ref.",
                )
            secret_path = self.secret_root / binding.secret_ref
            if not secret_path.exists():
                return blocked_result(
                    flow_state,
                    (
                        f"Worker secret `{binding.secret_ref}` is missing at "
                        f"`{secret_path}`."
                    ),
                )
            value = secret_path.read_text(encoding="utf-8").strip()
            for env_var in method.env_vars:
                env[env_var] = value

        if method.requires_mount_ref:
            if not binding.mount_ref:
                return blocked_result(
                    flow_state,
                    self._auth_setup_message(
                        binding,
                        method.method_id,
                        f"Worker auth method `{method.method_id}` requires mount_ref.",
                    ),
                )
            mount_path = self.state_root / "worker_mounts" / binding.mount_ref
            if not mount_path.exists():
                return blocked_result(
                    flow_state,
                    self._auth_setup_message(
                        binding,
                        method.method_id,
                        (
                            f"Worker auth mount `{binding.mount_ref}` is missing at "
                            f"`{mount_path}`."
                        ),
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
        if binding.credential_ref:
            setup_url = f"{setup_url}?credential={binding.credential_ref}"
        return (
            "Codex OAuth login is required before this agent can work. "
            f"Open {setup_url} and press `Sign in with OpenAI` for this credential."
        )

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
        return f"""
You are the `{instance.role_id}` role agent for Agentic Mesh project `{instance.project_id}`.

Role purpose:
{instance.template.purpose}

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

Configured role tools and skills:
{json.dumps(instance.template.default_tools, indent=2)}

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

Return only the final JSON object required by the provided schema. Put all
document changes in `document_updates`; do not rely on unreported filesystem
edits. For direct broadcast work, use the requested artifact path and do not
emit handoffs unless the prompt explicitly asks for one.

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
    content = (
        f"\n## Blocked: {flow_state.state_id}\n\n"
        f"- Status: `blocked`\n"
        f"- Reason: {reason}\n"
    )
    return AgentRunResult(
        status="blocked",
        message=reason,
        document_updates=[DocumentUpdate(path=flow_state.artifact_path, content=content)],
        handoffs=[],
    )


def summarize_worker_failure(detail: str, max_length: int = 2000) -> str:
    text = detail.strip()
    if len(text) <= max_length:
        return text
    return f"...\n{text[-max_length:].lstrip()}"


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
        updates.append(DocumentUpdate(path=path, content=content))

    if not updates and status == "completed" and not payload.get("handoffs"):
        raise ValueError("completed results must include a document update or handoff")

    if not updates and status != "completed":
        updates.append(
            DocumentUpdate(
                path=flow_state.artifact_path,
                content=f"\n## {status}: {flow_state.state_id}\n\n{message}\n",
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
        handoffs=handoffs,
    )
