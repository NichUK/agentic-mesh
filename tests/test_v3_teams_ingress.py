from __future__ import annotations

from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.project_config import load_project_config
from agentic_mesh_v3.teams_ingress import DatabaseConversationRecorder
from agentic_mesh_v3.teams_ingress import DatabaseApprovalResponseRecorder
from agentic_mesh_v3.teams_ingress import TeamsActivityRouter
from agentic_mesh_v3.teams_ingress import TeamsRoleIdentity
from agentic_mesh_v3.teams_ingress import approval_response_from_text
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
    assert message.reply_target_ref == "chat:dm-1"
    assert message.reply_thread_ref is None
    assert message.sender_ref == "user-1"
    assert message.text == "Give me a status update."


def test_normalize_personal_activity_falls_back_to_agent_display_name() -> None:
    message = normalize_teams_activity(
        {
            "id": "msg-1",
            "text": "Give me a status update.",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1", "name": "Nicholas"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        },
        role_identities=(),
    )

    assert message.source_type == "dm"
    assert message.conversation_ref == "dm:product-manager"
    assert message.reply_target_ref == "chat:dm-1"


def test_approval_response_parser_requires_id_and_decision() -> None:
    assert approval_response_from_text("approval-1 approved") == ("approval-1", "approved")
    assert approval_response_from_text("human-response-abc: rejected") == ("human-response-abc", "rejected")
    assert approval_response_from_text("approval-product-1 changes requested") == (
        "approval-product-1",
        "changes_requested",
    )
    assert approval_response_from_text("approved") is None
    assert approval_response_from_text("approval-1 noted") is None


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
    assert message.reply_target_ref == "team:team-1/channel:channel-1"
    assert message.reply_thread_ref == "root-msg-1"
    assert message.mentioned_roles == ("product-manager",)
    assert message.text == "AM-Product Manager please review this."


def test_normalize_channel_activity_falls_back_to_agent_mention_name_only() -> None:
    message = normalize_teams_activity(
        {
            "id": "msg-2",
            "text": "<at>AM-QA Engineer</at> please review this with <at>Nicholas Overend</at>.",
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
                    "mentioned": {"id": "bot-qa", "name": "AM-QA Engineer"},
                },
                {
                    "type": "mention",
                    "mentioned": {"id": "human-1", "name": "Nicholas Overend"},
                },
            ],
        },
        role_identities=(),
    )

    assert message.mentioned_roles == ("qa-engineer",)


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

    assert message.reply_target_ref == "team:team-1/channel:channel-1"
    assert message.reply_thread_ref == "msg-2"
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
    assert payload["reply_target_ref"] == "chat:dm-1"
    assert payload["reply_thread_ref"] is None


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


def test_teams_activity_router_records_approval_response_and_notifies_requesting_role(tmp_path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    db_path = tmp_path / "v3.sqlite3"
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Approve product definition",
            description="Needs sponsor sign-off.",
            state="waiting_human",
            owner_role="sponsor",
        )
        db.request_approval(
            approval_id="approval-1",
            work_item_id="work-1",
            requested_by_role="product-manager",
            question="Approve product definition?",
        )
    finally:
        db.close()

    router = TeamsActivityRouter(
        LocalTeamsBridge(broker),
        role_identities=(
            TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),
        ),
        approval_response_recorder=DatabaseApprovalResponseRecorder(db_path, broker=broker),
    )

    subjects = router.route_activity(
        {
            "id": "msg-approval",
            "text": "approval-1 approved",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1", "name": "Nicholas"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        }
    )

    db = V3Database(db_path)
    try:
        db.migrate()
        detail = db.work_item_detail("work-1")
        approval = db.approval_detail("approval-1")
    finally:
        db.close()

    assert subjects == ["agent.product-manager"]
    assert detail is not None
    assert detail.state == "waiting_agent"
    assert detail.owner_role == "product-manager"
    assert approval is not None
    assert approval["status"] == "approved"
    assert approval["response"] == "approval-1 approved"
    broker.ensure_consumer("agent-inbox", "pm", filter_subject="agent.product-manager")
    messages = broker.fetch("agent-inbox", "pm", batch=10)
    assert [message.payload["message_type"] for message in messages] == [
        "approval.response_recorded",
        "stakeholder.message",
    ]
    assert messages[0].payload["approval_id"] == "approval-1"
    assert messages[0].payload["status"] == "approved"
    assert messages[0].payload["source_message_id"] == "msg-approval"


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
