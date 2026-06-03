from __future__ import annotations

import time
from dataclasses import replace

from agentic_mesh import telemetry
from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.journal import EventJournal
from agentic_mesh.messaging import MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED
from agentic_mesh.messaging import build_human_response_request
from agentic_mesh.messaging import build_sdlc_handoff_connector_message
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
                        work_item_id=message.payload.get("work_item_id"),
                        message_id=handoff_message.message_id,
                        correlation_id=message.correlation_id,
                    )

            self.message_store.complete(message, result.status)
            return True

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
