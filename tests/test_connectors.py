import json
from pathlib import Path

from agentic_mesh import telemetry
from agentic_mesh.config import load_mesh_config
from agentic_mesh.connectors import BotFrameworkTeamsConnectorAdapter
from agentic_mesh.connectors import LocalTeamsConnectorAdapter
from agentic_mesh.connectors import GraphTeamsChannelIngressAdapter
from agentic_mesh.connectors import TeamsBotIngress
from agentic_mesh.connectors import load_graph_token
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


def test_teams_ingress_routes_all_agents_message_to_direct_role_work(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    connector_outbox = FileConnectorOutbox(
        state_root,
        mesh_config.project.project_id,
        journal,
    )
    teams_config = mesh_config.project.connectors["teams"]
    ingress = TeamsBotIngress(
        connector_id="teams-bot-listener",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        message_store=message_store,
        journal=journal,
        connector_config=teams_config,
        project_config=mesh_config.project,
        connector_outbox=connector_outbox,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/adoption",
            "serviceUrl": "https://smba.trafficmanager.net/uk/",
            "text": "<at>all-agents</at> Start an adoption process for Agentic Mesh.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-1"},
            "channelData": {
                "team": {"id": teams_config.team_id},
                "channel": {
                    "id": teams_config.channels["all-agents"].channel_id,
                    "name": "all-agents",
                },
            },
        }
    )

    assert result["routed"] is True
    roles = sorted(mesh_config.project.roles)
    assert result["target_roles"] == roles
    assert result["lifecycle_state"] is None
    assert result["work_item_id"].startswith("work-")
    assert all(message_store.pending_count(role_id) == 1 for role_id in roles)
    assert connector_outbox.pending_count("all-agents") == 1
    acknowledgement = connector_outbox.claim_next("all-agents", "test-connector")
    assert acknowledgement is not None
    assert acknowledgement.type == "sponsor_directive.acknowledged"
    assert acknowledgement.payload["role_count"] == len(roles)
    assert acknowledgement.payload["role_id"] == "delivery-manager"

    message = message_store.claim_next(
        "business-analyst",
        "agentic-mesh-dev.business-analyst.1",
    )
    assert message is not None
    assert message.type == "sponsor_directive.requested"
    assert message.source == "teams:teams-bot-listener:all-agents"
    assert message.payload["work_item_type"] == "directive"
    assert message.payload["work_mode"] == "direct_broadcast"
    assert message.payload["source_channel"] == "all-agents"
    assert message.payload["target_role"] == "business-analyst"
    assert message.payload["output_path"] == "documents/requirements/business-analyst.md"
    assert message.payload["teams_from_name"] == "Nich"
    assert "Start an adoption process" in message.payload["summary"]
    assert "<at>" not in message.payload["summary"]

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_bot_activity_received" in event_types
    assert "message_accepted" in event_types
    assert "teams_all_agents_directive_routed" in event_types


def test_teams_ingress_journals_unmapped_channel_message(
    tmp_path: Path,
) -> None:
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
        project_config=mesh_config.project,
    )

    result = ingress.receive_activity(
        {
            "type": "message",
            "id": "activity/unmapped",
            "text": "This should not disappear silently.",
            "from": {"id": "user-1", "name": "Nich"},
            "conversation": {"id": "conversation-unknown"},
            "channelData": {
                "channel": {"id": "unknown-channel", "name": "unknown"},
            },
        }
    )

    assert result == {"status": "accepted", "activity_id": "activity_unmapped"}
    assert message_store.pending_count("business-analyst") == 0
    ignored = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_channel_message_ignored"
    ]
    assert ignored[0]["reason"] == "unmapped_channel"


def test_graph_teams_channel_ingress_routes_all_agents_message(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    connector_outbox = FileConnectorOutbox(
        state_root,
        mesh_config.project.project_id,
        journal,
    )
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
        connector_outbox=connector_outbox,
    )
    ingress._list_channel_messages = lambda channel, max_messages: [
        {
            "id": "1780489884072",
            "createdDateTime": "2026-06-03T12:31:24.072Z",
            "subject": "Adopt this project",
            "body": {
                "contentType": "html",
                "content": (
                    "<div><at id=\"0\">all-agents</at> Start an adoption "
                    "process for Agentic Mesh.</div>"
                ),
            },
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [{"id": 0, "mentionText": "all-agents"}],
            "webUrl": "https://teams.example/message/1780489884072",
        }
    ]

    result = ingress.process_once("all-agents", max_messages=5)

    role_count = len(mesh_config.project.roles)
    assert result == {"routed": role_count, "skipped": 0, "seen": 1}
    assert connector_outbox.pending_count("all-agents") == 1
    assert message_store.pending_count("business-analyst") == 1
    message = message_store.claim_next(
        "business-analyst",
        "agentic-mesh-dev.business-analyst.1",
    )
    assert message is not None
    assert message.type == "sponsor_directive.requested"
    assert message.source == "teams:teams-graph-ingress:all-agents"
    assert message.payload["work_item_type"] == "directive"
    assert message.payload["source_channel"] == "all-agents"
    assert message.payload["teams_activity_id"] == "1780489884072"
    assert message.payload["teams_from_name"] == "Nich"

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "teams_graph_channel_message_received" in event_types
    assert "teams_all_agents_directive_routed" in event_types


