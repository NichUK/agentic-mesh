from pathlib import Path

from agentic_mesh.config import load_mesh_config
from agentic_mesh.messaging import (
    MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED,
    MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED,
    build_human_response_received_message,
    build_human_response_request,
)
from agentic_mesh.models import Message


def test_builds_human_response_request_message() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    flow_state = mesh_config.project.flow.states["release_review"]
    gate = flow_state.gates[0]
    source_message = Message.create(
        role_id="release-manager",
        message_type="sdlc.release_review",
        payload={
            "title": "Release v0",
            "summary": "Ready for release.",
            "work_item_id": "slice-release",
            "work_item_type": "slice",
            "lifecycle_state": "release_review",
        },
        source="test",
    )

    request = build_human_response_request(
        gate=gate,
        response_type=mesh_config.response_types[gate.response_type],
        source_message=source_message,
        source_instance=mesh_config.instances["agentic-mesh-dev.release-manager.1"],
        flow_state=flow_state,
    )

    assert request.channel == "approvals"
    assert request.type == MESSAGE_TYPE_HUMAN_RESPONSE_REQUESTED
    assert request.payload["gate_id"] == "release_decision_response"
    assert request.payload["response_type"] == "approve_not_approve"
    assert request.payload["response_template"]["input_mode"] == "choice"
    assert request.payload["work_item_id"] == "slice-release"


def test_builds_human_response_received_role_message() -> None:
    message = build_human_response_received_message(
        target_role="release-manager",
        work_item_id="slice-release",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        response_request_id="human-response-123",
        responder="release-sponsor",
        response_value={"decision": "approved"},
        source="local-cli",
    )

    assert message.role_id == "release-manager"
    assert message.type == MESSAGE_TYPE_HUMAN_RESPONSE_RECEIVED
    assert message.payload["response_value"] == {"decision": "approved"}
