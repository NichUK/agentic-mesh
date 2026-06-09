from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
import re

from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.human_gates import HumanGateRequest
from agentic_mesh.human_gates import HumanGateStoreError
from agentic_mesh.models import FlowGate
from agentic_mesh.models import MeshConfig
from agentic_mesh.models import utc_now_iso


SCHEMA_VERSION = "approval-status-v0"

APPROVAL_STATUSES = {
    "not_yet_reached",
    "pending",
    "completed",
    "not_approved",
    "timed_out",
    "request_failed",
    "stale_waiting",
    "superseded",
    "inconsistent",
    "unknown",
}

STATUS_LABELS = {
    "not_yet_reached": "Approval not reached yet",
    "pending": "Waiting for approval",
    "completed": "Decision recorded: Approve",
    "not_approved": "Decision recorded: Not Approve",
    "timed_out": "Approval timed out",
    "request_failed": "Approval request failed",
    "stale_waiting": "Approval needs recovery",
    "superseded": "Approval request superseded",
    "inconsistent": "Approval state inconsistent",
    "unknown": "Approval state unknown",
}

TERMINAL_STATUSES = {
    "completed",
    "not_approved",
    "timed_out",
    "superseded",
}

ATTENTION_OWNER = {
    "pending": "requested_responder",
    "request_failed": "operator",
    "stale_waiting": "operator",
    "inconsistent": "operator",
    "unknown": "operator",
    "timed_out": "operator",
    "not_approved": "requester",
}

FORBIDDEN_DEFAULT_FIELDS = {
    "tenant_id",
    "team_id",
    "channel_id",
    "conversation_id",
    "activity_id",
    "user_id",
    "bot_id",
    "service_url",
    "graph_url",
    "payload_ref",
    "raw_payload",
    "secret_ref",
    "mount_ref",
    "credential_ref",
    "stdout",
    "stderr",
    "support_refs",
}


@dataclass(frozen=True)
class ApprovalStatusDTO:
    schema_version: str
    work_item_id: str
    work_item_type: str | None
    queue_item_id: str | None
    gate_id: str | None
    lifecycle_state: str | None
    response_type: str | None
    approval_request_id: str | None
    response_request_id: str | None
    notification_attempt_id: str | None
    approval_request_status: str
    approval_request_status_label: str
    status_reason: str | None
    attention_reason: str | None
    attention_owner: str | None
    next_action: str
    requested_from: str | None
    logical_channel: str | None
    prompt: str | None
    completion_criteria_label: str | None
    requested_at: str | None
    sent_at: str | None
    responded_at: str | None
    completed_at: str | None
    timeout: str | None
    timeout_at: str | None
    on_timeout: str | None
    response_value_label: str | None
    responder_label: str | None
    actor_label: str | None
    source_anchor_summary: str | None
    source_thread_ref: str | None
    status_url: str | None
    notification_attempt_summary: dict[str, Any] | None
    evidence_refs: list[str]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        forbidden = FORBIDDEN_DEFAULT_FIELDS.intersection(payload)
        if forbidden:
            raise ValueError(f"approval status DTO contains forbidden fields: {sorted(forbidden)}")
        return payload


