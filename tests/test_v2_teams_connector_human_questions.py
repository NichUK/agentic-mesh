from pathlib import Path

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.safe_outputs import SafeOutputCall


class StaticWorker:
    def __init__(self, calls: list[SafeOutputCall]) -> None:
        self.calls = calls

    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        return self.calls


def _config() -> ConnectorConfig:
    return ConnectorConfig.from_dict(
        {
            "connector_id": "teams-agentic-mesh-dev",
            "project_id": "agentic-mesh-dev",
            "connector_type": "teams",
            "display_name": "Agentic Mesh Dev Teams",
            "project_team_ref": "team-dev",
            "default_project_channel_ref": "channel-project",
            "external_base_url": "http://linuxch:8100",
            "role_identities": {
                "product-manager": {
                    "external_ref": "bot-product-manager",
                    "display_name": "AM-Product Manager",
                    "alias": "product-manager",
                    "mention_handle": "@AM-Product Manager",
                    "identity_model": "separate_bot",
                    "enabled": True,
                },
            },
            "human_authorities": {
                "nicholas": ["sponsor", "operator"],
            },
            "retention": {
                "private_dm_days": 30,
                "project_channel_days": 90,
                "compacted_summary_days": 365,
                "delivery_record_days": 90,
                "idempotency_receipt_days": 30,
            },
            "team_wide_trigger": "@all-agents",
        }
    )


def test_agent_initiated_human_question_creates_delivery_and_thread_binding(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-dm-question-source",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "Please shape the Teams connector feature.",
        }
    )

    role = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="product-manager-1",
        safe_outputs=ConnectorSafeOutputService(db, adapter=adapter),
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="sponsor.ask_question",
                    payload={
                        "question": "Should feature channels be manually configured for v2?",
                        "reason": "This affects connector configuration scope.",
                        "conversation_id": replayed.conversation_id,
                        "destination_ref": "dm-nicholas-product",
                        "destination_type": "dm",
                        "thread_ref": "thread-human-question-1",
                        "work_item_id": "work-teams-connector",
                    },
                    terminal=True,
                )
            ]
        ),
    )
    receipt = role.run_assignment(
            RoleAssignment(
                role_id="product-manager",
                role_instance_id="product-manager-1",
                work_item_id=None,
                title="Teams connector shaping",
                summary="Ask sponsor for a connector scope decision.",
                conversation_context=("Please shape the Teams connector feature.",),
            )
    )
    assert receipt.terminal_tool == "sponsor.ask_question"

    reply = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-human-answer-1",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "thread_ref": "thread-human-question-1",
            "body": "Manual configuration first, runtime-created channels later.",
        }
    )
    assert reply.route_type == "role_direct_message"

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["safe_output_calls"] == 1
    assert snapshot["safe_output_calls"][0]["payload"]["question"] == "[redacted private conversation]"
    assert snapshot["safe_output_calls"][0]["payload"]["reason"] == "[redacted private conversation]"
    assert snapshot["counts"]["delivery_records"] == 1
    assert snapshot["delivery_records"][0]["purpose"] == "sponsor.ask_question"
    assert snapshot["delivery_records"][0]["status"] == "sent"
    assert snapshot["delivery_records"][0]["payload"]["body"] == "[redacted private conversation]"
    assert snapshot["delivery_records"][0]["payload"]["body_redacted"] is True
    assert snapshot["counts"]["thread_bindings"] == 2
    bindings = {binding["binding_type"]: binding for binding in snapshot["thread_bindings"]}
    assert bindings["human_question"]["target_ref"] == "work-teams-connector"
    assert bindings["teams_thread"]["target_ref"] == "work-teams-connector"
    assert snapshot["counts"]["connector_attention_items"] == 0
    assert snapshot["counts"]["conversation_events"] == 2
    assert all(event["body_preview"] == "[redacted private conversation]" for event in snapshot["conversation_events"])


def test_unbound_private_thread_reply_creates_attention(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-human-answer-unbound",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "thread_ref": "thread-with-no-question-binding",
            "body": "This answer has no stored work/question binding.",
        }
    )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["connector_attention_items"] == 1
    attention = snapshot["connector_attention_items"][0]
    assert attention["reason_class"] == "unbound_thread_reply"
    assert attention["source_ref"].startswith("receipt-")
    assert snapshot["thread_bindings"][0]["binding_type"] == "teams_thread"
    assert snapshot["thread_bindings"][0]["target_ref"] == "bot-product-manager"
    assert snapshot["conversation_events"][0]["body_preview"] == "[redacted private conversation]"


