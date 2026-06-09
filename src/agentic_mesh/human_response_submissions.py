from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from typing import Any

from agentic_mesh.approval_decisions import build_recorded_approval_decision
from agentic_mesh.approval_decisions import response_value_label
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.messaging import build_human_response_received_message
from agentic_mesh.models import Message
from agentic_mesh.models import utc_now_iso
from agentic_mesh.storage import FileMessageStore


@dataclass(frozen=True)
class HumanResponseSubmissionResult:
    schema_version: str
    response_request_id: str | None
    accepted: bool
    final: bool
    duplicate: bool
    validation_reason: str
    request_status: str | None
    decision_value: Any | None
    decision_label: str | None
    responder_display: str | None
    recorded_at: str | None
    message_id: str | None
    context_lookup: str
    context_completeness: str
    correlation_id: str | None
    recorded_view_model: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HumanResponseSubmissionService:
    def __init__(
        self,
        *,
        project_id: str,
        store: FileHumanGateRequestStore,
        message_store: FileMessageStore | None = None,
    ) -> None:
        self.project_id = project_id
        self.store = store
        self.message_store = message_store

    def submit(
        self,
        *,
        target_role: str,
        work_item_id: str,
        work_item_type: str | None,
        lifecycle_state: str,
        gate_id: str,
        response_type: str | None,
        response_request_id: str,
        responder: str | None,
        response_value: Any,
        authenticated: bool,
        source: str,
        approval_request_id: str | None = None,
        correlation_id: str | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> HumanResponseSubmissionResult:
        existing_request = self.store.read(response_request_id)
        accepted, reason, request = self.store.validate_response(
            project_id=self.project_id,
            work_item_id=work_item_id,
            work_item_type=work_item_type,
            lifecycle_state=lifecycle_state,
            gate_id=gate_id,
            response_type=response_type
            or (existing_request.response_type if existing_request is not None else ""),
            response_request_id=response_request_id,
            responder=responder,
            response_value=response_value,
            authenticated=authenticated,
            authoritative=True,
        )
        duplicate = reason == "duplicate_same_value"
        final = reason in {"accepted", "not_approved", "duplicate_same_value"}
        if request is None:
            return HumanResponseSubmissionResult(
                schema_version="human-response-submission-result-v1",
                response_request_id=response_request_id,
                accepted=False,
                final=False,
                duplicate=False,
                validation_reason=reason,
                request_status=None,
                decision_value=None,
                decision_label=None,
                responder_display=responder,
                recorded_at=None,
                message_id=None,
                context_lookup="missing",
                context_completeness="unavailable",
                correlation_id=correlation_id,
                recorded_view_model=None,
            )

        context = self.store.read_decision_context(response_request_id)
        context_lookup = "found" if context else "missing"
        self.store.record_context_lookup(
            response_request_id,
            result=context_lookup,
            reason=None if context else "missing_request_context",
        )
        decision_value = request.response_value if final else response_value
        recorded_at = request.completed_at if final else None
        decision_label = response_value_label(
            {"response_template": {"options": (context or {}).get("response_options") or []}},
            response_value=decision_value,
        ) if final else None
        recorded_model = None
        if final:
            metadata = {
                "project_id": self.project_id,
                "work_item_id": request.work_item_id,
                "work_item_type": request.work_item_type,
                "lifecycle_state": request.lifecycle_state,
                "gate_id": request.gate_id,
                "response_type": request.response_type,
                "response_request_id": request.response_request_id,
                "correlation_id": correlation_id,
            }
            recorded_model = build_recorded_approval_decision(
                context,
                request_metadata=metadata,
                decision_value=decision_value,
                responder_display=request.responder or responder,
                recorded_at=recorded_at or utc_now_iso(),
            ).to_dict()

        message_id = None
        if final and not duplicate and self.message_store is not None:
            message = build_human_response_received_message(
                target_role=target_role,
                work_item_id=request.work_item_id,
                work_item_type=request.work_item_type or "slice",
                lifecycle_state=request.lifecycle_state,
                gate_id=request.gate_id,
                response_type=request.response_type,
                approval_request_id=approval_request_id or request.approval_request_id,
                response_request_id=request.response_request_id,
                responder=str(request.responder or responder or "teams-user"),
                response_value=decision_value,
                source=source,
                connector_origin_authenticated=authenticated,
                prevalidated=True,
                correlation_id=correlation_id,
                trace_context=trace_context,
            )
            queued = self.message_store.enqueue(message)
            message_id = queued.message_id

        return HumanResponseSubmissionResult(
            schema_version="human-response-submission-result-v1",
            response_request_id=response_request_id,
            accepted=accepted,
            final=final,
            duplicate=duplicate,
            validation_reason=reason,
            request_status=request.status,
            decision_value=decision_value if final else None,
            decision_label=decision_label,
            responder_display=request.responder or responder,
            recorded_at=recorded_at,
            message_id=message_id,
            context_lookup=context_lookup,
            context_completeness=(
                str((recorded_model or {}).get("context_completeness") or "unavailable")
                if final
                else "unavailable"
            ),
            correlation_id=correlation_id,
            recorded_view_model=recorded_model,
        )
