from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any
from typing import TYPE_CHECKING

from agentic_mesh.agent_run_state import validate_logical_id
from agentic_mesh.models import Message
from agentic_mesh.models import RoleInstanceConfig
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso

if TYPE_CHECKING:
    from agentic_mesh.journal import EventJournal


WORKER_RUN_SCHEMA_VERSION = "worker-run-v0"
WORKER_RUN_OUTPUT_SCHEMA_VERSION = "worker-run-output-v0"

RUN_STATUSES = {
    "starting",
    "running",
    "completed",
    "failed",
    "timed_out",
    "canceled",
    "unknown",
}
RUN_CONDITIONS = {
    "active_output",
    "output_capped",
    "quiet_running",
    "stale_output",
    "completed_valid_result",
    "provider_limited",
    "provider_auth_failed",
    "timed_out",
    "invalid_result",
    "process_failed",
    "missing_executable",
    "unsupported_adapter",
    "runtime_publication_failed",
    "unknown_provider_failure",
}
OUTPUT_STREAMS = {"stdout", "stderr", "result_file", "artifact_event", "heartbeat"}
OUTPUT_RECORD_KINDS = {
    "output",
    "progress",
    "classifier_signal",
    "redaction_notice",
    "truncation_notice",
}
CLASSIFIER_HINTS = {
    "usage_limit",
    "credit_exhausted",
    "quota_exceeded",
    "rate_limited",
    "auth_failed",
    "timeout",
    "invalid_result",
}

PROVIDER_LIMIT_CLASSES = {
    "usage_limit",
    "credit_exhausted",
    "quota_exceeded",
    "rate_limited",
}
FAILURE_TO_CONDITION = {
    "usage_limit": "provider_limited",
    "credit_exhausted": "provider_limited",
    "quota_exceeded": "provider_limited",
    "rate_limited": "provider_limited",
    "auth_failed": "provider_auth_failed",
    "auth_missing": "provider_auth_failed",
    "timeout": "timed_out",
    "process_failed": "process_failed",
    "invalid_result": "invalid_result",
    "schema_failed": "invalid_result",
    "missing_executable": "missing_executable",
    "unsupported_adapter": "unsupported_adapter",
    "publication_failed": "runtime_publication_failed",
    "unknown_provider_failure": "unknown_provider_failure",
}

SAFE_SUMMARY_KEYS = {
    "schema_version",
    "run_id",
    "project_id",
    "role_id",
    "role_instance_id",
    "message_id",
    "work_item_id",
    "work_item_type",
    "queue_item_id",
    "source_message_id",
    "source_anchor_ref",
    "correlation_id",
    "lifecycle_state",
    "worker_adapter",
    "worker_model",
    "run_status",
    "run_condition",
    "condition_label",
    "started_at",
    "last_heartbeat_at",
    "last_progress_at",
    "last_output_at",
    "completed_at",
    "elapsed_seconds",
    "soft_warning_seconds",
    "stale_output_seconds",
    "hard_timeout_seconds",
    "max_timeout_seconds",
    "timeout_policy_source",
    "output_record_count",
    "output_truncated",
    "output_record_cap",
    "last_safe_output_summary",
    "failure_class",
    "provider_recovery_class",
    "retry_after_seconds",
    "retryable",
    "recovery_action",
    "next_action",
    "action_owner",
    "problem_status_ref",
    "status_url",
    "read_only",
}

FORBIDDEN_KEYS = {
    "tenant_id",
    "team_id",
    "channel_id",
    "activity_id",
    "conversation_id",
    "service_url",
    "bot_id",
    "secret_ref",
    "mount_ref",
    "credential_ref",
    "stdout",
    "stderr",
    "command",
    "command_line",
    "prompt",
    "model_response",
    "provider_error_body",
    "raw_output",
}

CONDITION_LABELS = {
    "active_output": "Active output",
    "output_capped": "Output capped",
    "quiet_running": "Quiet running",
    "stale_output": "Stale output",
    "completed_valid_result": "Completed with valid result",
    "provider_limited": "Provider limited",
    "provider_auth_failed": "Provider auth failed",
    "timed_out": "Timed out",
    "invalid_result": "Invalid result",
    "process_failed": "Process failed",
    "missing_executable": "Missing executable",
    "unsupported_adapter": "Unsupported adapter",
    "runtime_publication_failed": "Runtime publication failed",
    "unknown_provider_failure": "Unknown provider failure",
}


