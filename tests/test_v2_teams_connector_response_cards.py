from pathlib import Path

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall


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
                "release-manager": {
                    "external_ref": "bot-release-manager",
                    "display_name": "AM-Release Manager",
                    "alias": "release-manager",
                    "mention_handle": "@AM-Release Manager",
                    "identity_model": "separate_bot",
                    "enabled": True,
                },
            },
            "human_authorities": {
                "nicholas": ["sponsor", "operator", "release_approver"],
                "observer": [],
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


def _db_with_work_item(tmp_path: Path) -> tuple[V2Database, LocalTeamsTestAdapter, str]:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_queue_item(
        queue_item_id="queue-release-card",
        title="Release card slice",
        summary="Exercise release approval card behavior.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-release-card", actor_role="product-manager", reason="Ready for release test.")
    db.promote_queue_item(
        queue_item_id="queue-release-card",
        work_item_id="work-release-card",
        owner_role="product-manager",
    )
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-release-thread",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Release Manager please request release approval.",
            "mentioned_role_refs": ["@AM-Release Manager"],
            "thread_ref": "thread-release",
        }
    )
    return db, adapter, replayed.conversation_id


def _request_release_approval(
    db: V2Database,
    adapter: LocalTeamsTestAdapter,
    *,
    conversation_id: str,
    delivery_outcome: str = "sent",
    include_title: bool = True,
) -> str:
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-release-card",
        role_id="release-manager",
        role_instance_id="release-manager-1",
        work_item_id="work-release-card",
    )
    payload = {
        "work_item_id": "work-release-card",
        "question": "Approve release of work-release-card?",
        "conversation_id": conversation_id,
        "destination_ref": "channel-project",
        "destination_type": "channel",
        "thread_ref": "thread-release",
        "gate_id": "release_decision_response",
        "response_contract_id": "release-decision-v1",
        "required_authority": "release_approver",
        "delivery_outcome": delivery_outcome,
    }
    if include_title:
        payload["title"] = "Approve release of release card slice"
    service.record(
        run_id="run-release-card",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="release.request_approval",
            payload=payload,
            terminal=True,
        ),
    )
    return db.status_snapshot()["human_response_requests"][0]["request_id"]


def test_release_approval_card_delivery_and_authorized_submission(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)

    request_id = _request_release_approval(db, adapter, conversation_id=conversation_id)
    submission_id = adapter.submit_card_response(
        request_id=request_id,
        responder_ref="nicholas",
        response_value="approved",
        comment="Looks good.",
    )
    duplicate_submission_id = adapter.submit_card_response(
        request_id=request_id,
        responder_ref="nicholas",
        response_value="approved",
        comment="Looks good.",
    )

    snapshot = db.status_snapshot()
    request = snapshot["human_response_requests"][0]
    submissions = snapshot["human_response_submissions"]
    deliveries = {record["purpose"]: record for record in snapshot["delivery_records"]}
    followups = [
        assignment
        for assignment in snapshot["role_assignments"]
        if assignment["assignment_type"] == "human_response_followup"
    ]
    assert len(snapshot["human_response_requests"]) == 1
    assert request["connector_id"] == "teams-agentic-mesh-dev"
    assert request["status"] == "responded"
    assert duplicate_submission_id == submission_id
    assert request["response_value"] == "approve"
    assert request["responder_ref"] == "nicholas"
    assert request["required_authority"] == "release_approver"
    assert request["gate_id"] == "release_decision_response"
    assert submissions[0]["submission_id"] == submission_id
    assert submissions[0]["status"] == "accepted"
    assert submissions[0]["normalized_value"] == "approve"
    assert "release_approver" in submissions[0]["authority"]
    assert deliveries["release_approval.card"]["status"] == "sent"
    assert deliveries["release_approval.card"]["payload"]["card"]["type"] == "AdaptiveCard"
    assert deliveries["card.update"]["status"] == "sent"
    assert len(followups) == 1
    assert followups[0]["role_id"] == "release-manager"
    assert followups[0]["work_item_id"] == "work-release-card"
    assert followups[0]["source_ref"] == submission_id
    assert followups[0]["payload"]["request_id"] == request_id
    assert followups[0]["payload"]["question"] == "Approve release of work-release-card?"
    assert followups[0]["payload"]["response_value"] == "approve"
    assert followups[0]["payload"]["submission_comment"] == "Looks good."
    assert "release.deploy" in followups[0]["payload"]["allowed_tools"]
    assert any(binding["binding_type"] == "human_response" for binding in snapshot["thread_bindings"])
    assert snapshot["counts"]["connector_attention_items"] == 0


