from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from agentic_mesh.document_library import document_library_context
from agentic_mesh.document_library import resolve_document_library_root
from agentic_mesh.models import FlowState
from agentic_mesh.models import Message
from agentic_mesh.models import ProjectConfig
from agentic_mesh.models import RoleInstanceConfig
from agentic_mesh.models import utc_now_iso


PROMPT_AUDIT_SCHEMA_VERSION = "prompt-audit-v0"


def write_work_item_prompt_audit(
    *,
    document_library_root: Path,
    instance: RoleInstanceConfig,
    message: Message,
    flow_state: FlowState,
    prompt: str,
    command: list[str],
    workspace_root: Path,
) -> dict[str, str]:
    work_item_id = str(message.payload.get("work_item_id") or "").strip()
    if work_item_id:
        root = (
            document_library_root
            / "work-items"
            / _safe_path_part(work_item_id)
            / "debug"
            / "prompts"
            / _safe_path_part(instance.instance_id)
        )
    else:
        root = (
            document_library_root
            / "debug"
            / "work-item-prompts"
            / _safe_path_part(instance.instance_id)
        )
    stem = f"{_timestamp_path_part()}-{_safe_path_part(message.message_id)}"
    prompt_path = root / f"{stem}.prompt.txt"
    metadata_path = root / f"{stem}.metadata.json"
    metadata = {
        "schema_version": PROMPT_AUDIT_SCHEMA_VERSION,
        "audit_kind": "work_item_prompt",
        "recorded_at": utc_now_iso(),
        "project_id": instance.project_id,
        "role_id": instance.role_id,
        "role_instance_id": instance.instance_id,
        "work_item_id": work_item_id or None,
        "work_item_type": message.payload.get("work_item_type"),
        "queue_item_id": message.payload.get("queue_item_id"),
        "message_id": message.message_id,
        "message_type": message.type,
        "message_source": message.source,
        "correlation_id": message.correlation_id,
        "lifecycle_state": flow_state.state_id,
        "worker_adapter": instance.override.worker.adapter,
        "worker_model": instance.override.worker.model,
        "reasoning_effort": instance.override.worker.reasoning_effort,
        "sandbox_mode": instance.override.worker.sandbox_mode,
        "workspace_root": str(workspace_root),
        "command": command,
        "prompt_path": _relative(document_library_root, prompt_path),
        "metadata_path": _relative(document_library_root, metadata_path),
        "prompt_capture": "exact_stdin_sent_to_worker_adapter",
    }
    _write_text_atomic(prompt_path, prompt)
    _write_json_atomic(metadata_path, metadata)
    return {
        "prompt_path": metadata["prompt_path"],
        "metadata_path": metadata["metadata_path"],
    }


def write_startup_prompt_audit(
    *,
    document_library_root: Path,
    project: ProjectConfig,
    instance: RoleInstanceConfig,
    workspace_root: Path,
    reason: str,
    argv: list[str] | None = None,
) -> dict[str, str]:
    prompt = render_startup_prompt(
        project=project,
        instance=instance,
        workspace_root=workspace_root,
        reason=reason,
        argv=argv,
    )
    root = (
        document_library_root
        / "debug"
        / "startup-prompts"
        / _safe_path_part(instance.instance_id)
    )
    stem = f"{_timestamp_path_part()}-startup"
    prompt_path = root / f"{stem}.prompt.txt"
    metadata_path = root / f"{stem}.metadata.json"
    metadata = {
        "schema_version": PROMPT_AUDIT_SCHEMA_VERSION,
        "audit_kind": "agent_startup_prompt",
        "recorded_at": utc_now_iso(),
        "project_id": instance.project_id,
        "role_id": instance.role_id,
        "role_instance_id": instance.instance_id,
        "worker_adapter": instance.override.worker.adapter,
        "worker_model": instance.override.worker.model,
        "reasoning_effort": instance.override.worker.reasoning_effort,
        "sandbox_mode": instance.override.worker.sandbox_mode,
        "workspace_root": str(workspace_root),
        "reason": reason,
        "argv": argv or [],
        "prompt_path": _relative(document_library_root, prompt_path),
        "metadata_path": _relative(document_library_root, metadata_path),
        "prompt_capture": "agent_loop_startup_context",
    }
    _write_text_atomic(prompt_path, prompt)
    _write_json_atomic(metadata_path, metadata)
    return {
        "prompt_path": metadata["prompt_path"],
        "metadata_path": metadata["metadata_path"],
    }


def render_startup_prompt(
    *,
    project: ProjectConfig,
    instance: RoleInstanceConfig,
    workspace_root: Path,
    reason: str,
    argv: list[str] | None = None,
) -> str:
    repositories = {
        repository_id: asdict(repository)
        for repository_id, repository in sorted(project.workspace.repositories.items())
    }
    worker = instance.override.worker
    startup_context = {
        "schema_version": PROMPT_AUDIT_SCHEMA_VERSION,
        "audit_kind": "agent_startup_prompt",
        "reason": reason,
        "argv": argv or [],
        "project": {
            "project_id": project.project_id,
            "name": project.name,
            "goal": asdict(project.goal),
            "workspace": {
                "current_working_directory": str(workspace_root),
                "workspace_root": project.workspace.root,
                "default_repository": project.workspace.default_repository,
                "repositories": repositories,
            },
            "document_library": document_library_context(workspace_root, project),
        },
        "role_instance": {
            "instance_id": instance.instance_id,
            "role_id": instance.role_id,
            "ordinal": instance.ordinal,
            "telemetry_service_name": instance.telemetry_service_name,
        },
        "worker": {
            "adapter": worker.adapter,
            "model": worker.model,
            "reasoning_effort": worker.reasoning_effort,
            "sandbox_mode": worker.sandbox_mode,
            "timeout_seconds": worker.timeout_seconds,
            "progress_window_seconds": worker.progress_window_seconds,
            "max_timeout_seconds": worker.max_timeout_seconds,
            "auth": asdict(worker.auth) if worker.auth is not None else None,
        },
        "role_template": {
            "purpose": instance.template.purpose,
            "standing_instructions": instance.template.standing_instructions,
            "default_tools": instance.template.default_tools,
            "documentation_obligations": instance.template.documentation_obligations,
            "handoff_targets": instance.template.handoff_targets,
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
        },
        "project_role_override": {
            "instructions": instance.override.instructions,
            "write_paths": instance.override.write_paths,
            "channels": instance.override.channels,
        },
        "startup_instruction": (
            "Start the agent loop for this concrete role instance. Acknowledge "
            "new work promptly, process only work routed to this role, preserve "
            "role identity across loop iterations, use the configured lifecycle "
            "flow for handoffs, and record precise blockers when work cannot "
            "continue."
        ),
    }
    return (
        "Agentic Mesh agent startup prompt/context\n\n"
        + json.dumps(startup_context, indent=2, sort_keys=True)
    )


def document_library_root_for(project: ProjectConfig, workspace_root: Path) -> Path:
    return resolve_document_library_root(workspace_root, project.document_library)


def _safe_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return safe.strip(".-") or "unknown"


def _timestamp_path_part() -> str:
    return utc_now_iso().replace(":", "").replace("+", "Z").replace(".", "-")


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(content)
        if not content.endswith("\n"):
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)