@dataclass(frozen=True)
class WorkerRunTimeoutPolicy:
    soft_warning_seconds: int
    stale_output_seconds: int
    hard_timeout_seconds: int
    max_timeout_seconds: int | None
    timeout_policy_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "soft_warning_seconds": self.soft_warning_seconds,
            "stale_output_seconds": self.stale_output_seconds,
            "hard_timeout_seconds": self.hard_timeout_seconds,
            "max_timeout_seconds": self.max_timeout_seconds,
            "timeout_policy_source": self.timeout_policy_source,
        }


@dataclass(frozen=True)
class WorkerRun:
    run_id: str
    project_id: str
    role_id: str
    role_instance_id: str
    message_id: str
    work_item_id: str | None
    work_item_type: str | None
    queue_item_id: str | None
    source_message_id: str | None
    source_anchor_ref: str | None
    correlation_id: str
    lifecycle_state: str | None
    worker_adapter: str
    worker_model: str | None
    run_status: str
    run_condition: str
    started_at: str
    last_heartbeat_at: str | None = None
    last_progress_at: str | None = None
    last_output_at: str | None = None
    completed_at: str | None = None
    elapsed_seconds: int | None = None
    soft_warning_seconds: int = 900
    stale_output_seconds: int = 300
    hard_timeout_seconds: int = 1800
    max_timeout_seconds: int | None = None
    timeout_policy_source: str = "role_default"
    output_record_count: int = 0
    output_truncated: bool = False
    output_record_cap: int = 200
    last_safe_output_summary: str | None = None
    failure_class: str | None = None
    provider_recovery_class: str | None = None
    retry_after_seconds: int | None = None
    retryable: bool | None = None
    recovery_action: str | None = None
    next_action: str | None = None
    action_owner: str | None = None
    problem_status_ref: str | None = None
    status_url: str | None = None
    schema_version: str = WORKER_RUN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in [
            "run_id",
            "project_id",
            "role_id",
            "role_instance_id",
            "message_id",
            "correlation_id",
        ]:
            validate_logical_id(str(getattr(self, field_name)), field_name=field_name)
        if self.run_status not in RUN_STATUSES:
            raise ValueError(f"unsupported worker run status `{self.run_status}`")
        if self.run_condition not in RUN_CONDITIONS:
            raise ValueError(f"unsupported worker run condition `{self.run_condition}`")
        for field_name in [
            "soft_warning_seconds",
            "stale_output_seconds",
            "hard_timeout_seconds",
            "output_record_cap",
        ]:
            if int(getattr(self, field_name)) < 1:
                raise ValueError(f"{field_name} must be positive")
        if self.schema_version != WORKER_RUN_SCHEMA_VERSION:
            raise ValueError("unsupported worker-run schema version")

    @staticmethod
    def from_claim(
        *,
        instance: RoleInstanceConfig,
        message: Message,
        lifecycle_state: str | None,
        timeout_policy: WorkerRunTimeoutPolicy,
        status_url: str | None = None,
        output_record_cap: int = 200,
        observed_at: str | None = None,
    ) -> "WorkerRun":
        now = observed_at or utc_now_iso()
        source_anchor = message.payload.get("source_anchor")
        return WorkerRun(
            run_id=new_id("run"),
            project_id=instance.project_id,
            role_id=instance.role_id,
            role_instance_id=instance.instance_id,
            message_id=message.message_id,
            work_item_id=message.payload.get("work_item_id"),
            work_item_type=message.payload.get("work_item_type"),
            queue_item_id=message.payload.get("queue_item_id"),
            source_message_id=message.payload.get("source_message_id")
            or message.message_id,
            source_anchor_ref=_source_anchor_ref(source_anchor),
            correlation_id=message.correlation_id,
            lifecycle_state=lifecycle_state,
            worker_adapter=instance.override.worker.adapter,
            worker_model=instance.override.worker.model,
            run_status="starting",
            run_condition="quiet_running",
            started_at=now,
            last_heartbeat_at=now,
            soft_warning_seconds=timeout_policy.soft_warning_seconds,
            stale_output_seconds=timeout_policy.stale_output_seconds,
            hard_timeout_seconds=timeout_policy.hard_timeout_seconds,
            max_timeout_seconds=timeout_policy.max_timeout_seconds,
            timeout_policy_source=timeout_policy.timeout_policy_source,
            output_record_cap=output_record_cap,
            status_url=status_url,
        )

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "WorkerRun":
        if data.get("schema_version") != WORKER_RUN_SCHEMA_VERSION:
            raise ValueError("unsupported worker-run schema version")
        return WorkerRun(
            run_id=str(data["run_id"]),
            project_id=str(data["project_id"]),
            role_id=str(data["role_id"]),
            role_instance_id=str(data["role_instance_id"]),
            message_id=str(data["message_id"]),
            work_item_id=data.get("work_item_id"),
            work_item_type=data.get("work_item_type"),
            queue_item_id=data.get("queue_item_id"),
            source_message_id=data.get("source_message_id"),
            source_anchor_ref=data.get("source_anchor_ref"),
            correlation_id=str(data["correlation_id"]),
            lifecycle_state=data.get("lifecycle_state"),
            worker_adapter=str(data["worker_adapter"]),
            worker_model=data.get("worker_model"),
            run_status=str(data["run_status"]),
            run_condition=str(data["run_condition"]),
            started_at=str(data["started_at"]),
            last_heartbeat_at=data.get("last_heartbeat_at"),
            last_progress_at=data.get("last_progress_at"),
            last_output_at=data.get("last_output_at"),
            completed_at=data.get("completed_at"),
            elapsed_seconds=data.get("elapsed_seconds"),
            soft_warning_seconds=int(data["soft_warning_seconds"]),
            stale_output_seconds=int(data["stale_output_seconds"]),
            hard_timeout_seconds=int(data["hard_timeout_seconds"]),
            max_timeout_seconds=data.get("max_timeout_seconds"),
            timeout_policy_source=str(data.get("timeout_policy_source") or "role_default"),
            output_record_count=int(data.get("output_record_count", 0)),
            output_truncated=bool(data.get("output_truncated", False)),
            output_record_cap=int(data.get("output_record_cap", 200)),
            last_safe_output_summary=data.get("last_safe_output_summary"),
            failure_class=data.get("failure_class"),
            provider_recovery_class=data.get("provider_recovery_class"),
            retry_after_seconds=data.get("retry_after_seconds"),
            retryable=data.get("retryable"),
            recovery_action=data.get("recovery_action"),
            next_action=data.get("next_action"),
            action_owner=data.get("action_owner"),
            problem_status_ref=data.get("problem_status_ref"),
            status_url=data.get("status_url"),
            schema_version=str(data["schema_version"]),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}

    def safe_summary(self, *, generated_at: str | None = None) -> dict[str, Any]:
        payload = self.to_dict()
        payload["condition_label"] = CONDITION_LABELS[self.run_condition]
        payload["read_only"] = True
        if self.completed_at is None:
            payload["elapsed_seconds"] = _elapsed_seconds(self.started_at, generated_at)
        summary = {key: payload.get(key) for key in SAFE_SUMMARY_KEYS if key in payload}
        assert_worker_run_payload_safe(summary)
        return summary

    def journal_fields(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "work_item_id": self.work_item_id,
            "work_item_type": self.work_item_type,
            "queue_item_id": self.queue_item_id,
            "lifecycle_state": self.lifecycle_state,
            "role_id": self.role_id,
            "role_instance_id": self.role_instance_id,
            "message_id": self.message_id,
            "worker_adapter": self.worker_adapter,
            "worker_model": self.worker_model,
            "run_status": self.run_status,
            "run_condition": self.run_condition,
            "failure_class": self.failure_class,
            "provider_recovery_class": self.provider_recovery_class,
            "retryable": self.retryable,
            "recovery_action": self.recovery_action,
            "correlation_id": self.correlation_id,
            "output_record_count": self.output_record_count,
            "output_truncated": self.output_truncated,
            "elapsed_seconds": self.elapsed_seconds,
            "soft_warning_seconds": self.soft_warning_seconds,
            "stale_output_seconds": self.stale_output_seconds,
            "hard_timeout_seconds": self.hard_timeout_seconds,
        }


