from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_mesh.models import Message
from agentic_mesh.models import RoleInstanceConfig
from agentic_mesh.models import utc_now_iso


RUN_STATE_SCHEMA_VERSION = "agent-run-state-v0"
RUN_STATES = {"starting", "running", "completed", "failed", "timed_out", "unknown"}
SAFE_EVIDENCE_SOURCES = {
    "agent_run_started",
    "worker_run_started",
    "worker_progress",
    "worker_completed",
    "problem_status",
    "journal_event",
}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,160}$")


@dataclass(frozen=True)
class AgentRunState:
    project_id: str
    role_id: str
    role_instance_id: str
    message_id: str
    work_item_id: str | None
    work_item_type: str | None
    queue_item_id: str | None
    lifecycle_state: str | None
    correlation_id: str
    worker_adapter: str | None
    worker_model: str | None
    run_state: str
    started_at: str
    last_heartbeat_at: str | None = None
    last_progress_at: str | None = None
    completed_at: str | None = None
    timeout_seconds: int | None = None
    progress_window_seconds: int | None = None
    safe_progress_event_count: int = 0
    last_safe_evidence_source: str = "journal_event"
    schema_version: str = RUN_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in [
            "project_id",
            "role_id",
            "role_instance_id",
            "message_id",
            "correlation_id",
        ]:
            validate_logical_id(str(getattr(self, field_name)), field_name=field_name)
        if self.run_state not in RUN_STATES:
            raise ValueError(f"unsupported run_state `{self.run_state}`")
        if self.last_safe_evidence_source not in SAFE_EVIDENCE_SOURCES:
            raise ValueError(
                f"unsupported evidence source `{self.last_safe_evidence_source}`"
            )

    @staticmethod
    def from_claim(
        *,
        instance: RoleInstanceConfig,
        message: Message,
        run_state: str,
        timeout_seconds: int | None = None,
        progress_window_seconds: int | None = None,
        evidence_source: str,
        observed_at: str | None = None,
    ) -> "AgentRunState":
        now = observed_at or utc_now_iso()
        return AgentRunState(
            project_id=instance.project_id,
            role_id=instance.role_id,
            role_instance_id=instance.instance_id,
            message_id=message.message_id,
            work_item_id=message.payload.get("work_item_id"),
            work_item_type=message.payload.get("work_item_type"),
            queue_item_id=message.payload.get("queue_item_id"),
            lifecycle_state=message.payload.get("lifecycle_state"),
            correlation_id=message.correlation_id,
            worker_adapter=instance.override.worker.adapter,
            worker_model=instance.override.worker.model,
            run_state=run_state,
            started_at=now,
            last_heartbeat_at=now if run_state in {"starting", "running"} else None,
            last_progress_at=now if run_state == "running" else None,
            completed_at=now if run_state in {"completed", "failed", "timed_out"} else None,
            timeout_seconds=timeout_seconds,
            progress_window_seconds=progress_window_seconds,
            safe_progress_event_count=1 if run_state == "running" else 0,
            last_safe_evidence_source=evidence_source,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AgentRunState":
        if data.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported run-state schema version")
        return AgentRunState(
            project_id=str(data["project_id"]),
            role_id=str(data["role_id"]),
            role_instance_id=str(data["role_instance_id"]),
            message_id=str(data["message_id"]),
            work_item_id=data.get("work_item_id"),
            work_item_type=data.get("work_item_type"),
            queue_item_id=data.get("queue_item_id"),
            lifecycle_state=data.get("lifecycle_state"),
            correlation_id=str(data["correlation_id"]),
            worker_adapter=data.get("worker_adapter"),
            worker_model=data.get("worker_model"),
            run_state=str(data["run_state"]),
            started_at=str(data["started_at"]),
            last_heartbeat_at=data.get("last_heartbeat_at"),
            last_progress_at=data.get("last_progress_at"),
            completed_at=data.get("completed_at"),
            timeout_seconds=data.get("timeout_seconds"),
            progress_window_seconds=data.get("progress_window_seconds"),
            safe_progress_event_count=int(data.get("safe_progress_event_count", 0)),
            last_safe_evidence_source=str(
                data.get("last_safe_evidence_source") or "journal_event"
            ),
        )


class AgentRunStateStore:
    def write_current(self, state: AgentRunState) -> None:
        raise NotImplementedError

    def read_current(self, role_instance_id: str) -> AgentRunState | None:
        raise NotImplementedError

    def clear_current(
        self,
        role_instance_id: str,
        *,
        expected_message_id: str | None = None,
    ) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class RunStateReadError:
    role_instance_id: str
    error_class: str


class FileAgentRunStateStore(AgentRunStateStore):
    def __init__(self, state_root: Path, project_id: str) -> None:
        self.root = state_root / "projects" / validate_logical_id(project_id) / "agent_run_state"
        self.root.mkdir(parents=True, exist_ok=True)

    def write_current(self, state: AgentRunState) -> None:
        path = self.current_path(state.role_instance_id)
        _atomic_write_json(path, state.to_dict(), mode=0o600)

    def read_current(self, role_instance_id: str) -> AgentRunState | None:
        result = self.read_current_with_error(role_instance_id)
        if isinstance(result, RunStateReadError):
            return None
        return result

    def read_current_with_error(
        self,
        role_instance_id: str,
    ) -> AgentRunState | RunStateReadError | None:
        try:
            path = self.current_path(role_instance_id)
            if not path.exists():
                return None
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return AgentRunState.from_dict(data)
        except Exception as exc:
            return RunStateReadError(
                role_instance_id=str(role_instance_id),
                error_class=exc.__class__.__name__,
            )

    def clear_current(
        self,
        role_instance_id: str,
        *,
        expected_message_id: str | None = None,
    ) -> bool:
        path = self.current_path(role_instance_id)
        if not path.exists():
            return False
        if expected_message_id is not None:
            current = self.read_current(role_instance_id)
            if current is None or current.message_id != expected_message_id:
                return False
        path.unlink()
        _fsync_directory(path.parent)
        return True

    def current_path(self, role_instance_id: str) -> Path:
        safe_id = validate_logical_id(role_instance_id, field_name="role_instance_id")
        instance_root = _contained_dir(self.root, safe_id)
        path = (instance_root / "current.json").resolve()
        if instance_root.resolve() not in path.parents:
            raise ValueError("run-state path escapes configured root")
        if path.exists() and path.is_symlink():
            raise ValueError("run-state store refuses symlink targets")
        return path


def validate_logical_id(value: str, *, field_name: str = "id") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field_name} must not be empty")
    if "/" in candidate or "\\" in candidate or ".." in candidate:
        raise ValueError(f"{field_name} must be a contained logical id")
    if "://" in candidate:
        raise ValueError(f"{field_name} must not be a URI")
    if not _SAFE_ID_RE.match(candidate):
        raise ValueError(f"{field_name} contains unsupported characters")
    return candidate


def _contained_dir(root: Path, dirname: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("run-state store refuses symlink roots")
    root_resolved = root.resolve()
    path = (root / dirname).resolve()
    if path != root_resolved and root_resolved not in path.parents:
        raise ValueError("run-state path escapes configured root")
    if path.exists() and path.is_symlink():
        raise ValueError("run-state store refuses symlink directories")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("run-state store refuses symlink parent directories")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, mode)
        except OSError:
            pass
        os.replace(temp_name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
