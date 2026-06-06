from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentic_mesh.external_actions import safe_payload
from agentic_mesh.external_actions import safe_text
from agentic_mesh.external_actions import safe_token
from agentic_mesh.external_actions import status_location_for
from agentic_mesh.provider_conditions import ProviderConditionResult
from agentic_mesh.recovery_alerts import RecoveryAlertState
from agentic_mesh.work_item_recovery import RecoveryStatus


RECOVERY_OBSERVABILITY_SCHEMA_VERSION = "recovery-observability-view-v1"

SEVERITY_BY_RECOVERY_STATE = {
    "recovery_needed": "action_required",
    "recovery_failed": "incident",
    "runtime_fix_required": "action_required",
    "split_required": "action_required",
    "sponsor_decision_required": "action_required",
    "duplicate_active_work": "warning",
    "not_recoverable": "incident",
    "recovery_queued": "warning",
    "recovery_running": "warning",
    "recovery_succeeded": "info",
    "superseded": "info",
    "none": "info",
}
RETRY_POLICY_BY_RECOVERY = {
    "auto_retryable": "eligible_for_retry",
    "operator_retryable": "operator_review_required",
    "fix_runtime_first": "repair_required",
    "needs_sponsor_decision": "decision_required",
    "split_required": "decision_required",
    "not_recoverable": "not_retryable",
}


@dataclass(frozen=True)
class RecoveryObservabilityView:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return safe_payload(self.payload)


def build_recovery_observability_view(
    recovery: RecoveryStatus,
    *,
    provider_condition: ProviderConditionResult | dict[str, Any] | None = None,
    alert_state: RecoveryAlertState | dict[str, Any] | None = None,
    notification_state: dict[str, Any] | None = None,
) -> RecoveryObservabilityView:
    provider = _provider_payload(provider_condition or recovery.provider_condition)
    alert = _alert_payload(alert_state)
    notification = safe_payload(notification_state or recovery.notification_state or {})
    retry_policy_state = recovery.retry_policy_state or _retry_policy_state(recovery)
    status_location = recovery.status_location or status_location_for(
        "work_item",
        recovery.work_item_id,
    ).to_safe_dict()
    problem = recovery.problem_status or {}
    duplicate_guard = recovery.duplicate_guard.to_dict() if recovery.duplicate_guard else None
    payload = {
        "schema_version": RECOVERY_OBSERVABILITY_SCHEMA_VERSION,
        "project_id": safe_token(recovery.project_id),
        "work_item_id": safe_token(recovery.work_item_id),
        "work_item_type": safe_token(recovery.work_item_type)
        if recovery.work_item_type
        else None,
        "queue_item_id": safe_token(recovery.queue_item_id)
        if recovery.queue_item_id
        else None,
        "lifecycle_state": safe_token(recovery.lifecycle_state)
        if recovery.lifecycle_state
        else None,
        "affected_role": safe_token(recovery.affected_role)
        if recovery.affected_role
        else None,
        "role_instance_id": safe_token(recovery.role_instance_id)
        if recovery.role_instance_id
        else None,
        "source_anchor_ref": safe_token(recovery.source_anchor_ref)
        if recovery.source_anchor_ref
        else None,
        "source_summary": safe_text(recovery.source_anchor_summary, 160)
        if recovery.source_anchor_summary
        else None,
        "correlation_id": safe_token(recovery.correlation_id),
        "problem_status": safe_token(problem.get("status"))
        if problem.get("status")
        else None,
        "problem_kind": safe_token(problem.get("problem_kind"))
        if problem.get("problem_kind")
        else None,
        "failure_class": safe_token(problem.get("failure_class"))
        if problem.get("failure_class")
        else None,
        "blocker_display_label": recovery.recovery_reason_label,
        "blocker_reason_class": recovery.recovery_reason_class,
        "recovery_reason_class": recovery.recovery_reason_class,
        "recoverability_class": recovery.recoverability_class,
        "recovery_state": recovery.recovery_state,
        "recovery_state_label": recovery.recovery_state_label,
        "provider_condition_class": provider.get("condition_class"),
        "provider_display_label": provider.get("provider_display_label"),
        "provider_confidence": provider.get("confidence"),
        "redacted_diagnostic_class": provider.get("redacted_diagnostic_class"),
        "retry_policy_state": retry_policy_state,
        "retry_count": recovery.retry_count,
        "retry_limit": recovery.retry_limit,
        "next_retry_at": recovery.next_retry_at or recovery.retry_after,
        "next_probe_at": recovery.next_probe_at or provider.get("next_check_at"),
        "last_probe_at": recovery.last_probe_at,
        "last_retry_attempt_at": recovery.last_retry_attempt_at,
        "duplicate_guard_result": duplicate_guard.get("result") if duplicate_guard else None,
        "duplicate_guard_summary": duplicate_guard.get("decision_reason") if duplicate_guard else None,
        "partial_artifacts_present": recovery.partial_artifacts_present,
        "partial_artifact_action": recovery.partial_artifact_action,
        "action_owner": safe_text(recovery.action_owner, 120),
        "next_action": safe_text(recovery.next_action, 240),
        "severity": _severity(recovery),
        "alert_state": alert.get("alert_state") or "none",
        "alert_severity": alert.get("severity"),
        "silence_active": alert.get("silence_active"),
        "notification_state": notification.get("status") or notification.get("state"),
        "notification_route_label": notification.get("route_label"),
        "notification_dead_lettered": bool(notification.get("dead_lettered", False)),
        "status_location": safe_payload(status_location),
        "journal_refs": [safe_token(ref) for ref in recovery.journal_refs],
        "revision": recovery.revision,
        "updated_at": recovery.updated_at,
        "redaction_status": "allowlisted",
    }
    return RecoveryObservabilityView(
        {key: value for key, value in payload.items() if value is not None}
    )