def test_graph_teams_channel_ingress_skips_all_agents_acknowledgement_echo(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
    )
    ingress._list_channel_messages = lambda channel, max_messages: [
        {
            "id": "ack-1",
            "createdDateTime": "2026-06-03T12:32:24.072Z",
            "body": {
                "contentType": "html",
                "content": (
                    "<p><strong>Agentic Mesh received:</strong> All-agents "
                    "directive received. Created direct work item work-1.</p>"
                ),
            },
            "from": {
                "application": {
                    "id": "agentic-mesh-bot",
                    "displayName": "AM-Delivery Manager",
                },
            },
            "mentions": [],
        }
    ]

    result = ingress.process_once("all-agents", max_messages=5)

    assert result == {"routed": 0, "skipped": 1, "seen": 1}
    assert message_store.pending_count("business-analyst") == 0
    skipped = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_graph_channel_message_skipped"
    ]
    assert skipped[0]["reason"] == "connector_echo"


def test_graph_teams_channel_ingress_requires_real_all_agents_mention(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
    )
    ingress._list_channel_messages = lambda channel, max_messages: [
        {
            "id": "plain-1",
            "createdDateTime": "2026-06-03T12:32:24.072Z",
            "body": {
                "contentType": "html",
                "content": "<p>all-agents should have received the work item.</p>",
            },
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [],
        }
    ]

    result = ingress.process_once("all-agents", max_messages=5)

    assert result == {"routed": 0, "skipped": 1, "seen": 1}
    assert message_store.pending_count("business-analyst") == 0
    skipped = [
        event
        for event in journal.read_all()
        if event["event_type"] == "teams_graph_channel_message_skipped"
    ]
    assert skipped[0]["reason"] == "missing_channel_mention"


def test_graph_teams_channel_ingress_cursor_suppresses_duplicates(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    teams_config = mesh_config.project.connectors["teams"]
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id="teams-graph-ingress",
        project_id=mesh_config.project.project_id,
        state_root=state_root,
        connector_config=teams_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token="token",
    )
    graph_messages = [
        {
            "id": "message-1",
            "createdDateTime": "2026-06-03T12:31:24.072Z",
            "body": {
                "content": "<at id=\"0\">all-agents</at> Adopt the project.",
            },
            "from": {"user": {"id": "user-1", "displayName": "Nich"}},
            "mentions": [{"id": 0, "mentionText": "all-agents"}],
        }
    ]
    ingress._list_channel_messages = lambda channel, max_messages: graph_messages

    assert ingress.process_once("all-agents")["routed"] == len(mesh_config.project.roles)
    assert ingress.process_once("all-agents")["routed"] == 0
    assert message_store.pending_count("business-analyst") == 1


def test_load_graph_token_supports_client_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}
    secret_file = tmp_path / "graph-client-secret"
    secret_file.write_text("secret-value", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return b'{"access_token":"graph-token"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("agentic_mesh.connectors.request.urlopen", fake_urlopen)

    token = load_graph_token(
        None,
        None,
        tenant_id="tenant-id",
        client_id="client-id",
        client_secret_file=secret_file,
    )

    assert token == "graph-token"
    assert "/tenant-id/oauth2/v2.0/token" in captured["url"]
    assert "grant_type=client_credentials" in captured["body"]
    assert "client_id=client-id" in captured["body"]
    assert "client_secret=secret-value" in captured["body"]


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


def test_bot_connector_renders_sponsor_directive_acknowledgement() -> None:
    message = ConnectorMessage.create(
        channel="all-agents",
        message_type="sponsor_directive.acknowledged",
        payload={
            "title": "Adopt this project",
            "work_item_id": "work-adoption",
            "role_id": "delivery-manager",
            "role_count": 13,
            "target_roles": ["business-analyst", "product-manager"],
        },
        source="test",
    )

    rendered = BotFrameworkTeamsConnectorAdapter._render_text(message)

    assert "Agentic Mesh received" in rendered
    assert "not a lifecycle handoff" in rendered
    assert "work-adoption" in rendered


def test_bot_connector_renders_sponsor_directive_status() -> None:
    message = ConnectorMessage.create(
        channel="release",
        message_type="sponsor_directive.completed",
        payload={
            "title": "Adopt this project",
            "work_item_id": "work-adoption",
            "role_id": "release-manager",
            "role_instance_id": "agentic-mesh-dev.release-manager.1",
            "status": "blocked",
            "status_message": "Codex CLI failed before returning a valid agent result.",
            "artifact_paths": ["documents/requirements/release-manager.md"],
        },
        source="test",
    )

    rendered = BotFrameworkTeamsConnectorAdapter._render_text(message)

    assert "release-manager: blocked direct instruction" in rendered
    assert "work-adoption" in rendered
    assert "Codex CLI failed" in rendered
    assert "documents/requirements/release-manager.md" in rendered


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
