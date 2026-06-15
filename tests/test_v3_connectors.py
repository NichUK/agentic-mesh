from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.connectors import OutboundMessage
from agentic_mesh_v3.connectors import GraphTeamsBridge
from agentic_mesh_v3.connectors import StakeholderMessage


def test_local_teams_bridge_routes_dm_to_role_inbox() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "project.context"])
    bridge = LocalTeamsBridge(broker)

    subjects = bridge.route_inbound(
        StakeholderMessage(
            connector="teams",
            message_id="msg-1",
            source_type="dm",
            sender_ref="sponsor",
            conversation_ref="dm:product-manager",
            text="Give me a status update.",
        )
    )

    assert subjects == ["agent.product-manager"]
    broker.ensure_consumer("agent-inbox", "pm", filter_subject="agent.product-manager")
    assert broker.fetch("agent-inbox", "pm")[0].payload["text"] == "Give me a status update."


def test_local_teams_bridge_routes_unmentioned_channel_to_project_context() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "project.context"])
    bridge = LocalTeamsBridge(broker)

    subjects = bridge.route_inbound(
        StakeholderMessage(
            connector="teams",
            message_id="msg-1",
            source_type="channel",
            sender_ref="sponsor",
            conversation_ref="team:project/channel:project",
            text="General project context.",
        )
    )

    assert subjects == ["project.context"]


def test_local_teams_bridge_routes_unmentioned_channel_to_relevance_checks() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream(
        "agent-inbox",
        ["project.context", "agent.product-manager.relevance", "agent.qa-engineer.relevance"],
    )
    bridge = LocalTeamsBridge(broker, role_ids=("product-manager", "qa-engineer"))

    subjects = bridge.route_inbound(
        StakeholderMessage(
            connector="teams",
            message_id="msg-1",
            source_type="channel",
            sender_ref="sponsor",
            conversation_ref="team:project/channel:project",
            text="@all-agents this may affect the dashboard.",
            thread_ref="thread-1",
        )
    )

    assert subjects == ["project.context", "agent.product-manager.relevance", "agent.qa-engineer.relevance"]
    broker.ensure_consumer("agent-inbox", "context", filter_subject="project.context")
    broker.ensure_consumer("agent-inbox", "pm-relevance", filter_subject="agent.product-manager.relevance")
    context_payload = broker.fetch("agent-inbox", "context")[0].payload
    relevance_payload = broker.fetch("agent-inbox", "pm-relevance")[0].payload
    assert context_payload["route_type"] == "project_channel_context"
    assert relevance_payload["route_type"] == "team_wide_relevance_check"
    assert relevance_payload["thread_ref"] == "thread-1"


def test_local_teams_bridge_routes_mentioned_channel_to_role_and_shared_context() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["project.context", "agent.product-manager"])
    bridge = LocalTeamsBridge(broker, role_ids=("product-manager", "qa-engineer"))

    subjects = bridge.route_inbound(
        StakeholderMessage(
            connector="teams",
            message_id="msg-1",
            source_type="channel",
            sender_ref="sponsor",
            conversation_ref="team:project/channel:project",
            text="Can Product Manager look at this?",
            mentioned_roles=("product-manager",),
        )
    )

    assert subjects == ["project.context", "agent.product-manager"]
    broker.ensure_consumer("agent-inbox", "pm", filter_subject="agent.product-manager")
    payload = broker.fetch("agent-inbox", "pm")[0].payload
    assert payload["route_type"] == "mentioned_role_message"


def test_local_teams_bridge_records_outbound_delivery() -> None:
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)

    receipt = bridge.send(
        OutboundMessage(
            connector="teams",
            target_ref="dm:sponsor",
            text_markdown="**Please approve**",
            thread_ref="thread-1",
            importance="high",
        )
    )

    assert receipt.connector == "teams"
    assert receipt.thread_ref == "thread-1"
    assert bridge.deliveries[0].text_markdown == "**Please approve**"


class FakeGraphTeamsTransport:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []

    def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        self.posts.append((url, payload))
        return {"id": "graph-message-1"}


def test_graph_teams_bridge_posts_threaded_markdown_reply() -> None:
    transport = FakeGraphTeamsTransport()
    bridge = GraphTeamsBridge(transport=transport, graph_base_url="https://graph.test/v1.0")

    receipt = bridge.send(
        OutboundMessage(
            connector="teams",
            target_ref="team:team-1/channel:channel-1",
            thread_ref="message-1",
            text_markdown="**Approved**\n\nContinue.",
        )
    )

    assert receipt.delivery_id == "graph-message-1"
    assert transport.posts[0][0] == (
        "https://graph.test/v1.0/teams/team-1/channels/channel-1/messages/message-1/replies"
    )
    body = transport.posts[0][1]["body"]
    assert isinstance(body, dict)
    assert body["contentType"] == "html"
    assert "<strong>Approved</strong>" in str(body["content"])
