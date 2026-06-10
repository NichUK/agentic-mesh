from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse

from agentic_mesh import telemetry
from agentic_mesh.agent_run_state import AgentRunState
from agentic_mesh.agent_run_state import FileAgentRunStateStore
from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.journal import EventJournal
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.human_gates import HumanGateStoreError
from agentic_mesh.messaging import MESSAGE_TYPE_DIRECT_CONVERSATION_REQUESTED
from agentic_mesh.messaging import MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_PUBLISH_READY
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED
from agentic_mesh.messaging import build_direct_conversation_status_message
from agentic_mesh.messaging import build_human_response_request
from agentic_mesh.messaging import build_problem_status_connector_message
from agentic_mesh.messaging import build_route_status_connector_message
from agentic_mesh.messaging import build_sdlc_handoff_connector_message
from agentic_mesh.messaging import build_sponsor_directive_status_message
from agentic_mesh.models import AgentRunResult
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import FlowGate
from agentic_mesh.models import FlowState
from agentic_mesh.models import Message
from agentic_mesh.models import ProjectConfig
from agentic_mesh.models import RouteRequest
from agentic_mesh.models import ResponseTypeTemplate
from agentic_mesh.notifications import FileNotificationAttemptStore
from agentic_mesh.notifications import FileSourceRouteStore
from agentic_mesh.notifications import NotificationEvent
from agentic_mesh.notifications import NotificationPolicyEvaluator
from agentic_mesh.notifications import NotificationRouteResolver
from agentic_mesh.notifications import StatusLinkBuilder
from agentic_mesh.notifications import route_resolution_for_surface
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.problem_status import malformed_route_problem_status
from agentic_mesh.problem_status import ProblemStatus
from agentic_mesh.problem_status import ProblemStatusStore
from agentic_mesh.problem_status import role_problem_status
from agentic_mesh.problem_status import runtime_publication_problem_status
from agentic_mesh.route_status import CurrentRoute
from agentic_mesh.route_status import CurrentRouteStore
from agentic_mesh.route_status import current_route_from_payload
from agentic_mesh.route_status import route_id_for
from agentic_mesh.work_item_indexes import GLOBAL_INDEX_PATH
from agentic_mesh.work_item_indexes import LOCAL_INDEX_NAME
from agentic_mesh.work_item_indexes import NOT_FOUND
from agentic_mesh.work_item_indexes import UNKNOWN
from agentic_mesh.work_item_indexes import WorkItemDocumentRow
from agentic_mesh.work_item_indexes import WorkItemIndex
from agentic_mesh.work_item_indexes import WorkItemsIndexRow
from agentic_mesh.work_item_indexes import local_index_path
from agentic_mesh.work_item_indexes import parse_global_index
from agentic_mesh.work_item_indexes import parse_local_index
from agentic_mesh.work_item_indexes import render_global_index
from agentic_mesh.work_item_indexes import render_local_index
from agentic_mesh.work_item_indexes import review_status_from_content
from agentic_mesh.work_item_indexes import source_anchor_summary
from agentic_mesh.work_item_indexes import upsert_document_row
from agentic_mesh.work_item_indexes import upsert_global_row
from agentic_mesh.work_item_indexes import validate_work_item_id
from agentic_mesh.work_item_recovery import FileRecoveryStatusStore
from agentic_mesh.work_item_recovery import RecoveryClassifier
from agentic_mesh.workers import WorkerAdapter
from agentic_mesh.workers import WorkerRunOutcome
from agentic_mesh.workers import resolve_worker_timeout_policy


