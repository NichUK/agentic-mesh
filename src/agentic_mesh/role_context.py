from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agentic_mesh.journal import EventJournal
from agentic_mesh.models import Message
from agentic_mesh.models import ProjectConfig
from agentic_mesh.models import RoleInstanceConfig
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.threaded_context import ThreadedContext


MAX_MEMORY_CHARS = 6000
MAX_CONFIG_CHARS = 4000
MAX_CONTEXT_ROWS = 8


def build_role_context(
    *,
    project: ProjectConfig,
    instance: RoleInstanceConfig,
    message: Message,
    workspace_root: Path,
    state_root: Path,
) -> dict[str, Any]:
    memory_root = _resolve_project_path(workspace_root, project.role_memory.root)
    config_root = _resolve_project_path(workspace_root, project.role_memory.config_root)
    role_memory_dir = memory_root / instance.role_id
    role_config_dir = config_root / instance.role_id
    memory_path = role_memory_dir / project.role_memory.memory_filename

    return {
        "schema_version": "role-context-v0",
        "role_id": instance.role_id,
        "role_instance_id": instance.instance_id,
        "memory": {
            "enabled": project.role_memory.enabled,
            "scope": "shared_by_role_instances",
            "path": _display_path(memory_path, workspace_root),
            "exists": memory_path.exists(),
            "content": _read_text(memory_path, MAX_MEMORY_CHARS),
        },
        "role_config": {
            "root": _display_path(role_config_dir, workspace_root),
            "files": _role_config_files(role_config_dir, memory_path, workspace_root),
        },
        "current_conversation": _conversation_key(message),
        "recent_direct_conversation": _recent_direct_conversation(
            state_root=state_root,
            project_id=project.project_id,
            role_id=instance.role_id,
            message=message,
        ),
        "work_item_thread_context": _work_item_thread_context(
            state_root=state_root,
            project_id=project.project_id,
            work_item_id=_optional_string(message.payload.get("work_item_id")),
        ),
        "agent_expectation": (
            "Read memory and recent context before answering. If the current message "
            "is a short acknowledgement, correction, or continuation, infer its "
            "referent from recent conversation/work-item context before asking for "
            "identifiers. Update role memory through safe-output tools when a "
            "source-linked recurring decision, sponsor preference, risk, or handoff "
            "lesson should persist."
        ),
    }


def _resolve_project_path(workspace_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (workspace_root / path).resolve()


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace_root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _read_text(path: Path, max_chars: int) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if len(text) <= max_chars:
        return text.strip()
    return text[:max_chars].rstrip() + "\n\n[truncated]"


def _role_config_files(
    role_config_dir: Path,
    memory_path: Path,
    workspace_root: Path,
) -> list[dict[str, Any]]:
    if not role_config_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(role_config_dir.glob("*")):
        if not path.is_file() or path.resolve() == memory_path.resolve():
            continue
        if path.suffix.casefold() not in {".yaml", ".yml", ".md", ".json"}:
            continue
        rows.append(
            {
                "path": _display_path(path, workspace_root),
                "content": _read_text(path, MAX_CONFIG_CHARS),
            }
        )
    return rows


def _conversation_key(message: Message) -> dict[str, str | None]:
    payload = message.payload
    return {
        "source": message.source,
        "source_channel": _optional_string(payload.get("source_channel")),
        "source_connector": _optional_string(payload.get("source_connector")),
        "source_connector_id": _optional_string(payload.get("source_connector_id")),
        "teams_conversation_id": _optional_string(payload.get("teams_conversation_id")),
        "teams_reply_to_activity_id": _optional_string(
            payload.get("teams_reply_to_activity_id")
        ),
    }


def _recent_direct_conversation(
    *,
    state_root: Path,
    project_id: str,
    role_id: str,
    message: Message,
) -> list[dict[str, Any]]:
    current_key = _conversation_key(message)
    if not current_key["teams_conversation_id"] and not current_key["source_channel"]:
        return []
    store = FileMessageStore(state_root, project_id, EventJournal(state_root, project_id))
    root = store.root / role_id
    if not root.exists():
        return []

    rows: list[dict[str, Any]] = []
    for folder in ("completed", "claimed", "pending"):
        for path in sorted((root / folder).glob("**/*.json")):
            try:
                candidate = store._read_message(path)
            except Exception:
                continue
            if candidate.message_id == message.message_id:
                continue
            if candidate.type != "conversation.direct":
                continue
            if not _same_conversation(current_key, _conversation_key(candidate)):
                continue
            rows.append(_message_context_row(candidate))
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""))[-MAX_CONTEXT_ROWS:]


def _same_conversation(
    current: dict[str, str | None],
    candidate: dict[str, str | None],
) -> bool:
    if current.get("teams_conversation_id"):
        return current.get("teams_conversation_id") == candidate.get(
            "teams_conversation_id"
        )
    return (
        current.get("source_connector") == candidate.get("source_connector")
        and current.get("source_connector_id") == candidate.get("source_connector_id")
        and current.get("source_channel") == candidate.get("source_channel")
    )


def _message_context_row(message: Message) -> dict[str, Any]:
    payload = message.payload
    return {
        "message_id": message.message_id,
        "created_at": message.created_at,
        "status": "previous_message",
        "title": _optional_string(payload.get("title")),
        "summary": _optional_string(payload.get("summary")),
        "text": _optional_string(payload.get("text")),
        "work_item_id": _optional_string(payload.get("work_item_id")),
        "queue_item_id": _optional_string(payload.get("queue_item_id")),
        "lifecycle_state": _optional_string(payload.get("lifecycle_state")),
    }


def _work_item_thread_context(
    *,
    state_root: Path,
    project_id: str,
    work_item_id: str | None,
) -> list[dict[str, Any]]:
    if not work_item_id:
        return []
    context_root = (
        state_root
        / "projects"
        / project_id
        / "threaded_context"
        / "contexts"
        / work_item_id
    )
    if not context_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(context_root.glob("*.json")):
        try:
            context = ThreadedContext.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
        rows.append(_thread_context_row(context))
    return sorted(rows, key=lambda row: str(row.get("received_at") or ""))[
        -MAX_CONTEXT_ROWS:
    ]


def _thread_context_row(context: ThreadedContext) -> dict[str, Any]:
    row = asdict(context)
    return {
        key: row[key]
        for key in (
            "context_id",
            "received_at",
            "actor_label",
            "summary",
            "sanitized_text",
            "action_state",
            "attention_state",
            "context_kind",
            "lifecycle_state",
            "owner_role",
            "reason",
        )
    }


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