def test_release_approval_request_preserves_connector_metadata_and_default_title(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)

    _request_release_approval(
        db,
        adapter,
        conversation_id=conversation_id,
        include_title=False,
    )

    snapshot = db.status_snapshot()
    request = snapshot["human_response_requests"][0]
    delivery = snapshot["delivery_records"][0]
    connector = next(item for item in snapshot["connectors"] if item["connector_id"] == "teams-agentic-mesh-dev")
    assert connector["connector_type"] == "teams"
    assert connector["display_name"] == "Agentic Mesh Dev Teams"
    assert request["title"] == "Release approval requested"
    assert request["payload"]["card"]["title"] == "Release approval requested"
    assert delivery["payload"]["card"]["title"] == "Release approval requested"


def test_unauthorized_approval_submission_fails_closed(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)
    request_id = _request_release_approval(db, adapter, conversation_id=conversation_id)

    adapter.submit_card_response(
        request_id=request_id,
        responder_ref="observer",
        response_value="approve",
        comment="I am not allowed to approve.",
    )

    snapshot = db.status_snapshot()
    assert snapshot["human_response_requests"][0]["status"] == "awaiting_response"
    assert snapshot["human_response_submissions"][0]["status"] == "rejected_unauthorized"
    assert snapshot["human_response_submissions"][0]["normalized_value"] == "approve"
    attention = snapshot["connector_attention_items"][0]
    assert attention["reason_class"] == "unauthorized_card_submission"
    assert attention["retryable"] is False
    assert snapshot["counts"]["delivery_records"] == 1


def test_stale_card_submission_records_attention_without_mutating_response(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)
    request_id = _request_release_approval(db, adapter, conversation_id=conversation_id)
    adapter.submit_card_response(
        request_id=request_id,
        responder_ref="nicholas",
        response_value="approve",
    )

    adapter.submit_card_response(
        request_id=request_id,
        responder_ref="nicholas",
        response_value="reject",
        submission_id="submission-stale",
    )

    snapshot = db.status_snapshot()
    request = snapshot["human_response_requests"][0]
    submissions = {item["submission_id"]: item for item in snapshot["human_response_submissions"]}
    followups = [
        assignment
        for assignment in snapshot["role_assignments"]
        if assignment["assignment_type"] == "human_response_followup"
    ]
    assert request["status"] == "responded"
    assert request["response_value"] == "approve"
    assert submissions["submission-stale"]["status"] == "rejected_stale"
    assert submissions["submission-stale"]["normalized_value"] == "reject"
    assert len(followups) == 1
    assert followups[0]["payload"]["response_value"] == "approve"
    assert any(item["reason_class"] == "stale_card_submission" for item in snapshot["connector_attention_items"])


def test_card_delivery_failure_creates_attention_item(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)

    _request_release_approval(
        db,
        adapter,
        conversation_id=conversation_id,
        delivery_outcome="failed_transient",
    )

    snapshot = db.status_snapshot()
    assert snapshot["human_response_requests"][0]["status"] == "awaiting_response"
    assert snapshot["delivery_records"][0]["purpose"] == "release_approval.card"
    assert snapshot["delivery_records"][0]["status"] == "failed_transient"
    assert snapshot["connector_attention_items"][0]["reason_class"] == "delivery_failed_transient"


