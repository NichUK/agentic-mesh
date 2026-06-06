from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from agentic_mesh.agent_run_state import validate_logical_id
from agentic_mesh.external_actions import safe_payload
from agentic_mesh.external_actions import safe_text
from agentic_mesh.external_actions import safe_token
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso


RECOVERY_ALERT_SCHEMA_VERSION = "recovery-alert-state-v1"
RECOVERY_ALERT_RECEIPT_SCHEMA_VERSION = "recovery-alert-receipt-v1"

ALERT_SEVERITIES = {"info", "warning", "action_required", "incident", "critical"}
ALERT_STATES = {
    "none",
    "dashboard_only",
    "connector_notified",
    "ops_alert_open",
    "acknowledged",
    "silenced",
}
ALERT_RECEIPT_OUTCOMES = {"accepted", "denied", "current"}


@dataclass(frozen=True)
class RecoveryAlertState:
    alert_id: str
    recovery_id: str
    project_id: str
    work_item_id: str
    queue_item_id: str | None
    severity: str
    alert_state: str
    route_label: str
    source_anchor_ref: str | None = None
    fallback_reason: str | None = None
    acknowledged_by: str | None = None
    acknowledged_reason: str | None = None
    acknowledged_at: str | None = None
    silenced_by: str | None = None
    silenced_reason: str | None = None
    silence_scope: str | None = None
    silence_expires_at: str | None = None
    expected_recovery_revision: int | None = None
    journal_refs: tuple[str, ...] = ()
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    schema_version: str = RECOVERY_ALERT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, field_name in [
            (self.alert_id, "alert_id"),
            (self.recovery_id, "recovery_id"),
            (self.project_id, "project_id"),
            (self.work_item_id, "work_item_id"),
        ]:
            validate_logical_id(value, field_name=field_name)
        if self.severity not in ALERT_SEVERITIES:
            raise ValueError(f"unsupported alert severity `{self.severity}`")
        if self.alert_state not in ALERT_STATES:
            raise ValueError(f"unsupported alert state `{self.alert_state}`")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "alert_id": safe_token(self.alert_id),
            "recovery_id": safe_token(self.recovery_id),
            "project_id": safe_token(self.project_id),
            "work_item_id": safe_token(self.work_item_id),
            "queue_item_id": safe_token(self.queue_item_id) if self.queue_item_id else None,
            "severity": self.severity,
            "alert_state": self.alert_state,
            "route_label": safe_token(self.route_label),
            "source_anchor_ref": safe_token(self.source_anchor_ref)
            if self.source_anchor_ref
            else None,
            "fallback_reason": safe_token(self.fallback_reason)
            if self.fallback_reason
            else None,
            "acknowledged_by": safe_text(self.acknowledged_by, 120)
            if self.acknowledged_by
            else None,
            "acknowledged_reason": safe_text(self.acknowledged_reason, 200)
            if self.acknowledged_reason
            else None,
            "acknowledged_at": self.acknowledged_at,
            "silenced_by": safe_text(self.silenced_by, 120) if self.silenced_by else None,
            "silenced_reason": safe_text(self.silenced_reason, 200)
            if self.silenced_reason
            else None,
            "silence_scope": safe_token(self.silence_scope) if self.silence_scope else None,
            "silence_expires_at": self.silence_expires_at,
            "silence_active": self.silence_active(),
            "expected_recovery_revision": self.expected_recovery_revision,
            "journal_refs": [safe_token(ref) for ref in self.journal_refs],
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return {key: value for key, value in payload.items() if value is not None}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RecoveryAlertState":
        return RecoveryAlertState(
            alert_id=str(data["alert_id"]),
            recovery_id=str(data["recovery_id"]),
            project_id=str(data["project_id"]),
            work_item_id=str(data["work_item_id"]),
            queue_item_id=data.get("queue_item_id"),
            severity=str(data["severity"]),
            alert_state=str(data["alert_state"]),
            route_label=str(data["route_label"]),
            source_anchor_ref=data.get("source_anchor_ref"),
            fallback_reason=data.get("fallback_reason"),
            acknowledged_by=data.get("acknowledged_by"),
            acknowledged_reason=data.get("acknowledged_reason"),
            acknowledged_at=data.get("acknowledged_at"),
            silenced_by=data.get("silenced_by"),
            silenced_reason=data.get("silenced_reason"),
            silence_scope=data.get("silence_scope"),
            silence_expires_at=data.get("silence_expires_at"),
            expected_recovery_revision=data.get("expected_recovery_revision"),
            journal_refs=tuple(str(ref) for ref in data.get("journal_refs", [])),
            revision=int(data.get("revision", 1)),
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
        )

    def silence_active(self, *, now: str | None = None) -> bool:
        if self.alert_state != "silenced" or not self.silence_expires_at:
            return False
        expiry = _parse_time(self.silence_expires_at)
        current = _parse_time(now) or datetime.now(timezone.utc)
        return expiry is not None and expiry > current


@dataclass(frozen=True)
class RecoveryAlertReceipt:
    receipt_id: str
    action_type: str
    outcome: str
    decision_reason: str
    alert_state: dict[str, Any]
    recovery_remains_active: bool = True
    created_at: str = ""
    schema_version: str = RECOVERY_ALERT_RECEIPT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        if self.outcome not in ALERT_RECEIPT_OUTCOMES:
            raise ValueError(f"unsupported alert receipt outcome `{self.outcome}`")
        return safe_payload(
            {
                "schema_version": self.schema_version,
                "receipt_id": safe_token(self.receipt_id),
                "action_type": safe_token(self.action_type),
                "outcome": self.outcome,
                "decision_reason": safe_token(self.decision_reason),
                "alert_state": self.alert_state,
                "recovery_remains_active": self.recovery_remains_active,
                "created_at": self.created_at or utc_now_iso(),
            }
        )


