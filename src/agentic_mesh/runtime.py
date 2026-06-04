from __future__ import annotations

import time
from dataclasses import replace

from agentic_mesh import telemetry
from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.journal import EventJournal
from agentic_mesh.messaging import MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_PUBLISH_READY
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED
from agentic_mesh.messaging import build_human_response_request
from agentic_mesh.messaging import build_sdlc_handoff_connector_message
from agentic_mesh.messaging import build_sponsor_directive_status_message
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import FlowState
from agentic_mesh.models import Message
from agentic_mesh.models import ProjectConfig
from agentic_mesh.models import ResponseTypeTemplate
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.workers import WorkerAdapter


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

    def run_once(self, instance_id: str, instance_config) -> bool:
        message = self.message_store.claim_next(instance_config.role_id, instance_id)
        if message is None:
            return False
        if message.type == MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED:
            return self._run_direct_directive(instance_id, instance_config, message)
        state_id = message.payload.get("lifecycle_state", self.project.flow.entry_state)
        flow_state = self.project.flow.states[state_id]
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
                result = self.worker.run(instance_config, message, flow_state)
            for update in result.document_updates:
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

            telemetry.record_duration(
                "agentic_mesh.agent.run.duration",
                time.perf_counter() - agent_started,
                agent_attrs,
            )

            if message.type == MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED:
                response_attrs = telemetry.span_attributes(
                    **agent_attrs,
                    gate_id=message.payload.get("gate_id"),
                    response_request_id=message.payload.get("response_request_id"),
                    response_value=message.payload.get("response_value"),
                    human_response_value=message.payload.get("response_value"),
                )
                with telemetry.start_span(
                    "human_response.record",
                    correlation_id=message.correlation_id,
                    trace_context=message.trace_context,
                    attributes=response_attrs,
                ):
                    self.journal.append(
                        "human_response_recorded",
                        project_id=instance_config.project_id,
                        role_id=instance_config.role_id,
                        role_instance_id=instance_id,
                        work_item_id=message.payload.get("work_item_id"),
                        work_item_type=message.payload.get("work_item_type"),
                        lifecycle_state=state_id,
                        gate_id=message.payload.get("gate_id"),
                        response_request_id=message.payload.get("response_request_id"),
                        response_value=message.payload.get("response_value"),
                        correlation_id=message.correlation_id,
                    )
                    self.message_store.complete(message, "completed_after_human_response")
                return True

            human_gates = [
                gate
                for gate in flow_state.gates
                if gate.type == "human_response"
            ]
            if human_gates:
                for gate in human_gates:
                    if self.connector_outbox is None:
                        self.journal.append(
                            "human_response_request_unroutable",
                            project_id=instance_config.project_id,
                            role_id=instance_config.role_id,
                            role_instance_id=instance_id,
                            work_item_id=message.payload.get("work_item_id"),
                            work_item_type=message.payload.get("work_item_type"),
                            lifecycle_state=state_id,
                            gate_id=gate.gate_id,
                            response_type=gate.response_type,
                            correlation_id=message.correlation_id,
                            reason="connector_outbox_not_configured",
                        )
                        continue

                    with telemetry.start_span(
                        "human_response.request",
                        correlation_id=message.correlation_id,
                        trace_context=message.trace_context,
                        attributes=telemetry.span_attributes(
                            **agent_attrs,
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
                            source_message=replace(message, trace_context=response_trace_context),
                            source_instance=instance_config,
                            flow_state=flow_state,
                        )
                        request = self.connector_outbox.enqueue(request)
                        self.journal.append(
                            "human_response_requested",
                            project_id=instance_config.project_id,
                            role_id=instance_config.role_id,
                            role_instance_id=instance_id,
                            work_item_id=message.payload.get("work_item_id"),
                            work_item_type=message.payload.get("work_item_type"),
                            lifecycle_state=state_id,
                            gate_id=gate.gate_id,
                            response_request_id=request.payload["response_request_id"],
                            response_type=gate.response_type,
                            channel=request.channel,
                            connector_message_id=request.message_id,
                            correlation_id=message.correlation_id,
                        )

                self.message_store.complete(message, "waiting_for_human_response")
                return True

            for handoff in result.handoffs:
                handoff = self._normalise_handoff(
                    handoff=handoff,
                    source_message=message,
                    flow_state=flow_state,
                )
                with telemetry.start_span(
                    "handoff.emit",
                    correlation_id=message.correlation_id,
                    trace_context=message.trace_context,
                    attributes=telemetry.span_attributes(
                        **agent_attrs,
                        target_role=handoff.target_role,
                        source_lifecycle_state=state_id,
                        target_lifecycle_state=handoff.payload.get("lifecycle_state"),
                    ),
                ) as handoff_trace_context:
                    handoff_message = Message.create(
                        role_id=handoff.target_role,
                        message_type=handoff.message_type,
                        payload=handoff.payload,
                        source=instance_id,
                        correlation_id=message.correlation_id,
                        trace_context=handoff_trace_context,
                    )
                    self.journal.append(
                        "handoff_emitted",
                        project_id=instance_config.project_id,
                        role_id=instance_config.role_id,
                        role_instance_id=instance_id,
                        target_role=handoff.target_role,
                        source_lifecycle_state=state_id,
                        target_lifecycle_state=handoff.payload.get("lifecycle_state"),
                        out_of_flow=handoff.payload.get("out_of_flow", False),
                        out_of_flow_reason=handoff.payload.get("out_of_flow_reason"),
                        work_item_id=message.payload.get("work_item_id"),
                        message_id=handoff_message.message_id,
                        correlation_id=message.correlation_id,
                    )
                    handoff_message = self.message_store.enqueue(handoff_message)
                    self._queue_handoff_connector_message(
                        source_instance=instance_config,
                        source_message=message,
                        handoff_message=handoff_message,
                        target_role=handoff.target_role,
                    )
                    self.journal.append(
                        "message_routed",
                        project_id=instance_config.project_id,
                        source_role=instance_config.role_id,
                        target_role=handoff.target_role,
                        source_lifecycle_state=state_id,
                        target_lifecycle_state=handoff.payload.get("lifecycle_state"),
                        out_of_flow=handoff.payload.get("out_of_flow", False),
                        out_of_flow_reason=handoff.payload.get("out_of_flow_reason"),
                        work_item_id=message.payload.get("work_item_id"),
                        message_id=handoff_message.message_id,
                        correlation_id=message.correlation_id,
                    )

            self.message_store.complete(message, result.status)
            return True

    def _normalise_handoff(
        self,
        *,
        handoff,
        source_message: Message,
        flow_state: FlowState,
    ):
        transition = next(
            (
                candidate
                for candidate in flow_state.handoffs.values()
                if candidate.target_role == handoff.target_role
                and candidate.message_type == handoff.message_type
            ),
            None,
        )
        payload = dict(handoff.payload)
        if transition is None:
            target_state = payload.get("lifecycle_state")
            if not isinstance(target_state, str) or not target_state:
                raise ValueError(
                    "Worker emitted an unconfigured handoff from "
                    f"{flow_state.state_id} to {handoff.target_role} "
                    f"with message type {handoff.message_type}, but did not "
                    "provide a target lifecycle_state"
                )
            if target_state not in self.project.flow.states:
                raise ValueError(
                    "Worker emitted an out-of-flow handoff to unknown "
                    f"lifecycle_state {target_state}"
                )
            target_flow_state = self.project.flow.states[target_state]
            if target_flow_state.owner_role != handoff.target_role:
                raise ValueError(
                    "Worker emitted an out-of-flow handoff to "
                    f"{target_state}, which is owned by "
                    f"{target_flow_state.owner_role}, not {handoff.target_role}"
                )
            reason = payload.get("out_of_flow_reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(
                    "Worker emitted an out-of-flow handoff without "
                    "out_of_flow_reason"
                )
            payload["out_of_flow"] = True
        else:
            payload["lifecycle_state"] = transition.target_state

        payload.setdefault("title", source_message.payload.get("title"))
        payload.setdefault("summary", source_message.payload.get("summary"))
        payload["work_item_id"] = source_message.payload.get("work_item_id")
        payload["work_item_type"] = source_message.payload.get("work_item_type")
        payload["previous_lifecycle_state"] = flow_state.state_id
        payload["source_message_id"] = source_message.message_id
        return replace(handoff, payload=payload)

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
                result = self.worker.run(instance_config, message, direct_state)
            document_updates = (
                result.document_updates if result.status == "completed" else []
            )
            for update in document_updates:
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
        channel = str(source_message.payload.get("source_channel") or "all-agents")
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