@dataclass(frozen=True)
class WorkerRunOutputRecord:
    run_id: str
    sequence: int
    observed_at: str
    stream: str
    record_kind: str
    byte_count: int | None = None
    line_count: int | None = None
    safe_excerpt: str | None = None
    content_redacted: bool = True
    redaction_reason: str | None = None
    classifier_hints: tuple[str, ...] = ()
    schema_version: str = WORKER_RUN_OUTPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_logical_id(self.run_id, field_name="run_id")
        if self.sequence < 1:
            raise ValueError("output sequence must be positive")
        if self.stream not in OUTPUT_STREAMS:
            raise ValueError(f"unsupported worker-run output stream `{self.stream}`")
        if self.record_kind not in OUTPUT_RECORD_KINDS:
            raise ValueError(f"unsupported worker-run output kind `{self.record_kind}`")
        unsupported = set(self.classifier_hints) - CLASSIFIER_HINTS
        if unsupported:
            raise ValueError(f"unsupported classifier hints: {sorted(unsupported)}")
        if self.safe_excerpt is not None:
            assert_safe_text(self.safe_excerpt)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["classifier_hints"] = list(self.classifier_hints)
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(frozen=True)
class WorkerRunReadError:
    role_instance_id: str | None
    run_id: str | None
    error_class: str

    def safe_summary(self) -> dict[str, Any]:
        return {
            "schema_version": WORKER_RUN_SCHEMA_VERSION,
            "role_instance_id": self.role_instance_id,
            "run_id": self.run_id,
            "status": "unavailable",
            "summary": "WorkerRun evidence unavailable.",
            "error_class": self.error_class,
            "read_only": True,
        }


