from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import BotFrameworkRoleIdentity
from agentic_mesh_v3.connectors import BotFrameworkTeamsBridge
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


def test_local_teams_bridge_ensures_inbound_subject_before_publish() -> None:
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)

    subjects = bridge.route_inbound(
        StakeholderMessage(
            connector="teams",
            message_id="msg-1",
            source_type="dm",
            sender_ref="sponsor",
            conversation_ref="dm:product-manager",
            text="Are you there?",
        )
    )

    assert subjects == ["agent.product-manager"]
    broker.ensure_consumer("agent-inbox", "pm", filter_subject="agent.product-manager")
    assert broker.fetch("agent-inbox", "pm")[0].payload["text"] == "Are you there?"


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
            reply_target_ref="team:project/channel:project",
            reply_thread_ref="thread-1",
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
    assert relevance_payload["reply_target_ref"] == "team:project/channel:project"
    assert relevance_payload["reply_thread_ref"] == "thread-1"


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
        self.gets: list[str] = []
        self.get_responses: dict[str, dict[str, object]] = {}

    def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        self.posts.append((url, payload))
        return {"id": "graph-message-1"}

    def get_json(self, url: str) -> dict[str, object]:
        self.gets.append(url)
        return self.get_responses.get(url, {"value": []})


class FakeBotFrameworkTransport:
    def __init__(self) -> None:
        self.forms: list[tuple[str, dict[str, str]]] = []
        self.posts: list[tuple[str, dict[str, object], str]] = []

    def post_form(self, url: str, payload: dict[str, str]) -> dict[str, object]:
        self.forms.append((url, payload))
        return {"access_token": "bot-token", "expires_in": 3600}

    def post_json(self, url: str, payload: dict[str, object], *, authorization: str) -> dict[str, object]:
        self.posts.append((url, payload, authorization))
        if url.endswith("/v3/conversations"):
            return {"id": "conversation-1"}
        return {"id": "activity-1"}


def test_bot_framework_teams_bridge_sends_as_role_bot_to_personal_user() -> None:
    transport = FakeBotFrameworkTransport()
    bridge = BotFrameworkTeamsBridge(
        role_identities={
            "product-manager": BotFrameworkRoleIdentity(
                role_id="product-manager",
                app_id="pm-app-id",
                app_secret="pm-secret",
                display_name="AM-Product Manager",
            )
        },
        service_url="https://smba.test/teams",
        tenant_id="tenant-1",
        transport=transport,
    )

    receipt = bridge.send(
        OutboundMessage(
            connector="teams",
            target_ref="user:sponsor-user",
            text_markdown="**Please approve**",
            sender_role="product-manager",
        )
    )

    assert receipt.delivery_id == "activity-1"
    assert transport.forms[0][1]["client_id"] == "pm-app-id"
    assert transport.forms[0][1]["scope"] == "https://api.botframework.com/.default"
    assert transport.posts[0][0] == "https://smba.test/teams/v3/conversations"
    assert transport.posts[0][1]["bot"] == {"id": "pm-app-id", "name": "AM-Product Manager"}
    assert transport.posts[1][0] == "https://smba.test/teams/v3/conversations/conversation-1/activities"
    assert transport.posts[1][1]["textFormat"] == "markdown"
    assert transport.posts[1][2] == "Bearer bot-token"


def test_bot_framework_teams_bridge_requires_sender_role_when_multiple_bots() -> None:
    bridge = BotFrameworkTeamsBridge(
        role_identities={
            "product-manager": BotFrameworkRoleIdentity(
                role_id="product-manager",
                app_id="pm-app-id",
                app_secret="pm-secret",
                display_name="AM-Product Manager",
            ),
            "release-manager": BotFrameworkRoleIdentity(
                role_id="release-manager",
                app_id="rm-app-id",
                app_secret="rm-secret",
                display_name="AM-Release Manager",
            ),
        },
        service_url="https://smba.test/teams",
        transport=FakeBotFrameworkTransport(),
    )

    try:
        bridge.send(
            OutboundMessage(
                connector="teams",
                target_ref="user:sponsor-user",
                text_markdown="No sender.",
            )
        )
    except Exception as exc:
        assert "sender_role is required" in str(exc)
    else:
        raise AssertionError("multiple role bot identities require a sender_role")


def test_bot_framework_teams_bridge_routes_inbound_through_broker() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    bridge = BotFrameworkTeamsBridge(
        role_identities={
            "product-manager": BotFrameworkRoleIdentity(
                role_id="product-manager",
                app_id="pm-app-id",
                app_secret="pm-secret",
                display_name="AM-Product Manager",
            )
        },
        service_url="https://smba.test/teams",
        transport=FakeBotFrameworkTransport(),
        inbound_broker=broker,
    )

    subjects = bridge.route_inbound(
        StakeholderMessage(
            connector="teams",
            message_id="msg-1",
            source_type="dm",
            sender_ref="sponsor",
            conversation_ref="dm:product-manager",
            text="Hello.",
        )
    )

    assert subjects == ["agent.product-manager"]


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