def test_product_sponsor_ready_creates_dm_response_card_from_channel_source(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_queue_item(
        queue_item_id="queue-product-signoff",
        title="Product signoff slice",
        summary="Exercise product sign-off card delivery.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-product-signoff", actor_role="product-manager", reason="Ready to shape.")
    db.promote_queue_item(
        queue_item_id="queue-product-signoff",
        work_item_id="work-product-signoff",
        owner_role="product-manager",
    )
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-product-signoff-source",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Product Manager shape this small dashboard feature.",
            "mentioned_role_refs": ["@AM-Product Manager"],
            "thread_ref": "thread-product-signoff",
            "service_url": "https://smba.trafficmanager.net/uk/tenant/",
        }
    )

    role = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="product-manager-1",
        safe_outputs=ConnectorSafeOutputService(db, adapter=adapter),
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="product.mark_sponsor_ready",
                    payload={
                        "work_item_id": "work-product-signoff",
                        "summary": "The product definition is ready for sponsor review.",
                    },
                    terminal=True,
                )
            ]
        ),
    )
    receipt = role.run_next_assignment()

    snapshot = db.status_snapshot()
    request = snapshot["human_response_requests"][0]
    delivery = snapshot["delivery_records"][0]
    raw_delivery = db.get_delivery_record(str(delivery["delivery_id"]))
    work_item = db.get_work_item("work-product-signoff")
    assert receipt is not None
    assert receipt.terminal_tool == "product.mark_sponsor_ready"
    assert work_item.state == "waiting_human"
    assert request["connector_id"] == "teams-agentic-mesh-dev"
    assert request["request_type"] == "product_signoff"
    assert request["destination_ref"].startswith("dm-")
    assert request["destination_type"] == "dm"
    assert request["thread_ref"] == "thread-product-signoff"
    assert delivery["purpose"] == "product_signoff.card"
    assert delivery["destination_ref"].startswith("dm-")
    assert delivery["destination_type"] == "dm"
    assert delivery["payload"]["card"]["response_contract_id"] == "product-signoff-v1"
    assert delivery["payload"]["body"] == "[redacted private conversation]"
    assert raw_delivery is not None
    assert raw_delivery["payload"]["body"].startswith("**Product sign-off:")
    assert raw_delivery["payload"]["recipient_ref"] == "nicholas"
    assert snapshot["counts"]["connector_attention_items"] == 0


def test_product_sponsor_ready_preserves_agent_channel_override_with_reason(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_queue_item(
        queue_item_id="queue-product-signoff-channel",
        title="Product signoff channel override",
        summary="Exercise product sign-off route override.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-product-signoff-channel", actor_role="product-manager", reason="Ready to shape.")
    db.promote_queue_item(
        queue_item_id="queue-product-signoff-channel",
        work_item_id="work-product-signoff-channel",
        owner_role="product-manager",
    )
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-product-signoff-channel-source",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Product Manager keep this sign-off in the project thread.",
            "mentioned_role_refs": ["@AM-Product Manager"],
            "thread_ref": "thread-product-signoff-channel",
            "service_url": "https://smba.trafficmanager.net/uk/tenant/",
        }
    )

    role = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="product-manager-1",
        safe_outputs=ConnectorSafeOutputService(db, adapter=adapter),
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="product.mark_sponsor_ready",
                    payload={
                        "work_item_id": "work-product-signoff-channel",
                        "summary": "The product definition is ready for sponsor review.",
                        "conversation_id": replayed.conversation_id,
                        "destination_ref": "channel-project",
                        "destination_type": "channel",
                        "route_override_reason": "Sponsor explicitly asked to keep this sign-off in the project thread.",
                    },
                    terminal=True,
                )
            ]
        ),
    )
    receipt = role.run_next_assignment()

    snapshot = db.status_snapshot()
    request = snapshot["human_response_requests"][0]
    delivery = snapshot["delivery_records"][0]
    assert receipt is not None
    assert request["destination_ref"] == "channel-project"
    assert request["destination_type"] == "channel"
    assert delivery["destination_ref"] == "channel-project"
    assert delivery["destination_type"] == "channel"
    assert delivery["payload"]["recipient_ref"] is None
    assert snapshot["counts"]["connector_attention_items"] == 0