class AgentRuntime:
    def __init__(
        self,
        message_store: FileMessageStore,
        artifact_store: ArtifactStore,
        journal: EventJournal,
        project: ProjectConfig,
        worker: WorkerAdapter,
        connector_outbox: FileConnectorOutbox | None = None,
        response_types: dict[str, ResponseTypeTemplate] | None = None,
    ) -> None:
        self.message_store = message_store
        self.artifact_store = artifact_store
        self.journal = journal
        self.project = project
        self.worker = worker
        self.connector_outbox = connector_outbox
        self.response_types = response_types or {}
        self.problem_status_store = ProblemStatusStore(
            message_store.root.parents[2],
            project.project_id,
        )
        self.recovery_status_store = FileRecoveryStatusStore(
            message_store.root.parents[2],
            project.project_id,
        )
        self.recovery_classifier = RecoveryClassifier()
        self.current_route_store = CurrentRouteStore(
            message_store.root.parents[2],
            project.project_id,
        )
        self.agent_run_state_store = FileAgentRunStateStore(
            message_store.root.parents[2],
            project.project_id,
        )
        self.notification_policy = NotificationPolicyEvaluator(
            project.notification_policy
        )
        self.notification_attempt_store = FileNotificationAttemptStore(
            message_store.root.parents[2],
            project.project_id,
        )
        self.human_gate_store = FileHumanGateRequestStore(
            message_store.root.parents[2],
            project.project_id,
        )

    def _status_link_builder(self) -> StatusLinkBuilder:
        return StatusLinkBuilder(base_url=self.project.control_plane.safe_external_base_url)

    def run_once(self, instance_id: str, instance_config) -> bool:
        message = self.message_store.claim_next(instance_config.role_id, instance_id)
        if message is None:
            return False
        self._write_run_state(
            instance_config=instance_config,
            message=message,
            run_state="starting",
            evidence_source="agent_run_started",
        )
        try:
            return self._run_claimed_message(instance_id, instance_config, message)
        except Exception as exc:
            self._write_run_state(
                instance_config=instance_config,
                message=message,
                run_state="failed",
                evidence_source="problem_status",
            )
            self.journal.append(
                "agent_run_failed",
                project_id=instance_config.project_id,
                role_id=instance_config.role_id,
                role_instance_id=instance_id,
                work_item_id=message.payload.get("work_item_id"),
                work_item_type=message.payload.get("work_item_type"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            self.message_store.complete(message, "failed")
            return True

    def _run_claimed_message(self, instance_id: str, instance_config, message: Message) -> bool:
        if message.type == MESSAGE_TYPE_DIRECT_CONVERSATION_REQUESTED:
            return self._run_direct_conversation(instance_id, instance_config, message)
        if message.type == MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED:
            return self._run_direct_directive(instance_id, instance_config, message)
        state_id = message.payload.get("lifecycle_state", self.project.flow.entry_state)
        flow_state = self.project.flow.states[state_id]
        if message.type == MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED:
            return self._handle_human_response_received(
                instance_id=instance_id,
                instance_config=instance_config,
                message=message,
                state_id=state_id,
            )
        if flow_state.owner_role != instance_config.role_id:
            raise ValueError(
                f"Message state {state_id} is owned by {flow_state.owner_role}, "
                f"not {instance_config.role_id}"
            )

        agent_attrs = telemetry.span_attributes(
            project_id=instance_config.project_id,
            role_id=instance_config.role_id,
            role_instance_id=instance_id,
            work_item_id=message.payload.get("work_item_id"),
            work_item_type=message.payload.get("work_item_type"),
            lifecycle_state=state_id,
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            worker_adapter=instance_config.override.worker.adapter,
            worker_model=instance_config.override.worker.model,
        )
        agent_started = time.perf_counter()
        with telemetry.start_span(
            "agent.run",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=agent_attrs,
        ) as agent_trace_context:
            message = replace(message, trace_context=agent_trace_context)
            message = self._with_runtime_instructions(
                message,
                flow_state,
                direct_work=False,
            )
            self.journal.append(
                "agent_run_started",
                project_id=instance_config.project_id,
                role_id=instance_config.role_id,
                role_instance_id=instance_id,
                work_item_id=message.payload.get("work_item_id"),
                work_item_type=message.payload.get("work_item_type"),
                lifecycle_state=state_id,
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                worker_adapter=instance_config.override.worker.adapter,
                worker_model=instance_config.override.worker.model,
            )
            self.journal.append(
                "local_trace_span",
                span_name="agent.run",
                project_id=instance_config.project_id,
                role_id=instance_config.role_id,
                role_instance_id=instance_id,
                work_item_id=message.payload.get("work_item_id"),
                lifecycle_state=state_id,
                correlation_id=message.correlation_id,
            )

            with telemetry.start_span(
                "worker.run",
                correlation_id=message.correlation_id,
                trace_context=message.trace_context,
                attributes=agent_attrs,
            ):
                self._write_run_state(
                    instance_config=instance_config,
                    message=message,
                    run_state="running",
                    evidence_source="worker_run_started",
                )
                worker_output = self.worker.run(instance_config, message, flow_state)
            result: AgentRunResult
            if isinstance(worker_output, WorkerRunOutcome):
                if worker_output.problem_status is not None:
                    self._write_run_state(
                        instance_config=instance_config,
                        message=message,
                        run_state=(
                            "timed_out"
                            if worker_output.problem_status.failure_class == "timeout"
                            else "failed"
                        ),
                        evidence_source="problem_status",
                        progress_observed_at=worker_output.progress_observed_at
                        or worker_output.problem_status.progress_observed_at,
                    )
                    self._record_problem_status(
                        worker_output.problem_status,
                        source_instance=instance_config,
                        source_message=message,
                    )
                    self.message_store.complete(message, "needs_runtime_recovery")
                    return True
                if worker_output.role_result is None:
                    raise ValueError("worker outcome did not include a role result")
                result = worker_output.role_result
            else:
                result = worker_output

            if result.status in {"blocked", "failed"}:
                work_item_id = str(
                    message.payload.get("work_item_id") or message.message_id
                )
                problem_status = role_problem_status(
                    status=result.status,
                    message=result.message,
                    project_id=self.project.project_id,
                    role_id=instance_config.role_id,
                    role_instance_id=instance_id,
                    work_item_id=work_item_id,
                    work_item_type=message.payload.get("work_item_type"),
                    lifecycle_state=state_id,
                    queue_item_id=message.payload.get("queue_item_id"),
                    source_message_id=message.message_id,
                    source_anchor=message.payload.get("source_anchor"),
                    correlation_id=message.correlation_id,
                    status_url=self._work_item_status_url(work_item_id),
                )
                self._record_problem_status(
                    problem_status,
                    source_instance=instance_config,
                    source_message=message,
                )
                self._write_run_state(
                    instance_config=instance_config,
                    message=message,
                    run_state="failed",
                    evidence_source="problem_status",
                )
                self.message_store.complete(message, result.status)
                return True

            try:
                for update in result.document_updates:
                    update = self._resolve_document_update_path(
                        update,
                        source_message=message,
                        flow_state=flow_state,
                        role_id=instance_config.role_id,
                    )
                    self.artifact_store.write_update(
                        update=update,
                        role_id=instance_config.role_id,
                        role_instance_id=instance_id,
                        correlation_id=message.correlation_id,
                        work_item_id=message.payload.get("work_item_id"),
                        work_item_type=message.payload.get("work_item_type"),
                        lifecycle_state=state_id,
                        trace_context=message.trace_context,
                    )
                    self._maintain_work_item_indexes(
                        update=update,
                        source_message=message,
                        flow_state=flow_state,
                        role_id=instance_config.role_id,
                        correlation_id=message.correlation_id,
                        trace_context=message.trace_context,
                    )
            except Exception:
                work_item_id = str(
                    message.payload.get("work_item_id") or message.message_id
                )
                problem_status = runtime_publication_problem_status(
                    reason="Runtime could not publish or index the role result artifacts.",
                    role_id=instance_config.role_id,
                    role_instance_id=instance_id,
                    message_payload=message.payload,
                    source_message_id=message.message_id,
                    correlation_id=message.correlation_id,
                    lifecycle_state=state_id,
                    status_url=self._work_item_status_url(work_item_id),
                )
                self._record_problem_status(
                    problem_status,
                    source_instance=instance_config,
                    source_message=message,
                )
                self._write_run_state(
                    instance_config=instance_config,
                    message=message,
                    run_state="failed",
                    evidence_source="problem_status",
                )
                self.message_store.complete(message, "needs_runtime_recovery")
                return True

            telemetry.record_duration(
                "agentic_mesh.agent.run.duration",
                time.perf_counter() - agent_started,
                agent_attrs,
            )
            self._clear_problem_status_if_present(
                work_item_id=message.payload.get("work_item_id"),
                role_id=instance_config.role_id,
                role_instance_id=instance_id,
                lifecycle_state=state_id,
                correlation_id=message.correlation_id,
            )

            if message.type == MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED:
                is_valid, validation_reason, request_record = (
                    self.human_gate_store.validate_response(
                        project_id=instance_config.project_id,
                        work_item_id=str(message.payload.get("work_item_id") or ""),
                        work_item_type=message.payload.get("work_item_type"),
                        lifecycle_state=state_id,
                        gate_id=str(message.payload.get("gate_id") or ""),
                        response_type=str(message.payload.get("response_type") or ""),
                        response_request_id=str(
                            message.payload.get("response_request_id") or ""
                        ),
                        responder=(
                            str(message.payload.get("responder"))
                            if message.payload.get("responder") is not None
                            else None
                        ),
                        response_value=message.payload.get("response_value"),
                        authenticated=bool(
                            message.payload.get("connector_origin_authenticated", False)
                        ),
                        authoritative=False,
                    )
                )
                response_attrs = telemetry.span_attributes(
                    **agent_attrs,
                    gate_id=message.payload.get("gate_id"),
                    response_request_id=message.payload.get("response_request_id"),
                    response_validation=validation_reason,
                )
                with telemetry.start_span(
                    "human_response.response.validate",
                    correlation_id=message.correlation_id,
                    trace_context=message.trace_context,
                    attributes=response_attrs,
                ):
                    event_type = (
                        "human_response_recorded"
                        if is_valid
                        else "human_response_invalid"
                    )
                    self.journal.append(
                        event_type,
                        project_id=instance_config.project_id,
                        role_id=instance_config.role_id,
                        role_instance_id=instance_id,
                        work_item_id=message.payload.get("work_item_id"),
                        work_item_type=message.payload.get("work_item_type"),
                        lifecycle_state=state_id,
                        gate_id=message.payload.get("gate_id"),
                        response_request_id=message.payload.get("response_request_id"),
                        approval_request_id=(
                            request_record.approval_request_id
                            if request_record
                            else message.payload.get("approval_request_id")
                        ),
                        response_type=message.payload.get("response_type"),
                        response_status=(
                            request_record.status if request_record else "unknown"
                        ),
                        validation_reason=validation_reason,
                        correlation_id=message.correlation_id,
                    )
                    if is_valid:
                        self._enqueue_human_response_continuation(
                            source_instance=instance_config,
                            source_message=message,
                            state_id=state_id,
                            request_record=request_record,
                        )
                    self.message_store.complete(
                        message,
                        "human_response_recorded" if is_valid else "invalid_human_response",
                    )
                self._write_run_state(
                    instance_config=instance_config,
                    message=message,
                    run_state="completed",
                    evidence_source="worker_completed",
                )
                return True

            if result.status == "needs_clarification":
                clarification_gate = self._sponsor_clarification_gate(
                    result=result,
                    source_message=message,
                )
                if self._request_human_responses(
                    gates=[clarification_gate],
                    source_message=message,
                    source_instance=instance_config,
                    flow_state=flow_state,
                    trace_attributes=agent_attrs,
                ):
                    self.message_store.complete(
                        message,
                        "waiting_for_human_response",
                        result_message=result.message,
                    )
                    self._write_run_state(
                        instance_config=instance_config,
                        message=message,
                        run_state="completed",
                        evidence_source="worker_completed",
                    )
                    return True
                self.message_store.complete(
                    message,
                    "needs_clarification",
                    result_message=result.message,
                )
                self._write_run_state(
                    instance_config=instance_config,
                    message=message,
                    run_state="failed",
                    evidence_source="worker_completed",
                )
                return True

            human_gates = [
                gate
                for gate in flow_state.gates
                if gate.type == "human_response"
                and not self._human_gate_satisfied_by_message(gate, message)
            ]
            if human_gates:
                requested = self._request_human_responses(
                    gates=human_gates,
                    source_message=message,
                    source_instance=instance_config,
                    flow_state=flow_state,
                    trace_attributes=agent_attrs,
                )
                self.message_store.complete(
                    message,
                    "waiting_for_human_response"
                    if requested
                    else "human_response_request_failed",
                )
                self._write_run_state(
                    instance_config=instance_config,
                    message=message,
                    run_state="completed" if requested else "failed",
                    evidence_source="worker_completed",
                )
                return True

            routes = list(result.routes) + [
                RouteRequest(
                    target_role=handoff.target_role,
                    message_type=handoff.message_type,
                    payload=handoff.payload,
                    origin="handoff",
                )
                for handoff in result.handoffs
            ]
            if not routes and result.status == "completed":
                completed_handoff = flow_state.handoffs.get("completed")
                if completed_handoff is not None:
                    routes.append(
                        RouteRequest(
                            target_role=completed_handoff.target_role,
                            message_type=completed_handoff.message_type,
                            payload={},
                            origin="implicit_completed_handoff",
                        )
                    )
                    self.journal.append(
                        "implicit_completed_handoff_created",
                        project_id=instance_config.project_id,
                        role_id=instance_config.role_id,
                        role_instance_id=instance_id,
                        work_item_id=message.payload.get("work_item_id"),
                        work_item_type=message.payload.get("work_item_type"),
                        lifecycle_state=state_id,
                        target_role=completed_handoff.target_role,
                        target_lifecycle_state=completed_handoff.target_state,
                        message_type=completed_handoff.message_type,
                        message_id=message.message_id,
                        correlation_id=message.correlation_id,
                        reason="worker_completed_non_terminal_state_without_route",
                    )
            for route_request in routes:
                try:
                    normalized = self._normalise_route(
                        route=route_request,
                        source_message=message,
                        source_instance=instance_config,
                        flow_state=flow_state,
                    )
                    if not isinstance(normalized, ProblemStatus):
                        self._deliver_route(
                            route=normalized,
                            source_instance=instance_config,
                            source_message=message,
                            source_lifecycle_state=state_id,
                            trace_attributes=agent_attrs,
                        )
                        continue
                except Exception as exc:
                    self.journal.append(
                        "route_delivery_failed",
                        project_id=instance_config.project_id,
                        role_id=instance_config.role_id,
                        role_instance_id=instance_id,
                        work_item_id=message.payload.get("work_item_id"),
                        work_item_type=message.payload.get("work_item_type"),
                        lifecycle_state=state_id,
                        target_role=route_request.target_role,
                        message_type=route_request.message_type,
                        correlation_id=message.correlation_id,
                        failure_type=type(exc).__name__,
                        failure_message=str(exc),
                    )
                    normalized = self._malformed_route_problem(
                        route=route_request,
                        source_message=message,
                        source_instance=instance_config,
                        flow_state=flow_state,
                        reason=(
                            "Runtime failed while validating or delivering a route: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                if isinstance(normalized, ProblemStatus):
                    self._record_problem_status(
                        normalized,
                        source_instance=instance_config,
                        source_message=message,
                    )
                    self.message_store.complete(message, "needs_runtime_recovery")
                    self._write_run_state(
                        instance_config=instance_config,
                        message=message,
                        run_state="failed",
                        evidence_source="problem_status",
                    )
                    return True

            self.message_store.complete(
                message,
                result.status,
                result_message=result.message,
            )
            self._write_run_state(
                instance_config=instance_config,
                message=message,
                run_state="completed",
                evidence_source="worker_completed",
            )
            return True

    def _handle_human_response_received(
        self,
        *,
        instance_id: str,
        instance_config,
        message: Message,
        state_id: str,
    ) -> bool:
        authoritative = str(message.source or "").startswith("teams:")
        is_valid, validation_reason, request_record = (
            self.human_gate_store.validate_response(
                project_id=instance_config.project_id,
                work_item_id=str(message.payload.get("work_item_id") or ""),
                work_item_type=message.payload.get("work_item_type"),
                lifecycle_state=state_id,
                gate_id=str(message.payload.get("gate_id") or ""),
                response_type=str(message.payload.get("response_type") or ""),
                response_request_id=str(message.payload.get("response_request_id") or ""),
                responder=(
                    str(message.payload.get("responder"))
                    if message.payload.get("responder") is not None
                    else None
                ),
                response_value=message.payload.get("response_value"),
                authenticated=bool(
                    message.payload.get("connector_origin_authenticated", False)
                ),
                authoritative=authoritative,
            )
        )
        if validation_reason == "duplicate_same_value":
            event_type = "human_response_duplicate"
        elif validation_reason == "not_approved":
            event_type = "human_response_not_approved"
        elif is_valid:
            event_type = "human_response_recorded"
        else:
            event_type = "human_response_invalid"
        self.journal.append(
            event_type,
            project_id=instance_config.project_id,
            role_id=instance_config.role_id,
            role_instance_id=instance_id,
            work_item_id=message.payload.get("work_item_id"),
            work_item_type=message.payload.get("work_item_type"),
            lifecycle_state=state_id,
            gate_id=message.payload.get("gate_id"),
            response_request_id=message.payload.get("response_request_id"),
            approval_request_id=(
                request_record.approval_request_id
                if request_record
                else message.payload.get("approval_request_id")
            ),
            response_type=message.payload.get("response_type"),
            response_status=(request_record.status if request_record else "unknown"),
            validation_result=validation_reason,
            validation_reason=validation_reason,
            correlation_id=message.correlation_id,
        )
        satisfied = is_valid and validation_reason != "duplicate_same_value"
        if satisfied:
            self._enqueue_human_response_continuation(
                source_instance=instance_config,
                source_message=message,
                state_id=state_id,
                request_record=request_record,
            )
        self.message_store.complete(
            message,
            "human_response_recorded" if satisfied else "human_response_not_satisfied",
        )
        self._write_run_state(
            instance_config=instance_config,
            message=message,
            run_state="completed" if satisfied else "failed",
            evidence_source="journal_event",
        )
        return True

    def _human_gate_satisfied_by_message(
        self,
        gate: FlowGate,
        message: Message,
    ) -> bool:
        human_response = message.payload.get("human_response")
        if not isinstance(human_response, dict):
            return False
        if human_response.get("gate_id") != gate.gate_id:
            return False
        status = str(human_response.get("response_status") or "").casefold()
        if status and status != "completed":
            return False
        accepted_values = gate.completion_criteria.get("accepted_values")
        if not accepted_values:
            return True
        response_value = str(human_response.get("response_value") or "")
        return response_value in {str(value) for value in accepted_values}

    def _enqueue_human_response_continuation(
        self,
        *,
        source_instance,
        source_message: Message,
        state_id: str,
        request_record,
    ) -> None:
        flow_state = self.project.flow.states.get(state_id)
        target_role = flow_state.owner_role if flow_state else source_instance.role_id
        gate_id = str(source_message.payload.get("gate_id") or "")
        work_item_id = str(source_message.payload.get("work_item_id") or "")
        payload = {
            "title": f"Continue {state_id} after human response",
            "summary": (
                "A required human response has been recorded. Continue this "
                "lifecycle state and update the artifact with the final "
                "decision, release action, evidence, and closure outcome."
            ),
            "work_item_id": work_item_id,
            "work_item_type": source_message.payload.get("work_item_type") or "slice",
            "lifecycle_state": state_id,
            "previous_lifecycle_state": state_id,
            "human_response": {
                "gate_id": gate_id,
                "response_request_id": source_message.payload.get(
                    "response_request_id"
                ),
                "approval_request_id": (
                    request_record.approval_request_id
                    if request_record
                    else source_message.payload.get("approval_request_id")
                ),
                "responder": source_message.payload.get("responder"),
                "response_value": source_message.payload.get("response_value"),
                "response_status": request_record.status if request_record else None,
            },
            "continuation_reason": "human_response_recorded",
            "source_human_response_message_id": source_message.message_id,
        }
        continuation = Message.create(
            role_id=target_role,
            message_type=f"sdlc.{state_id}",
            payload=payload,
            source=source_instance.instance_id,
            correlation_id=source_message.correlation_id,
            trace_context=source_message.trace_context,
        )
        delivered = self.message_store.enqueue(continuation)
        self.journal.append(
            "human_response_continuation_enqueued",
            project_id=source_instance.project_id,
            role_id=source_instance.role_id,
            role_instance_id=source_instance.instance_id,
            work_item_id=work_item_id,
            work_item_type=payload["work_item_type"],
            lifecycle_state=state_id,
            gate_id=gate_id,
            source_message_id=source_message.message_id,
            message_id=delivered.message_id,
            target_role=target_role,
            message_type=continuation.type,
            correlation_id=source_message.correlation_id,
        )

    def _sponsor_clarification_gate(
        self,
        *,
        result: AgentRunResult,
        source_message: Message,
    ) -> FlowGate:
        prompt = result.message.strip() or (
            "Please answer the open sponsor clarification questions for this work item."
        )
        channel = str(source_message.payload.get("source_channel") or "all-agents")
        return FlowGate(
            gate_id="sponsor_clarification_response",
            type="human_response",
            response_type="multiline_text",
            prompt=prompt,
            requested_from="sponsor",
            channel=channel,
            timeout="PT48H",
            on_timeout="escalate",
            completion_criteria={},
        )

    def _request_human_responses(
        self,
        *,
        gates: list[FlowGate],
        source_message: Message,
        source_instance,
        flow_state: FlowState,
        trace_attributes: dict,
    ) -> bool:
        any_enqueued = False
        for gate in gates:
            try:
                gate_request, created_request = (
                    self.human_gate_store.ensure_request(
                        work_item_id=str(
                            source_message.payload.get("work_item_id")
                            or source_message.message_id
                        ),
                        work_item_type=source_message.payload.get("work_item_type"),
                        lifecycle_state=flow_state.state_id,
                        gate=gate,
                    )
                )
            except Exception:
                self.journal.append(
                    "human_response_request_failed",
                    project_id=source_instance.project_id,
                    role_id=source_instance.role_id,
                    role_instance_id=source_instance.instance_id,
                    work_item_id=source_message.payload.get("work_item_id"),
                    work_item_type=source_message.payload.get("work_item_type"),
                    lifecycle_state=flow_state.state_id,
                    gate_id=gate.gate_id,
                    response_type=gate.response_type,
                    correlation_id=source_message.correlation_id,
                    reason="approval_request_persistence_failed",
                )
                continue
            if self.connector_outbox is None:
                try:
                    self.human_gate_store.mark_failed(
                        gate_request.response_request_id,
                        reason="approval_request_outbox_unavailable",
                    )
                except HumanGateStoreError:
                    pass
                self.journal.append(
                    "human_response_request_unroutable",
                    project_id=source_instance.project_id,
                    role_id=source_instance.role_id,
                    role_instance_id=source_instance.instance_id,
                    work_item_id=source_message.payload.get("work_item_id"),
                    work_item_type=source_message.payload.get("work_item_type"),
                    lifecycle_state=flow_state.state_id,
                    gate_id=gate.gate_id,
                    response_request_id=gate_request.response_request_id,
                    response_type=gate.response_type,
                    correlation_id=source_message.correlation_id,
                    reason="approval_request_outbox_unavailable",
                )
                continue
            if not created_request and gate_request.status == "waiting_for_response":
                self.journal.append(
                    "human_response_request_reused",
                    project_id=source_instance.project_id,
                    role_id=source_instance.role_id,
                    role_instance_id=source_instance.instance_id,
                    work_item_id=source_message.payload.get("work_item_id"),
                    work_item_type=source_message.payload.get("work_item_type"),
                    lifecycle_state=flow_state.state_id,
                    gate_id=gate.gate_id,
                    response_request_id=gate_request.response_request_id,
                    response_type=gate.response_type,
                    correlation_id=source_message.correlation_id,
                )
                any_enqueued = True
                continue

            with telemetry.start_span(
                "human_response.request.ensure",
                correlation_id=source_message.correlation_id,
                trace_context=source_message.trace_context,
                attributes=telemetry.span_attributes(
                    **trace_attributes,
                    gate_id=gate.gate_id,
                    response_type=gate.response_type,
                    channel=gate.channel,
                ),
            ) as response_trace_context:
                request = build_human_response_request(
                    gate=gate,
                    response_type=(
                        self.response_types.get(gate.response_type)
                        if gate.response_type
                        else None
                    ),
                    source_message=replace(
                        source_message,
                        trace_context=response_trace_context,
                    ),
                    source_instance=source_instance,
                    flow_state=flow_state,
                    approval_context=self._build_approval_context(
                        source_message=source_message,
                        flow_state=flow_state,
                    ),
                    response_request_id=gate_request.response_request_id,
                    approval_request_id=gate_request.approval_request_id
                    or gate_request.response_request_id,
                    notification_attempt_id=(
                        gate_request.current_notification_attempt_id
                    ),
                )
                try:
                    request = self.connector_outbox.enqueue(request)
                    decision_context = request.payload.get("approval_decision_view")
                    if isinstance(decision_context, dict):
                        self.human_gate_store.record_decision_context(
                            gate_request.response_request_id,
                            decision_context=decision_context,
                        )
                        self.journal.append(
                            "approval_decision_context_created",
                            project_id=source_instance.project_id,
                            role_id=source_instance.role_id,
                            role_instance_id=source_instance.instance_id,
                            work_item_id=source_message.payload.get("work_item_id"),
                            work_item_type=source_message.payload.get("work_item_type"),
                            lifecycle_state=flow_state.state_id,
                            gate_id=gate.gate_id,
                            response_request_id=gate_request.response_request_id,
                            approval_request_id=gate_request.approval_request_id
                            or gate_request.response_request_id,
                            context_completeness=decision_context.get(
                                "context_completeness",
                            ),
                            correlation_id=source_message.correlation_id,
                        )
                    self.human_gate_store.mark_enqueue_succeeded(
                        gate_request.response_request_id,
                        connector_message_id=request.message_id,
                    )
                except Exception:
                    self.human_gate_store.mark_failed(
                        gate_request.response_request_id,
                        reason="approval_request_enqueue_failed",
                    )
                    self.journal.append(
                        "human_response_request_failed",
                        project_id=source_instance.project_id,
                        role_id=source_instance.role_id,
                        role_instance_id=source_instance.instance_id,
                        work_item_id=source_message.payload.get("work_item_id"),
                        work_item_type=source_message.payload.get("work_item_type"),
                        lifecycle_state=flow_state.state_id,
                        gate_id=gate.gate_id,
                        response_request_id=gate_request.response_request_id,
                        response_type=gate.response_type,
                        channel=gate.channel,
                        correlation_id=source_message.correlation_id,
                        reason="approval_request_enqueue_failed",
                    )
                    continue
                any_enqueued = True
                self.journal.append(
                    "human_response_requested",
                    project_id=source_instance.project_id,
                    role_id=source_instance.role_id,
                    role_instance_id=source_instance.instance_id,
                    work_item_id=source_message.payload.get("work_item_id"),
                    work_item_type=source_message.payload.get("work_item_type"),
                    lifecycle_state=flow_state.state_id,
                    gate_id=gate.gate_id,
                    response_request_id=request.payload["response_request_id"],
                    approval_request_id=request.payload.get("approval_request_id"),
                    notification_attempt_id=request.payload.get(
                        "notification_attempt_id"
                    ),
                    response_type=gate.response_type,
                    channel=request.channel,
                    connector_message_id=request.message_id,
                    correlation_id=source_message.correlation_id,
                )
        return any_enqueued

    def _write_run_state(
        self,
        *,
        instance_config,
        message: Message,
        run_state: str,
        evidence_source: str,
        progress_observed_at: str | None = None,
    ) -> None:
        timeout_policy = resolve_worker_timeout_policy(instance_config)
        state = AgentRunState.from_claim(
            instance=instance_config,
            message=message,
            run_state=run_state,
            timeout_seconds=timeout_policy["timeout_seconds"],
            progress_window_seconds=timeout_policy["progress_window_seconds"],
            evidence_source=evidence_source,
        )
        if progress_observed_at:
            state = replace(
                state,
                last_progress_at=progress_observed_at,
                safe_progress_event_count=max(1, state.safe_progress_event_count),
                last_safe_evidence_source="worker_progress",
        )
        self.agent_run_state_store.write_current(state)

    def _normalise_handoff(
        self,
        *,
        handoff,
        source_message: Message,
        flow_state: FlowState,
    ):
        route = self._normalise_route(
            route=RouteRequest(
                target_role=handoff.target_role,
                message_type=handoff.message_type,
                payload=handoff.payload,
                origin="handoff",
            ),
            source_message=source_message,
            source_instance=None,
            flow_state=flow_state,
        )
        if isinstance(route, ProblemStatus):
            return route
        return replace(handoff, payload=route.to_dict())

    def _normalise_route(
        self,
        *,
        route: RouteRequest,
        source_message: Message,
        source_instance,
        flow_state: FlowState,
    ) -> CurrentRoute | ProblemStatus:
        payload = dict(route.payload)
        configured_route_id: str | None = None
        route_kind: str | None = None
        route_status: str | None = None
        target_state = payload.get("lifecycle_state")

        handoff_transition = next(
            (
                (status, candidate)
                for status, candidate in flow_state.handoffs.items()
                if candidate.target_role == route.target_role
                and candidate.message_type == route.message_type
                and target_state in {None, "", candidate.target_state}
            ),
            None,
        )
        if handoff_transition is None and isinstance(target_state, str) and target_state:
            handoff_transition = next(
                (
                    (status, candidate)
                    for status, candidate in flow_state.handoffs.items()
                    if candidate.target_role == route.target_role
                    and target_state == candidate.target_state
                ),
                None,
            )
        if handoff_transition is not None:
            configured_route_id, transition = handoff_transition
            target_state = transition.target_state
            route = replace(route, message_type=transition.message_type)
            route_kind = "configured_handoff"
            route_status = "handoff_requested"

        consult_transition = None
        if route_kind is None:
            consult_transition = next(
                (
                    (consult_id, candidate)
                    for consult_id, candidate in flow_state.consults.items()
                    if candidate.target_role == route.target_role
                    and candidate.message_type == route.message_type
                    and target_state == candidate.target_state
                ),
                None,
            )
        if consult_transition is not None:
            configured_route_id, consult = consult_transition
            target_state = consult.target_state
            if self._is_correction_route(payload):
                route_kind = "configured_correction"
                route_status = "correction_requested"
                payload.setdefault("correction_status", "correction_requested")
            else:
                route_kind = "configured_consult"
                route_status = "consult_requested"

        if route_kind is None:
            if not isinstance(target_state, str) or not target_state:
                return self._malformed_route_problem(
                    route=route,
                    source_message=source_message,
                    source_instance=source_instance,
                    flow_state=flow_state,
                    reason=(
                        "Worker emitted an unconfigured route without a target "
                        "lifecycle_state."
                    ),
                )
            if target_state not in self.project.flow.states:
                return self._malformed_route_problem(
                    route=route,
                    source_message=source_message,
                    source_instance=source_instance,
                    flow_state=flow_state,
                    reason="Worker emitted a route to an unknown lifecycle_state.",
                    attempted_lifecycle_state=str(target_state),
                )
            target_flow_state = self.project.flow.states[target_state]
            if target_flow_state.owner_role != route.target_role:
                return self._malformed_route_problem(
                    route=route,
                    source_message=source_message,
                    source_instance=source_instance,
                    flow_state=flow_state,
                    reason="Worker emitted a route to a lifecycle state owned by a different role.",
                    attempted_lifecycle_state=str(target_state),
                    expected_owner=target_flow_state.owner_role,
                )
            reason = payload.get("out_of_flow_reason")
            if not isinstance(reason, str) or not reason.strip():
                return self._malformed_route_problem(
                    route=route,
                    source_message=source_message,
                    source_instance=source_instance,
                    flow_state=flow_state,
                    reason="Worker emitted an unconfigured route without out_of_flow_reason.",
                    attempted_lifecycle_state=str(target_state),
                    expected_owner=target_flow_state.owner_role,
                )
            route_kind = "reasoned_out_of_flow"
            route_status = "out_of_flow_requested"
            payload["out_of_flow"] = True

        payload.setdefault("title", source_message.payload.get("title"))
        payload.setdefault("summary", source_message.payload.get("summary"))
        payload["work_item_id"] = source_message.payload.get("work_item_id")
        payload["work_item_type"] = source_message.payload.get("work_item_type")
        if source_message.payload.get("queue_item_id"):
            payload["queue_item_id"] = source_message.payload.get("queue_item_id")
        if source_message.payload.get("source_anchor"):
            payload["source_anchor"] = source_message.payload.get("source_anchor")
        payload["previous_lifecycle_state"] = flow_state.state_id
        payload["lifecycle_state"] = target_state
        payload["source_message_id"] = source_message.message_id
        payload["out_of_flow"] = bool(payload.get("out_of_flow"))
        route_id = route_id_for(
            source_message_id=source_message.message_id,
            target_role=route.target_role,
            message_type=route.message_type,
            target_lifecycle_state=str(target_state),
            payload=payload,
        )
        payload["route_id"] = route_id
        payload["route_kind"] = route_kind
        payload["route_status"] = route_status
        if configured_route_id:
            payload["configured_route_id"] = configured_route_id
        current_route = current_route_from_payload(
            work_item_id=str(
                source_message.payload.get("work_item_id")
                or source_message.message_id
            ),
            work_item_type=source_message.payload.get("work_item_type"),
            queue_item_id=source_message.payload.get("queue_item_id"),
            source_message_id=source_message.message_id,
            source_anchor=source_message.payload.get("source_anchor"),
            correlation_id=source_message.correlation_id,
            route_id=route_id,
            route_kind=str(route_kind),
            route_status=str(route_status),
            configured_route_id=configured_route_id,
            source_role=source_message.role_id,
            target_role=route.target_role,
            source_lifecycle_state=flow_state.state_id,
            target_lifecycle_state=str(target_state),
            message_type=route.message_type,
            payload=payload,
            out_of_flow=bool(payload.get("out_of_flow")),
            status_url=self._work_item_status_url(
                str(source_message.payload.get("work_item_id") or source_message.message_id)
            ),
        )
        object.__setattr__(current_route, "_target_payload", payload)
        return current_route

    @staticmethod
    def _is_correction_route(payload: dict[str, object]) -> bool:
        correction_keys = {
            "defect_id",
            "required_change",
            "evidence_required",
            "gate_id",
        }
        if payload.get("correction_status") == "correction_requested":
            return True
        if payload.get("review_status") == "changes_requested":
            return True
        return any(payload.get(key) for key in correction_keys)

    def _malformed_route_problem(
        self,
        *,
        route: RouteRequest,
        source_message: Message,
        source_instance,
        flow_state: FlowState,
        reason: str,
        attempted_lifecycle_state: str | None = None,
        expected_owner: str | None = None,
    ) -> ProblemStatus:
        role_instance_id = source_instance.instance_id if source_instance else None
        return malformed_route_problem_status(
            failure_class=(
                "malformed_handoff" if route.origin == "handoff" else "malformed_route"
            ),
            reason=reason,
            role_id=source_message.role_id,
            role_instance_id=role_instance_id,
            message_payload=source_message.payload,
            source_message_id=source_message.message_id,
            correlation_id=source_message.correlation_id,
            lifecycle_state=flow_state.state_id,
            attempted_target_role=route.target_role,
            attempted_message_type=route.message_type,
            attempted_lifecycle_state=attempted_lifecycle_state
            or route.payload.get("lifecycle_state"),
            expected_owner=expected_owner,
            route_validation_errors=(reason,),
            status_url=self._work_item_status_url(
                str(source_message.payload.get("work_item_id") or source_message.message_id)
            ),
        )

    def _deliver_route(
        self,
        *,
        route: CurrentRoute,
        source_instance,
        source_message: Message,
        source_lifecycle_state: str,
        trace_attributes: dict[str, object],
    ) -> None:
        existing = self.current_route_store.read_current(route.work_item_id)
        if existing and existing.get("route_id") == route.route_id and existing.get("delivered_message_id"):
            self.journal.append(
                "route_delivery_deduplicated",
                project_id=self.project.project_id,
                **route.journal_fields(),
            )
            return
        self.current_route_store.write_current(route)
        self.journal.append(
            "current_route_recorded",
            project_id=self.project.project_id,
            **route.journal_fields(),
        )
        payload = dict(getattr(route, "_target_payload"))
        span_name = "handoff.emit" if route.route_kind == "configured_handoff" else "route.emit"
        with telemetry.start_span(
            span_name,
            correlation_id=source_message.correlation_id,
            trace_context=source_message.trace_context,
            attributes=telemetry.span_attributes(
                **trace_attributes,
                target_role=route.target_role,
                source_lifecycle_state=source_lifecycle_state,
                target_lifecycle_state=route.target_lifecycle_state,
                route_id=route.route_id,
                route_kind=route.route_kind,
                route_status=route.route_status,
            ),
        ) as route_trace_context:
            target_message = Message.create(
                role_id=route.target_role,
                message_type=route.message_type,
                payload=payload,
                source=source_instance.instance_id,
                correlation_id=source_message.correlation_id,
                trace_context=route_trace_context,
            )
            self.journal.append(
                "route_emitted",
                project_id=self.project.project_id,
                role_id=source_instance.role_id,
                role_instance_id=source_instance.instance_id,
                target_role=route.target_role,
                source_lifecycle_state=source_lifecycle_state,
                target_lifecycle_state=route.target_lifecycle_state,
                route_id=route.route_id,
                route_kind=route.route_kind,
                route_status=route.route_status,
                configured_route_id=route.configured_route_id,
                out_of_flow=route.out_of_flow,
                work_item_id=route.work_item_id,
                message_id=target_message.message_id,
                correlation_id=source_message.correlation_id,
            )
            target_message = self.message_store.enqueue(target_message)
            delivered_route = replace(route, delivered_message_id=target_message.message_id)
            self.current_route_store.write_current(delivered_route)
            if route.route_kind == "configured_handoff":
                self.journal.append(
                    "handoff_emitted",
                    project_id=self.project.project_id,
                    role_id=source_instance.role_id,
                    role_instance_id=source_instance.instance_id,
                    target_role=route.target_role,
                    source_lifecycle_state=source_lifecycle_state,
                    target_lifecycle_state=route.target_lifecycle_state,
                    out_of_flow=route.out_of_flow,
                    out_of_flow_reason=route.out_of_flow_reason,
                    work_item_id=route.work_item_id,
                    message_id=target_message.message_id,
                    correlation_id=source_message.correlation_id,
                )
            self._queue_route_connector_message(
                source_instance=source_instance,
                source_message=source_message,
                route=delivered_route,
            )
            self.journal.append(
                "message_routed",
                project_id=self.project.project_id,
                source_role=source_instance.role_id,
                target_role=route.target_role,
                source_lifecycle_state=source_lifecycle_state,
                target_lifecycle_state=route.target_lifecycle_state,
                out_of_flow=route.out_of_flow,
                out_of_flow_reason=route.out_of_flow_reason,
                route_id=route.route_id,
                route_kind=route.route_kind,
                route_status=route.route_status,
                work_item_id=route.work_item_id,
                message_id=target_message.message_id,
                correlation_id=source_message.correlation_id,
            )

    def _run_direct_directive(self, instance_id: str, instance_config, message: Message) -> bool:
        direct_state = FlowState(
            state_id=str(message.payload.get("work_mode") or "direct_instruction"),
            owner_role=instance_config.role_id,
            purpose=(
                "Direct sponsor instruction addressed to this role. "
                "Use the loaded project context and lifecycle flow as reference only."
            ),
            artifact_path=str(
                message.payload.get("output_path")
                or f"documents/analysis/{instance_config.role_id}.md"
            ),
            handoffs={},
            consults={},
            gates=[],
        )
        attrs = telemetry.span_attributes(
            project_id=instance_config.project_id,
            role_id=instance_config.role_id,
            role_instance_id=instance_id,
            work_item_id=message.payload.get("work_item_id"),
            work_item_type=message.payload.get("work_item_type"),
            lifecycle_state=direct_state.state_id,
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            worker_adapter=instance_config.override.worker.adapter,
            worker_model=instance_config.override.worker.model,
        )
        started = time.perf_counter()
        with telemetry.start_span(
            "agent.run",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=attrs,
        ) as trace_context:
            message = replace(message, trace_context=trace_context)
            message = self._with_runtime_instructions(
                message,
                direct_state,
                direct_work=True,
            )
            self.journal.append(
                "agent_directive_run_started",
                project_id=instance_config.project_id,
                role_id=instance_config.role_id,
                role_instance_id=instance_id,
                work_item_id=message.payload.get("work_item_id"),
                work_item_type=message.payload.get("work_item_type"),
                work_mode=message.payload.get("work_mode"),
                message_id=message.message_id,
                correlation_id=message.correlation_id,
            )
            self._queue_directive_status_connector_message(
                source_instance=instance_config,
                source_message=message,
                status="started",
                status_message=(
                    "Instruction received. Running direct role work outside the "
                    "lifecycle flow."
                ),
                artifact_paths=[],
            )
            with telemetry.start_span(
                "worker.run",
                correlation_id=message.correlation_id,
                trace_context=message.trace_context,
                attributes=attrs,
            ):
                worker_output = self.worker.run(instance_config, message, direct_state)
            if isinstance(worker_output, WorkerRunOutcome):
                if worker_output.problem_status is not None:
                    self._record_problem_status(
                        worker_output.problem_status,
                        source_instance=instance_config,
                        source_message=message,
                    )
                    self._queue_directive_status_connector_message(
                        source_instance=instance_config,
                        source_message=message,
                        status="needs_runtime_recovery",
                        status_message=worker_output.problem_status.reason_summary,
                        artifact_paths=[],
                    )
                    self.message_store.complete(message, "needs_runtime_recovery")
                    self._queue_directive_publish_ready_if_complete(
                        source_instance=instance_config,
                        source_message=message,
                    )
                    return True
                if worker_output.role_result is None:
                    raise ValueError("worker outcome did not include a role result")
                result = worker_output.role_result
            else:
                result = worker_output
            if result.status in {"blocked", "failed"}:
                work_item_id = str(
                    message.payload.get("work_item_id") or message.message_id
                )
                self._record_problem_status(
                    role_problem_status(
                        status=result.status,
                        message=result.message,
                        project_id=self.project.project_id,
                        role_id=instance_config.role_id,
                        role_instance_id=instance_id,
                        work_item_id=work_item_id,
                        work_item_type=message.payload.get("work_item_type"),
                        lifecycle_state=direct_state.state_id,
                        queue_item_id=message.payload.get("queue_item_id"),
                        source_message_id=message.message_id,
                        source_anchor=message.payload.get("source_anchor"),
                        correlation_id=message.correlation_id,
                        status_url=self._work_item_status_url(work_item_id),
                    ),
                    source_instance=instance_config,
                    source_message=message,
                )
            document_updates = (
                result.document_updates if result.status == "completed" else []
            )
            for update in document_updates:
                update = self._resolve_document_update_path(
                    update,
                    source_message=message,
                    flow_state=direct_state,
                    role_id=instance_config.role_id,
                )
                self.artifact_store.write_update(
                    update=update,
                    role_id=instance_config.role_id,
                    role_instance_id=instance_id,
                    correlation_id=message.correlation_id,
                    work_item_id=message.payload.get("work_item_id"),
                    work_item_type=message.payload.get("work_item_type"),
                    lifecycle_state=direct_state.state_id,
                    trace_context=message.trace_context,
                )
            telemetry.record_duration(
                "agentic_mesh.agent.run.duration",
                time.perf_counter() - started,
                attrs,
            )
            if result.status == "completed":
                self._clear_problem_status_if_present(
                    work_item_id=message.payload.get("work_item_id"),
                    role_id=instance_config.role_id,
                    role_instance_id=instance_id,
                    lifecycle_state=direct_state.state_id,
                    correlation_id=message.correlation_id,
                )
            for handoff in result.handoffs:
                self.journal.append(
                    "directive_handoff_ignored",
                    project_id=instance_config.project_id,
                    role_id=instance_config.role_id,
                    role_instance_id=instance_id,
                    target_role=handoff.target_role,
                    work_item_id=message.payload.get("work_item_id"),
                    correlation_id=message.correlation_id,
                    reason="direct_broadcast_does_not_emit_handoffs",
                )
            self._queue_directive_status_connector_message(
                source_instance=instance_config,
                source_message=message,
                status=result.status,
                status_message=result.message,
                artifact_paths=[update.path for update in document_updates],
            )
            self.message_store.complete(message, result.status)
            self._queue_directive_publish_ready_if_complete(
                source_instance=instance_config,
                source_message=message,
            )
            return True

    def _run_direct_conversation(self, instance_id: str, instance_config, message: Message) -> bool:
        direct_state = FlowState(
            state_id="direct_conversation",
            owner_role=instance_config.role_id,
            purpose=(
                "Direct conversation addressed to this role. Answer in role. "
                "Propose tracked work only when the conversation crosses the "
                "conversation-to-work boundary."
            ),
            artifact_path="",
            handoffs={},
            consults={},
            gates=[],
        )
        attrs = telemetry.span_attributes(
            project_id=instance_config.project_id,
            role_id=instance_config.role_id,
            role_instance_id=instance_id,
            lifecycle_state=direct_state.state_id,
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            worker_adapter=instance_config.override.worker.adapter,
            worker_model=instance_config.override.worker.model,
        )
        started = time.perf_counter()
        with telemetry.start_span(
            "agent.run",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=attrs,
        ) as trace_context:
            message = replace(message, trace_context=trace_context)
            message = self._with_runtime_instructions(
                message,
                direct_state,
                direct_work=True,
            )
            self.journal.append(
                "agent_conversation_run_started",
                project_id=instance_config.project_id,
                role_id=instance_config.role_id,
                role_instance_id=instance_id,
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                conversation_mode=message.payload.get("conversation_mode"),
            )
            self._queue_direct_conversation_status_connector_message(
                source_instance=instance_config,
                source_message=message,
                status="started",
                status_message="Message received. Preparing an in-role reply.",
            )
            with telemetry.start_span(
                "worker.run",
                correlation_id=message.correlation_id,
                trace_context=message.trace_context,
                attributes=attrs,
            ):
                worker_output = self.worker.run(instance_config, message, direct_state)
            if isinstance(worker_output, WorkerRunOutcome):
                if worker_output.problem_status is not None:
                    status_message = worker_output.problem_status.reason_summary
                    self._queue_direct_conversation_status_connector_message(
                        source_instance=instance_config,
                        source_message=message,
                        status="needs_runtime_recovery",
                        status_message=status_message,
                    )
                    self.message_store.complete(
                        message,
                        "needs_runtime_recovery",
                        result_message=status_message,
                    )
                    return True
                if worker_output.role_result is None:
                    raise ValueError("worker outcome did not include a role result")
                result = worker_output.role_result
            else:
                result = worker_output
            if result.document_updates:
                self.journal.append(
                    "conversation_document_updates_ignored",
                    project_id=instance_config.project_id,
                    role_id=instance_config.role_id,
                    role_instance_id=instance_id,
                    message_id=message.message_id,
                    update_count=len(result.document_updates),
                    reason="direct_conversation_does_not_publish_artifacts",
                    correlation_id=message.correlation_id,
                )
            if result.handoffs:
                self.journal.append(
                    "conversation_handoffs_ignored",
                    project_id=instance_config.project_id,
                    role_id=instance_config.role_id,
                    role_instance_id=instance_id,
                    message_id=message.message_id,
                    handoff_count=len(result.handoffs),
                    reason="direct_conversation_must_propose_tracked_work_via_safe_outputs",
                    correlation_id=message.correlation_id,
                )
            telemetry.record_duration(
                "agentic_mesh.agent.run.duration",
                time.perf_counter() - started,
                attrs,
            )
            self._queue_direct_conversation_status_connector_message(
                source_instance=instance_config,
                source_message=message,
                status=result.status,
                status_message=result.message,
            )
            self.message_store.complete(
                message,
                result.status,
                result_message=result.message,
            )
            return True

    def _with_runtime_instructions(
        self,
        message: Message,
        flow_state: FlowState,
        *,
        direct_work: bool,
    ) -> Message:
        handoff_options = [
            {
                "status": status,
                "target_role": handoff.target_role,
                "target_state": handoff.target_state,
                "message_type": handoff.message_type,
                "target_mesh": handoff.target_mesh,
                "create_work_item": handoff.create_work_item,
            }
            for status, handoff in sorted(flow_state.handoffs.items())
        ]
        consult_options = [
            {
                "consult_id": consult_id,
                "target_role": consult.target_role,
                "target_state": consult.target_state,
                "message_type": consult.message_type,
                "purpose": consult.purpose,
            }
            for consult_id, consult in sorted(flow_state.consults.items())
        ]
        if direct_work:
            scope = (
                "This work was addressed directly to this role and is not currently "
                "inside the lifecycle flow."
            )
        else:
            scope = (
                f"This work is currently in lifecycle state `{flow_state.state_id}`, "
                f"owned by `{flow_state.owner_role}`."
            )
        payload = dict(message.payload)
        payload["runtime_instructions"] = {
            "scope": scope,
            "lifecycle_state": None if direct_work else flow_state.state_id,
            "state_purpose": flow_state.purpose,
            "artifact_path": flow_state.artifact_path,
            "document_visibility": (
                "Publish or update the state artifact as you work so sponsors "
                "can inspect current documentation, add direction, and see review "
                "status before the next lifecycle handoff."
            ),
            "document_quality_bar": (
                "All real work must produce enterprise-grade documentation. "
                "For slice or subslice work, write slice-scoped artifacts under "
                "`work-items/{work_item_id}/` unless you are deliberately updating "
                "a durable project document. Include objective, scope, assumptions, "
                "decisions, evidence, risks, review log, and clear next handoff "
                "or closure criteria where relevant."
            ),
            "gates": [
                {
                    "gate_id": gate.gate_id,
                    "type": gate.type,
                    "required_documents": gate.required_documents,
                    "required_review_status": gate.required_review_status,
                    "reviewer_role": gate.reviewer_role,
                    "affected_roles": gate.affected_roles,
                    "review_outcomes": gate.review_outcomes,
                    "max_resolution_loops": gate.max_resolution_loops,
                }
                for gate in flow_state.gates
            ],
            "git_branch": payload.get("git_branch"),
            "publication": payload.get("publication"),
            "handoff_guidance": (
                "Use the available handoff routes as options, not commands. "
                "Only emit a handoff when you decide your work is complete or "
                "blocked in a way that another role should own next."
            ),
            "available_handoffs": handoff_options,
            "available_consults": consult_options,
            "error_guidance": (
                "If you cannot complete the work, return a blocked, "
                "needs_clarification, or failed result with the reason, evidence, "
                "and any specific role or human input needed. Do not create a "
                "handoff just to hide an error."
            ),
        }
        return replace(message, payload=payload)

    @staticmethod
    def _resolve_document_update_path(
        update,
        *,
        source_message: Message,
        flow_state: FlowState,
        role_id: str,
    ):
        path = update.path.format(
            work_item_id=source_message.payload.get(
                "work_item_id",
                source_message.message_id,
            ),
            work_item_type=source_message.payload.get("work_item_type", "slice"),
            lifecycle_state=flow_state.state_id,
            role_id=role_id,
        )
        return replace(update, path=path)

    def _maintain_work_item_indexes(
        self,
        *,
        update,
        source_message: Message,
        flow_state: FlowState,
        role_id: str,
        correlation_id: str,
        trace_context: dict[str, str],
    ) -> None:
        work_item_id = source_message.payload.get("work_item_id")
        if update.maintain_work_item_index is False:
            self.journal.append(
                "work_item_index_skipped",
                project_id=self.project.project_id,
                role_id=role_id,
                work_item_id=work_item_id,
                lifecycle_state=flow_state.state_id,
                path=update.path,
                correlation_id=correlation_id,
                reason="document_update_opt_out",
            )
            return
        if not isinstance(work_item_id, str) or not work_item_id:
            return
        try:
            validate_work_item_id(work_item_id)
        except ValueError:
            return
        normalized_path = update.path.replace("\\", "/")
        expected_prefix = f"work-items/{work_item_id}/"
        if not normalized_path.startswith(expected_prefix):
            return
        if Path(normalized_path).name == LOCAL_INDEX_NAME:
            return
        try:
            self._write_work_item_indexes(
                update=update,
                work_item_id=work_item_id,
                source_message=source_message,
                flow_state=flow_state,
                role_id=role_id,
                correlation_id=correlation_id,
                trace_context=trace_context,
            )
        except Exception as exc:
            index_path = local_index_path(work_item_id)
            self.journal.append(
                "work_item_index_update_failed",
                project_id=self.project.project_id,
                role_id=role_id,
                work_item_id=work_item_id,
                work_item_type=source_message.payload.get("work_item_type"),
                lifecycle_state=flow_state.state_id,
                artifact_path=normalized_path,
                index_path=index_path,
                operation="runtime_publication",
                correlation_id=correlation_id,
                reason=exc.__class__.__name__,
            )
            raise RuntimeError(
                "work item index update failed: "
                f"work_item_id={work_item_id} artifact_path={normalized_path} "
                f"index_path={index_path} reason={exc.__class__.__name__}"
            ) from exc

    def _write_work_item_indexes(
        self,
        *,
        update,
        work_item_id: str,
        source_message: Message,
        flow_state: FlowState,
        role_id: str,
        correlation_id: str,
        trace_context: dict[str, str],
    ) -> None:
        root = self.artifact_store.document_library_root
        local_relative = local_index_path(work_item_id)
        local_path = root / local_relative
        if local_path.exists():
            index = parse_local_index(
                local_path.read_text(encoding="utf-8"),
                work_item_id=work_item_id,
            )
        else:
            index = WorkItemIndex(work_item_id=work_item_id)
        document_filename = Path(update.path).name
        purpose = update.purpose or flow_state.purpose or f"Lifecycle artifact for {flow_state.state_id}"
        review_status = update.review_status or review_status_from_content(
            update.content,
            "draft",
        )
        summary = (
            update.index_summary
            or source_message.payload.get("summary")
            or source_message.payload.get("title")
            or NOT_FOUND
        )
        index = upsert_document_row(
            index,
            WorkItemDocumentRow(
                document_path=document_filename,
                purpose=purpose,
                owner_role=role_id or UNKNOWN,
                review_status=review_status,
            ),
            lifecycle_state=flow_state.state_id,
            owner_role=role_id,
            queue_item_id=source_message.payload.get("queue_item_id") or NOT_FOUND,
            source_message=source_message.payload.get("source_message_id")
            or source_message.message_id,
            source_anchor=source_anchor_summary(source_message.payload.get("source_anchor")),
            summary=summary,
            work_item_type=source_message.payload.get("work_item_type") or UNKNOWN,
        )
        self.artifact_store.write_maintained_index(
            relative_path=local_relative,
            content=render_local_index(index),
            work_item_id=work_item_id,
            index_kind="local",
            operation="runtime_publication",
            queue_item_id=source_message.payload.get("queue_item_id"),
            work_item_type=source_message.payload.get("work_item_type"),
            lifecycle_state=flow_state.state_id,
            correlation_id=correlation_id,
            trace_context=trace_context,
        )

        global_path = root / GLOBAL_INDEX_PATH
        global_rows = (
            parse_global_index(global_path.read_text(encoding="utf-8"))
            if global_path.exists()
            else []
        )
        global_rows = upsert_global_row(
            global_rows,
            WorkItemsIndexRow(
                work_item_id=work_item_id,
                work_item_type=source_message.payload.get("work_item_type") or UNKNOWN,
                summary=summary,
            ),
        )
        self.artifact_store.write_maintained_index(
            relative_path=GLOBAL_INDEX_PATH,
            content=render_global_index(global_rows),
            work_item_id=work_item_id,
            index_kind="global",
            operation="runtime_publication",
            queue_item_id=source_message.payload.get("queue_item_id"),
            work_item_type=source_message.payload.get("work_item_type"),
            lifecycle_state=flow_state.state_id,
            correlation_id=correlation_id,
            trace_context=trace_context,
        )

    def _build_approval_context(
        self,
        *,
        source_message: Message,
        flow_state: FlowState,
    ) -> dict[str, Any]:
        work_item_id = source_message.payload.get("work_item_id")
        role_ids = sorted(self.project.roles)
        summary = (
            self.message_store.work_item_summary(str(work_item_id), role_ids)
            if work_item_id
            else {}
        )
        completed_roles = [
            role_id
            for role_id, role_summary in summary.items()
            if role_summary.get("completed", 0) > 0
        ]
        blocked_roles = sorted(
            {
                role_id
                for role_id, role_summary in summary.items()
                for status in role_summary.get("completion_statuses", [])
                if status not in {"completed", "waiting_for_human_response"}
            }
        )
        artifact_paths = sorted(
            {
                path
                for role_summary in summary.values()
                for path in role_summary.get("artifact_paths", [])
                if path
            }
        )
        journal_artifacts = {
            str(event.get("path"))
            for event in self.journal.read_all()
            if event.get("event_type") == "documentation_updated"
            and event.get("work_item_id") == work_item_id
            and event.get("path")
        }
        artifact_paths = sorted(set(artifact_paths) | journal_artifacts)
        context: dict[str, Any] = {
            "work_performed_summary": source_message.payload.get("summary") or "",
            "approval_reason": flow_state.purpose,
            "completed_roles": completed_roles,
            "blocked_roles": blocked_roles,
            "artifact_paths": artifact_paths,
            "artifact_count": len(artifact_paths),
        }
        status_url = self._work_item_status_url(str(work_item_id)) if work_item_id else None
        if status_url:
            context["status_url"] = status_url
            if source_message.payload.get("work_item_type") in {"slice", "subslice", "feature"}:
                context["test_url"] = status_url
                context["test_url_label"] = "Review and test this work item"
        return context

    @staticmethod
    def _work_item_status_url(work_item_id: str) -> str | None:
        base_url = os.environ.get("AGENTIC_MESH_STATUS_BASE_URL")
        if not base_url:
            return None
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            return None
        hostname = (parsed.hostname or "").lower()
        if hostname in {"localhost", "::1"} or hostname.startswith("127."):
            return None
        return f"{base_url.rstrip('/')}/work-items/{quote(work_item_id, safe='')}"

    def _queue_handoff_connector_message(
        self,
        *,
        source_instance,
        source_message: Message,
        handoff_message: Message,
        target_role: str,
    ) -> None:
        if self.connector_outbox is None:
            return
        target_override = self.project.roles[target_role]
        channel = (
            target_override.channels.get("handoff_inbox")
            or target_override.channels.get("primary")
        )
        if not channel:
            self.journal.append(
                "handoff_connector_message_unroutable",
                project_id=source_instance.project_id,
                role_id=source_instance.role_id,
                role_instance_id=source_instance.instance_id,
                target_role=target_role,
                work_item_id=source_message.payload.get("work_item_id"),
                correlation_id=source_message.correlation_id,
                reason="target_role_has_no_channel",
            )
            return
        connector_message = build_sdlc_handoff_connector_message(
            channel=channel,
            source_role=source_instance.role_id,
            source_instance_id=source_instance.instance_id,
            target_role=target_role,
            target_role_display_name=target_role,
            source_message=source_message,
            handoff_message=handoff_message,
        )
        self.connector_outbox.enqueue(connector_message)
        self.journal.append(
            "handoff_connector_message_queued",
            project_id=source_instance.project_id,
            role_id=source_instance.role_id,
            role_instance_id=source_instance.instance_id,
            target_role=target_role,
            channel=channel,
            connector_message_id=connector_message.message_id,
            work_item_id=source_message.payload.get("work_item_id"),
            correlation_id=source_message.correlation_id,
        )

    def _queue_route_connector_message(
        self,
        *,
        source_instance,
        source_message: Message,
        route: CurrentRoute,
    ) -> None:
        if self.connector_outbox is None:
            return
        event_kind = self._route_event_kind(route)
        visibility = self.notification_policy.visibility_for(
            event_kind=event_kind,
            route_kind=route.route_kind,
            role_id=source_instance.role_id,
        )
        if visibility != "notify":
            event = NotificationEvent.create(
                event_kind=event_kind,
                event_group="lifecycle",
                visibility=visibility,
                project_id=self.project.project_id,
                work_item_id=route.work_item_id,
                work_item_type=route.work_item_type,
                lifecycle_state=route.target_lifecycle_state,
                queue_item_id=route.queue_item_id,
                source_message_id=source_message.message_id,
                source_anchor_ref=route.source_anchor_ref,
                source_anchor_summary=route.source_anchor_summary,
                title=source_message.payload.get("title"),
                summary=source_message.payload.get("summary"),
                status_label=route.route_status,
                status_detail=f"{route.source_role} routed work to {route.target_role}",
                status_links=(
                    self._status_link_builder().work_item(route.work_item_id),
                ),
                route_hint="dashboard",
                correlation_id=source_message.correlation_id,
                trace_context=source_message.trace_context,
                dedupe_key=f"dedupe-{route.route_id}",
            )
            self.journal.append(
                "notification_policy_evaluated",
                project_id=self.project.project_id,
                event_id=event.event_id,
                event_kind=event.event_kind,
                visibility=visibility,
                route_id=route.route_id,
                route_kind=route.route_kind,
                route_status=route.route_status,
                work_item_id=route.work_item_id,
                queue_item_id=route.queue_item_id,
                correlation_id=source_message.correlation_id,
                notification_result="dashboard_only",
            )
            return
        event = NotificationEvent.create(
            event_kind=event_kind,
            event_group="lifecycle",
            visibility=visibility,
            project_id=self.project.project_id,
            work_item_id=route.work_item_id,
            work_item_type=route.work_item_type,
            lifecycle_state=route.target_lifecycle_state,
            queue_item_id=route.queue_item_id,
            source_message_id=source_message.message_id,
            source_anchor_ref=route.source_anchor_ref,
            source_anchor_summary=route.source_anchor_summary,
            title=source_message.payload.get("title"),
            summary=source_message.payload.get("summary"),
            status_label=route.route_status,
            status_detail=f"{route.source_role} routed work to {route.target_role}",
            status_links=(self._status_link_builder().work_item(route.work_item_id),),
            route_hint="source_anchor" if route.source_anchor_ref else "status_fallback",
            correlation_id=source_message.correlation_id,
            trace_context=source_message.trace_context,
            dedupe_key=f"dedupe-{route.route_id}",
        )
        resolver = NotificationRouteResolver(
            policy=self.project.notification_policy,
            source_route_store=FileSourceRouteStore(
                self.message_store.root.parents[2],
                self.project.project_id,
            ),
        )
        resolution = resolver.resolve(event)
        attempt = self.notification_attempt_store.create_or_dedupe(event, resolution)
        if resolution.dispatch_route:
            channel = resolution.dispatch_route
        else:
            target_override = self.project.roles[route.target_role]
            channel = (
                target_override.channels.get("handoff_inbox")
                or target_override.channels.get("primary")
            )
        if not channel:
            self.journal.append(
                "route_status_notification_failed",
                project_id=self.project.project_id,
                **route.journal_fields(),
                notification_result="unroutable",
                notification_error_class="missing_channel",
                notification_attempt_id=attempt.attempt_id,
            )
            return
        try:
            if route.route_kind == "configured_handoff":
                handoff_message = Message.create(
                    role_id=route.target_role,
                    message_type=route.message_type,
                    payload={
                        "lifecycle_state": route.target_lifecycle_state,
                        "work_item_id": route.work_item_id,
                        "work_item_type": route.work_item_type,
                    },
                    source=source_instance.instance_id,
                    correlation_id=source_message.correlation_id,
                    trace_context=source_message.trace_context,
                )
                connector_message = build_sdlc_handoff_connector_message(
                    channel=channel,
                    source_role=source_instance.role_id,
                    source_instance_id=source_instance.instance_id,
                    target_role=route.target_role,
                    target_role_display_name=route.target_role,
                    source_message=source_message,
                    handoff_message=replace(
                        handoff_message,
                        message_id=route.delivered_message_id or handoff_message.message_id,
                    ),
                )
            else:
                connector_message = build_route_status_connector_message(
                    channel=channel,
                    source_instance=source_instance,
                    source_message=source_message,
                    route_status=route.to_dict(),
                    target_role_display_name=route.target_role,
                    fallback=bool(resolution.fallback_reason),
                )
            connector_message = self.connector_outbox.enqueue(connector_message)
        except Exception as exc:
            self.journal.append(
                "route_status_notification_failed",
                project_id=self.project.project_id,
                **route.journal_fields(),
                notification_result="failed",
                notification_error_class=exc.__class__.__name__,
            )
            return
        event_type = (
            "handoff_connector_message_queued"
            if route.route_kind == "configured_handoff"
            else "route_status_connector_message_queued"
        )
        self.journal.append(
            event_type,
            project_id=self.project.project_id,
            **route.journal_fields(),
            channel=channel,
            connector_message_id=connector_message.message_id,
            connector_message_type=connector_message.type,
            notification_attempt_id=attempt.attempt_id,
            route_resolution_id=resolution.route_resolution_id,
            route_resolution_result=resolution.result,
            fallback_used=bool(resolution.fallback_reason),
            fallback_reason=resolution.fallback_reason,
            notification_result="queued",
        )

    @staticmethod
    def _route_event_kind(route: CurrentRoute) -> str:
        if route.route_kind == "configured_handoff":
            return "lifecycle.handoff_requested"
        if route.route_status == "consult_requested":
            return "lifecycle.consult_requested"
        if route.route_status == "correction_requested":
            return "lifecycle.review_loop_requested"
        return "lifecycle.route_requested"

    def _queue_directive_status_connector_message(
        self,
        *,
        source_instance,
        source_message: Message,
        status: str,
        status_message: str,
        artifact_paths: list[str],
    ) -> None:
        if self.connector_outbox is None:
            return
        role_override = self.project.roles[source_instance.role_id]
        channel = role_override.channels.get("primary")
        if not channel:
            self.journal.append(
                "directive_status_connector_message_unroutable",
                project_id=source_instance.project_id,
                role_id=source_instance.role_id,
                role_instance_id=source_instance.instance_id,
                work_item_id=source_message.payload.get("work_item_id"),
                status=status,
                correlation_id=source_message.correlation_id,
                reason="role_has_no_primary_channel",
            )
            return
        connector_message = build_sponsor_directive_status_message(
            channel=channel,
            source_instance=source_instance,
            source_message=source_message,
            status=status,
            status_message=status_message,
            artifact_paths=artifact_paths,
        )
        self.connector_outbox.enqueue(connector_message)
        self.journal.append(
            "directive_status_connector_message_queued",
            project_id=source_instance.project_id,
            role_id=source_instance.role_id,
            role_instance_id=source_instance.instance_id,
            channel=channel,
            connector_message_id=connector_message.message_id,
            connector_message_type=connector_message.type,
            work_item_id=source_message.payload.get("work_item_id"),
            status=status,
            correlation_id=source_message.correlation_id,
        )

    def _queue_direct_conversation_status_connector_message(
        self,
        *,
        source_instance,
        source_message: Message,
        status: str,
        status_message: str,
    ) -> None:
        if self.connector_outbox is None:
            return
        channel = self._direct_conversation_reply_channel(
            source_instance=source_instance,
            source_message=source_message,
        )
        if not channel:
            self.journal.append(
                "conversation_status_connector_message_unroutable",
                project_id=source_instance.project_id,
                role_id=source_instance.role_id,
                role_instance_id=source_instance.instance_id,
                status=status,
                correlation_id=source_message.correlation_id,
                reason="conversation_has_no_reply_channel",
            )
            return
        connector_message = build_direct_conversation_status_message(
            channel=channel,
            source_instance=source_instance,
            source_message=source_message,
            status=status,
            status_message=status_message,
        )
        self.connector_outbox.enqueue(connector_message)
        self.journal.append(
            "conversation_status_connector_message_queued",
            project_id=source_instance.project_id,
            role_id=source_instance.role_id,
            role_instance_id=source_instance.instance_id,
            channel=channel,
            connector_message_id=connector_message.message_id,
            connector_message_type=connector_message.type,
            status=status,
            correlation_id=source_message.correlation_id,
        )

    def _direct_conversation_reply_channel(
        self,
        *,
        source_instance,
        source_message: Message,
    ) -> str | None:
        source_channel = str(source_message.payload.get("source_channel") or "")
        if source_channel and source_channel != "dm":
            return source_channel
        role_override = self.project.roles[source_instance.role_id]
        return role_override.channels.get("primary")

    def _record_problem_status(
        self,
        problem_status: ProblemStatus,
        *,
        source_instance,
        source_message: Message,
    ) -> None:
        self.problem_status_store.write_current(problem_status)
        current_recovery = self.recovery_status_store.get_current(
            problem_status.work_item_id
        )
        recovery_status = self.recovery_classifier.classify_problem(
            project_id=self.project.project_id,
            problem_status=problem_status,
            current=current_recovery,
        )
        self.recovery_status_store.write_current(recovery_status)
        self.journal.append(
            "problem_status_recorded",
            project_id=self.project.project_id,
            **problem_status.journal_fields(),
        )
        recovery_fields = recovery_status.journal_fields()
        recovery_fields.pop("project_id", None)
        self.journal.append(
            "recovery_status_recorded",
            project_id=self.project.project_id,
            source="runtime_problem_status",
            **recovery_fields,
        )
        self._queue_problem_status_connector_message(
            source_instance=source_instance,
            source_message=source_message,
            problem_status=problem_status,
        )

    def _clear_problem_status_if_present(
        self,
        *,
        work_item_id,
        role_id: str,
        role_instance_id: str,
        lifecycle_state: str,
        correlation_id: str,
    ) -> None:
        if not isinstance(work_item_id, str) or not work_item_id:
            return
        if not self.problem_status_store.clear_current(work_item_id):
            return
        current_recovery = self.recovery_status_store.get_current(work_item_id)
        if current_recovery is not None:
            self.recovery_status_store.apply_transition(
                current_recovery,
                expected_revision=current_recovery.revision,
                recovery_state="recovery_succeeded",
                next_action="Problem cleared by successful lifecycle progress.",
                action_owner="none",
                journal_ref="problem_status_cleared",
            )
        self.journal.append(
            "problem_status_cleared",
            project_id=self.project.project_id,
            role_id=role_id,
            role_instance_id=role_instance_id,
            work_item_id=work_item_id,
            lifecycle_state=lifecycle_state,
            correlation_id=correlation_id,
        )

    def _queue_problem_status_connector_message(
        self,
        *,
        source_instance,
        source_message: Message,
        problem_status: ProblemStatus,
    ) -> None:
        if self.connector_outbox is None:
            return
        event_kind = (
            "problem.failed"
            if problem_status.status == "failed"
            else "problem.blocked"
            if problem_status.status == "blocked"
            else "problem.needs_runtime_recovery"
        )
        visibility = self.notification_policy.visibility_for(
            event_kind=event_kind,
            role_id=source_instance.role_id,
        )
        if visibility != "notify":
            return
        source_anchor = source_message.payload.get("source_anchor") or {}
        source_scope = (
            source_anchor.get("source_scope") if isinstance(source_anchor, dict) else None
        )
        surface = self.notification_policy.preferred_surface_for(event_kind)
        channel = str(source_scope) if source_scope else (surface.route if surface else None)
        event = NotificationEvent.create(
            event_kind=event_kind,
            event_group="problem_status",
            visibility=visibility,
            project_id=self.project.project_id,
            action_needed=True,
            urgency="important",
            action_owner=problem_status.action_owner,
            next_action=problem_status.next_action,
            work_item_id=problem_status.work_item_id,
            work_item_type=problem_status.work_item_type,
            lifecycle_state=problem_status.lifecycle_state,
            queue_item_id=problem_status.queue_item_id,
            source_message_id=source_message.message_id,
            source_anchor_ref=problem_status.source_anchor_ref,
            source_anchor_summary=problem_status.source_anchor_summary,
            title=source_message.payload.get("title"),
            summary=problem_status.reason_summary,
            status_label=problem_status.status,
            status_detail=problem_status.problem_kind,
            status_links=(
                self._status_link_builder().work_item(problem_status.work_item_id),
            ),
            route_hint="source_anchor" if source_scope else "status_fallback",
            correlation_id=source_message.correlation_id,
            trace_context=source_message.trace_context,
            dedupe_key=(
                "dedupe-"
                f"{problem_status.work_item_id}-{problem_status.status}-"
                f"{problem_status.affected_role}"
            ),
        )
        resolution = route_resolution_for_surface(
            event=event,
            surface=surface if not source_scope else None,
            fallback_reason=None if source_scope else "source_anchor_unavailable",
        )
        attempt = self.notification_attempt_store.create_or_dedupe(event, resolution)
        if not channel:
            self.journal.append(
                "problem_status_notification_failed",
                project_id=self.project.project_id,
                **problem_status.journal_fields(),
                notification_result="unroutable",
                notification_error_class="missing_channel",
                notification_attempt_id=attempt.attempt_id,
            )
            return
        connector_message = build_problem_status_connector_message(
            channel=channel,
            source_instance=source_instance,
            source_message=source_message,
            problem_status=problem_status.to_dict(),
            fallback=not bool(source_scope),
        )
        self.connector_outbox.enqueue(connector_message)
        self.journal.append(
            "problem_status_notification_queued",
            project_id=self.project.project_id,
            **problem_status.journal_fields(),
            channel=channel,
            connector_message_id=connector_message.message_id,
            connector_message_type=connector_message.type,
            notification_attempt_id=attempt.attempt_id,
            notification_result="queued",
        )

    def _queue_directive_publish_ready_if_complete(
        self,
        *,
        source_instance,
        source_message: Message,
    ) -> None:
        if self.connector_outbox is None:
            return
        work_item_id = source_message.payload.get("work_item_id")
        if not work_item_id:
            return
        requested_roles = [
            str(role_id)
            for role_id in source_message.payload.get("requested_roles") or []
            if role_id
        ]
        if not requested_roles:
            return
        summary = self.message_store.work_item_summary(work_item_id, requested_roles)
        if any(data["pending"] or data["claimed"] for data in summary.values()):
            return
        if any(data["completed"] == 0 for data in summary.values()):
            return
        role_statuses = {
            role_id: sorted(set(data.get("completion_statuses") or ["completed"]))
            for role_id, data in summary.items()
        }
        blocked_roles = sorted(
            role_id
            for role_id, statuses in role_statuses.items()
            if any(status != "completed" for status in statuses)
        )
        publication_status = (
            "terminal_with_blockers"
            if blocked_roles
            else "ready_to_commit_and_push"
        )

        artifact_paths = sorted(
            {
                path
                for data in summary.values()
                for path in data["artifact_paths"]
                if path
            }
        )
        payload = {
            "project_id": source_instance.project_id,
            "role_id": "delivery-manager",
            "title": source_message.payload.get("title"),
            "summary": source_message.payload.get("summary"),
            "work_item_id": work_item_id,
            "work_item_type": source_message.payload.get("work_item_type"),
            "queue_item_id": source_message.payload.get("queue_item_id"),
            "source_anchor": source_message.payload.get("source_anchor"),
            "work_mode": source_message.payload.get("work_mode"),
            "source_channel": source_message.payload.get("source_channel"),
            "target_roles": requested_roles,
            "role_count": len(requested_roles),
            "git_branch": source_message.payload.get("git_branch"),
            "publication": {
                **(source_message.payload.get("publication") or {}),
                "status": publication_status,
            },
            "terminal_status": publication_status,
            "role_statuses": role_statuses,
            "blocked_roles": blocked_roles,
            "artifact_paths": artifact_paths,
        }
        if not self.message_store.mark_work_item_publish_ready(work_item_id, payload):
            return
        source_anchor = source_message.payload.get("source_anchor") or {}
        channel = str(
            source_anchor.get("source_scope")
            or source_message.payload.get("source_channel")
            or "all-agents"
        )
        connector_message = ConnectorMessage.create(
            channel=channel,
            message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_PUBLISH_READY,
            payload=payload,
            source=source_instance.instance_id,
            correlation_id=source_message.correlation_id,
            trace_context=source_message.trace_context,
        )
        self.connector_outbox.enqueue(connector_message)
        self.journal.append(
            "directive_publish_ready_connector_message_queued",
            project_id=source_instance.project_id,
            role_id=source_instance.role_id,
            role_instance_id=source_instance.instance_id,
            channel=channel,
            connector_message_id=connector_message.message_id,
            work_item_id=work_item_id,
            git_branch=source_message.payload.get("git_branch"),
            artifact_paths=artifact_paths,
            correlation_id=source_message.correlation_id,
        )
