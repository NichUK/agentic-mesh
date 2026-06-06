from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentic_mesh import telemetry
from agentic_mesh.agent_run_state import FileAgentRunStateStore
from agentic_mesh.agent_run_state import RunStateReadError
from agentic_mesh.agent_run_state import validate_logical_id
from agentic_mesh.external_actions import safe_payload
from agentic_mesh.external_actions import safe_text
from agentic_mesh.external_actions import safe_token
from agentic_mesh.external_actions import status_location_for
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import Message
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso
from agentic_mesh.problem_status import ProblemStatus
from agentic_mesh.storage import FileMessageStore


RECOVERY_SCHEMA_VERSION = "work-item-recovery-v0"
RECOVERY_RECEIPT_SCHEMA_VERSION = "work-item-recovery-receipt-v0"
DUPLICATE_GUARD_SCHEMA_VERSION = "work-item-recovery-duplicate-guard-v0"

REASON_CLASSES = {
    "provider_limit",
    "provider_auth",
    "provider_rate_limit",
    "provider_timeout",
    "worker_timeout",
    "malformed_handoff",
    "connector_failure",
    "stale_claim",
    "crashed_worker",
    "role_blocker",
    "human_decision_required",
    "superseded",
    "duplicate",
    "unknown",
}
RECOVERABILITY_CLASSES = {
    "auto_retryable",
    "operator_retryable",
    "split_required",
    "fix_runtime_first",
    "needs_sponsor_decision",
    "not_recoverable",
}
RECOVERY_STATES = {
    "none",
    "recovery_needed",
    "recovery_queued",
    "recovery_running",
    "recovery_succeeded",
    "recovery_failed",
    "split_required",
    "runtime_fix_required",
    "sponsor_decision_required",
    "duplicate_active_work",
    "superseded",
    "not_recoverable",
}
RECEIPT_OUTCOMES = {
    "current",
    "accepted",
    "queued",
    "running",
    "succeeded",
    "failed",
    "denied",
    "noop",
    "duplicate",
    "superseded",
    "not_recoverable",
}
GUARD_RESULTS = {
    "clear",
    "duplicate_active_work",
    "guard_unavailable",
    "unsafe_to_recover",
}
RECOVERY_MUTATION_ACTIONS = {
    "retry_work_item_recovery",
    "record_work_item_recovery",
    "supersede_work_item_recovery",
    "mark_work_item_recovery_needs_decision",
}
RECOVERY_READ_ACTION = "read_work_item_recovery"

ACTIVE_RECOVERY_STATES = {"recovery_queued", "recovery_running"}
NON_TERMINAL_RECOVERY_STATES = {
    "recovery_needed",
    "recovery_queued",
    "recovery_running",
    "runtime_fix_required",
    "sponsor_decision_required",
    "duplicate_active_work",
}

RECOVERY_STATE_LABELS = {
    "none": "No recovery state",
    "recovery_needed": "Recovery needed",
    "recovery_queued": "Recovery queued",
    "recovery_running": "Recovery running",
    "recovery_succeeded": "Recovery succeeded",
    "recovery_failed": "Recovery failed",
    "split_required": "Split required",
    "runtime_fix_required": "Runtime fix required",
    "sponsor_decision_required": "Sponsor decision required",
    "duplicate_active_work": "Duplicate active work",
    "superseded": "Superseded",
    "not_recoverable": "Not recoverable",
}
REASON_LABELS = {
    "provider_limit": "Provider capacity limit",
    "provider_auth": "Provider authentication",
    "provider_rate_limit": "Provider rate limit",
    "provider_timeout": "Provider timeout",
    "worker_timeout": "Worker timeout",
    "malformed_handoff": "Malformed handoff",
    "connector_failure": "Connector failure",
    "stale_claim": "Stale claim",
    "crashed_worker": "Crashed worker",
    "role_blocker": "Role blocker",
    "human_decision_required": "Human decision required",
    "superseded": "Superseded",
    "duplicate": "Duplicate",
    "unknown": "Unknown",
}


@dataclass(frozen=True)
class DuplicateGuardResult:
    result: str
    checked_sources: tuple[str, ...]
    active_references: tuple[dict[str, str], ...] = ()
    decision_reason: str = "clear"
    checked_at: str = field(default_factory=utc_now_iso)
    schema_version: str = DUPLICATE_GUARD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.result not in GUARD_RESULTS:
            raise ValueError(f"unsupported duplicate guard result `{self.result}`")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "result": self.result,
            "checked_sources": list(self.checked_sources),
            "active_references": [safe_payload(ref) for ref in self.active_references],
            "decision_reason": safe_text(self.decision_reason, 160),
            "checked_at": self.checked_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "DuplicateGuardResult":
        return DuplicateGuardResult(
            result=str(data["result"]),
            checked_sources=tuple(str(item) for item in data.get("checked_sources", [])),
            active_references=tuple(
                {
                    str(key): str(value)
                    for key, value in dict(ref).items()
                    if value is not None
                }
                for ref in data.get("active_references", [])
            ),
            decision_reason=str(data.get("decision_reason") or "clear"),
            checked_at=str(data.get("checked_at") or utc_now_iso()),
        )


