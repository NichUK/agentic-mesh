from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_mesh.models import utc_now_iso


PROBLEM_STATUS_SCHEMA_VERSION = "problem-status-v0"
ROLE_RESULT_STATUSES = {"completed", "blocked", "needs_clarification", "failed"}
PROBLEM_STATUSES = {"blocked", "failed", "needs_runtime_recovery"}
PROBLEM_KINDS = {"role_blocker", "role_failure", "worker_failed", "runtime_failed"}
FAILURE_CLASSES = {
    "timeout",
    "usage_limit",
    "credit_exhausted",
    "quota_exceeded",
    "rate_limited",
    "auth_failed",
    "process_failed",
    "invalid_result",
    "missing_executable",
    "unsupported_adapter",
    "auth_missing",
    "publication_failed",
    "schema_failed",
    "malformed_route",
    "malformed_handoff",
    "source_changed_running_service_not_updated",
    "activation_blocked",
    "activation_smoke_failed",
    "activation_target_unavailable",
    "unknown_provider_failure",
}
RECOVERY_ACTIONS = {
    "retry_same_state",
    "retry_safe_output_contract",
    "repair_configuration",
    "repair_auth",
    "reconcile_partial_artifacts",
    "operator_review",
}

STATUS_LABELS = {
    "blocked": "Blocked",
    "failed": "Failed",
    "needs_runtime_recovery": "Needs runtime recovery",
}
PROBLEM_LABELS = {
    "role_blocker": "Role blocker",
    "role_failure": "Role failure",
    "worker_failed": "Worker failed",
    "runtime_failed": "Runtime failed",
}
RETRYABILITY_LABELS = {
    True: "Retryable after the listed action",
    False: "Not retryable without owner action",
}

FORBIDDEN_FIELD_NAMES = {
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
    "raw_payload_ref",
    "stdout",
    "stderr",
    "command",
    "error_message",
}


