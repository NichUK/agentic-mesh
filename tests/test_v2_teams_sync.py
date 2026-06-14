from pathlib import Path
from typing import Any

from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.project_config import load_teams_connector_config
from agentic_mesh_v2.teams_sync import sync_project_channel_messages


PROJECT_FILE = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml")


class FakeGraphClient:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str]] = []

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append((method, path))
        return self.responses.get(path, {"value": []})


def test_graph_sync_replays_unmentioned_bound_thread_reply_to_owning_agent(tmp_path: Path) -> None:
    config = load_teams_connector_config(PROJECT_FILE, external_base_url="http://linuxch:8100")
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, config)
    adapter.install()
    db.upsert_conversation(
        conversation_id="conversation-project",
        connector=config.connector_id,
        external_ref=config.default_project_channel_ref,
        source_type="channel",
        sponsor_ref="nicholas",
    )
    request_id = adapter.deliver_response_card(
        call_id="call-product-signoff",
        role_id="product-manager",
        payload={
            "title": "Product sign-off",
            "question": "Please approve the product definition.",
            "conversation_id": "conversation-project",
            "destination_ref": config.default_project_channel_ref,
            "destination_type": "dm",
            "recipient_ref": "nicholas",
            "thread_ref": "root-message-1",
            "work_item_id": None,
            "delivery_outcome": "sent",
        },
        request_type="product_signoff",
    )
    channel_path = (
        f"/teams/{config.project_team_ref}/channels/"
        f"{config.default_project_channel_ref.replace(':', '%3A').replace('@', '%40')}/messages?%24top=25"
    )
    reply_path = (
        f"/teams/{config.project_team_ref}/channels/"
        f"{config.default_project_channel_ref.replace(':', '%3A').replace('@', '%40')}"
        "/messages/root-message-1/replies?%24top=25"
    )
    graph = FakeGraphClient(
        {
            channel_path: {
                "value": [
                    {
                        "id": "root-message-1",
                        "from": {"application": {"id": "bot-product-manager", "displayName": "AM-Product Manager"}},
                        "body": {"content": "<p>Product sign-off card</p>"},
                    }
                ]
            },
            reply_path: {
                "value": [
                    {
                        "id": "reply-message-1",
                        "from": {"user": {"id": "nicholas", "displayName": "Nicholas Overend"}},
                        "body": {"content": "<p>Send this approval to me as a direct message.</p>"},
                    }
                ]
            },
        }
    )

    result = sync_project_channel_messages(
        db=db,
        config=config,
        graph_client=graph,
        max_messages=25,
        include_replies=True,
    )

    snapshot = db.status_snapshot()
    assignments = snapshot["role_assignments"]
    assert result.skipped_bot_messages == 1
    assert result.messages_replayed == 1
    assert any(
        assignment["assignment_type"] == "bound_thread_reply"
        and assignment["role_id"] == "product-manager"
        and assignment["payload"]["human_response_request_id"] == request_id
        for assignment in assignments
    )