@dataclass(frozen=True)
class RecoveryStatus:
    recovery_id: str
    project_id: str
    work_item_id: str
    work_item_type: str | None
    queue_item_id: str | None
    lifecycle_state: str | None
    affected_role: str | None
    role_instance_id: str | None
    source_message_id: str | None
    source_anchor_ref: str | None
    source_anchor_summary: str | None
    correlation_id: str
    problem_status_ref: str | None
    problem_status: dict[str, Any] | None
    recovery_reason_class: str
    recoverability_class: str
    recovery_state: str
    retry_count: int = 0
    retry_limit: int = 2
    retry_after: str | None = None
    next_retry_at: str | None = None
    retry_policy_state: str | None = None
    partial_artifacts_present: bool = False
    partial_artifact_action: str = "none"
    next_action: str = "Inspect recovery status."
    action_owner: str = "operator"
    duplicate_guard: DuplicateGuardResult | None = None
    notification_state: dict[str, Any] = field(default_factory=dict)
    status_location: dict[str, Any] = field(default_factory=dict)
    journal_refs: tuple[str, ...] = ()
    revision: int = 1
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    last_state_at: str = field(default_factory=utc_now_iso)
    schema_version: str = RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, field_name in [
            (self.recovery_id, "recovery_id"),
            (self.project_id, "project_id"),
            (self.work_item_id, "work_item_id"),
            (self.correlation_id, "correlation_id"),
        ]:
            validate_logical_id(value, field_name=field_name)
        if self.lifecycle_state:
            validate_logical_id(self.lifecycle_state, field_name="lifecycle_state")
        if self.affected_role:
            validate_logical_id(self.affected_role, field_name="affected_role")
        if self.recovery_reason_class not in REASON_CLASSES:
            raise ValueError(
                f"unsupported recovery reason `{self.recovery_reason_class}`"
            )
        if self.recoverability_class not in RECOVERABILITY_CLASSES:
            raise ValueError(
                f"unsupported recoverability `{self.recoverability_class}`"
            )
        if self.recovery_state not in RECOVERY_STATES:
            raise ValueError(f"unsupported recovery state `{self.recovery_state}`")
        if self.retry_count < 0 or self.retry_limit < 0:
            raise ValueError("retry counts must be non-negative")

    @property
    def recovery_state_label(self) -> str:
        return RECOVERY_STATE_LABELS[self.recovery_state]

    @property
    def recovery_reason_label(self) -> str:
        return REASON_LABELS[self.recovery_reason_class]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "recovery_id": self.recovery_id,
            "revision": self.revision,
            "project_id": self.project_id,
            "work_item_id": self.work_item_id,
            "work_item_type": self.work_item_type,
            "queue_item_id": self.queue_item_id,
            "lifecycle_state": self.lifecycle_state,
            "affected_role": self.affected_role,
            "role_instance_id": self.role_instance_id,
            "source_message_id": self.source_message_id,
            "source_anchor_ref": self.source_anchor_ref,
            "source_anchor_summary": safe_text(self.source_anchor_summary, 240),
            "correlation_id": self.correlation_id,
            "problem_status_ref": self.problem_status_ref,
            "problem_status": safe_payload(self.problem_status or {}),
            "recovery_reason_class": self.recovery_reason_class,
            "recovery_reason_label": self.recovery_reason_label,
            "recoverability_class": self.recoverability_class,
            "recovery_state": self.recovery_state,
            "recovery_state_label": self.recovery_state_label,
            "retry_count": self.retry_count,
            "retry_limit": self.retry_limit,
            "retry_after": self.retry_after,
            "next_retry_at": self.next_retry_at,
            "retry_policy_state": safe_text(self.retry_policy_state, 120),
            "partial_artifacts_present": self.partial_artifacts_present,
            "partial_artifact_action": self.partial_artifact_action,
            "next_action": safe_text(self.next_action, 240),
            "action_owner": safe_text(self.action_owner, 120),
            "duplicate_guard": (
                self.duplicate_guard.to_dict() if self.duplicate_guard else None
            ),
            "notification_state": safe_payload(self.notification_state),
            "status_location": safe_payload(
                self.status_location
                or status_location_for("work_item", self.work_item_id).to_safe_dict()
            ),
            "journal_refs": [safe_token(ref) for ref in self.journal_refs],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_state_at": self.last_state_at,
        }
        return {key: value for key, value in payload.items() if value is not None}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RecoveryStatus":
        guard_data = data.get("duplicate_guard")
        return RecoveryStatus(
            recovery_id=str(data["recovery_id"]),
            project_id=str(data["project_id"]),
            work_item_id=str(data["work_item_id"]),
            work_item_type=data.get("work_item_type"),
            queue_item_id=data.get("queue_item_id"),
            lifecycle_state=data.get("lifecycle_state"),
            affected_role=data.get("affected_role"),
            role_instance_id=data.get("role_instance_id"),
            source_message_id=data.get("source_message_id"),
            source_anchor_ref=data.get("source_anchor_ref"),
            source_anchor_summary=data.get("source_anchor_summary"),
            correlation_id=str(data["correlation_id"]),
            problem_status_ref=data.get("problem_status_ref"),
            problem_status=dict(data.get("problem_status") or {}),
            recovery_reason_class=str(data["recovery_reason_class"]),
            recoverability_class=str(data["recoverability_class"]),
            recovery_state=str(data["recovery_state"]),
            retry_count=int(data.get("retry_count", 0)),
            retry_limit=int(data.get("retry_limit", 2)),
            retry_after=data.get("retry_after"),
            next_retry_at=data.get("next_retry_at"),
            retry_policy_state=data.get("retry_policy_state"),
            partial_artifacts_present=bool(
                data.get("partial_artifacts_present", False)
            ),
            partial_artifact_action=str(data.get("partial_artifact_action") or "none"),
            next_action=str(data.get("next_action") or "Inspect recovery status."),
            action_owner=str(data.get("action_owner") or "operator"),
            duplicate_guard=(
                DuplicateGuardResult.from_dict(guard_data)
                if isinstance(guard_data, dict)
                else None
            ),
            notification_state=dict(data.get("notification_state") or {}),
            status_location=dict(data.get("status_location") or {}),
            journal_refs=tuple(str(ref) for ref in data.get("journal_refs", [])),
            revision=int(data.get("revision", 1)),
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            last_state_at=str(data.get("last_state_at") or utc_now_iso()),
        )

    def journal_fields(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "work_item_id": self.work_item_id,
            "work_item_type": self.work_item_type,
            "queue_item_id": self.queue_item_id,
            "lifecycle_state": self.lifecycle_state,
            "affected_role": self.affected_role,
            "role_instance_id": self.role_instance_id,
            "recovery_reason_class": self.recovery_reason_class,
            "recoverability_class": self.recoverability_class,
            "recovery_state": self.recovery_state,
            "retry_count": self.retry_count,
            "retry_limit": self.retry_limit,
            "duplicate_guard_result": (
                self.duplicate_guard.result if self.duplicate_guard else None
            ),
            "correlation_id": self.correlation_id,
            "source_anchor_ref": self.source_anchor_ref,
            "problem_kind": (self.problem_status or {}).get("problem_kind"),
            "failure_class": (self.problem_status or {}).get("failure_class"),
        }


