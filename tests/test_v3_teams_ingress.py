from __future__ import annotations

from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.project_config import load_project_config
from agentic_mesh_v3.teams_ingress import DatabaseConversationRecorder
from agentic_mesh_v3.teams_ingress import TeamsActivityRouter
from agentic_mesh_v3.teams_ingress import TeamsRoleIdentity
from agentic_mesh_v3.teams_ingress import normalize_teams_activity
from agentic_mesh_v3.teams_ingress import teams_role_identities_from_project_config


def test_normalize_personal_teams_activity_targets_recipient_role() -> None:
    message = normalize_teams_activity(
        {
            "id": "msg-1",
            "text": "Give me a status update.",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1", "name": "Nicholas"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        },
        role_identities=(
            TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),
        ),
    )

    assert message.source_type == "dm"
    assert message.conversation_ref == "dm:product-manager"
    assert message.sender_ref == "user-1"
    assert message.text == "Give me a status update."


def test_normalize_channel_activity_preserves_thread_and_mentions() -> None:
    message = normalize_teams_activity(
        {
            "id": "msg-2",
            "text": "<at>AM-Product Manager</at> please review this.",
            "replyToId": "root-msg-1",
            "conversation": {"id": "channel-conv", "conversationType": "channel"},
            "from": {"aadObjectId": "aad-user-1", "name": "Nicholas"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
            "channelData": {
                "team": {"id": "team-1"},
                "channel": {"id": "channel-1"},
            },
            "entities": [
                {
                    "type": "mention",
                    "mentioned": {"id": "bot-product", "name": "AM-Product Manager"},
                }
            ],
        },
        role_identities=(
            TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),
        ),
    )

    assert message.source_type == "channel"
    assert message.conversation_ref == "team:team-1/channel:channel-1"
    assert message.thread_ref == "root-msg-1"
    assert message.mentioned_roles == ("product-manager",)
    assert message.text == "AM-Product Manager please review this."


def test_normalized_channel_message_routes_through_local_bridge() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["project.context", "agent.product-manager"])
    bridge = LocalTeamsBridge(broker)
    message = normalize_teams_activity(
        {
            "id": "msg-2",
            "text": "<at>AM-Product Manager</at> please review this.",
            "conversation": {"id": "channel-conv", "conversationType": "channel"},
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
            "channelData": {
                "team": {"id": "team-1"},
                "channel": {"id": "channel-1"},
            },
            "entities": [
                {
                    "type": "mention",
                    "mentioned": {"id": "bot-product", "name": "AM-Product Manager"},
                }
            ],
        },
        role_identities=(
            TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),
        ),
    )

    subjects = bridge.route_inbound(message)

    assert subjects == ["project.context", "agent.product-manager"]


def test_teams_activity_router_normalizes_and_routes_to_agent_inbox() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    bridge = LocalTeamsBridge(broker)
    router = TeamsActivityRouter(
        bridge,
        role_identities=(
            TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),
        ),
    )

    subjects = router.route_activity(
        {
            "id": "msg-3",
            "text": "Give me a status update.",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        }
    )

    assert subjects == ["agent.product-manager"]
    broker.ensure_consumer("agent-inbox", "pm", filter_subject="agent.product-manager")
    payload = broker.fetch("agent-inbox", "pm")[0].payload
    assert payload["route_type"] == "role_dm"
    assert payload["conversation_ref"] == "dm:product-manager"


def test_teams_activity_router_records_conversation_context(tmp_path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["project.context", "agent.product-manager"])
    db_path = tmp_path / "v3.sqlite3"
    router = TeamsActivityRouter(
        LocalTeamsBridge(broker),
        role_identities=(
            TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),
        ),
        conversation_recorder=DatabaseConversationRecorder(db_path),
    )

    subjects = router.route_activity(
        {
            "id": "msg-3",
            "text": "<at>AM-Product Manager</at> please review this.",
            "conversation": {"id": "channel-conv", "conversationType": "channel"},
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
            "channelData": {
                "team": {"id": "team-1"},
                "channel": {"id": "channel-1"},
            },
            "entities": [
                {
                    "type": "mention",
                    "mentioned": {"id": "bot-product", "name": "AM-Product Manager"},
                }
            ],
        }
    )

    db = V3Database(db_path)
    try:
        db.migrate()
        messages = db.list_conversation_messages("team:team-1/channel:channel-1")
    finally:
        db.close()

    assert subjects == ["project.context", "agent.product-manager"]
    assert messages[0]["message_id"] == "msg-3"
    assert messages[0]["text"] == "AM-Product Manager please review this."
    assert messages[0]["sender_ref"] == "user-1"
    assert messages[0]["mentioned_roles"] == ("product-manager",)


def test_teams_role_identities_load_from_project_config(tmp_path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
roles:
  product-manager:
    instances: 1
connectors:
  teams:
    role_bots:
      product-manager:
        display_name: AM-Product Manager
        bot_id_ref: bot-product
""",
        encoding="utf-8",
    )

    identities = teams_role_identities_from_project_config(load_project_config(project_file))

    assert identities == (TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),)