def test_graph_teams_bridge_can_send_dm_to_user_target() -> None:
    transport = FakeGraphTeamsTransport()
    bridge = GraphTeamsBridge(
        transport=transport,
        graph_base_url="https://graph.test/v1.0",
        sender_user_ref="sender-user",
    )

    receipt = bridge.send(
        OutboundMessage(
            connector="teams",
            target_ref="user:sponsor-user",
            text_markdown="**Question**\n\nPlease approve.",
        )
    )

    assert receipt.delivery_id == "graph-message-1"
    assert transport.posts[0][0] == "https://graph.test/v1.0/chats"
    assert transport.posts[0][1]["chatType"] == "oneOnOne"
    members = transport.posts[0][1]["members"]
    assert isinstance(members, list)
    assert members[0]["user@odata.bind"] == "https://graph.test/v1.0/users('sender-user')"
    assert members[1]["user@odata.bind"] == "https://graph.test/v1.0/users('sponsor-user')"
    assert transport.posts[1][0] == "https://graph.test/v1.0/chats/graph-message-1/messages"
    body = transport.posts[1][1]["body"]
    assert isinstance(body, dict)
    assert "<strong>Question</strong>" in str(body["content"])


def test_graph_teams_bridge_refuses_self_user_target() -> None:
    transport = FakeGraphTeamsTransport()
    bridge = GraphTeamsBridge(
        transport=transport,
        graph_base_url="https://graph.test/v1.0",
        sender_user_ref="sender-user",
    )

    try:
        bridge.send(
            OutboundMessage(
                connector="teams",
                target_ref="user:sender-user",
                text_markdown="**Update**\n\nDone.",
            )
        )
    except ValueError as exc:
        assert "refusing to send Teams delegated DM to the sender user" in str(exc)
    else:
        raise AssertionError("delegated Teams self-DM targets must fail closed")

    assert transport.gets == []
    assert transport.posts == []


def test_graph_teams_bridge_user_target_requires_sender_user_ref() -> None:
    bridge = GraphTeamsBridge(transport=FakeGraphTeamsTransport(), graph_base_url="https://graph.test/v1.0")

    try:
        bridge.send(
            OutboundMessage(
                connector="teams",
                target_ref="user:sponsor-user",
                text_markdown="Please approve.",
            )
        )
    except ValueError as exc:
        assert "sender_user_ref is required" in str(exc)
    else:
        raise AssertionError("user Teams targets should require sender_user_ref")


def test_graph_teams_bridge_routes_inbound_through_configured_bridge() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    inbound_bridge = LocalTeamsBridge(broker)
    bridge = GraphTeamsBridge(
        transport=FakeGraphTeamsTransport(),
        graph_base_url="https://graph.test/v1.0",
        inbound_bridge=inbound_bridge,
    )

    subjects = bridge.route_inbound(
        StakeholderMessage(
            connector="teams",
            message_id="msg-1",
            source_type="dm",
            sender_ref="sponsor",
            conversation_ref="dm:product-manager",
            text="Please respond.",
        )
    )

    assert subjects == ["agent.product-manager"]
    broker.ensure_consumer("agent-inbox", "pm", filter_subject="agent.product-manager")
    assert broker.fetch("agent-inbox", "pm")[0].payload["text"] == "Please respond."


def test_graph_teams_bridge_can_create_broker_backed_inbound_bridge() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["project.context", "agent.product-manager.relevance"])
    bridge = GraphTeamsBridge(
        transport=FakeGraphTeamsTransport(),
        graph_base_url="https://graph.test/v1.0",
        inbound_broker=broker,
        role_ids=("product-manager",),
    )

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

    assert subjects == ["project.context", "agent.product-manager.relevance"]


def test_graph_teams_bridge_rejects_ambiguous_inbound_configuration() -> None:
    broker = InMemoryBrokerAdapter()
    try:
        GraphTeamsBridge(
            transport=FakeGraphTeamsTransport(),
            inbound_bridge=LocalTeamsBridge(broker),
            inbound_broker=broker,
        )
    except ValueError as error:
        assert "either inbound_bridge or inbound_broker" in str(error)
    else:
        raise AssertionError("ambiguous inbound configuration should fail")


def test_graph_teams_bridge_sanitizes_agent_markdown_html() -> None:
    transport = FakeGraphTeamsTransport()
    bridge = GraphTeamsBridge(transport=transport, graph_base_url="https://graph.test/v1.0")

    bridge.send(
        OutboundMessage(
            connector="teams",
            target_ref="chat:chat-1",
            text_markdown=(
                "**Safe**\n\n"
                "<script>alert('x')</script>"
                "<style>body{display:none}</style>"
                "<img src=x onerror=alert(1)>"
                "<a href=\"javascript:alert(1)\" onclick=\"bad()\">bad link</a>"
            ),
        )
    )

    body = transport.posts[0][1]["body"]
    assert isinstance(body, dict)
    content = str(body["content"])
    assert "<strong>Safe</strong>" in content
    assert "script" not in content.casefold()
    assert "style" not in content.casefold()
    assert "alert" not in content.casefold()
    assert "onerror" not in content.casefold()
    assert "onclick" not in content.casefold()
    assert "<img" not in content.casefold()
    assert "<a>bad link</a>" in content
