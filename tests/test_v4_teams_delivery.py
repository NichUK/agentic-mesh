from __future__ import annotations

from agentic_mesh_v4.teams_delivery import TeamsReplySender


class CapturingTransport:
    def __init__(self) -> None:
        self.form_urls: list[str] = []

    def post_form(self, url: str, payload: dict[str, str]) -> dict[str, object]:
        self.form_urls.append(url)
        return {"access_token": "token", "expires_in": 3600}

    def post_json(self, url: str, payload: dict[str, object], *, authorization: str) -> dict[str, object]:
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
