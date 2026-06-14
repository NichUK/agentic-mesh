from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputError
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.state_machine import TransitionRequest


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
        "route_override_reason": "Release approval is being exercised in the project channel test fixture.",
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


def _move_release_card_to_release_review(db: V2Database) -> None:
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-card",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Product ready.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-card",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering complete.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-card",
            from_state="active",
            to_state="release_review",
            actor_role="qa-engineer",
            reason="QA passed.",
        )
    )


def _record_release_card_decision(service: ConnectorSafeOutputService, *, run_id: str) -> str:
    return service.record(
        run_id=run_id,
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="release.record_decision",
            payload={
                "work_item_id": "work-release-card",
                "decision": "approve",
                "reason": "Sponsor approved release after reviewing evidence.",
            },
        ),
    )


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


def test_direct_message_inline_response_submissions_record_approvals_and_still_reach_agent(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)
    for index in range(2):
        db.create_run(
            run_id=f"run-product-signoff-{index}",
            role_id="product-manager",
            role_instance_id="product-manager-1",
            work_item_id=None,
        )
        service.record(
            run_id=f"run-product-signoff-{index}",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="human_response.request",
                payload={
                    "title": f"Product sign-off {index}",
                    "question": "Approve the product definition?",
                    "response_contract_id": "product-signoff-v1",
                    "required_authority": "sponsor",
                    "conversation_id": "conversation-dm-nicholas-product",
                    "destination_ref": "dm-nicholas-product",
                    "destination_type": "dm",
                },
                terminal=True,
            ),
        )
    request_ids = [str(row["request_id"]) for row in db.list_human_response_requests()]

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-inline-approvals",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": f"{request_ids[0]}: approved\n{request_ids[1]}: approved",
        }
    )

    snapshot = db.status_snapshot()
    requests = {row["request_id"]: row for row in snapshot["human_response_requests"]}
    submissions = snapshot["human_response_submissions"]
    direct_assignments = [
        row for row in snapshot["role_assignments"] if row["assignment_type"] == "direct_conversation"
    ]
    assert replayed.route_type == "role_direct_message"
    assert {requests[request_id]["status"] for request_id in request_ids} == {"responded"}
    assert {requests[request_id]["response_value"] for request_id in request_ids} == {"approve"}
    assert len([row for row in submissions if row["status"] == "accepted"]) == 2
    assert len(direct_assignments) == 1
    assert direct_assignments[0]["role_id"] == "product-manager"


def test_release_notification_is_not_sent_when_close_validation_fails(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-release-notify-invalid-close",
        role_id="release-manager",
        role_instance_id="release-manager-1",
        work_item_id="work-release-card",
    )

    with pytest.raises(SafeOutputError):
        service.record(
            run_id="run-release-notify-invalid-close",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.close",
                payload={
                    "work_item_id": "work-release-card",
                    "reason": "Cannot close without release evidence.",
                    "conversation_id": conversation_id,
                    "destination_ref": "channel-project",
                    "destination_type": "channel",
                    "notification_message": "This message must not be sent.",
                },
                terminal=True,
            ),
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["safe_output_calls"] == 0
    assert snapshot["counts"]["delivery_records"] == 0


