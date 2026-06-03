import json
from pathlib import Path

from agentic_mesh import telemetry
from agentic_mesh.config import load_mesh_config
from agentic_mesh.connectors import BotFrameworkTeamsConnectorAdapter
from agentic_mesh.connectors import LocalTeamsConnectorAdapter
from agentic_mesh.connectors import TeamsBotIngress
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.journal import EventJournal
from agentic_mesh.messaging import build_human_response_request
from agentic_mesh.models import Message
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore


def test_local_teams_connector_renders_human_response_card(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    outbox = FileConnectorOutbox(state_root, mesh_config.project.project_id, journal)
    flow_state = mesh_config.project.flow.states["release_review"]
    gate = flow_state.gates[0]
    source_message = Message.create(
        role_id="release-manager",
        message_type="sdlc.release_review",
        payload={
            "title": "Release local runtime",
            "summary": "Confirm release readiness.",
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
    outbox.enqueue(request)

    connector = LocalTeamsConnectorAdapter(
        connector_id="local-teams-connector",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        outbox=outbox,
        journal=journal,
    )

    assert connector.process_once("approvals") is True
    assert connector.process_once("approvals") is False

    sent_files = list(
        (
            state_root
            / "projects"
            / mesh_config.project.project_id
            / "connectors"
            / "teams"
            / "approvals"
            / "sent"
        ).glob("*.json")
    )
    assert len(sent_files) == 1
    rendered = json.loads(sent_files[0].read_text(encoding="utf-8"))
    assert rendered["connector"] == "microsoft-teams-local"
    assert rendered["adaptive_card"]["type"] == "AdaptiveCard"
    assert [
        action["title"]
        for action in rendered["adaptive_card"]["actions"]
    ] == ["Approve", "Not Approve"]
    assert rendered["adaptive_card"]["actions"][0]["data"]["response_value"] == "approved"
    assert rendered["adaptive_card"]["actions"][0]["data"]["role_id"] == "release-manager"
    assert rendered["adaptive_card"]["actions"][0]["data"]["work_item_id"] == "slice-release"

    assert [event["event_type"] for event in journal.read_all()] == [
        "connector_message_queued",
        "connector_message_claimed",
        "teams_connector_message_prepared",
        "connector_message_completed",
    ]


def test_teams_ingress_records_human_response_submit(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
    )

    result = ingress.receive_activity(
        {
            "type": "invoke",
            "id": "activity/123",
            "serviceUrl": "https://smba.trafficmanager.net/emea/",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "value": {
                "action": "human_response.submit",
                "role_id": "release-manager",
                "work_item_id": "slice-release",
                "work_item_type": "slice",
                "lifecycle_state": "release_review",
                "gate_id": "release_decision_response",
                "response_request_id": "human-response-1",
                "response_value": "approved",
                "correlation_id": "corr-release",
            },
        }
    )

    assert result["statusCode"] == 200
    assert result["type"] == "application/vnd.microsoft.card.adaptive"
    completed_card = result["value"]
    assert completed_card["type"] == "AdaptiveCard"
    assert [action["title"] for action in completed_card["actions"]] == ["Approve"]
    assert completed_card["actions"][0]["isEnabled"] is False
    assert completed_card["actions"][0]["data"]["action"] == "human_response.completed"
    assert message_store.pending_count("release-manager") == 1
    message = message_store.claim_next(
        "release-manager",
        "agentic-mesh-dev.release-manager.1",
    )
    assert message is not None
    assert message.type == "human_response.received"
    assert message.payload["response_value"] == "approved"
    assert message.payload["responder"] == "Nich"

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_bot_activity_received" in event_types
    assert "human_response_received_from_teams" in event_types
    assert "human_response_completion_card_returned" in event_types


def test_teams_ingress_updates_original_human_response_card(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=mesh_config.project.connectors["teams"],
        secrets=object(),
    )
    captured = {}

    def capture_update(**kwargs):
        captured.update(kwargs)
        return {"id": kwargs["activity_id"]}

    ingress._update_activity = capture_update

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/456",
            "serviceUrl": "https://smba.trafficmanager.net/uk/tenant/",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "replyToId": "activity-original",
            "value": {
                "action": "human_response.submit",
                "role_id": "release-manager",
                "work_item_id": "slice-release",
                "work_item_type": "slice",
                "lifecycle_state": "release_review",
                "gate_id": "release_decision_response",
                "response_request_id": "human-response-1",
                "response_value": "approved",
                "correlation_id": "corr-release",
            },
        }
    )

    assert result["statusCode"] == 200
    assert captured["service_url"] == "https://smba.trafficmanager.net/uk/tenant/"
    assert captured["conversation_id"] == "conversation-1"
    assert captured["activity_id"] == "activity-original"
    assert captured["role_id"] == "release-manager"
    actions = captured["card"]["actions"]
    assert [action["title"] for action in actions] == ["Approve"]
    assert actions[0]["isEnabled"] is False
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_bot_card_updated" in event_types


def test_bot_connector_selects_source_role_for_handoff_sender() -> None:
    message = ConnectorMessage.create(
        channel="engineering",
        message_type="sdlc.handoff",
        payload={
            "source_role": "platform-engineer",
            "target_role": "engineering",
            "source_lifecycle_state": "platform_readiness",
            "target_lifecycle_state": "implementation",
            "work_item_id": "slice-bot-sender",
        },
        source="agentic-mesh-dev.platform-engineer.1",
    )
    connector = BotFrameworkTeamsConnectorAdapter.__new__(
        BotFrameworkTeamsConnectorAdapter
    )

    assert connector._sender_role(message) == "platform-engineer"
    rendered = connector._render_text(message)
    assert "platform_readiness" in rendered
    assert "implementation" in rendered


def test_bot_connector_attaches_human_response_card() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    flow_state = mesh_config.project.flow.states["release_review"]
    gate = flow_state.gates[0]
    source_message = Message.create(
        role_id="release-manager",
        message_type="sdlc.release_review",
        payload={
            "title": "Release local runtime",
            "summary": "Confirm release readiness.",
            "work_item_id": "slice-release-card",
            "work_item_type": "slice",
            "lifecycle_state": "release_review",
        },
        source="test",
        correlation_id="corr-1234567890abcdef1234567890abcdef",
        trace_context={"trace_id": "1234567890abcdef1234567890abcdef"},
    )
    request = build_human_response_request(
        gate=gate,
        response_type=mesh_config.response_types[gate.response_type],
        source_message=source_message,
        source_instance=mesh_config.instances["agentic-mesh-dev.release-manager.1"],
        flow_state=flow_state,
    )
    connector = BotFrameworkTeamsConnectorAdapter.__new__(
        BotFrameworkTeamsConnectorAdapter
    )

    activity = connector._build_activity(request)

    assert activity["type"] == "message"
    assert activity["attachments"][0]["contentType"] == (
        "application/vnd.microsoft.card.adaptive"
    )
    card = activity["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert [action["title"] for action in card["actions"]] == [
        "Approve",
        "Not Approve",
    ]
    assert card["actions"][0]["data"]["action"] == "human_response.submit"
    assert card["actions"][0]["data"]["response_value"] == "approved"
    assert card["actions"][0]["data"]["trace_context"] == {
        "trace_id": "1234567890abcdef1234567890abcdef"
    }


def test_teams_ingress_human_response_joins_trace_context(tmp_path: Path) -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        mesh_config = load_mesh_config(Path.cwd())
        state_root = tmp_path / "state"
        journal = EventJournal(state_root, mesh_config.project.project_id)
        message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
        ingress = TeamsBotIngress(
            connector_id="teams-bot-listener",
            project_id=mesh_config.project.project_id,
            state_root=state_root,
            message_store=message_store,
            journal=journal,
        )

        ingress.receive_activity(
            {
                "type": "message",
                "id": "activity/trace",
                "serviceUrl": "https://smba.trafficmanager.net/uk/tenant/",
                "from": {"id": "user-1", "name": "Nich"},
                "conversation": {"id": "conversation-1"},
                "replyToId": "activity-original",
                "value": {
                    "action": "human_response.submit",
                    "role_id": "release-manager",
                    "work_item_id": "slice-release",
                    "work_item_type": "slice",
                    "lifecycle_state": "release_review",
                    "gate_id": "release_decision_response",
                    "response_request_id": "human-response-1",
                    "response_value": "approved",
                    "correlation_id": "corr-1234567890abcdef1234567890abcdef",
                    "trace_context": {
                        "trace_id": "1234567890abcdef1234567890abcdef"
                    },
                },
            }
        )
    finally:
        telemetry.set_test_sink(None)

    span_names = [span.name for span in sink.spans]
    assert "teams.receive" in span_names
    assert "human_response.wait" in span_names
    assert "work.enqueue" in span_names
    assert all(
        span.trace_context["trace_id"] == "1234567890abcdef1234567890abcdef"
        for span in sink.spans
        if span.name in {"teams.receive", "human_response.wait", "work.enqueue"}
    )