def test_invalid_card_submission_records_rejection_and_attention(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)
    request_id = _request_release_approval(db, adapter, conversation_id=conversation_id)

    submission_id = adapter.submit_card_response(
        request_id=request_id,
        responder_ref="nicholas",
        response_value="maybe",
        comment="This is not a configured card action.",
    )

    snapshot = db.status_snapshot()
    assert snapshot["human_response_requests"][0]["status"] == "awaiting_response"
    submission = snapshot["human_response_submissions"][0]
    assert submission["submission_id"] == submission_id
    assert submission["status"] == "rejected_invalid"
    assert submission["normalized_value"] == "invalid"
    attention = snapshot["connector_attention_items"][0]
    assert attention["reason_class"] == "invalid_card_submission"
    assert attention["retryable"] is True
    assert snapshot["counts"]["delivery_records"] == 1


def test_general_human_response_request_uses_structured_card(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-product-response",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "Ask me to confirm the product direction.",
        }
    )
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-human-response",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )

    service.record(
        run_id="run-human-response",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="human_response.request",
            payload={
                "title": "Confirm PRIVATE_RESPONSE_SENTINEL product direction",
                "question": "Should PRIVATE_RESPONSE_SENTINEL be shaped as a dashboard refinement?",
                "response_contract_id": "product-direction-v1",
                "required_authority": "sponsor",
                "conversation_id": replayed.conversation_id,
                "destination_ref": "dm-nicholas-product",
                "destination_type": "dm",
            },
            terminal=True,
        ),
    )

    snapshot = db.status_snapshot()
    followups = [
        assignment
        for assignment in snapshot["role_assignments"]
        if assignment["assignment_type"] == "human_response_followup"
    ]
    assert snapshot["human_response_requests"][0]["request_type"] == "human_response"
    assert snapshot["human_response_requests"][0]["required_authority"] == "sponsor"
    assert snapshot["human_response_requests"][0]["title"] == "[redacted private conversation]"
    assert snapshot["human_response_requests"][0]["question"] == "[redacted private conversation]"
    assert snapshot["delivery_records"][0]["purpose"] == "human_response.card"
    assert snapshot["delivery_records"][0]["payload"]["card"]["response_contract_id"] == "product-direction-v1"
    assert followups == []
    assert "PRIVATE_RESPONSE_SENTINEL" not in str(snapshot)


def test_general_human_response_submission_queues_requesting_role_followup(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-product-followup",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "Ask me to confirm the product direction.",
        }
    )
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-human-response-followup",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )
    service.record(
        run_id="run-human-response-followup",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="human_response.request",
            payload={
                "title": "Confirm PRIVATE_FOLLOWUP_SENTINEL product direction",
                "question": "Should PRIVATE_FOLLOWUP_SENTINEL be shaped as a dashboard refinement?",
                "response_contract_id": "product-direction-v1",
                "required_authority": "sponsor",
                "conversation_id": replayed.conversation_id,
                "destination_ref": "dm-nicholas-product",
                "destination_type": "dm",
            },
            terminal=True,
        ),
    )
    request_id = db.status_snapshot()["human_response_requests"][0]["request_id"]

    submission_id = adapter.submit_card_response(
        request_id=request_id,
        responder_ref="nicholas",
        response_value="request_changes",
        comment="PRIVATE_FOLLOWUP_SENTINEL make it broader than headings.",
    )

    snapshot = db.status_snapshot()
    followups = [
        assignment
        for assignment in snapshot["role_assignments"]
        if assignment["assignment_type"] == "human_response_followup"
    ]
    assert len(followups) == 1
    assert followups[0]["role_id"] == "product-manager"
    assert followups[0]["work_item_id"] is None
    assert followups[0]["source_ref"] == submission_id
    assert followups[0]["visibility_scope"] == "private"
    assert followups[0]["title"] == "[redacted private conversation]"
    assert followups[0]["summary"] == "[redacted private conversation]"
    assert followups[0]["payload"]["request_id"] == request_id
    assert followups[0]["payload"]["response_value"] == "request_changes"
    assert followups[0]["payload"]["question"] == "[redacted private conversation]"
    assert followups[0]["payload"]["submission_comment"] == "[redacted private conversation]"
    assert "queue.propose_item" in followups[0]["payload"]["allowed_tools"]
    assert "PRIVATE_FOLLOWUP_SENTINEL" not in str(snapshot)