def connector_display_facts(view: RecoveryObservabilityView | dict[str, Any]) -> dict[str, Any]:
    payload = view.to_dict() if isinstance(view, RecoveryObservabilityView) else dict(view)
    return safe_payload(
        {
            "schema_version": "recovery-display-facts-v1",
            "primary_label": payload.get("blocker_display_label"),
            "severity": payload.get("severity"),
            "work_item_id": payload.get("work_item_id"),
            "lifecycle_state": payload.get("lifecycle_state"),
            "owner": payload.get("action_owner"),
            "next_action": payload.get("next_action"),
            "recovery_state_label": payload.get("recovery_state_label"),
            "retry_policy_state": payload.get("retry_policy_state"),
            "next_retry_at": payload.get("next_retry_at"),
            "next_probe_at": payload.get("next_probe_at"),
            "status_location": payload.get("status_location"),
            "route_label": payload.get("notification_route_label"),
            "fallback_reason": payload.get("fallback_reason"),
            "source_anchor_ref": payload.get("source_anchor_ref"),
            "redaction_status": payload.get("redaction_status"),
        }
    )


def _provider_payload(
    provider_condition: ProviderConditionResult | dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(provider_condition, ProviderConditionResult):
        return provider_condition.to_dict()
    if isinstance(provider_condition, dict):
        return safe_payload(provider_condition)
    return {}


def _alert_payload(alert_state: RecoveryAlertState | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(alert_state, RecoveryAlertState):
        return alert_state.to_dict()
    if isinstance(alert_state, dict):
        return safe_payload(alert_state)
    return {}


def _retry_policy_state(recovery: RecoveryStatus) -> str:
    if recovery.recovery_state == "recovery_queued":
        return "retry_scheduled"
    if recovery.recovery_state == "recovery_running":
        return "retry_running"
    if recovery.retry_count >= recovery.retry_limit:
        return "retry_limit_reached"
    return RETRY_POLICY_BY_RECOVERY.get(recovery.recoverability_class, "operator_review_required")


def _severity(recovery: RecoveryStatus) -> str:
    if recovery.recovery_state == "recovery_failed":
        return "incident"
    if recovery.recovery_state in {"runtime_fix_required", "split_required", "sponsor_decision_required"}:
        return "action_required"
    return SEVERITY_BY_RECOVERY_STATE.get(recovery.recovery_state, "warning")
