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


def _create_product_signoff_request(
    tmp_path: Path,
    *,
    work_item_id: str = "work-product-signoff-gate",
) -> tuple[V2Database, LocalTeamsTestAdapter, str]:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    queue_item_id = f"queue-{work_item_id}"
    db.create_queue_item(
        queue_item_id=queue_item_id,
        title="Product signoff gate slice",
        summary="Exercise product sign-off gate behavior.",
        owner_role="product-manager",
    )
    db.mark_queue_ready(queue_item_id, actor_role="product-manager", reason="Ready to shape.")
    db.promote_queue_item(
        queue_item_id=queue_item_id,
        work_item_id=work_item_id,
        owner_role="product-manager",
    )
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id=f"run-{work_item_id}-signoff",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=work_item_id,
    )
    service.record(
        run_id=f"run-{work_item_id}-signoff",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="product.mark_sponsor_ready",
            payload={
                "work_item_id": work_item_id,
                "summary": "The product definition is ready for sponsor review.",
                "connector_id": "teams-agentic-mesh-dev",
                "destination_ref": "dm-nicholas",
                "destination_type": "dm",
            },
            terminal=True,
        ),
    )
    return db, adapter, str(db.status_snapshot()["human_response_requests"][0]["request_id"])


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
    db.add_artifact(
        artifact_id="artifact-product-definition",
        work_item_id="work-product-signoff",
        path="work-items/work-product-signoff/020-product-definition.md",
        document_type="product_definition",
        status="drafted",
        created_by_role="product-manager",
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
    assert "[Open work item](http://linuxch:8100/work-items/work-product-signoff)" in raw_delivery["payload"]["body"]
    assert (
        "[Open product definition]"
        "(http://linuxch:8100/artifact-viewer/work-items%2Fwork-product-signoff%2F020-product-definition.md)"
        in raw_delivery["payload"]["body"]
    )
    assert raw_delivery["payload"]["card"]["links"] == [
        {"title": "Open work item", "url": "http://linuxch:8100/work-items/work-product-signoff"},
        {
            "title": "Open product definition",
            "url": "http://linuxch:8100/artifact-viewer/work-items%2Fwork-product-signoff%2F020-product-definition.md",
        },
    ]
    assert raw_delivery["payload"]["card"]["actions"][0]["type"] == "Action.OpenUrl"
    assert raw_delivery["payload"]["recipient_ref"] == "nicholas"
    assert snapshot["counts"]["connector_attention_items"] == 0

    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-product-signoff-thread-reply",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "Please send this approval to me as a direct message.",
            "thread_ref": "thread-product-signoff",
            "reply_to_id": "msg-product-signoff-thread-reply",
            "service_url": "https://smba.trafficmanager.net/uk/tenant/",
        }
    )
    assignments = db.status_snapshot()["role_assignments"]
    assert any(
        assignment["assignment_type"] == "bound_thread_reply"
        and assignment["role_id"] == "product-manager"
        and assignment["payload"]["human_response_request_id"] == request["request_id"]
        for assignment in assignments
    )


def test_product_signoff_approval_advances_work_to_implementation(tmp_path: Path) -> None:
    db, adapter, request_id = _create_product_signoff_request(tmp_path)

    submission_id = adapter.submit_card_response(
        request_id=request_id,
        responder_ref="nicholas",
        response_value="approved",
        comment="Approved for build.",
    )

    snapshot = db.status_snapshot()
    work_item = db.get_work_item("work-product-signoff-gate")
    implementation_assignments = [
        assignment
        for assignment in snapshot["role_assignments"]
        if assignment["assignment_type"] == "implementation"
    ]
    followups = [
        assignment
        for assignment in snapshot["role_assignments"]
        if assignment["assignment_type"] == "human_response_followup"
    ]
    assert work_item.state == "ready"
    assert snapshot["work_items"][0]["current_role"] == "engineering"
    assert len(implementation_assignments) == 1
    assert implementation_assignments[0]["role_id"] == "engineering"
    assert implementation_assignments[0]["source_ref"] == submission_id
    assert implementation_assignments[0]["payload"]["request_id"] == request_id
    assert implementation_assignments[0]["payload"]["response_value"] == "approve"
    assert followups == []


def test_product_signoff_request_changes_returns_work_to_product_rework(tmp_path: Path) -> None:
    db, adapter, request_id = _create_product_signoff_request(
        tmp_path,
        work_item_id="work-product-signoff-rework",
    )

    submission_id = adapter.submit_card_response(
        request_id=request_id,
        responder_ref="nicholas",
        response_value="request changes",
        comment="Tighten the acceptance criteria.",
    )

    snapshot = db.status_snapshot()
    work_item = db.get_work_item("work-product-signoff-rework")
    rework_assignments = [
        assignment
        for assignment in snapshot["role_assignments"]
        if assignment["assignment_type"] == "product_rework"
    ]
    raw_rework_assignment = db.get_role_assignment(rework_assignments[0]["assignment_id"])
    assert work_item.state == "shaping"
    assert snapshot["work_items"][0]["current_role"] == "product-manager"
    assert len(rework_assignments) == 1
    assert rework_assignments[0]["role_id"] == "product-manager"
    assert rework_assignments[0]["source_ref"] == submission_id
    assert rework_assignments[0]["payload"]["response_value"] == "request_changes"
    assert raw_rework_assignment is not None
    assert raw_rework_assignment["payload"]["submission_comment"] == "Tighten the acceptance criteria."


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