@dataclass(frozen=True)
class ArtifactVerificationRecord:
    path: str
    verification: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ProblemStatus:
    work_item_id: str
    work_item_type: str | None
    queue_item_id: str | None
    source_message_id: str | None
    source_anchor_ref: str | None
    source_anchor_summary: str | None
    correlation_id: str
    status: str
    problem_kind: str
    lifecycle_state: str
    affected_role: str
    role_instance_id: str | None
    reason: str
    next_action: str
    action_owner: str
    retryable: bool
    recovery_action: str | None = None
    failure_class: str | None = None
    worker_adapter: str | None = None
    worker_model: str | None = None
    timeout_seconds: int | None = None
    progress_observed_at: str | None = None
    partial_artifacts_present: bool = False
    artifact_paths: tuple[str, ...] = ()
    artifact_verification: tuple[ArtifactVerificationRecord, ...] = ()
    status_url: str | None = None
    attempted_target_role: str | None = None
    attempted_message_type: str | None = None
    attempted_lifecycle_state: str | None = None
    expected_owner: str | None = None
    matched_configured_route: str | None = None
    route_validation_errors: tuple[str, ...] = ()
    occurred_at: str = ""
    schema_version: str = PROBLEM_STATUS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in PROBLEM_STATUSES:
            raise ValueError(f"unsupported problem status `{self.status}`")
        if self.problem_kind not in PROBLEM_KINDS:
            raise ValueError(f"unsupported problem kind `{self.problem_kind}`")
        if self.problem_kind in {"worker_failed", "runtime_failed"}:
            if self.failure_class not in FAILURE_CLASSES:
                raise ValueError("worker/runtime problem status requires failure_class")
            if self.recovery_action not in RECOVERY_ACTIONS:
                raise ValueError("worker/runtime problem status requires recovery_action")
        if self.status == "needs_runtime_recovery" and self.problem_kind.startswith("role_"):
            raise ValueError("role-owned problems cannot use needs_runtime_recovery")
        if not self.occurred_at:
            object.__setattr__(self, "occurred_at", utc_now_iso())
        object.__setattr__(self, "reason", redact_text(self.reason))
        object.__setattr__(self, "next_action", redact_text(self.next_action))
        object.__setattr__(self, "source_anchor_summary", redact_optional_text(self.source_anchor_summary))
        for field_name in [
            "attempted_target_role",
            "attempted_message_type",
            "attempted_lifecycle_state",
            "expected_owner",
            "matched_configured_route",
        ]:
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, redact_text(str(value), max_length=160))
        object.__setattr__(
            self,
            "route_validation_errors",
            tuple(
                redact_text(error, max_length=240)
                for error in self.route_validation_errors
                if str(error).strip()
            ),
        )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def problem_label(self) -> str:
        label = PROBLEM_LABELS[self.problem_kind]
        if self.failure_class:
            return f"{label}: {self.failure_class}"
        return label

    @property
    def reason_summary(self) -> str:
        return truncate_text(self.reason, 240)

    @property
    def retryability_label(self) -> str:
        return RETRYABILITY_LABELS[self.retryable]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "work_item_id": self.work_item_id,
            "work_item_type": self.work_item_type,
            "queue_item_id": self.queue_item_id,
            "source_message_id": self.source_message_id,
            "source_anchor_ref": self.source_anchor_ref,
            "source_anchor_summary": self.source_anchor_summary,
            "correlation_id": self.correlation_id,
            "status": self.status,
            "status_label": self.status_label,
            "problem_kind": self.problem_kind,
            "problem_label": self.problem_label,
            "failure_class": self.failure_class,
            "lifecycle_state": self.lifecycle_state,
            "affected_role": self.affected_role,
            "role_instance_id": self.role_instance_id,
            "reason": self.reason,
            "reason_summary": self.reason_summary,
            "occurred_at": self.occurred_at,
            "next_action": self.next_action,
            "action_owner": self.action_owner,
            "retryable": self.retryable,
            "retryability_label": self.retryability_label,
            "recovery_action": self.recovery_action,
            "worker_adapter": self.worker_adapter,
            "worker_model": self.worker_model,
            "timeout_seconds": self.timeout_seconds,
            "progress_observed_at": self.progress_observed_at,
            "partial_artifacts_present": self.partial_artifacts_present,
            "artifact_paths": list(self.artifact_paths),
            "artifact_verification": [
                record.to_dict() for record in self.artifact_verification
            ],
            "status_url": self.status_url,
            "attempted_target_role": self.attempted_target_role,
            "attempted_message_type": self.attempted_message_type,
            "attempted_lifecycle_state": self.attempted_lifecycle_state,
            "expected_owner": self.expected_owner,
            "matched_configured_route": self.matched_configured_route,
            "route_validation_errors": list(self.route_validation_errors),
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        if self.failure_class in {"malformed_route", "malformed_handoff"}:
            payload.setdefault("attempted_target_role", self.attempted_target_role)
            payload.setdefault("attempted_message_type", self.attempted_message_type)
            payload.setdefault(
                "attempted_lifecycle_state",
                self.attempted_lifecycle_state,
            )
            payload.setdefault("expected_owner", self.expected_owner)
            payload.setdefault("matched_configured_route", self.matched_configured_route)
            payload.setdefault("route_validation_errors", list(self.route_validation_errors))
        return payload

    def journal_fields(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "work_item_id": self.work_item_id,
            "work_item_type": self.work_item_type,
            "queue_item_id": self.queue_item_id,
            "lifecycle_state": self.lifecycle_state,
            "role_id": self.affected_role,
            "role_instance_id": self.role_instance_id,
            "worker_adapter": self.worker_adapter,
            "worker_model": self.worker_model,
            "status": self.status,
            "problem_kind": self.problem_kind,
            "failure_class": self.failure_class,
            "retryable": self.retryable,
            "recovery_action": self.recovery_action,
            "correlation_id": self.correlation_id,
            "source_anchor_ref": self.source_anchor_ref,
            "artifact_count": len(self.artifact_paths),
            "partial_artifacts_present": self.partial_artifacts_present,
            "attempted_target_role": self.attempted_target_role,
            "attempted_message_type": self.attempted_message_type,
            "attempted_lifecycle_state": self.attempted_lifecycle_state,
            "expected_owner": self.expected_owner,
            "matched_configured_route": self.matched_configured_route,
            "route_validation_error_count": len(self.route_validation_errors),
        }