@dataclass(frozen=True)
class ApprovalReconcileResult:
    schema_version: str
    dry_run: bool
    work_item_id: str
    gate_id: str | None
    before: dict[str, Any]
    proposed_after: dict[str, Any]
    mutation_performed: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalStatusService:
    def __init__(
        self,
        *,
        mesh_config: MeshConfig,
        state_root: Path,
    ) -> None:
        self.mesh_config = mesh_config
        self.state_root = state_root
        self.project_id = mesh_config.project.project_id
        self.store = FileHumanGateRequestStore(state_root, self.project_id)

    def status_for_work_item(
        self,
        work_item_id: str,
        *,
        lifecycle_state: str | None = None,
        gate_id: str | None = None,
        current_status: str | None = None,
        status_url: str | None = None,
    ) -> ApprovalStatusDTO:
        requests = [
            item
            for item in self.store.read_by_work_item(work_item_id)
            if gate_id in {None, "", item.gate_id}
            and lifecycle_state in {None, "", item.lifecycle_state}
        ]
        if len([item for item in requests if item.active]) > 1:
            return self._unknown_or_synthetic(
                work_item_id=work_item_id,
                lifecycle_state=lifecycle_state,
                gate_id=gate_id,
                status="inconsistent",
                status_reason="multiple_active_approval_requests",
                status_url=status_url,
            )
        if requests:
            return self._dto_from_request(requests[-1], status_url=status_url)
        if current_status == "waiting_for_human_response":
            return self._unknown_or_synthetic(
                work_item_id=work_item_id,
                lifecycle_state=lifecycle_state,
                gate_id=gate_id,
                status="stale_waiting",
                status_reason="historical_waiting_without_active_request",
                status_url=status_url,
            )
        gate_state, gate = self._gate(lifecycle_state, gate_id)
        return self._unknown_or_synthetic(
            work_item_id=work_item_id,
            lifecycle_state=gate_state or lifecycle_state,
            gate_id=(gate.gate_id if gate else gate_id),
            status="not_yet_reached" if gate else "unknown",
            status_reason=None if gate else "approval_gate_not_found",
            gate=gate,
            status_url=status_url,
        )

    def reconcile(
        self,
        *,
        work_item_id: str,
        lifecycle_state: str | None,
        gate_id: str | None,
        dry_run: bool,
        actor: str | None = None,
        reason: str | None = None,
        current_status: str | None = None,
    ) -> ApprovalReconcileResult:
        before = self.status_for_work_item(
            work_item_id,
            lifecycle_state=lifecycle_state,
            gate_id=gate_id,
            current_status=current_status,
        )
        proposed = before
        mutation = False
        if before.approval_request_status == "pending" and not before.notification_attempt_id:
            proposed = self._replace_status(
                before,
                status="request_failed",
                status_reason="approval_request_missing_notification_attempt",
            )
        elif before.approval_request_status == "stale_waiting":
            proposed = before
        if dry_run:
            return ApprovalReconcileResult(
                schema_version="approval-reconcile-v0",
                dry_run=True,
                work_item_id=work_item_id,
                gate_id=gate_id,
                before=before.to_dict(),
                proposed_after=proposed.to_dict(),
                mutation_performed=False,
                message="Dry run only - no approval records, connector messages, or journal events were changed.",
            )
        if not actor or not reason:
            raise ValueError("Actor and reason are required before this recovery action can run.")
        if before.response_request_id and before.approval_request_status == "pending":
            self.store.mark_failed(
                before.response_request_id,
                reason="approval_request_recovery_required",
            )
            mutation = True
            proposed = self.status_for_work_item(
                work_item_id,
                lifecycle_state=lifecycle_state,
                gate_id=gate_id,
            )
        return ApprovalReconcileResult(
            schema_version="approval-reconcile-v0",
            dry_run=False,
            work_item_id=work_item_id,
            gate_id=gate_id,
            before=before.to_dict(),
            proposed_after=proposed.to_dict(),
            mutation_performed=mutation,
            message="Approval reconcile completed.",
        )

    def resend(
        self,
        *,
        work_item_id: str,
        lifecycle_state: str | None,
        gate_id: str | None,
        actor: str,
        reason: str,
    ) -> ApprovalStatusDTO:
        status = self.status_for_work_item(
            work_item_id,
            lifecycle_state=lifecycle_state,
            gate_id=gate_id,
        )
        if not status.response_request_id:
            raise ValueError("No durable approval request exists to resend.")
        if status.approval_request_status in TERMINAL_STATUSES:
            raise ValueError(
                f"Cannot resend terminal approval request status {status.approval_request_status}."
            )
        try:
            self.store.prepare_resend_attempt(
                status.response_request_id,
                actor=actor,
                reason=reason,
            )
        except HumanGateStoreError as exc:
            raise ValueError(str(exc)) from exc
        return self.status_for_work_item(
            work_item_id,
            lifecycle_state=lifecycle_state,
            gate_id=gate_id,
        )

    def mark_superseded(
        self,
        *,
        work_item_id: str,
        lifecycle_state: str | None,
        gate_id: str | None,
        actor: str,
        reason: str,
    ) -> ApprovalStatusDTO:
        status = self.status_for_work_item(
            work_item_id,
            lifecycle_state=lifecycle_state,
            gate_id=gate_id,
        )
        if not status.response_request_id:
            raise ValueError("No durable approval request exists to supersede.")
        self.store.mark_superseded(
            status.response_request_id,
            actor=actor,
            reason=reason,
        )
        return self.status_for_work_item(
            work_item_id,
            lifecycle_state=lifecycle_state,
            gate_id=gate_id,
        )

    def _dto_from_request(
        self,
        request: HumanGateRequest,
        *,
        status_url: str | None = None,
    ) -> ApprovalStatusDTO:
        status = _canonical_status(request.status)
        attempt = _latest_attempt(request)
        response_value = request.response_value
        timeout_at = _derived_timeout_at(request)
        if status == "pending" and timeout_at is not None and _is_past(timeout_at):
            status = "timed_out"
        return ApprovalStatusDTO(
            schema_version=SCHEMA_VERSION,
            work_item_id=request.work_item_id,
            work_item_type=request.work_item_type,
            queue_item_id=None,
            gate_id=request.gate_id,
            lifecycle_state=request.lifecycle_state,
            response_type=request.response_type,
            approval_request_id=request.approval_request_id or request.response_request_id,
            response_request_id=request.response_request_id,
            notification_attempt_id=(
                str(attempt.get("notification_attempt_id")) if attempt else None
            ),
            approval_request_status=status,
            approval_request_status_label=STATUS_LABELS[status],
            status_reason=request.status_reason,
            attention_reason=_attention_reason(status, request.status_reason),
            attention_owner=ATTENTION_OWNER.get(status),
            next_action=_next_action(status),
            requested_from=request.requested_from,
            logical_channel=request.channel,
            prompt=request.prompt,
            completion_criteria_label=_completion_label(request.accepted_values),
            requested_at=request.requested_at,
            sent_at=str(attempt.get("sent_at")) if attempt and attempt.get("sent_at") else None,
            responded_at=request.completed_at if status in {"completed", "not_approved"} else None,
            completed_at=request.completed_at,
            timeout=request.timeout,
            timeout_at=timeout_at if status == "timed_out" else None,
            on_timeout=request.on_timeout,
            response_value_label=_response_value_label(response_value),
            responder_label=request.responder,
            actor_label=None,
            source_anchor_summary=None,
            source_thread_ref=None,
            status_url=status_url,
            notification_attempt_summary=_safe_attempt_summary(attempt),
            evidence_refs=_evidence_refs(request, attempt),
        )

    def _unknown_or_synthetic(
        self,
        *,
        work_item_id: str,
        lifecycle_state: str | None,
        gate_id: str | None,
        status: str,
        status_reason: str | None,
        gate: FlowGate | None = None,
        status_url: str | None,
    ) -> ApprovalStatusDTO:
        status = status if status in APPROVAL_STATUSES else "unknown"
        return ApprovalStatusDTO(
            schema_version=SCHEMA_VERSION,
            work_item_id=work_item_id,
            work_item_type=None,
            queue_item_id=None,
            gate_id=gate_id or (gate.gate_id if gate else None),
            lifecycle_state=lifecycle_state,
            response_type=(gate.response_type if gate else None),
            approval_request_id=None,
            response_request_id=None,
            notification_attempt_id=None,
            approval_request_status=status,
            approval_request_status_label=STATUS_LABELS[status],
            status_reason=status_reason,
            attention_reason=_attention_reason(status, status_reason),
            attention_owner=ATTENTION_OWNER.get(status),
            next_action=_next_action(status),
            requested_from=gate.requested_from if gate else None,
            logical_channel=gate.channel if gate else None,
            prompt=gate.prompt if gate else None,
            completion_criteria_label=_completion_label(
                list((gate.completion_criteria or {}).get("accepted_values") or [])
                if gate
                else []
            ),
            requested_at=None,
            sent_at=None,
            responded_at=None,
            completed_at=None,
            timeout=gate.timeout if gate else None,
            timeout_at=None,
            on_timeout=gate.on_timeout if gate else None,
            response_value_label=None,
            responder_label=None,
            actor_label=None,
            source_anchor_summary=None,
            source_thread_ref=None,
            status_url=status_url,
            notification_attempt_summary=None,
            evidence_refs=[],
        )

    def _replace_status(
        self,
        dto: ApprovalStatusDTO,
        *,
        status: str,
        status_reason: str,
    ) -> ApprovalStatusDTO:
        data = dto.to_dict()
        data.update(
            {
                "approval_request_status": status,
                "approval_request_status_label": STATUS_LABELS[status],
                "status_reason": status_reason,
                "attention_reason": _attention_reason(status, status_reason),
                "attention_owner": ATTENTION_OWNER.get(status),
                "next_action": _next_action(status),
            }
        )
        return ApprovalStatusDTO(**data)

    def _gate(
        self,
        lifecycle_state: str | None,
        gate_id: str | None,
    ) -> tuple[str | None, FlowGate | None]:
        fallback: tuple[str | None, FlowGate | None] = (None, None)
        for state_id, state in self.mesh_config.project.flow.states.items():
            for gate in state.gates:
                if gate.type != "human_response":
                    continue
                if gate_id and gate.gate_id != gate_id:
                    continue
                if gate_id and gate.gate_id == gate_id and fallback == (None, None):
                    fallback = (state_id, gate)
                if lifecycle_state and state_id != lifecycle_state:
                    continue
                return state_id, gate
        return fallback


