from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from agentic_mesh import telemetry
from agentic_mesh.external_actions import safe_payload
from agentic_mesh.models import utc_now_iso
from agentic_mesh.work_item_recovery import FileRecoveryStatusStore
from agentic_mesh.work_item_recovery import RecoveryActionRequest
from agentic_mesh.work_item_recovery import RecoveryActionService
from agentic_mesh.work_item_recovery import RecoveryStatus


RECOVERY_POLLER_HEARTBEAT_SCHEMA_VERSION = "recovery-poller-heartbeat-v1"
RECOVERY_POLLER_RECEIPT_SCHEMA_VERSION = "recovery-poller-receipt-v1"


@dataclass(frozen=True)
class RecoveryPollerPolicy:
    enabled: bool = True
    actor: str = "runtime"
    source_type: str = "recovery_poller"


class RecoveryPoller:
    def __init__(
        self,
        *,
        project_id: str,
        state_root: Path,
        store: FileRecoveryStatusStore,
        action_service: RecoveryActionService,
        policy: RecoveryPollerPolicy | None = None,
    ) -> None:
        self.project_id = project_id
        self.state_root = state_root
        self.store = store
        self.action_service = action_service
        self.policy = policy or RecoveryPollerPolicy()

    @property
    def heartbeat_path(self) -> Path:
        return (
            self.state_root
            / "projects"
            / self.project_id
            / "recovery"
            / "poller-heartbeat.json"
        )

    def run_once(self, *, now: str | None = None) -> dict[str, Any]:
        current_now = now or utc_now_iso()
        rows, errors = self.store.list_current()
        outcomes: list[dict[str, Any]] = []
        if not self.policy.enabled:
            for row in rows:
                outcomes.append(self._skip(row, "poller_disabled", current_now))
            self._write_heartbeat(current_now, outcomes, errors)
            return self._summary(current_now, outcomes, errors)
        for row in rows:
            outcomes.append(self._evaluate(row, current_now))
        self._write_heartbeat(current_now, outcomes, errors)
        return self._summary(current_now, outcomes, errors)

    def _evaluate(self, status: RecoveryStatus, now: str) -> dict[str, Any]:
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            work_item_id=status.work_item_id,
            lifecycle_state=status.lifecycle_state,
            affected_role=status.affected_role,
            recovery_reason_class=status.recovery_reason_class,
            recovery_state=status.recovery_state,
            retry_policy_state=status.retry_policy_state,
            correlation_id=status.correlation_id,
            schema_version=RECOVERY_POLLER_RECEIPT_SCHEMA_VERSION,
        )
        with telemetry.start_span(
            "recovery.policy.evaluate",
            correlation_id=status.correlation_id,
            attributes=attrs,
        ):
            denial = self._policy_denial(status, now)
            if denial:
                return self._skip(status, denial, now)
            request = RecoveryActionRequest(
                action_type="retry_work_item_recovery",
                project_id=self.project_id,
                work_item_id=status.work_item_id,
                actor=self.policy.actor,
                reason="Automatic retry scheduled by recovery poller after safe retry-after evidence.",
                idempotency_key=f"poller|{status.recovery_id}|{status.revision}|{status.retry_after or status.next_retry_at}",
                expected_revision=status.revision,
                correlation_id=status.correlation_id,
                lifecycle_state=status.lifecycle_state,
                affected_role=status.affected_role,
                work_item_type=status.work_item_type,
                queue_item_id=status.queue_item_id,
                source_type=self.policy.source_type,
            )
            receipt = self.action_service.execute(request)
            return {
                "schema_version": RECOVERY_POLLER_RECEIPT_SCHEMA_VERSION,
                "work_item_id": status.work_item_id,
                "outcome": receipt.outcome,
                "decision_reason": receipt.decision_reason,
                "recovery_state": receipt.recovery_status.get("recovery_state"),
                "retry_policy_state": receipt.recovery_status.get("retry_policy_state"),
                "queued_message_id": receipt.queued_message_id,
                "correlation_id": status.correlation_id,
                "evaluated_at": now,
            }

    def _policy_denial(self, status: RecoveryStatus, now: str) -> str | None:
        if status.recovery_reason_class != "provider_rate_limit":
            return "not_safe_rate_limit_retry"
        if status.recoverability_class != "auto_retryable":
            return "not_auto_retryable"
        retry_time = _parse_time(status.next_retry_at or status.retry_after)
        if retry_time is None:
            return "retry_after_required"
        current = _parse_time(now) or datetime.now(timezone.utc)
        if retry_time > current:
            return "retry_after_not_due"
        if status.partial_artifacts_present:
            return "partial_artifact_reconciliation_required"
        if status.retry_count >= status.retry_limit:
            return "retry_limit_exceeded"
        return None

    def _skip(self, status: RecoveryStatus, decision_reason: str, now: str) -> dict[str, Any]:
        payload = {
            "schema_version": RECOVERY_POLLER_RECEIPT_SCHEMA_VERSION,
            "work_item_id": status.work_item_id,
            "outcome": "skipped",
            "decision_reason": decision_reason,
            "recovery_state": status.recovery_state,
            "retry_policy_state": status.retry_policy_state,
            "correlation_id": status.correlation_id,
            "evaluated_at": now,
        }
        self.action_service.journal.append("recovery_poll_skipped", **payload)
        telemetry.record_journal_event({"event_type": "recovery_poll_skipped", **payload})
        return payload

    def _write_heartbeat(
        self,
        now: str,
        outcomes: list[dict[str, Any]],
        errors: list[dict[str, str]],
    ) -> None:
        payload = {
            "schema_version": RECOVERY_POLLER_HEARTBEAT_SCHEMA_VERSION,
            "project_id": self.project_id,
            "last_poll_at": now,
            "degraded": bool(errors),
            "unreadable_store_errors": errors,
            "scanned_count": len(outcomes),
            "scheduled_count": sum(1 for item in outcomes if item.get("outcome") == "queued"),
            "skipped_count": sum(1 for item in outcomes if item.get("outcome") == "skipped"),
        }
        _atomic_write_json(self.heartbeat_path, payload)

    def _summary(
        self,
        now: str,
        outcomes: list[dict[str, Any]],
        errors: list[dict[str, str]],
    ) -> dict[str, Any]:
        return safe_payload(
            {
                "schema_version": RECOVERY_POLLER_HEARTBEAT_SCHEMA_VERSION,
                "project_id": self.project_id,
                "last_poll_at": now,
                "degraded": bool(errors),
                "unreadable_store_errors": errors,
                "outcomes": outcomes,
                "heartbeat_path": "recovery/poller-heartbeat.json",
            }
        )


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


def _atomic_write_json(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
