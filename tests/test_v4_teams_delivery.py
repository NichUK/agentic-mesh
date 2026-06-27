from __future__ import annotations

from agentic_mesh_v4.teams_delivery import TeamsReplySender


class CapturingTransport:
    def __init__(self) -> None:
        self.form_urls: list[str] = []
        self.json_calls: list[tuple[str, dict[str, object], str]] = []

    def post_form(self, url: str, payload: dict[str, str]) -> dict[str, object]:
        self.form_urls.append(url)
        return {"access_token": "token", "expires_in": 3600}

    def post_json(self, url: str, payload: dict[str, object], *, authorization: str) -> dict[str, object]:
        self.json_calls.append((url, payload, authorization))
        return {"id": "activity-1"}


def test_teams_reply_sender_uses_general_tenant_fallback(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_TENANT_ID", raising=False)
    monkeypatch.setenv("AGENTIC_MESH_TENANT_ID", "tenant-123")
    monkeypatch.setenv("TEAMS_BOT_PROJECT_MANAGER_APP_ID", "app-123")
    monkeypatch.setenv("TEAMS_BOT_PROJECT_MANAGER_SECRET", "secret-123")
    transport = CapturingTransport()
    sender = TeamsReplySender.from_env()
    sender.transport = transport

    sender.send_reply(
        role_id="project-manager",
        activity={
            "serviceUrl": "https://smba.trafficmanager.net/uk/tenant-123/",
            "conversation": {"id": "conversation-123"},
            "id": "activity-123",
        },
        text_markdown="Hello",
    )

    assert transport.form_urls == [
        "https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token"
    ]


def test_processing_reaction_is_unsupported_without_graph_token(monkeypatch) -> None:
    monkeypatch.setenv("TEAMS_BOT_QA_ENGINEER_APP_ID", "app-123")
    monkeypatch.setenv("TEAMS_BOT_QA_ENGINEER_SECRET", "secret-123")
    monkeypatch.delenv("AGENTIC_MESH_TEAMS_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_DELEGATED_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_SCOPES", raising=False)
    transport = CapturingTransport()
    sender = TeamsReplySender(transport=transport, tenant_id="tenant-123")

    result = sender.add_processing_reaction(
        role_id="qa-engineer",
        activity={
            "serviceUrl": "https://smba.trafficmanager.net/uk/tenant-123/",
            "conversation": {"id": "19:thread@thread.tacv2"},
            "id": "activity-123",
        },
    )

    assert result is None
    assert transport.form_urls == []
    assert transport.json_calls == []


def test_processing_reaction_uses_graph_set_reaction_for_channel_message(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_SCOPES", raising=False)
    monkeypatch.setenv("AGENTIC_MESH_TEAMS_TOKEN", "graph-token")
    monkeypatch.setenv("AGENTIC_MESH_GRAPH_BASE_URL", "https://graph.test/v1.0")
    transport = CapturingTransport()
    sender = TeamsReplySender(transport=transport, tenant_id="tenant-123")

    result = sender.add_processing_reaction(
        role_id="qa-engineer",
        activity={
            "channelData": {
                "team": {"id": "team-123"},
                "channel": {"id": "19:channel@thread.tacv2"},
            },
            "id": "message-123",
        },
    )

    assert result is not None
    assert transport.form_urls == []
    assert transport.json_calls == [
        (
            "https://graph.test/v1.0/teams/team-123/channels/19%3Achannel%40thread.tacv2/messages/message-123/setReaction",
            {"reactionType": "\U0001F440"},
            "Bearer graph-token",
        )
    ]


def test_processing_reaction_uses_graph_set_reaction_for_channel_reply(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_GRAPH_SCOPES", raising=False)
    monkeypatch.setenv("AGENTIC_MESH_TEAMS_TOKEN", "graph-token")
    monkeypatch.setenv("AGENTIC_MESH_GRAPH_BASE_URL", "https://graph.test/v1.0")
    transport = CapturingTransport()
    sender = TeamsReplySender(transport=transport, tenant_id="tenant-123")

    result = sender.add_processing_reaction(
        role_id="qa-engineer",
        activity={
            "channelData": {
                "team": {"id": "team-123"},
                "channel": {"id": "19:channel@thread.tacv2"},
            },
            "replyToId": "root-message-1",
            "id": "reply-message-1",
        },
    )

    assert result is not None
    assert transport.json_calls == [
        (
            "https://graph.test/v1.0/teams/team-123/channels/19%3Achannel%40thread.tacv2/messages/root-message-1/replies/reply-message-1/setReaction",
            {"reactionType": "\U0001F440"},
            "Bearer graph-token",
        )
    ]
