from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import FlowGate
from agentic_mesh.models import FlowState
from agentic_mesh.models import Message
from agentic_mesh.models import ResponseTypeTemplate
from agentic_mesh.models import RoleInstanceConfig
from agentic_mesh.models import new_id


MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED = "human_response.requested"
MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED = "human_response.received"
MESSAGE_TYPE_SDLC_HANDOFF = "sdlc.handoff"
MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED = "sponsor_directive.requested"
MESSAGE_TYPE_SPONSOR_DIRECTIVE_ACKNOWLEDGED = "sponsor_directive.acknowledged"
MESSAGE_TYPE_SPONSOR_DIRECTIVE_STARTED = "sponsor_directive.started"
MESSAGE_TYPE_SPONSOR_DIRECTIVE_COMPLETED = "sponsor_directive.completed"
MESSAGE_TYPE_SPONSOR_DIRECTIVE_PUBLISH_READY = "sponsor_directive.publish_ready"


def build_human_response_request(
    *,
    gate: FlowGate,
    response_type: ResponseTypeTemplate | None,
    source_message: Message,
    source_instance: RoleInstanceConfig,
    flow_state: FlowState,
) -> ConnectorMessage:
    if not gate.response_type:
        raise ValueError(f"Gate {gate.gate_id} does not declare response_type")
    if not gate.channel:
        raise ValueError(f"Gate {gate.gate_id} does not declare channel")

    response_request_id = new_id("human-response")
    payload: dict[str, Any] = {
        "response_request_id": response_request_id,
        "gate_id": gate.gate_id,
        "response_type": gate.response_type,
        "prompt": gate.prompt,
        "requested_from": gate.requested_from,
        "completion_criteria": gate.completion_criteria,
        "timeout": gate.timeout,
        "on_timeout": gate.on_timeout,
        "project_id": source_instance.project_id,
        "role_id": source_instance.role_id,
        "role_instance_id": source_instance.instance_id,
        "work_item_id": source_message.payload.get("work_item_id"),
        "work_item_type": source_message.payload.get("work_item_type"),
        "lifecycle_state": flow_state.state_id,
        "source_message_id": source_message.message_id,
        "correlation_id": source_message.correlation_id,
        "trace_context": source_message.trace_context,
        "requested_at": source_message.created_at,
        "title": source_message.payload.get("title"),
        "summary": source_message.payload.get("summary"),
    }
    if response_type is not None:
        payload["response_template"] = asdict(response_type)

    return ConnectorMessage.create(
        channel=gate.channel,
        message_type=MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED,
        payload=payload,
        source=source_instance.instance_id,
        correlation_id=source_message.correlation_id,
        trace_context=source_message.trace_context,
    )


def build_human_response_received_message(
    *,
    target_role: str,
    work_item_id: str,
    work_item_type: str,
    lifecycle_state: str,
    gate_id: str,
    response_request_id: str,
    responder: str,
    response_value: Any,
    source: str,
    correlation_id: str | None = None,
    trace_context: dict[str, str] | None = None,
) -> Message:
    return Message.create(
        role_id=target_role,
        message_type=MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED,
        payload={
            "title": f"Human response received for {gate_id}",
            "summary": f"{responder} responded to {gate_id}.",
            "work_item_id": work_item_id,
            "work_item_type": work_item_type,
            "lifecycle_state": lifecycle_state,
            "gate_id": gate_id,
            "response_request_id": response_request_id,
            "responder": responder,
            "response_value": response_value,
        },
        source=source,
        correlation_id=correlation_id,
        trace_context=trace_context,
    )


def build_sdlc_handoff_connector_message(
    *,
    channel: str,
    source_role: str,
    source_instance_id: str,
    target_role: str,
    target_role_display_name: str,
    source_message: Message,
    handoff_message: Message,
) -> ConnectorMessage:
    return ConnectorMessage.create(
        channel=channel,
        message_type=MESSAGE_TYPE_SDLC_HANDOFF,
        payload={
            "project_id": source_instance_id.split(".", 1)[0],
            "source_role": source_role,
            "source_instance_id": source_instance_id,
            "target_role": target_role,
            "target_role_display_name": target_role_display_name,
            "source_lifecycle_state": source_message.payload.get("lifecycle_state"),
            "target_lifecycle_state": handoff_message.payload.get("lifecycle_state"),
            "work_item_id": source_message.payload.get("work_item_id"),
            "work_item_type": source_message.payload.get("work_item_type"),
            "title": source_message.payload.get("title"),
            "summary": source_message.payload.get("summary"),
            "source_message_id": source_message.message_id,
            "handoff_message_id": handoff_message.message_id,
        },
        source=source_instance_id,
        correlation_id=source_message.correlation_id,
        trace_context=source_message.trace_context,
    )


def build_sponsor_directive_status_message(
    *,
    channel: str,
    source_instance: RoleInstanceConfig,
    source_message: Message,
    status: str,
    status_message: str,
    artifact_paths: list[str] | None = None,
) -> ConnectorMessage:
    message_type = (
        MESSAGE_TYPE_SPONSOR_DIRECTIVE_STARTED
        if status == "started"
        else MESSAGE_TYPE_SPONSOR_DIRECTIVE_COMPLETED
    )
    return ConnectorMessage.create(
        channel=channel,
        message_type=message_type,
        payload={
            "project_id": source_instance.project_id,
            "role_id": source_instance.role_id,
            "role_instance_id": source_instance.instance_id,
            "status": status,
            "status_message": status_message,
            "artifact_paths": artifact_paths or [],
            "title": source_message.payload.get("title"),
            "summary": source_message.payload.get("summary"),
            "work_item_id": source_message.payload.get("work_item_id"),
            "work_item_type": source_message.payload.get("work_item_type"),
            "work_mode": source_message.payload.get("work_mode"),
            "git_branch": source_message.payload.get("git_branch"),
            "publication": source_message.payload.get("publication"),
            "source_channel": source_message.payload.get("source_channel"),
            "source_message_id": source_message.message_id,
        },
        source=source_instance.instance_id,
        correlation_id=source_message.correlation_id,
        trace_context=source_message.trace_context,
    )
