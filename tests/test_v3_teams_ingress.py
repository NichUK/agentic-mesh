from __future__ import annotations

from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.teams_ingress import TeamsRoleIdentity
from agentic_mesh_v3.teams_ingress import normalize_teams_activity


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