@dataclass(frozen=True)
class RecoveryActionRequest:
    action_type: str
    project_id: str
    work_item_id: str
    actor: str | None
    reason: str | None
    idempotency_key: str | None
    expected_revision: int | None
    correlation_id: str
    lifecycle_state: str | None = None
    affected_role: str | None = None
    work_item_type: str | None = None
    queue_item_id: str | None = None
    source_type: str = "cli"
    repair_confirmed: bool = False
    replacement_work_item_id: str | None = None
    decision_owner: str | None = None
    retry_after: str | None = None
    action_id: str = field(default_factory=lambda: new_id("recovery-action"))
    requested_at: str = field(default_factory=utc_now_iso)

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "action_id": safe_token(self.action_id),
            "action_type": safe_token(self.action_type),
            "project_id": safe_token(self.project_id),
            "work_item_id": safe_token(self.work_item_id),
            "work_item_type": safe_token(self.work_item_type) if self.work_item_type else None,
            "queue_item_id": safe_token(self.queue_item_id) if self.queue_item_id else None,
            "lifecycle_state": safe_token(self.lifecycle_state) if self.lifecycle_state else None,
            "affected_role": safe_token(self.affected_role) if self.affected_role else None,
            "actor": safe_text(self.actor, 120) if self.actor else None,
            "reason": safe_text(self.reason, 200) if self.reason else None,
            "idempotency_key_ref": safe_token(_safe_digest(self.idempotency_key)) if self.idempotency_key else None,
            "expected_revision": self.expected_revision,
            "correlation_id": safe_token(self.correlation_id),
            "source_type": safe_token(self.source_type),
            "repair_confirmed": self.repair_confirmed,
            "replacement_work_item_id": (
                safe_token(self.replacement_work_item_id)
                if self.replacement_work_item_id
                else None
            ),
            "decision_owner": safe_text(self.decision_owner, 120) if self.decision_owner else None,
            "retry_after": self.retry_after,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True)
class RecoveryActionReceipt:
    receipt_id: str
    action_id: str
    action_type: str
    outcome: str
    work_item_id: str
    lifecycle_state: str | None
    affected_role: str | None
    recovery_correlation_id: str
    recovery_status: dict[str, Any]
    duplicate_guard: DuplicateGuardResult | None = None
    queued_message_id: str | None = None
    notification_state: dict[str, Any] = field(default_factory=dict)
    journal_ref: str | None = None
    actor_summary: dict[str, Any] = field(default_factory=dict)
    safe_reason: str | None = None
    decision_reason: str = "allowed"
    status_location: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = RECOVERY_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.outcome not in RECEIPT_OUTCOMES:
            raise ValueError(f"unsupported recovery receipt outcome `{self.outcome}`")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "receipt_id": safe_token(self.receipt_id),
            "action_id": safe_token(self.action_id),
            "action_type": safe_token(self.action_type),
            "outcome": self.outcome,
            "work_item_id": safe_token(self.work_item_id),
            "lifecycle_state": safe_token(self.lifecycle_state) if self.lifecycle_state else None,
            "affected_role": safe_token(self.affected_role) if self.affected_role else None,
            "recovery_correlation_id": safe_token(self.recovery_correlation_id),
            "recovery_status": safe_payload(self.recovery_status),
            "duplicate_guard": self.duplicate_guard.to_dict() if self.duplicate_guard else None,
            "queued_message_id": safe_token(self.queued_message_id) if self.queued_message_id else None,
            "work_was_queued": self.queued_message_id is not None,
            "notification_state": safe_payload(self.notification_state),
            "journal_ref": safe_token(self.journal_ref) if self.journal_ref else None,
            "actor_summary": safe_payload(self.actor_summary),
            "safe_reason": safe_text(self.safe_reason, 200) if self.safe_reason else None,
            "decision_reason": safe_token(self.decision_reason),
            "status_location": safe_payload(
                self.status_location
                or status_location_for("work_item", self.work_item_id).to_safe_dict()
            ),
            "created_at": self.created_at,
        }
        return {key: value for key, value in payload.items() if value is not None}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RecoveryActionReceipt":
        guard_data = data.get("duplicate_guard")
        return RecoveryActionReceipt(
            receipt_id=str(data["receipt_id"]),
            action_id=str(data["action_id"]),
            action_type=str(data["action_type"]),
            outcome=str(data["outcome"]),
            work_item_id=str(data["work_item_id"]),
            lifecycle_state=data.get("lifecycle_state"),
            affected_role=data.get("affected_role"),
            recovery_correlation_id=str(data["recovery_correlation_id"]),
            recovery_status=dict(data.get("recovery_status") or {}),
            duplicate_guard=(
                DuplicateGuardResult.from_dict(guard_data)
                if isinstance(guard_data, dict)
                else None
            ),
            queued_message_id=data.get("queued_message_id"),
            notification_state=dict(data.get("notification_state") or {}),
            journal_ref=data.get("journal_ref"),
            actor_summary=dict(data.get("actor_summary") or {}),
            safe_reason=data.get("safe_reason"),
            decision_reason=str(data.get("decision_reason") or "allowed"),
            status_location=dict(data.get("status_location") or {}),
            created_at=str(data.get("created_at") or utc_now_iso()),
        )