class StaleAlertWrite(ValueError):
    pass


class FileRecoveryAlertStore:
    def __init__(self, state_root: Path, project_id: str, *, create_dirs: bool = True) -> None:
        self.project_id = validate_logical_id(project_id, field_name="project_id")
        self.root = state_root / "projects" / self.project_id / "recovery" / "alerts"
        self.create_dirs = create_dirs
        if create_dirs:
            self.root.mkdir(parents=True, exist_ok=True)

    def current_path(self, work_item_id: str) -> Path:
        return self._item_dir(work_item_id, create=self.create_dirs) / "recovery-alert.json"

    def history_path(self, work_item_id: str) -> Path:
        return self._item_dir(work_item_id, create=self.create_dirs) / "recovery-alert-history.jsonl"

    def get_current(self, work_item_id: str) -> RecoveryAlertState | None:
        path = self.current_path(work_item_id)
        if not path.exists():
            return None
        return RecoveryAlertState.from_dict(_read_json(path))

    def write_current(self, state: RecoveryAlertState) -> RecoveryAlertState:
        current = replace(
            state,
            created_at=state.created_at or utc_now_iso(),
            updated_at=state.updated_at or utc_now_iso(),
        )
        _atomic_write_json(self.current_path(current.work_item_id), current.to_dict())
        with self.history_path(current.work_item_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(current.to_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return current

    def acknowledge(
        self,
        *,
        work_item_id: str,
        actor: str,
        reason: str,
        scope: str,
        expected_revision: int,
        correlation_id: str,
    ) -> RecoveryAlertReceipt:
        denial = _alert_control_denial(
            actor=actor,
            reason=reason,
            scope=scope,
            expected_revision=expected_revision,
            correlation_id=correlation_id,
        )
        current = self.get_current(work_item_id)
        if current is None:
            return self._receipt("acknowledge", "denied", "current_alert_required", None)
        if denial:
            return self._receipt("acknowledge", "denied", denial, current)
        if current.revision != expected_revision:
            return self._receipt("acknowledge", "denied", "stale_expected_revision", current)
        updated = self.write_current(
            replace(
                current,
                alert_state="acknowledged",
                acknowledged_by=actor,
                acknowledged_reason=reason,
                acknowledged_at=utc_now_iso(),
                revision=current.revision + 1,
                updated_at=utc_now_iso(),
            )
        )
        return self._receipt("acknowledge", "accepted", "allowed", updated)

    def silence(
        self,
        *,
        work_item_id: str,
        actor: str,
        reason: str,
        scope: str,
        silence_expires_at: str,
        expected_revision: int,
        correlation_id: str,
        human_decision_prompt: bool = False,
    ) -> RecoveryAlertReceipt:
        denial = _alert_control_denial(
            actor=actor,
            reason=reason,
            scope=scope,
            expected_revision=expected_revision,
            correlation_id=correlation_id,
        )
        current = self.get_current(work_item_id)
        if current is None:
            return self._receipt("silence", "denied", "current_alert_required", None)
        if denial:
            return self._receipt("silence", "denied", denial, current)
        if current.revision != expected_revision:
            return self._receipt("silence", "denied", "stale_expected_revision", current)
        expiry = _parse_time(silence_expires_at)
        if expiry is None or expiry <= datetime.now(timezone.utc):
            return self._receipt("silence", "denied", "finite_future_expiry_required", current)
        if human_decision_prompt:
            return self._receipt("silence", "denied", "human_decision_silence_denied", current)
        updated = self.write_current(
            replace(
                current,
                alert_state="silenced",
                silenced_by=actor,
                silenced_reason=reason,
                silence_scope=scope,
                silence_expires_at=silence_expires_at,
                revision=current.revision + 1,
                updated_at=utc_now_iso(),
            )
        )
        return self._receipt("silence", "accepted", "allowed", updated)

    def _receipt(
        self,
        action_type: str,
        outcome: str,
        decision_reason: str,
        state: RecoveryAlertState | None,
    ) -> RecoveryAlertReceipt:
        return RecoveryAlertReceipt(
            receipt_id=new_id("recovery-alert-receipt"),
            action_type=f"recovery_alert_{action_type}",
            outcome=outcome,
            decision_reason=decision_reason,
            alert_state=state.to_dict() if state is not None else {},
            recovery_remains_active=True,
            created_at=utc_now_iso(),
        )

    def _item_dir(self, work_item_id: str, *, create: bool) -> Path:
        safe_id = validate_logical_id(work_item_id, field_name="work_item_id")
        path = (self.root / safe_id).resolve()
        root = self.root.resolve()
        if root != path and root not in path.parents:
            raise ValueError("recovery alert path escapes alert root")
        if path.exists() and path.is_symlink():
            raise ValueError("recovery alert store refuses symlink item directories")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path


def _alert_control_denial(
    *,
    actor: str | None,
    reason: str | None,
    scope: str | None,
    expected_revision: int | None,
    correlation_id: str | None,
) -> str | None:
    if not actor:
        return "actor_required"
    if not reason:
        return "reason_required"
    if not scope:
        return "scope_required"
    if expected_revision is None:
        return "expected_revision_required"
    if not correlation_id:
        return "correlation_id_required"
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if path.exists() and path.is_symlink():
        raise ValueError("recovery alert store refuses symlink targets")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def _atomic_write_json(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("recovery alert store refuses symlink parent directories")
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


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