class ProblemStatusStore:
    def __init__(self, state_root: Path, project_id: str) -> None:
        self.root = state_root / "projects" / project_id / "work_items"

    def current_path(self, work_item_id: str) -> Path:
        return self.root / work_item_id / "problem-status.json"

    def history_path(self, work_item_id: str) -> Path:
        return self.root / work_item_id / "problem-status-history.jsonl"

    def write_current(self, status: ProblemStatus) -> Path:
        path = self.current_path(status.work_item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(status.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
        with self.history_path(status.work_item_id).open("a", encoding="utf-8") as handle:
            json.dump(status.to_dict(), handle, sort_keys=True)
            handle.write("\n")
        return path

    def read_current(self, work_item_id: str) -> dict[str, Any] | None:
        path = self.current_path(work_item_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def clear_current(self, work_item_id: str) -> bool:
        path = self.current_path(work_item_id)
        if not path.exists():
            return False
        path.unlink()
        return True


def role_problem_status(
    *,
    status: str,
    message: str,
    project_id: str,
    role_id: str,
    role_instance_id: str | None,
    work_item_id: str,
    work_item_type: str | None,
    lifecycle_state: str,
    queue_item_id: str | None,
    source_message_id: str | None,
    source_anchor: dict[str, Any] | None,
    correlation_id: str,
    status_url: str | None,
) -> ProblemStatus:
    del project_id
    problem_kind = "role_blocker" if status == "blocked" else "role_failure"
    next_action = "Review the role-provided blocker and provide the requested input."
    action_owner = role_id
    retryable = status == "blocked"
    if status == "failed":
        next_action = "Review the role failure and decide whether to retry or rescope."
    return ProblemStatus(
        work_item_id=work_item_id,
        work_item_type=work_item_type,
        queue_item_id=queue_item_id,
        source_message_id=source_message_id,
        source_anchor_ref=source_anchor_ref(source_anchor),
        source_anchor_summary=source_anchor_summary(source_anchor),
        correlation_id=correlation_id,
        status=status,
        problem_kind=problem_kind,
        lifecycle_state=lifecycle_state,
        affected_role=role_id,
        role_instance_id=role_instance_id,
        reason=message,
        next_action=next_action,
        action_owner=action_owner,
        retryable=retryable,
        status_url=status_url,
    )


def worker_problem_status(
    *,
    failure_class: str,
    reason: str,
    recovery_action: str,
    retryable: bool,
    role_id: str,
    role_instance_id: str | None,
    message_payload: dict[str, Any],
    source_message_id: str | None,
    correlation_id: str,
    lifecycle_state: str,
    worker_adapter: str | None,
    worker_model: str | None,
    timeout_seconds: int | None = None,
    progress_observed_at: str | None = None,
    artifact_paths: list[str] | None = None,
    status_url: str | None = None,
) -> ProblemStatus:
    paths = tuple(safe_artifact_paths(artifact_paths or []))
    verification = tuple(
        ArtifactVerificationRecord(
            path=path,
            verification="unverified_partial",
            label="Unverified partial artifact",
        )
        for path in paths
    )
    return ProblemStatus(
        work_item_id=str(message_payload.get("work_item_id") or source_message_id or "unknown"),
        work_item_type=message_payload.get("work_item_type"),
        queue_item_id=message_payload.get("queue_item_id"),
        source_message_id=source_message_id,
        source_anchor_ref=source_anchor_ref(message_payload.get("source_anchor")),
        source_anchor_summary=source_anchor_summary(message_payload.get("source_anchor")),
        correlation_id=correlation_id,
        status="needs_runtime_recovery",
        problem_kind="worker_failed",
        failure_class=failure_class,
        lifecycle_state=lifecycle_state,
        affected_role=role_id,
        role_instance_id=role_instance_id,
        reason=reason,
        next_action=recovery_action_label(recovery_action),
        action_owner="runtime/operator",
        retryable=retryable,
        recovery_action=recovery_action,
        worker_adapter=worker_adapter,
        worker_model=worker_model,
        timeout_seconds=timeout_seconds,
        progress_observed_at=progress_observed_at,
        partial_artifacts_present=bool(paths),
        artifact_paths=paths,
        artifact_verification=verification,
        status_url=status_url,
    )


def runtime_publication_problem_status(
    *,
    reason: str,
    role_id: str,
    role_instance_id: str | None,
    message_payload: dict[str, Any],
    source_message_id: str | None,
    correlation_id: str,
    lifecycle_state: str,
    status_url: str | None,
) -> ProblemStatus:
    return ProblemStatus(
        work_item_id=str(message_payload.get("work_item_id") or source_message_id or "unknown"),
        work_item_type=message_payload.get("work_item_type"),
        queue_item_id=message_payload.get("queue_item_id"),
        source_message_id=source_message_id,
        source_anchor_ref=source_anchor_ref(message_payload.get("source_anchor")),
        source_anchor_summary=source_anchor_summary(message_payload.get("source_anchor")),
        correlation_id=correlation_id,
        status="needs_runtime_recovery",
        problem_kind="runtime_failed",
        failure_class="publication_failed",
        lifecycle_state=lifecycle_state,
        affected_role=role_id,
        role_instance_id=role_instance_id,
        reason=reason,
        next_action=recovery_action_label("operator_review"),
        action_owner="runtime/operator",
        retryable=True,
        recovery_action="operator_review",
        status_url=status_url,
    )


def malformed_route_problem_status(
    *,
    failure_class: str,
    reason: str,
    role_id: str,
    role_instance_id: str | None,
    message_payload: dict[str, Any],
    source_message_id: str | None,
    correlation_id: str,
    lifecycle_state: str,
    attempted_target_role: str | None,
    attempted_message_type: str | None = None,
    attempted_lifecycle_state: str | None = None,
    expected_owner: str | None = None,
    matched_configured_route: str | None = None,
    route_validation_errors: tuple[str, ...] = (),
    status_url: str | None = None,
) -> ProblemStatus:
    return ProblemStatus(
        work_item_id=str(message_payload.get("work_item_id") or source_message_id or "unknown"),
        work_item_type=message_payload.get("work_item_type"),
        queue_item_id=message_payload.get("queue_item_id"),
        source_message_id=source_message_id,
        source_anchor_ref=source_anchor_ref(message_payload.get("source_anchor")),
        source_anchor_summary=source_anchor_summary(message_payload.get("source_anchor")),
        correlation_id=correlation_id,
        status="needs_runtime_recovery",
        problem_kind="runtime_failed",
        failure_class=failure_class,
        lifecycle_state=lifecycle_state,
        affected_role=role_id,
        role_instance_id=role_instance_id,
        reason=reason,
        next_action=recovery_action_label("operator_review"),
        action_owner="runtime/operator",
        retryable=True,
        recovery_action="operator_review",
        status_url=status_url,
        attempted_target_role=attempted_target_role,
        attempted_message_type=attempted_message_type,
        attempted_lifecycle_state=attempted_lifecycle_state,
        expected_owner=expected_owner,
        matched_configured_route=matched_configured_route,
        route_validation_errors=route_validation_errors,
    )


def recovery_action_label(action: str) -> str:
    return {
        "retry_same_state": "Retry the same lifecycle state after runtime recovery.",
        "retry_safe_output_contract": (
            "Retry the agent run with the safe-output contract enforced; "
            "if it repeats, inspect prompt/tool wiring."
        ),
        "repair_configuration": "Repair worker configuration before retrying.",
        "repair_auth": "Repair worker authentication before retrying.",
        "reconcile_partial_artifacts": "Reconcile unverified partial artifacts before handoff.",
        "operator_review": "Operator review is required before retry or reconciliation.",
    }.get(action, "Operator review is required.")


def source_anchor_ref(source_anchor: Any) -> str | None:
    if not isinstance(source_anchor, dict):
        return None
    value = source_anchor.get("source_anchor_ref")
    if value is None:
        return None
    return truncate_text(str(value), 128)


def source_anchor_summary(source_anchor: Any) -> str | None:
    if not isinstance(source_anchor, dict):
        return None
    label = source_anchor.get("display_label") or source_anchor.get("source_scope")
    if label is None:
        return None
    return redact_text(str(label), max_length=160)


def redact_optional_text(value: str | None) -> str | None:
    return redact_text(value) if value is not None else None


def redact_text(value: str, *, max_length: int = 500) -> str:
    text = truncate_text(str(value).strip(), max_length)
    text = re.sub(r"[A-Za-z]:\\[^\s`]+", "[redacted-path]", text)
    text = re.sub(r"/(?:[^\s`]+/){2,}[^\s`]+", "[redacted-path]", text)
    forbidden_keys = "|".join(
        re.escape(key)
        for key in sorted(FORBIDDEN_FIELD_NAMES | {"raw_payload"}, key=len, reverse=True)
    )
    text = re.sub(
        rf"(?i)`?(?:{forbidden_keys})`?\s*[`:= ]+\S+",
        "[redacted]",
        text,
    )
    text = re.sub(r"(?i)\b(secret|token|password|bearer)\s*[:=]\s*\S+", "[redacted]", text)
    return text


def truncate_text(value: str, max_length: int = 500) -> str:
    text = str(value)
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def safe_artifact_paths(paths: list[str]) -> list[str]:
    safe: list[str] = []
    for path in paths:
        normalized = str(path).strip().replace("\\", "/")
        requested = Path(normalized)
        if (
            not normalized
            or normalized.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:/", normalized)
            or requested.is_absolute()
            or ".." in requested.parts
            or normalized.startswith(".env")
            or normalized.startswith("state/")
        ):
            continue
        if normalized not in safe:
            safe.append(normalized)
    return safe


def assert_problem_status_payload_safe(payload: dict[str, Any]) -> None:
    forbidden = set(payload) & FORBIDDEN_FIELD_NAMES
    if forbidden:
        raise ValueError(f"problem status contains forbidden fields: {sorted(forbidden)}")


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