class FileRecoveryStatusStore:
    def __init__(
        self,
        state_root: Path,
        project_id: str,
        *,
        create_dirs: bool = True,
    ) -> None:
        self.project_id = validate_logical_id(project_id, field_name="project_id")
        self.create_dirs = create_dirs
        self.root = state_root / "projects" / self.project_id
        self.work_items_root = self.root / "work_items"
        self.recovery_root = self.root / "recovery"
        self.idempotency_dir = self.recovery_root / "index" / "idempotency"
        self.target_dir = self.recovery_root / "index" / "targets"
        self.locks_dir = self.recovery_root / "locks"
        if create_dirs:
            for path in [
                self.work_items_root,
                self.idempotency_dir,
                self.target_dir,
                self.locks_dir,
            ]:
                path.mkdir(parents=True, exist_ok=True)

    def current_path(self, work_item_id: str) -> Path:
        return self._work_item_dir(work_item_id, create=self.create_dirs) / "recovery-status.json"

    def history_path(self, work_item_id: str) -> Path:
        return self._work_item_dir(work_item_id, create=self.create_dirs) / "recovery-status-history.jsonl"

    def problem_status_path(self, work_item_id: str) -> Path:
        return self._work_item_dir(work_item_id, create=self.create_dirs) / "problem-status.json"

    def receipts_dir(self, work_item_id: str) -> Path:
        path = self._work_item_dir(work_item_id, create=self.create_dirs) / "recovery-receipts"
        if self.create_dirs:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def get_current(self, work_item_id: str) -> RecoveryStatus | None:
        path = self.current_path(work_item_id)
        if not path.exists():
            return None
        return RecoveryStatus.from_dict(_read_json(path))

    def list_current(self) -> tuple[list[RecoveryStatus], list[dict[str, str]]]:
        rows: list[RecoveryStatus] = []
        errors: list[dict[str, str]] = []
        for path in sorted(self.work_items_root.glob("*/recovery-status.json")):
            try:
                rows.append(RecoveryStatus.from_dict(_read_json(path)))
            except Exception as exc:
                errors.append(
                    {
                        "work_item_id": path.parent.name,
                        "error_class": exc.__class__.__name__,
                    }
                )
        return rows, errors

    def write_current(self, status: RecoveryStatus) -> RecoveryStatus:
        path = self.current_path(status.work_item_id)
        _atomic_write_json(path, status.to_dict(), mode=0o600)
        self._append_history(status)
        target_path = self.target_dir / f"{_target_key(status)}.json"
        _atomic_write_json(
            target_path,
            {
                "recovery_id": status.recovery_id,
                "work_item_id": status.work_item_id,
                "lifecycle_state": status.lifecycle_state,
                "affected_role": status.affected_role,
                "recovery_state": status.recovery_state,
                "revision": status.revision,
                "updated_at": status.updated_at,
            },
            mode=0o600,
        )
        return status

    def clear_current_problem_status(self, work_item_id: str) -> bool:
        path = self.problem_status_path(work_item_id)
        if not path.exists():
            return False
        if path.is_symlink():
            raise ValueError("recovery store refuses symlink problem status")
        path.unlink()
        _fsync_directory(path.parent)
        return True

    def apply_transition(
        self,
        status: RecoveryStatus,
        *,
        expected_revision: int,
        recovery_state: str | None = None,
        recoverability_class: str | None = None,
        retry_count: int | None = None,
        duplicate_guard: DuplicateGuardResult | None = None,
        notification_state: dict[str, Any] | None = None,
        next_action: str | None = None,
        action_owner: str | None = None,
        journal_ref: str | None = None,
    ) -> RecoveryStatus:
        current = self.get_current(status.work_item_id)
        if current is None:
            raise StaleRecoveryWrite("missing_current_recovery")
        if current.revision != expected_revision:
            raise StaleRecoveryWrite("stale_expected_revision")
        now = utc_now_iso()
        updated = replace(
            current,
            revision=current.revision + 1,
            recovery_state=recovery_state or current.recovery_state,
            recoverability_class=recoverability_class or current.recoverability_class,
            retry_count=retry_count if retry_count is not None else current.retry_count,
            duplicate_guard=duplicate_guard or current.duplicate_guard,
            notification_state=(
                notification_state if notification_state is not None else current.notification_state
            ),
            next_action=next_action or current.next_action,
            action_owner=action_owner or current.action_owner,
            journal_refs=(
                current.journal_refs + (journal_ref,)
                if journal_ref
                else current.journal_refs
            ),
            updated_at=now,
            last_state_at=now,
        )
        return self.write_current(updated)

    def record_receipt(
        self,
        request: RecoveryActionRequest,
        receipt: RecoveryActionReceipt,
    ) -> None:
        path = self.receipts_dir(receipt.work_item_id) / f"{receipt.receipt_id}.json"
        _atomic_write_json(path, receipt.to_dict(), mode=0o600)
        if request.idempotency_key:
            index = self.idempotency_dir / f"{_safe_digest(_idempotency_scope(request))}.json"
            _atomic_write_json(
                index,
                {
                    "receipt_id": receipt.receipt_id,
                    "work_item_id": receipt.work_item_id,
                    "action_type": receipt.action_type,
                    "created_at": receipt.created_at,
                },
                mode=0o600,
            )

    def receipt_for_idempotency_key(
        self,
        request: RecoveryActionRequest,
    ) -> RecoveryActionReceipt | None:
        if not request.idempotency_key:
            return None
        index = self.idempotency_dir / f"{_safe_digest(_idempotency_scope(request))}.json"
        if not index.exists():
            return None
        data = _read_json(index)
        receipt_id = str(data.get("receipt_id") or "")
        if not receipt_id:
            return None
        path = self.receipts_dir(request.work_item_id) / f"{receipt_id}.json"
        if not path.exists():
            return None
        return RecoveryActionReceipt.from_dict(_read_json(path))

    def _append_history(self, status: RecoveryStatus) -> None:
        path = self.history_path(status.work_item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(status.to_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _work_item_dir(self, work_item_id: str, *, create: bool) -> Path:
        safe_id = validate_logical_id(work_item_id, field_name="work_item_id")
        path = (self.work_items_root / safe_id).resolve()
        root = self.work_items_root.resolve()
        if root != path and root not in path.parents:
            raise ValueError("recovery path escapes work item root")
        if path.exists() and path.is_symlink():
            raise ValueError("recovery store refuses symlink work item directories")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path


class StaleRecoveryWrite(ValueError):
    pass


class RecoveryClassifier:
    def __init__(self, *, retry_limit: int = 2) -> None:
        self.retry_limit = retry_limit

    def classify_problem(
        self,
        *,
        project_id: str,
        problem_status: ProblemStatus | dict[str, Any],
        current: RecoveryStatus | None = None,
        retry_after: str | None = None,
        notification_state: dict[str, Any] | None = None,
    ) -> RecoveryStatus:
        problem = (
            problem_status.to_dict()
            if isinstance(problem_status, ProblemStatus)
            else dict(problem_status)
        )
        reason = _reason_from_problem(problem)
        retry_count = current.retry_count if current and current.recovery_reason_class == reason else 0
        recoverability, state, next_action, owner = _recovery_defaults(
            problem,
            reason,
            retry_count=retry_count,
            retry_limit=self.retry_limit,
            retry_after=retry_after or problem.get("retry_after"),
        )
        partial = bool(problem.get("partial_artifacts_present", False))
        if partial and state not in {"split_required", "not_recoverable"}:
            recoverability = "fix_runtime_first"
            state = "runtime_fix_required"
            next_action = "Reconcile partial artifacts before retry."
            owner = problem.get("action_owner") or "operator"
        now = utc_now_iso()
        status = RecoveryStatus(
            recovery_id=current.recovery_id if current else new_id("recovery"),
            project_id=project_id,
            work_item_id=str(problem["work_item_id"]),
            work_item_type=problem.get("work_item_type"),
            queue_item_id=problem.get("queue_item_id"),
            lifecycle_state=problem.get("lifecycle_state"),
            affected_role=problem.get("affected_role"),
            role_instance_id=problem.get("role_instance_id"),
            source_message_id=problem.get("source_message_id"),
            source_anchor_ref=problem.get("source_anchor_ref"),
            source_anchor_summary=problem.get("source_anchor_summary"),
            correlation_id=str(problem.get("correlation_id") or new_id("corr")),
            problem_status_ref=str(problem.get("occurred_at") or now),
            problem_status=problem,
            recovery_reason_class=reason,
            recoverability_class=recoverability,
            recovery_state=state,
            retry_count=retry_count,
            retry_limit=self.retry_limit,
            retry_after=retry_after or problem.get("retry_after"),
            partial_artifacts_present=partial,
            partial_artifact_action=(
                "reconcile_before_retry" if partial else "none"
            ),
            next_action=str(next_action),
            action_owner=str(owner),
            notification_state=notification_state or {},
            status_location=status_location_for(
                "work_item",
                str(problem["work_item_id"]),
            ).to_safe_dict(),
            journal_refs=current.journal_refs if current else (),
            revision=(current.revision + 1) if current else 1,
            created_at=current.created_at if current else now,
            updated_at=now,
            last_state_at=now,
        )
        return status


class DuplicateActiveWorkGuard:
    def __init__(
        self,
        *,
        state_root: Path,
        project_id: str,
        required_sources: tuple[str, ...] = (
            "pending_messages",
            "claimed_messages",
            "agent_run_state",
            "active_recovery",
        ),
    ) -> None:
        self.state_root = state_root
        self.project_id = project_id
        self.required_sources = required_sources

    def check(
        self,
        *,
        work_item_id: str,
        lifecycle_state: str | None,
        affected_role: str | None,
        current_recovery_id: str | None = None,
    ) -> DuplicateGuardResult:
        checked: list[str] = []
        references: list[dict[str, str]] = []
        try:
            if "pending_messages" in self.required_sources:
                checked.append("pending_messages")
                references.extend(
                    self._message_refs(
                        work_item_id,
                        lifecycle_state,
                        affected_role,
                        state="pending",
                    )
                )
            if "claimed_messages" in self.required_sources:
                checked.append("claimed_messages")
                references.extend(
                    self._message_refs(
                        work_item_id,
                        lifecycle_state,
                        affected_role,
                        state="claimed",
                    )
                )
            if "agent_run_state" in self.required_sources:
                checked.append("agent_run_state")
                references.extend(
                    self._run_state_refs(work_item_id, lifecycle_state, affected_role)
                )
            if "active_recovery" in self.required_sources:
                checked.append("active_recovery")
                references.extend(
                    self._active_recovery_refs(
                        work_item_id,
                        lifecycle_state,
                        affected_role,
                        current_recovery_id,
                    )
                )
        except Exception as exc:
            return DuplicateGuardResult(
                result="guard_unavailable",
                checked_sources=tuple(checked),
                decision_reason=exc.__class__.__name__,
            )
        if references:
            return DuplicateGuardResult(
                result="duplicate_active_work",
                checked_sources=tuple(checked),
                active_references=tuple(references),
                decision_reason="active_work_found",
            )
        return DuplicateGuardResult(
            result="clear",
            checked_sources=tuple(checked),
            decision_reason="no_active_work_found",
        )

    def _message_refs(
        self,
        work_item_id: str,
        lifecycle_state: str | None,
        affected_role: str | None,
        *,
        state: str,
    ) -> list[dict[str, str]]:
        if not affected_role:
            return []
        queue_root = self.state_root / "projects" / self.project_id / "queues" / affected_role
        paths = (
            sorted((queue_root / "pending").glob("*.json"))
            if state == "pending"
            else sorted((queue_root / "claimed").glob("*/*.json"))
        )
        refs: list[dict[str, str]] = []
        for path in paths:
            data = _read_json(path)
            payload = data.get("payload") or {}
            if payload.get("work_item_id") != work_item_id:
                continue
            if lifecycle_state and payload.get("lifecycle_state") != lifecycle_state:
                continue
            refs.append(
                {
                    "source": f"{state}_message",
                    "message_id": safe_token(str(data.get("message_id") or "unknown")),
                    "role_id": safe_token(str(data.get("role_id") or affected_role)),
                }
            )
        return refs

    def _run_state_refs(
        self,
        work_item_id: str,
        lifecycle_state: str | None,
        affected_role: str | None,
    ) -> list[dict[str, str]]:
        root = (
            self.state_root
            / "projects"
            / self.project_id
            / "agent_run_state"
        )
        if not root.exists():
            return []
        store = FileAgentRunStateStore(self.state_root, self.project_id)
        refs: list[dict[str, str]] = []
        for path in sorted(root.glob("*/current.json")):
            role_instance_id = path.parent.name
            state = store.read_current_with_error(role_instance_id)
            if isinstance(state, RunStateReadError):
                raise ValueError("agent_run_state_unreadable")
            if state is None or state.run_state not in {"starting", "running"}:
                continue
            if state.work_item_id != work_item_id:
                continue
            if lifecycle_state and state.lifecycle_state != lifecycle_state:
                continue
            if affected_role and state.role_id != affected_role:
                continue
            if not self._claimed_run_state_message_exists(state):
                continue
            refs.append(
                {
                    "source": "active_run_state",
                    "role_instance_id": safe_token(state.role_instance_id),
                    "message_id": safe_token(state.message_id),
                }
            )
        return refs

    def _claimed_run_state_message_exists(self, state) -> bool:
        queue_root = (
            self.state_root
            / "projects"
            / self.project_id
            / "queues"
            / state.role_id
            / "claimed"
            / state.role_instance_id
        )
        if not queue_root.exists():
            return False
        for path in sorted(queue_root.glob("*.json")):
            data = _read_json(path)
            if data.get("message_id") == state.message_id:
                return True
        return False

    def _active_recovery_refs(
        self,
        work_item_id: str,
        lifecycle_state: str | None,
        affected_role: str | None,
        current_recovery_id: str | None,
    ) -> list[dict[str, str]]:
        store = FileRecoveryStatusStore(self.state_root, self.project_id)
        refs: list[dict[str, str]] = []
        for status, _ in [(row, None) for row in store.list_current()[0]]:
            if status.work_item_id != work_item_id:
                continue
            if current_recovery_id and status.recovery_id == current_recovery_id:
                continue
            if lifecycle_state and status.lifecycle_state != lifecycle_state:
                continue
            if affected_role and status.affected_role != affected_role:
                continue
            if status.recovery_state in ACTIVE_RECOVERY_STATES:
                refs.append(
                    {
                        "source": "active_recovery",
                        "recovery_id": safe_token(status.recovery_id),
                        "recovery_state": status.recovery_state,
                    }
                )
        return refs


class RecoveryActionService:
    def __init__(
        self,
        *,
        project_id: str,
        store: FileRecoveryStatusStore,
        message_store: FileMessageStore,
        journal: EventJournal,
        duplicate_guard: DuplicateActiveWorkGuard,
    ) -> None:
        self.project_id = project_id
        self.store = store
        self.message_store = message_store
        self.journal = journal
        self.duplicate_guard = duplicate_guard

    def execute(self, request: RecoveryActionRequest) -> RecoveryActionReceipt:
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            work_item_id=request.work_item_id,
            lifecycle_state=request.lifecycle_state,
            affected_role=request.affected_role,
            action_type=request.action_type,
            actor_type=_actor_type(request.actor),
            correlation_id=request.correlation_id,
            schema_version=RECOVERY_SCHEMA_VERSION,
        )
        with telemetry.start_span(
            "work_item_recovery.action.receive",
            correlation_id=request.correlation_id,
            attributes=attrs,
        ):
            self.journal.append(
                "recovery_action_received",
                **{
                    key: value
                    for key, value in attrs.items()
                    if not key.startswith("resource.")
                },
            )
            duplicate = self.store.receipt_for_idempotency_key(request)
            if duplicate is not None:
                self.journal.append(
                    "recovery_action_replay_detected",
                    project_id=self.project_id,
                    work_item_id=request.work_item_id,
                    action_type=request.action_type,
                    receipt_outcome="duplicate",
                    correlation_id=request.correlation_id,
                    schema_version=RECOVERY_SCHEMA_VERSION,
                )
                return replace(duplicate, outcome="duplicate")
            receipt = self._execute_once(request)
            self.store.record_receipt(request, receipt)
            self.journal.append(
                "recovery_action_receipt_recorded",
                project_id=self.project_id,
                work_item_id=request.work_item_id,
                lifecycle_state=request.lifecycle_state,
                affected_role=request.affected_role,
                action_type=request.action_type,
                receipt_outcome=receipt.outcome,
                duplicate_guard_result=(
                    receipt.duplicate_guard.result if receipt.duplicate_guard else None
                ),
                correlation_id=request.correlation_id,
                schema_version=RECOVERY_SCHEMA_VERSION,
            )
            return receipt

    def _execute_once(
        self,
        request: RecoveryActionRequest,
    ) -> RecoveryActionReceipt:
        current = self.store.get_current(request.work_item_id)
        if current is None:
            current = _empty_status_from_request(self.project_id, request)
            self.store.write_current(current)
        if request.action_type == RECOVERY_READ_ACTION:
            return self._receipt(request, "current", current)
        denial = _request_denial(request, current)
        if denial:
            return self._receipt(request, "denied", current, decision_reason=denial)
        if request.action_type == "supersede_work_item_recovery":
            return self._supersede(request, current)
        if request.action_type == "mark_work_item_recovery_needs_decision":
            return self._mark_needs_decision(request, current)
        if request.action_type in {"retry_work_item_recovery", "record_work_item_recovery"}:
            return self._retry_or_recover(request, current)
        return self._receipt(
            request,
            "denied",
            current,
            decision_reason="unsupported_action_type",
        )

    def _retry_or_recover(
        self,
        request: RecoveryActionRequest,
        current: RecoveryStatus,
    ) -> RecoveryActionReceipt:
        retry_denial = _retry_denial(current, request)
        if retry_denial:
            return self._receipt(request, "denied", current, decision_reason=retry_denial)
        guard = self.duplicate_guard.check(
            work_item_id=current.work_item_id,
            lifecycle_state=current.lifecycle_state,
            affected_role=current.affected_role,
            current_recovery_id=current.recovery_id,
        )
        if guard.result != "clear":
            updated = self.store.apply_transition(
                current,
                expected_revision=request.expected_revision or current.revision,
                recovery_state=(
                    "duplicate_active_work"
                    if guard.result == "duplicate_active_work"
                    else current.recovery_state
                ),
                duplicate_guard=guard,
                next_action="Do not enqueue duplicate recovery work; inspect active reference.",
                action_owner="operator",
                journal_ref="recovery_duplicate_guard_checked",
            )
            self.journal.append(
                "recovery_duplicate_guard_checked",
                **updated.journal_fields(),
            )
            return self._receipt(
                request,
                "duplicate" if guard.result == "duplicate_active_work" else "denied",
                updated,
                guard=guard,
                decision_reason=guard.result,
            )
        message = Message.create(
            role_id=current.affected_role or request.affected_role or "engineering",
            message_type="sdlc.recovery_retry",
            source="work-item-recovery",
            correlation_id=request.correlation_id,
            payload={
                "work_item_id": current.work_item_id,
                "work_item_type": current.work_item_type,
                "queue_item_id": current.queue_item_id,
                "lifecycle_state": current.lifecycle_state,
                "recovery_id": current.recovery_id,
                "recovery_reason_class": current.recovery_reason_class,
                "retry_count": current.retry_count + 1,
                "source_anchor_ref": current.source_anchor_ref,
            },
        )
        queued = self.message_store.enqueue(message)
        updated = self.store.apply_transition(
            current,
            expected_revision=request.expected_revision or current.revision,
            recovery_state="recovery_queued",
            retry_count=current.retry_count + 1,
            duplicate_guard=guard,
            next_action="Recovered work has been queued for the affected role.",
            action_owner=current.affected_role or "operator",
            journal_ref="recovery_retry_queued",
        )
        self.journal.append(
            "recovery_retry_queued",
            **updated.journal_fields(),
            message_id=queued.message_id,
            receipt_outcome="queued",
        )
        problem_status_cleared = self.store.clear_current_problem_status(current.work_item_id)
        if problem_status_cleared:
            self.journal.append(
                "problem_status_cleared_for_recovery",
                **updated.journal_fields(),
                message_id=queued.message_id,
                receipt_outcome="queued",
            )
        return self._receipt(request, "queued", updated, guard=guard, queued_message_id=queued.message_id)

    def _supersede(
        self,
        request: RecoveryActionRequest,
        current: RecoveryStatus,
    ) -> RecoveryActionReceipt:
        if not request.replacement_work_item_id:
            return self._receipt(
                request,
                "denied",
                current,
                decision_reason="replacement_work_item_id_required",
            )
        updated = self.store.apply_transition(
            current,
            expected_revision=request.expected_revision or current.revision,
            recovery_state="superseded",
            recoverability_class="not_recoverable",
            next_action=f"Use replacement work item {safe_token(request.replacement_work_item_id)}.",
            action_owner="product-manager",
            journal_ref="recovery_superseded",
        )
        self.journal.append(
            "recovery_superseded",
            **updated.journal_fields(),
            replacement_work_item_id=safe_token(request.replacement_work_item_id),
            receipt_outcome="superseded",
        )
        problem_status_cleared = self.store.clear_current_problem_status(current.work_item_id)
        if problem_status_cleared:
            self.journal.append(
                "problem_status_cleared_for_recovery",
                **updated.journal_fields(),
                replacement_work_item_id=safe_token(request.replacement_work_item_id),
                receipt_outcome="superseded",
            )
        return self._receipt(request, "superseded", updated)

    def _mark_needs_decision(
        self,
        request: RecoveryActionRequest,
        current: RecoveryStatus,
    ) -> RecoveryActionReceipt:
        owner = request.decision_owner or current.action_owner or "sponsor"
        updated = self.store.apply_transition(
            current,
            expected_revision=request.expected_revision or current.revision,
            recovery_state="sponsor_decision_required",
            recoverability_class="needs_sponsor_decision",
            next_action="Decision is required before recovery can continue.",
            action_owner=owner,
            journal_ref="recovery_decision_needed",
        )
        self.journal.append(
            "recovery_decision_needed",
            **updated.journal_fields(),
            receipt_outcome="accepted",
        )
        return self._receipt(request, "accepted", updated)

    def _receipt(
        self,
        request: RecoveryActionRequest,
        outcome: str,
        status: RecoveryStatus,
        *,
        guard: DuplicateGuardResult | None = None,
        queued_message_id: str | None = None,
        decision_reason: str = "allowed",
    ) -> RecoveryActionReceipt:
        return RecoveryActionReceipt(
            receipt_id=new_id("recovery-receipt"),
            action_id=request.action_id,
            action_type=request.action_type,
            outcome=outcome,
            work_item_id=status.work_item_id,
            lifecycle_state=status.lifecycle_state,
            affected_role=status.affected_role,
            recovery_correlation_id=request.correlation_id,
            recovery_status=status.to_dict(),
            duplicate_guard=guard or status.duplicate_guard,
            queued_message_id=queued_message_id,
            notification_state=status.notification_state,
            journal_ref="recovery_action_receipt_recorded",
            actor_summary={"actor_type": _actor_type(request.actor), "display_label": safe_text(request.actor, 120)},
            safe_reason=request.reason,
            decision_reason=decision_reason,
            status_location=status.status_location,
        )


def _reason_from_problem(problem: dict[str, Any]) -> str:
    failure_class = str(problem.get("failure_class") or "")
    problem_kind = str(problem.get("problem_kind") or "")
    if failure_class in {"usage_limit", "credit_exhausted", "quota_exceeded"}:
        return "provider_limit"
    if failure_class == "rate_limited":
        return "provider_rate_limit"
    if failure_class in {"auth_failed", "auth_missing"}:
        return "provider_auth"
    if failure_class == "timeout":
        return "worker_timeout"
    if failure_class in {"malformed_route", "malformed_handoff", "schema_failed"}:
        return "malformed_handoff"
    if problem_kind == "role_blocker":
        owner = str(problem.get("action_owner") or "")
        if owner in {"sponsor", "release-sponsor", "product-manager", "delivery-manager"}:
            return "human_decision_required"
        return "role_blocker"
    if problem_kind == "role_failure":
        return "role_blocker"
    return "unknown"


def _recovery_defaults(
    problem: dict[str, Any],
    reason: str,
    *,
    retry_count: int,
    retry_limit: int,
    retry_after: str | None,
) -> tuple[str, str, str, str]:
    if reason == "provider_limit":
        return (
            "operator_retryable",
            "recovery_needed",
            "Restore provider capacity or request sponsor decision before retry.",
            "operator",
        )
    if reason == "provider_auth":
        return (
            "fix_runtime_first",
            "runtime_fix_required",
            "Repair provider authentication before recovery retry.",
            "operator",
        )
    if reason == "provider_rate_limit":
        return (
            "auto_retryable" if retry_after else "operator_retryable",
            "recovery_needed",
            "Wait for retry-after and re-run duplicate guard before retry."
            if retry_after
            else "Operator review is required before retry.",
            "runtime" if retry_after else "operator",
        )
    if reason in {"provider_timeout", "worker_timeout"}:
        if retry_count >= retry_limit:
            return (
                "split_required",
                "split_required",
                "Product or Delivery must split the work before retry.",
                "product-manager",
            )
        return (
            "operator_retryable",
            "recovery_needed",
            "Operator may retry once duplicate guard passes.",
            "operator",
        )
    if reason == "malformed_handoff":
        return (
            "fix_runtime_first",
            "runtime_fix_required",
            "Repair route, schema, or prompt normalization before retry.",
            "engineering",
        )
    if reason in {"role_blocker", "human_decision_required"}:
        return (
            "needs_sponsor_decision",
            "sponsor_decision_required",
            str(problem.get("next_action") or "Named owner must decide before recovery."),
            str(problem.get("action_owner") or "sponsor"),
        )
    if reason in {"superseded", "duplicate"}:
        return (
            "not_recoverable",
            "not_recoverable",
            "Use the active or replacement work item.",
            "product-manager",
        )
    return (
        "fix_runtime_first",
        "runtime_fix_required",
        "Operator must classify and repair the runtime condition before retry.",
        "operator",
    )


def _request_denial(
    request: RecoveryActionRequest,
    current: RecoveryStatus,
) -> str | None:
    if request.action_type not in RECOVERY_MUTATION_ACTIONS:
        return None
    if request.source_type in {"api", "mcp", "web", "connector_card"}:
        return "mutation_surface_disabled_by_default"
    if not request.actor:
        return "actor_required"
    if not request.reason:
        return "reason_required"
    if not request.idempotency_key:
        return "idempotency_key_required"
    if not request.correlation_id:
        return "correlation_id_required"
    if request.expected_revision is None:
        return "expected_revision_required"
    if request.expected_revision != current.revision:
        return "stale_expected_revision"
    return None


def _retry_denial(
    current: RecoveryStatus,
    request: RecoveryActionRequest,
) -> str | None:
    if current.recovery_state in {"split_required", "superseded", "not_recoverable"}:
        return current.recovery_state
    repair_override = (
        request.action_type == "record_work_item_recovery"
        and request.repair_confirmed
    )
    if current.retry_count >= current.retry_limit and not repair_override:
        return "retry_limit_exceeded"
    if current.partial_artifacts_present and not repair_override:
        return "partial_artifact_reconciliation_required"
    if current.recoverability_class == "needs_sponsor_decision":
        return "specialist_or_sponsor_decision_required"
    if current.recovery_reason_class in {"provider_auth", "malformed_handoff", "unknown"} and not request.repair_confirmed:
        return "repair_confirmation_required"
    if current.recovery_reason_class in {"stale_claim", "crashed_worker"}:
        return "health_evidence_required"
    if current.recoverability_class not in {"operator_retryable", "auto_retryable", "fix_runtime_first"}:
        return "not_retryable"
    return None


def _empty_status_from_request(
    project_id: str,
    request: RecoveryActionRequest,
) -> RecoveryStatus:
    return RecoveryStatus(
        recovery_id=new_id("recovery"),
        project_id=project_id,
        work_item_id=request.work_item_id,
        work_item_type=request.work_item_type,
        queue_item_id=request.queue_item_id,
        lifecycle_state=request.lifecycle_state,
        affected_role=request.affected_role,
        role_instance_id=None,
        source_message_id=None,
        source_anchor_ref=None,
        source_anchor_summary=None,
        correlation_id=request.correlation_id,
        problem_status_ref=None,
        problem_status=None,
        recovery_reason_class="unknown",
        recoverability_class="fix_runtime_first",
        recovery_state="runtime_fix_required",
        next_action="No current recovery record exists; classify the stopped work first.",
        action_owner="operator",
        status_location=status_location_for("work_item", request.work_item_id).to_safe_dict(),
    )


def _actor_type(actor: str | None) -> str:
    if not actor:
        return "unknown"
    if actor in {"operator", "runtime", "system"}:
        return actor
    if actor.endswith("-agent") or actor in {"engineering", "qa-engineer"}:
        return "role"
    return "human"


def _idempotency_scope(request: RecoveryActionRequest) -> str:
    return "|".join(
        [
            request.idempotency_key or "",
            request.action_type,
            request.project_id,
            request.work_item_id,
            request.lifecycle_state or "",
            request.affected_role or "",
            request.actor or "",
        ]
    )


def _target_key(status: RecoveryStatus) -> str:
    return _safe_digest(
        "|".join(
            [
                status.work_item_id,
                status.lifecycle_state or "",
                status.affected_role or "",
            ]
        )
    )


def _safe_digest(value: str | None) -> str:
    import hashlib

    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
    return f"ref-{digest[:32]}"


def _read_json(path: Path) -> dict[str, Any]:
    if path.exists() and path.is_symlink():
        raise ValueError("recovery store refuses symlink targets")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def _atomic_write_json(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("recovery store refuses symlink parent directories")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(safe_payload(data), handle, indent=2, sort_keys=True)
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