def _canonical_status(status: str) -> str:
    if status == "waiting_for_response":
        return "pending"
    if status == "request_pending_delivery":
        return "request_failed"
    if status in APPROVAL_STATUSES:
        return status
    if status == "invalid_response":
        return "unknown"
    return "unknown"


def _latest_attempt(request: HumanGateRequest) -> dict[str, Any] | None:
    if request.notification_attempts:
        return dict(request.notification_attempts[-1])
    if request.connector_message_id:
        return {
            "notification_attempt_id": request.current_notification_attempt_id,
            "approval_request_id": request.approval_request_id
            or request.response_request_id,
            "response_request_id": request.response_request_id,
            "status": "queued",
            "connector_message_id": request.connector_message_id,
            "logical_route": request.channel,
            "route_label": request.channel,
        }
    return None


def _safe_attempt_summary(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not attempt:
        return None
    return {
        "notification_attempt_id": attempt.get("notification_attempt_id"),
        "attempt_number": attempt.get("attempt_number"),
        "status": attempt.get("status"),
        "logical_route": attempt.get("logical_route"),
        "route_label": attempt.get("route_label"),
        "failure_class": attempt.get("failure_class"),
        "safe_reason": attempt.get("safe_reason"),
        "updated_at": attempt.get("updated_at"),
    }


def _completion_label(values: list[Any]) -> str | None:
    if not values:
        return None
    return "Accepted values: " + ", ".join(str(value) for value in values)


def _response_value_label(value: Any) -> str | None:
    if value == "approved":
        return "Approve"
    if value == "not_approved":
        return "Not Approve"
    return str(value)[:120] if value is not None else None


def _attention_reason(status: str, reason: str | None) -> str | None:
    if reason:
        return reason[:160]
    if status in {"request_failed", "stale_waiting", "inconsistent", "unknown"}:
        return "Approval request needs operator review."
    if status == "pending":
        return "Approval is waiting for the requested responder."
    if status == "not_approved":
        return "Approval was not granted."
    if status == "timed_out":
        return "Approval request timed out."
    return None


def _next_action(status: str) -> str:
    if status == "not_yet_reached":
        return "Continue lifecycle until the approval gate is reached."
    if status == "pending":
        return "Wait for the requested responder or run approval-status for details."
    if status == "completed":
        return "No approval recovery action is required."
    if status == "not_approved":
        return "Do not treat the gate as satisfied; route per lifecycle policy."
    if status == "timed_out":
        return "Review timeout evidence and decide the next lifecycle action."
    if status == "request_failed":
        return "Run approval-reconcile --dry-run, then resend or supersede if appropriate."
    if status == "stale_waiting":
        return "Run approval-reconcile --dry-run before any resend or supersede action."
    if status == "superseded":
        return "No active approval request remains for this gate attempt."
    if status == "inconsistent":
        return "Resolve duplicate or conflicting approval evidence before resend."
    return "Review approval evidence before taking action."


def _evidence_refs(
    request: HumanGateRequest,
    attempt: dict[str, Any] | None,
) -> list[str]:
    refs = [f"approval_request:{request.approval_request_id or request.response_request_id}"]
    if attempt and attempt.get("notification_attempt_id"):
        refs.append(f"notification_attempt:{attempt['notification_attempt_id']}")
    if request.connector_message_id:
        refs.append(f"connector_message:{request.connector_message_id}")
    return refs


def _derived_timeout_at(request: HumanGateRequest) -> str | None:
    duration = _parse_iso_duration(request.timeout)
    if duration is None:
        return None
    try:
        requested_at = datetime.fromisoformat(request.requested_at)
    except ValueError:
        return None
    return (requested_at + duration).isoformat()


def _is_past(timestamp: str) -> bool:
    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    now = datetime.fromisoformat(utc_now_iso())
    if value.tzinfo is None:
        value = value.replace(tzinfo=now.tzinfo)
    return value <= now


def _parse_iso_duration(value: str | None) -> timedelta | None:
    if not value:
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        value,
    )
    if not match:
        return None
    parts = {key: int(raw or 0) for key, raw in match.groupdict().items()}
    duration = timedelta(**parts)
    return duration if duration.total_seconds() > 0 else None


def now_for_approval_command() -> str:
    return utc_now_iso()
