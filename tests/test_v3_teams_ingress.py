from __future__ import annotations

from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.project_config import load_project_config
from agentic_mesh_v3.teams_ingress import DatabaseConversationRecorder
from agentic_mesh_v3.teams_ingress import DatabaseApprovalResponseRecorder
from agentic_mesh_v3.teams_ingress import DatabaseStakeholderQuestionResponseRecorder
from agentic_mesh_v3.teams_ingress import TeamsActivityRouter
from agentic_mesh_v3.teams_ingress import TeamsRoleIdentity
from agentic_mesh_v3.teams_ingress import approval_response_from_text
from agentic_mesh_v3.teams_ingress import normalize_teams_activity
from agentic_mesh_v3.teams_ingress import stakeholder_question_response_id_from_text
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


def test_stakeholder_question_response_parser_requires_question_id() -> None:
    assert stakeholder_question_response_id_from_text("question-1: use DMs please") == "question-1"
    assert stakeholder_question_response_id_from_text("answered") is None


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
        route_event = db.connection.execute(
            """
            SELECT event_type, aggregate_id, payload_json
            FROM events
            WHERE aggregate_id='msg-3' AND event_type='teams_activity.route_result'
            """
        ).fetchone()
        journal_rows = db.connection.execute(
            """
            SELECT stage, status, broker_subject
            FROM message_journal
            WHERE correlation_id='corr-msg-3'
            ORDER BY created_at ASC, journal_id ASC
            """
        ).fetchall()
    finally:
        db.close()

    assert subjects == ["project.context", "agent.product-manager"]
    assert messages[0]["message_id"] == "msg-3"
    assert messages[0]["text"] == "AM-Product Manager please review this."
    assert messages[0]["sender_ref"] == "user-1"
    assert messages[0]["mentioned_roles"] == ("product-manager",)
    assert route_event is not None
    assert '"status": "routed"' in route_event["payload_json"]
    assert '"agent.product-manager"' in route_event["payload_json"]
    assert sorted(row["stage"] for row in journal_rows) == ["published", "published", "received", "routed", "routed"]
    assert {row["broker_subject"] for row in journal_rows if row["broker_subject"]} == {
        "project.context",
        "agent.product-manager",
    }


def test_teams_activity_router_records_route_failure_after_context_capture(tmp_path) -> None:
    class FailingBridge:
        def route_inbound(self, message):  # noqa: ANN001
            raise RuntimeError("broker unavailable")

    db_path = tmp_path / "v3.sqlite3"
    router = TeamsActivityRouter(
        FailingBridge(),  # type: ignore[arg-type]
        role_identities=(
            TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),
        ),
        conversation_recorder=DatabaseConversationRecorder(db_path),
    )

    try:
        router.route_activity(
            {
                "id": "msg-failed-route",
                "text": "Please respond.",
                "conversation": {"id": "dm-1", "conversationType": "personal"},
                "from": {"id": "user-1"},
                "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
            }
        )
    except RuntimeError as exc:
        assert "broker unavailable" in str(exc)
    else:
        raise AssertionError("route failure should be surfaced")

    db = V3Database(db_path)
    try:
        db.migrate()
        messages = db.list_conversation_messages("dm:product-manager")
        route_event = db.connection.execute(
            """
            SELECT payload_json
            FROM events
            WHERE aggregate_id='msg-failed-route' AND event_type='teams_activity.route_result'
            """
        ).fetchone()
    finally:
        db.close()

    assert messages[0]["message_id"] == "msg-failed-route"
    assert route_event is not None
    assert '"status": "failed"' in route_event["payload_json"]
    assert "RuntimeError: broker unavailable" in route_event["payload_json"]


def test_teams_activity_router_routes_even_when_sidecar_recorder_fails(tmp_path) -> None:
    class FailingApprovalRecorder:
        def record(self, message):  # noqa: ANN001
            raise RuntimeError("approval database unavailable")

    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    db_path = tmp_path / "v3.sqlite3"
    router = TeamsActivityRouter(
        LocalTeamsBridge(broker),
        role_identities=(
            TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),
        ),
        conversation_recorder=DatabaseConversationRecorder(db_path),
        approval_response_recorder=FailingApprovalRecorder(),
    )

    subjects = router.route_activity(
        {
            "id": "msg-sidecar-failed",
            "text": "Please respond.",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        }
    )

    broker.ensure_consumer("agent-inbox", "pm", filter_subject="agent.product-manager")
    message = broker.fetch("agent-inbox", "pm")[0]
    db = V3Database(db_path)
    try:
        db.migrate()
        route_event = db.connection.execute(
            """
            SELECT payload_json
            FROM events
            WHERE aggregate_id='msg-sidecar-failed' AND event_type='teams_activity.route_result'
            """
        ).fetchone()
    finally:
        db.close()

    assert subjects == ["agent.product-manager"]
    assert message.payload["text"] == "Please respond."
    assert route_event is not None
    assert '"status": "routed"' in route_event["payload_json"]
    assert "approval_response_recorder: RuntimeError: approval database unavailable" in route_event["payload_json"]


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


def test_teams_activity_router_records_stakeholder_question_response_and_notifies_asking_role(tmp_path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    db_path = tmp_path / "v3.sqlite3"
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Answer question",
            description="Needs sponsor answer.",
            state="waiting_human",
            owner_role="sponsor",
            current_phase="product-shaping",
        )
        db.record_governance_record(
            record_id="question-1",
            work_item_id="work-1",
            record_type="stakeholder.ask_question",
            role_instance_id="agentic-mesh-dev.product-manager.1",
            target_ref="dm:sponsor",
            summary="Use DMs for approvals?",
            status="requested",
            payload={"question": "Use DMs for approvals?"},
        )
    finally:
        db.close()

    router = TeamsActivityRouter(
        LocalTeamsBridge(broker),
        role_identities=(
            TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),
        ),
        question_response_recorder=DatabaseStakeholderQuestionResponseRecorder(db_path, broker=broker),
    )

    subjects = router.route_activity(
        {
            "id": "msg-question-answer",
            "text": "question-1: yes, use direct messages for approvals.",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1", "name": "Nicholas"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        }
    )

    db = V3Database(db_path)
    try:
        db.migrate()
        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert subjects == ["agent.product-manager"]
    assert detail is not None
    assert detail.state == "waiting_agent"
    assert detail.owner_role == "product-manager"
    assert detail.governance_records[0].status == "answered"
    broker.ensure_consumer("agent-inbox", "pm", filter_subject="agent.product-manager")
    messages = broker.fetch("agent-inbox", "pm", batch=10)
    assert [message.payload["message_type"] for message in messages] == [
        "stakeholder.question_answered",
        "stakeholder.message",
    ]
    assert messages[0].payload["question_id"] == "question-1"
    assert messages[0].payload["work_item_id"] == "work-1"
    assert messages[0].payload["source_message_id"] == "msg-question-answer"


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