class FileWorkerRunStore:
    def __init__(
        self,
        state_root: Path,
        project_id: str,
        journal: "EventJournal | None" = None,
    ) -> None:
        self.project_id = validate_logical_id(project_id, field_name="project_id")
        self.journal = journal
        self.root = state_root / "projects" / self.project_id / "worker_runs"
        self.current_root = _contained_dir(self.root, "current")
        self.runs_root = _contained_dir(self.root, "runs")

    def start_run(self, run: WorkerRun) -> WorkerRun:
        self.update_run(run)
        self.emit_run_event("worker_run_started", run)
        return run

    def update_run(self, run: WorkerRun) -> WorkerRun:
        _atomic_write_json(self._run_path(run.role_instance_id, run.run_id), run.to_dict())
        _atomic_write_json(self._current_path(run.role_instance_id), run.to_dict())
        return run

    def complete_run(self, run: WorkerRun) -> WorkerRun:
        self.update_run(run)
        self.clear_current(run.role_instance_id, expected_run_id=run.run_id)
        if run.run_status == "timed_out":
            self.emit_run_event("worker_run_timed_out", run)
        elif run.run_status == "completed":
            self.emit_run_event("worker_run_completed", run)
        elif run.run_status == "failed":
            self.emit_run_event("worker_run_failed", run)
        if run.provider_recovery_class:
            self.emit_run_event("worker_run_provider_recovery_classified", run)
        return run

    def clear_current(
        self,
        role_instance_id: str,
        *,
        expected_run_id: str | None = None,
    ) -> bool:
        path = self._current_path(role_instance_id)
        if not path.exists():
            return False
        if expected_run_id is not None:
            current = self.read_current(role_instance_id)
            if current is None or current.run_id != expected_run_id:
                return False
        path.unlink()
        _fsync_directory(path.parent)
        return True

    def append_output(
        self,
        *,
        role_instance_id: str,
        record: WorkerRunOutputRecord,
    ) -> WorkerRunOutputRecord:
        path = self._output_path(role_instance_id, record.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise ValueError("worker-run store refuses symlink parent directories")
        with path.open("a", encoding="utf-8") as handle:
            json.dump(record.to_dict(), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
        return record

    def emit_run_event(
        self,
        event_type: str,
        run: WorkerRun,
        **fields: Any,
    ) -> dict[str, Any] | None:
        if self.journal is None:
            return None
        event_fields = {
            **run.journal_fields(),
            **_safe_worker_run_event_fields(fields),
        }
        assert_worker_run_payload_safe(event_fields)
        return self.journal.append(event_type, **event_fields)

    def emit_output_event(
        self,
        event_type: str,
        run: WorkerRun,
        record: WorkerRunOutputRecord,
    ) -> dict[str, Any] | None:
        return self.emit_run_event(
            event_type,
            run,
            output_sequence=record.sequence,
            output_stream=record.stream,
            output_record_kind=record.record_kind,
            output_byte_count=record.byte_count,
            output_line_count=record.line_count,
            output_content_redacted=record.content_redacted,
            output_redaction_reason=record.redaction_reason,
            output_classifier_hints=list(record.classifier_hints),
            safe_excerpt_present=record.safe_excerpt is not None,
        )

    def read_current(self, role_instance_id: str) -> WorkerRun | None:
        result = self.read_current_with_error(role_instance_id)
        if isinstance(result, WorkerRunReadError):
            return None
        return result

    def read_current_with_error(
        self,
        role_instance_id: str,
    ) -> WorkerRun | WorkerRunReadError | None:
        try:
            path = self._current_path(role_instance_id)
            if not path.exists():
                return None
            return WorkerRun.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            return WorkerRunReadError(
                role_instance_id=str(role_instance_id),
                run_id=None,
                error_class=exc.__class__.__name__,
            )

    def list_current(self) -> list[WorkerRun | WorkerRunReadError]:
        rows: list[WorkerRun | WorkerRunReadError] = []
        for path in sorted(self.current_root.glob("*.json")):
            try:
                rows.append(WorkerRun.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except Exception as exc:
                rows.append(
                    WorkerRunReadError(
                        role_instance_id=path.stem,
                        run_id=None,
                        error_class=exc.__class__.__name__,
                    )
                )
        return rows

    def list_recent(
        self,
        *,
        role_instance_id: str | None = None,
        work_item_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkerRun | WorkerRunReadError]:
        rows: list[WorkerRun | WorkerRunReadError] = []
        run_paths = sorted(self.runs_root.glob("*/*/run.json"))
        for path in run_paths:
            if role_instance_id and path.parents[1].name != role_instance_id:
                continue
            try:
                run = WorkerRun.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:
                rows.append(
                    WorkerRunReadError(
                        role_instance_id=path.parents[1].name,
                        run_id=path.parent.name,
                        error_class=exc.__class__.__name__,
                    )
                )
                continue
            if work_item_id and run.work_item_id != work_item_id:
                continue
            rows.append(run)
        rows.sort(
            key=lambda item: (
                getattr(item, "completed_at", None)
                or getattr(item, "started_at", "")
                or ""
            ),
            reverse=True,
        )
        return rows[:limit] if limit else rows

    def list_output_summary(self, *, role_instance_id: str, run_id: str) -> dict[str, Any]:
        path = self._output_path(role_instance_id, run_id)
        if not path.exists():
            return {"run_id": run_id, "record_count": 0, "records": [], "read_only": True}
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    records.append({"record_unavailable": True})
                    continue
                safe = {
                    key: record.get(key)
                    for key in [
                        "schema_version",
                        "run_id",
                        "sequence",
                        "observed_at",
                        "stream",
                        "record_kind",
                        "byte_count",
                        "line_count",
                        "safe_excerpt",
                        "content_redacted",
                        "redaction_reason",
                        "classifier_hints",
                    ]
                    if key in record
                }
                assert_worker_run_payload_safe(safe)
                records.append(safe)
        return {
            "run_id": run_id,
            "record_count": len(records),
            "records": records,
            "read_only": True,
        }

    def prune(self, role_instance_id: str, *, keep_terminal: int = 25) -> dict[str, Any]:
        safe_instance = validate_logical_id(role_instance_id, field_name="role_instance_id")
        current = self.read_current(safe_instance)
        runs = [
            run
            for run in self.list_recent(role_instance_id=safe_instance)
            if isinstance(run, WorkerRun)
        ]
        terminal = [run for run in runs if run.run_status in {"completed", "failed", "timed_out", "canceled"}]
        terminal.sort(key=lambda run: run.completed_at or run.started_at, reverse=True)
        keep_ids = {run.run_id for run in terminal[:keep_terminal]}
        if current is not None:
            keep_ids.add(current.run_id)
        pruned = 0
        for run in terminal[keep_terminal:]:
            if run.run_id in keep_ids:
                continue
            run_dir = self._run_dir(safe_instance, run.run_id)
            _remove_tree(run_dir)
            pruned += 1
        if pruned:
            self.emit_run_event(
                "worker_run_pruned",
                terminal[0],
                records_pruned=pruned,
                terminal_records_kept=keep_terminal,
            )
        return {
            "schema_version": WORKER_RUN_SCHEMA_VERSION,
            "role_instance_id": safe_instance,
            "records_pruned": pruned,
            "terminal_records_kept": keep_terminal,
            "read_only": True,
        }

    def _current_path(self, role_instance_id: str) -> Path:
        safe_id = validate_logical_id(role_instance_id, field_name="role_instance_id")
        path = (self.current_root / f"{safe_id}.json").resolve()
        _assert_contained(self.current_root, path)
        if path.exists() and path.is_symlink():
            raise ValueError("worker-run store refuses symlink targets")
        return path

    def _run_dir(self, role_instance_id: str, run_id: str) -> Path:
        safe_instance = validate_logical_id(role_instance_id, field_name="role_instance_id")
        safe_run = validate_logical_id(run_id, field_name="run_id")
        instance_dir = _contained_dir(self.runs_root, safe_instance)
        run_dir = _contained_dir(instance_dir, safe_run)
        _assert_contained(self.runs_root, run_dir)
        return run_dir

    def _run_path(self, role_instance_id: str, run_id: str) -> Path:
        return self._run_dir(role_instance_id, run_id) / "run.json"

    def _output_path(self, role_instance_id: str, run_id: str) -> Path:
        return self._run_dir(role_instance_id, run_id) / "output.jsonl"


class WorkerRunObserver:
    def __init__(self, *, store: FileWorkerRunStore, run: WorkerRun) -> None:
        self.store = store
        self.run = run
        self._truncation_written = False

    def mark_running(self) -> WorkerRun:
        now = utc_now_iso()
        self.run = replace(
            self.run,
            run_status="running",
            run_condition="quiet_running",
            last_heartbeat_at=now,
        )
        self.store.update_run(self.run)
        return self.run

    def observe_output(self, stream: str, text: str) -> WorkerRun:
        if not text:
            return self.run
        observed_at = utc_now_iso()
        hints = classify_output_hints(text)
        excerpt, redacted, reason = redact_output_excerpt(text)
        record = WorkerRunOutputRecord(
            run_id=self.run.run_id,
            sequence=self.run.output_record_count + 1,
            observed_at=observed_at,
            stream=stream,
            record_kind="output",
            byte_count=len(text.encode("utf-8", errors="replace")),
            line_count=max(1, len(text.splitlines())),
            safe_excerpt=excerpt,
            content_redacted=redacted,
            redaction_reason=reason,
            classifier_hints=tuple(sorted(hints)),
        )
        self._append_record(
            record,
            condition="active_output",
            observed_at=observed_at,
            last_safe_output_summary=excerpt,
        )
        self.store.emit_output_event("worker_run_output_recorded", self.run, record)
        return self.run

    def observe_progress(self, stream: str, *, record_kind: str = "progress") -> WorkerRun:
        observed_at = utc_now_iso()
        record = WorkerRunOutputRecord(
            run_id=self.run.run_id,
            sequence=self.run.output_record_count + 1,
            observed_at=observed_at,
            stream=stream,
            record_kind=record_kind,
            content_redacted=False,
        )
        self._append_record(
            record,
            condition=self.run.run_condition,
            observed_at=observed_at,
        )
        self.store.emit_output_event("worker_run_progress_observed", self.run, record)
        return self.run

    def mark_stale_if_needed(self) -> WorkerRun:
        last = self.run.last_progress_at or self.run.last_heartbeat_at or self.run.started_at
        age = _elapsed_seconds(last, utc_now_iso()) or 0
        if age < self.run.stale_output_seconds:
            return self.run
        self.run = replace(
            self.run,
            run_condition="stale_output",
            next_action="Operator should inspect worker progress before retry or recovery.",
            action_owner="runtime/operator",
        )
        self.store.update_run(self.run)
        self.store.emit_run_event("worker_run_stale_output_detected", self.run)
        return self.run

    def mark_terminal(
        self,
        *,
        run_status: str,
        run_condition: str,
        failure_class: str | None = None,
        provider_recovery_class: str | None = None,
        retryable: bool | None = None,
        recovery_action: str | None = None,
        next_action: str | None = None,
        action_owner: str | None = None,
        problem_status_ref: str | None = None,
    ) -> WorkerRun:
        completed_at = utc_now_iso()
        self.run = replace(
            self.run,
            run_status=run_status,
            run_condition=run_condition,
            completed_at=completed_at,
            elapsed_seconds=_elapsed_seconds(self.run.started_at, completed_at),
            failure_class=failure_class,
            provider_recovery_class=provider_recovery_class,
            retryable=retryable,
            recovery_action=recovery_action,
            next_action=next_action,
            action_owner=action_owner,
            problem_status_ref=problem_status_ref,
        )
        self.store.complete_run(self.run)
        self.store.prune(self.run.role_instance_id)
        return self.run

    def _append_record(
        self,
        record: WorkerRunOutputRecord,
        *,
        condition: str,
        observed_at: str,
        last_safe_output_summary: str | None = None,
    ) -> WorkerRun:
        if self.run.output_record_count >= self.run.output_record_cap:
            wrote_truncation = False
            if not self._truncation_written and not self.run.output_truncated:
                truncation = WorkerRunOutputRecord(
                    run_id=self.run.run_id,
                    sequence=self.run.output_record_count + 1,
                    observed_at=observed_at,
                    stream=record.stream,
                    record_kind="truncation_notice",
                    content_redacted=True,
                    redaction_reason="output_record_cap_reached",
                )
                self.store.append_output(
                    role_instance_id=self.run.role_instance_id,
                    record=truncation,
                )
                self.store.emit_output_event(
                    "worker_run_output_truncated",
                    self.run,
                    truncation,
                )
                self._truncation_written = True
                wrote_truncation = True
            self.run = replace(
                self.run,
                run_condition="output_capped" if record.record_kind == "output" else self.run.run_condition,
                output_record_count=self.run.output_record_count + (1 if wrote_truncation else 0),
                output_truncated=True,
                last_heartbeat_at=observed_at,
                last_progress_at=observed_at,
                last_output_at=self.run.last_output_at,
                next_action="Worker is still producing output, but the captured output cap has been reached.",
                action_owner="runtime/operator",
            )
            self.store.update_run(self.run)
            return self.run

        self.store.append_output(role_instance_id=self.run.role_instance_id, record=record)
        self.run = replace(
            self.run,
            run_status="running" if self.run.run_status == "starting" else self.run.run_status,
            run_condition=condition,
            output_record_count=self.run.output_record_count + 1,
            last_heartbeat_at=observed_at,
            last_progress_at=observed_at,
            last_output_at=observed_at if record.record_kind == "output" else self.run.last_output_at,
            last_safe_output_summary=last_safe_output_summary
            or self.run.last_safe_output_summary,
        )
        self.store.update_run(self.run)
        return self.run


def resolve_worker_run_timeout_policy(instance: RoleInstanceConfig) -> WorkerRunTimeoutPolicy:
    worker = instance.override.worker
    if instance.role_id == "engineering":
        soft, stale, hard = 1800, 600, 3600
    else:
        soft, stale, hard = 900, 300, 1800
    source = "role_default"
    if worker.timeout_seconds is not None:
        hard = worker.timeout_seconds
        source = "role_override"
    if worker.progress_window_seconds is not None:
        stale = worker.progress_window_seconds
        source = "role_override"
    max_timeout = worker.max_timeout_seconds

    env_timeout = os.environ.get("AGENTIC_MESH_WORKER_TIMEOUT_SECONDS")
    env_progress = os.environ.get("AGENTIC_MESH_WORKER_PROGRESS_WINDOW_SECONDS")
    if env_timeout:
        hard = int(env_timeout)
        source = "operator_env_override"
    if env_progress:
        stale = int(env_progress)
        source = "operator_env_override"
    if max_timeout is not None:
        hard = min(hard, max_timeout)
    if soft < 60 or stale < 60 or hard < 60:
        raise ValueError("worker timeout policy values must be at least 60 seconds")
    return WorkerRunTimeoutPolicy(
        soft_warning_seconds=soft,
        stale_output_seconds=stale,
        hard_timeout_seconds=hard,
        max_timeout_seconds=max_timeout,
        timeout_policy_source=source,
    )


def worker_run_condition_for_failure(failure_class: str | None) -> str:
    return FAILURE_TO_CONDITION.get(str(failure_class or ""), "unknown_provider_failure")


def worker_run_status_for_failure(failure_class: str | None) -> str:
    return "timed_out" if failure_class == "timeout" else "failed"


def provider_recovery_class_for_failure(failure_class: str | None) -> str | None:
    if failure_class in PROVIDER_LIMIT_CLASSES:
        return failure_class
    if failure_class in {"auth_failed", "auth_missing"}:
        return "auth_failed"
    if failure_class == "schema_failed":
        return "invalid_result"
    if failure_class in FAILURE_TO_CONDITION:
        return failure_class
    return "unknown_provider_failure" if failure_class else None


def classify_output_hints(text: str) -> set[str]:
    lowered = text.lower()
    hints: set[str] = set()
    if "usage limit" in lowered or "usage_limit" in lowered:
        hints.add("usage_limit")
    if "credit" in lowered and ("exhaust" in lowered or "insufficient" in lowered):
        hints.add("credit_exhausted")
    if "quota" in lowered:
        hints.add("quota_exceeded")
    if "rate limit" in lowered or "rate_limit" in lowered or "429" in lowered:
        hints.add("rate_limited")
    if "auth" in lowered or "unauthorized" in lowered or "401" in lowered:
        hints.add("auth_failed")
    if "timeout" in lowered or "timed out" in lowered:
        hints.add("timeout")
    if "invalid json" in lowered or "schema" in lowered:
        hints.add("invalid_result")
    return hints


def redact_output_excerpt(text: str) -> tuple[str | None, bool, str | None]:
    cleaned = " ".join(str(text).strip().split())
    if not cleaned:
        return None, True, "empty_output"
    if _contains_forbidden_value(cleaned):
        return None, True, "forbidden_value"
    excerpt = cleaned[:160]
    assert_safe_text(excerpt)
    return excerpt, False, None


def assert_worker_run_payload_safe(payload: Any) -> None:
    def walk(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            forbidden = set(value) & FORBIDDEN_KEYS
            if forbidden:
                raise ValueError(f"worker-run payload contains forbidden keys: {sorted(forbidden)}")
            for child in value.values():
                walk(child, path)
        elif isinstance(value, list):
            for child in value:
                walk(child, path)
        elif isinstance(value, str):
            assert_safe_text(value)

    walk(payload)


def _safe_worker_run_event_fields(fields: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "output_sequence",
        "output_stream",
        "output_record_kind",
        "output_byte_count",
        "output_line_count",
        "output_content_redacted",
        "output_redaction_reason",
        "output_classifier_hints",
        "safe_excerpt_present",
        "records_pruned",
        "terminal_records_kept",
    }
    safe = {key: value for key, value in fields.items() if key in allowed and value is not None}
    assert_worker_run_payload_safe(safe)
    return safe


def assert_safe_text(value: str) -> None:
    if _contains_forbidden_value(value):
        raise ValueError("unsafe worker-run text")


def _contains_forbidden_value(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "secret_ref",
        "mount_ref",
        "credential_ref",
        "bearer ",
        "password",
        "private key",
        "oauth",
        "service_url",
        "graph.microsoft.com",
        "model response",
        "provider error body",
        "container_id",
        "process_id",
        "hostname",
        "command:",
        "prompt:",
    ]
    if any(marker in lowered for marker in markers):
        return True
    if re.search(r"(?i)\b(secret|token|api[_-]?key)\s*[:=]\s*\S+", text):
        return True
    if re.search(r"https?://\S+", text):
        return True
    if re.search(r"[A-Za-z]:\\", text):
        return True
    if re.search(r"/(?:[^\s`]+/){2,}[^\s`]+", text):
        return True
    return False


def _source_anchor_ref(source_anchor: Any) -> str | None:
    if not isinstance(source_anchor, dict):
        return None
    value = source_anchor.get("source_anchor_ref")
    if value is None:
        return None
    text = str(value)
    if "/" in text or "\\" in text or ".." in text:
        return None
    return text[:128]


def _elapsed_seconds(started_at: str | None, generated_at: str | None) -> int | None:
    if not started_at:
        return None
    try:
        from datetime import datetime

        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(generated_at or utc_now_iso())
        return max(0, int((end - start).total_seconds()))
    except Exception:
        return None


def _contained_dir(root: Path, dirname: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("worker-run store refuses symlink roots")
    root_resolved = root.resolve()
    path = (root / dirname).resolve()
    if path != root_resolved and root_resolved not in path.parents:
        raise ValueError("worker-run path escapes configured root")
    if path.exists() and path.is_symlink():
        raise ValueError("worker-run store refuses symlink directories")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _assert_contained(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    path_resolved = path.resolve() if path.exists() else path.parent.resolve() / path.name
    if path_resolved != root_resolved and root_resolved not in path_resolved.parents:
        raise ValueError("worker-run path escapes configured root")


def _atomic_write_json(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("worker-run store refuses symlink parent directories")
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


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        raise ValueError("worker-run store refuses symlink removal")
    for child in sorted(path.rglob("*"), key=lambda candidate: len(candidate.parts), reverse=True):
        if child.is_symlink():
            raise ValueError("worker-run store refuses symlink removal")
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()