def test_release_notification_is_not_duplicated_for_already_closed_work(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)
    _move_release_card_to_release_review(db)
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-release-notify-close-once",
        role_id="release-manager",
        role_instance_id="release-manager-1",
        work_item_id="work-release-card",
    )
    approval_ref = _record_release_card_decision(service, run_id="run-release-notify-close-once")
    service.record(
        run_id="run-release-notify-close-once",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="release.record_no_deployment",
            payload={
                "work_item_id": "work-release-card",
                "release_id": "release-card-no-deployment",
                "reason": "No deployment needed for notification regression.",
                "scope": "Release notification regression.",
                "rollback_plan": "Reopen the work item if the evidence is wrong.",
                "residual_risks": "None known.",
                "approval_ref": approval_ref,
                "commit_ref": "commit-release-card",
            },
        ),
    )
    close_payload = {
        "work_item_id": "work-release-card",
        "reason": "Sponsor approved no-deployment release.",
        "conversation_id": conversation_id,
        "destination_ref": "channel-project",
        "destination_type": "channel",
        "notification_message": "Release notification should appear once.",
    }
    service.record(
        run_id="run-release-notify-close-once",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="work_item.close",
            payload=close_payload,
            terminal=True,
        ),
    )
    db.create_run(
        run_id="run-release-notify-close-again",
        role_id="release-manager",
        role_instance_id="release-manager-1",
        work_item_id="work-release-card",
    )
    service.record(
        run_id="run-release-notify-close-again",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="work_item.close",
            payload={**close_payload, "notification_message": "Duplicate notification must not be sent."},
            terminal=True,
        ),
    )

    notifications = [
        record
        for record in db.status_snapshot()["delivery_records"]
        if record["purpose"] == "release.notification"
    ]
    assert len(notifications) == 1
    assert notifications[0]["payload"]["body"] == "Release notification should appear once."


def test_release_notification_rejects_mismatched_conversation_destination(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-release-notify-mismatched-destination",
        role_id="release-manager",
        role_instance_id="release-manager-1",
        work_item_id="work-release-card",
    )

    with pytest.raises(ValueError, match="does not match conversation"):
        service.record(
            run_id="run-release-notify-mismatched-destination",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.close",
                payload={
                    "work_item_id": "work-release-card",
                    "reason": "Do not notify the wrong channel.",
                    "conversation_id": conversation_id,
                    "destination_ref": "channel-other",
                    "destination_type": "channel",
                    "notification_message": "This message must not be sent.",
                },
                terminal=True,
            ),
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["safe_output_calls"] == 0
    assert snapshot["counts"]["delivery_records"] == 0


def test_release_notification_rejects_mismatched_conversation_destination_type(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)
    assert db.get_conversation(conversation_id)["source_type"] == "channel"
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-release-notify-mismatched-destination-type",
        role_id="release-manager",
        role_instance_id="release-manager-1",
        work_item_id="work-release-card",
    )

    with pytest.raises(ValueError, match="destination type `dm` does not match channel conversation"):
        service.record(
            run_id="run-release-notify-mismatched-destination-type",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.close",
                payload={
                    "work_item_id": "work-release-card",
                    "reason": "Do not notify a channel conversation as a DM.",
                    "conversation_id": conversation_id,
                    "destination_ref": "channel-project",
                    "destination_type": "dm",
                    "notification_message": "This message must not be sent.",
                },
                terminal=True,
            ),
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["safe_output_calls"] == 0
    assert snapshot["counts"]["delivery_records"] == 0


def test_release_notification_rejects_dm_conversation_with_channel_destination_type(tmp_path: Path) -> None:
    db, adapter, _conversation_id = _db_with_work_item(tmp_path)
    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-release-dm-context",
            "conversation_ref": "dm-nicholas-release",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "release-manager",
            "target_ref": "bot-release-manager",
            "body": "Please tell me when this release closes.",
        }
    )
    assert db.get_conversation(replayed.conversation_id)["source_type"] == "dm"
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-release-notify-dm-as-channel",
        role_id="release-manager",
        role_instance_id="release-manager-1",
        work_item_id="work-release-card",
    )

    with pytest.raises(ValueError, match="destination type `channel` does not match DM conversation"):
        service.record(
            run_id="run-release-notify-dm-as-channel",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.close",
                payload={
                    "work_item_id": "work-release-card",
                    "reason": "Do not notify a DM conversation as a channel.",
                    "conversation_id": replayed.conversation_id,
                    "destination_ref": "dm-nicholas-release",
                    "destination_type": "channel",
                    "notification_message": "This message must not be sent.",
                },
                terminal=True,
            ),
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["safe_output_calls"] == 0
    assert snapshot["counts"]["delivery_records"] == 0


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
