from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputError
from agentic_mesh_v2.safe_outputs import SafeOutputService


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
                "engineering": {
                    "external_ref": "bot-engineering",
                    "display_name": "AM-Engineering",
                    "alias": "engineering",
                    "mention_handle": "@AM-Engineering",
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


def test_role_can_promote_private_dm_to_queue_proposal_without_leaking_body(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-private-proposal",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "PRIVATE_SENTINEL please turn this into a small dashboard refinement story",
        }
    )
    event = db.status_snapshot()["conversation_events"][0]
    db.create_run(
        run_id="run-product-proposal",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )

    proposal_call = service.record(
        run_id="run-product-proposal",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="queue.propose_item",
            payload={
                "title": "Refine status dashboard density",
                "summary": "Improve the status dashboard table so active and attention-needed work is easier to scan.",
                "source_ref": replayed.receipt_id,
                "source_conversation_id": replayed.conversation_id,
                "source_conversation_event_id": event["conversation_event_id"],
                "rationale": "The sponsor identified that the current dashboard is not compact enough for daily use.",
                "urgency": "normal",
                "suggested_owner": "product-manager",
                "work_type": "slice",
                "classification": "product_refinement",
                "initiated_by": "sponsor:nicholas",
                "target_artifact": "status-dashboard",
            },
            terminal=True,
        ),
    )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["queue_items"] == 1
    assert snapshot["counts"]["work_proposals"] == 1
    assert snapshot["counts"]["work_items"] == 0
    queue_item = snapshot["queue_items"][0]
    proposal = snapshot["work_proposals"][0]
    assert queue_item["status"] == "queued"
    assert queue_item["source_ref"] == replayed.receipt_id
    assert queue_item["title"] == "Refine status dashboard density"
    assert proposal["queue_item_id"] == queue_item["queue_item_id"]
    assert proposal["safe_output_ref"] == proposal_call
    assert proposal["source_conversation_id"] == replayed.conversation_id
    assert proposal["source_conversation_event_id"] == event["conversation_event_id"]
    assert proposal["source_receipt_id"] == replayed.receipt_id
    assert proposal["classification"] == "product_refinement"
    assert proposal["redaction"] == "private_source_redacted"
    assert proposal["initiated_by"] == "sponsor:nicholas"
    assert "PRIVATE_SENTINEL" not in str(snapshot)


def test_queue_proposal_effect_processing_is_idempotent(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-private-proposal-replay",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "Please make this tracked work.",
        }
    )
    event = db.status_snapshot()["conversation_events"][0]
    db.create_run(
        run_id="run-product-proposal-replay",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )
    call = SafeOutputCall(
        role_id="product-manager",
        tool_name="queue.propose_item",
        payload={
            "title": "Replay-safe queue proposal",
            "summary": "Ensure proposal effects can be replayed safely.",
            "source_ref": replayed.receipt_id,
            "source_conversation_id": replayed.conversation_id,
            "source_conversation_event_id": event["conversation_event_id"],
            "rationale": "Role service finalization may replay recorded safe-output calls.",
            "urgency": "normal",
            "suggested_owner": "product-manager",
            "work_type": "slice",
            "classification": "runtime_correctness",
            "initiated_by": "sponsor:nicholas",
        },
        terminal=True,
    )
    call_id = service.record(run_id="run-product-proposal-replay", call=call)

    service.process_recorded_call(call_id=call_id, run_id="run-product-proposal-replay", call=call)
    snapshot = db.status_snapshot()

    assert snapshot["counts"]["queue_items"] == 1
    assert snapshot["counts"]["work_proposals"] == 1
    assert snapshot["work_proposals"][0]["safe_output_ref"] == call_id


def test_queue_promote_preserves_source_conversation_for_sponsor_dm_signoff(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    connector_outputs = ConnectorSafeOutputService(db, adapter=adapter)

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-promote-from-channel",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Product Manager please shape this feature.",
            "mentioned_role_refs": ["@AM-Product Manager"],
            "thread_ref": "thread-promote",
            "service_url": "https://smba.trafficmanager.net/uk/tenant/",
        }
    )
    event = db.status_snapshot()["conversation_events"][0]
    db.create_run(
        run_id="run-propose-queue",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )
    connector_outputs.record(
        run_id="run-propose-queue",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="queue.propose_item",
            payload={
                "title": "Preserve sponsor route",
                "summary": "Keep sponsor routing after queue promotion.",
                "source_ref": replayed.receipt_id,
                "source_conversation_id": replayed.conversation_id,
                "source_conversation_event_id": event["conversation_event_id"],
                "rationale": "Sponsor approvals should be sent as DMs even after queue promotion.",
                "urgency": "normal",
                "suggested_owner": "product-manager",
                "work_type": "slice",
                "classification": "runtime_fix",
                "initiated_by": "sponsor:nicholas",
            },
            terminal=True,
        ),
    )
    queue_item = db.status_snapshot()["queue_items"][0]

    db.create_run(
        run_id="run-promote-queue",
        role_id="delivery-manager",
        role_instance_id="delivery-manager-1",
        work_item_id=None,
    )
    core_outputs = SafeOutputService(db)
    promote_call = SafeOutputCall(
        role_id="delivery-manager",
        tool_name="queue.promote",
        payload={"queue_item_id": queue_item["queue_item_id"], "reason": "Ready for product shaping."},
        terminal=True,
    )
    promote_call_id = core_outputs.record(run_id="run-promote-queue", call=promote_call)
    core_outputs.process_recorded_call(call_id=promote_call_id, run_id="run-promote-queue", call=promote_call)
    assignment = next(
        item
        for item in db.status_snapshot()["role_assignments"]
        if item["assignment_type"] == "product_shaping"
    )
    payload = assignment["payload"]

    assert payload["conversation_id"] == replayed.conversation_id
    assert payload["conversation_event_id"] == event["conversation_event_id"]
    assert payload["service_url"] == "https://smba.trafficmanager.net/uk/tenant/"
    assert payload["destination_ref"] == "channel-project"
    assert payload["destination_type"] == "channel"

    with db.connection:
        db.connection.execute(
            """
            UPDATE role_assignments
            SET status = 'claimed',
                role_instance_id = 'product-manager-1'
            WHERE assignment_id = ?
            """,
            (assignment["assignment_id"],),
        )
    db.create_run(
        run_id="run-product-signoff",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=assignment["work_item_id"],
    )
    connector_outputs.record(
        run_id="run-product-signoff",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="product.mark_sponsor_ready",
            payload={
                "work_item_id": assignment["work_item_id"],
                "summary": "Product shaping is ready for sponsor sign-off.",
            },
            terminal=True,
        ),
    )

    request = db.status_snapshot()["human_response_requests"][0]
    delivery = db.get_delivery_record(str(request["delivery_ref"]))
    assert request["destination_type"] == "dm"
    assert request["destination_ref"].startswith("dm-")
    assert delivery is not None
    assert delivery["destination_type"] == "dm"
    assert delivery["payload"]["recipient_ref"] == "nicholas"


def test_project_channel_free_text_does_not_create_queue_item_by_inference(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-context-only",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "We should eventually make the dashboard easier to scan.",
        }
    )

    snapshot = db.status_snapshot()
    assert replayed.route_type == "project_channel_context"
    assert snapshot["counts"]["queue_items"] == 0
    assert snapshot["counts"]["work_proposals"] == 0
    assert snapshot["counts"]["work_items"] == 0
    assert snapshot["counts"]["role_assignments"] == 0


def test_status_reply_can_reference_only_existing_queue_proposal(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-proposal-reply",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "Can we capture a dashboard density improvement?",
        }
    )
    event = db.status_snapshot()["conversation_events"][0]
    db.create_run(
        run_id="run-product-reply",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )

    with pytest.raises(ValueError, match="unknown queue item"):
        service.record(
            run_id="run-product-reply",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="status.reply",
                payload={
                    "message": "I proposed a tracked item: queue-not-real.",
                    "queue_item_id": "queue-not-real",
                    "conversation_id": replayed.conversation_id,
                    "destination_ref": "dm-nicholas-product",
                    "destination_type": "dm",
                },
            ),
        )
    with pytest.raises(ValueError, match="unknown work item"):
        service.record(
            run_id="run-product-reply",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="status.reply",
                payload={
                    "message": "I proposed a tracked work item: work-not-real.",
                    "work_item_id": "work-not-real",
                    "conversation_id": replayed.conversation_id,
                    "destination_ref": "dm-nicholas-product",
                    "destination_type": "dm",
                },
            ),
        )

    service.record(
        run_id="run-product-reply",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="queue.propose_item",
            payload={
                "title": "Improve dashboard scan density",
                "summary": "Capture the sponsor request as a product refinement slice.",
                "source_ref": replayed.receipt_id,
                "source_conversation_id": replayed.conversation_id,
                "source_conversation_event_id": event["conversation_event_id"],
                "rationale": "The sponsor asked for a durable follow-up.",
                "urgency": "normal",
                "suggested_owner": "product-manager",
                "work_type": "slice",
                "classification": "product_refinement",
                "initiated_by": "sponsor:nicholas",
            },
        ),
    )
    queue_item_id = db.list_queue_items()[0]["queue_item_id"]
    service.record(
        run_id="run-product-reply",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="status.reply",
            payload={
                "message": f"I proposed a tracked item for this: {queue_item_id}.",
                "queue_item_id": queue_item_id,
                "conversation_id": replayed.conversation_id,
                "destination_ref": "dm-nicholas-product",
                "destination_type": "dm",
            },
            terminal=True,
        ),
    )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["queue_items"] == 1
    assert snapshot["counts"]["work_proposals"] == 1
    assert snapshot["counts"]["delivery_records"] == 1
    assert snapshot["delivery_records"][0]["purpose"] == "status.reply"


def test_queue_proposal_rejects_raw_conversation_text_payload(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-unsafe-proposal",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )

    with pytest.raises(SafeOutputError, match="not raw conversation text"):
        service.record(
            run_id="run-unsafe-proposal",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="queue.propose_item",
                payload={
                    "title": "Unsafe proposal",
                    "summary": "This should fail because it stores raw chat text.",
                    "source_ref": "receipt-unsafe",
                    "rationale": "Private source should be referenced, not copied.",
                    "urgency": "normal",
                    "suggested_owner": "product-manager",
                    "work_type": "slice",
                    "classification": "product_refinement",
                    "initiated_by": "sponsor:nicholas",
                    "body": "PRIVATE_SENTINEL raw chat text",
                },
            ),
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["safe_output_calls"] == 0
    assert snapshot["counts"]["queue_items"] == 0
    assert snapshot["counts"]["work_proposals"] == 0


def test_queue_proposal_rejects_unknown_source_before_safe_output_record(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-fake-source-proposal",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )

    with pytest.raises(ValueError, match="unknown source conversation event"):
        service.record(
            run_id="run-fake-source-proposal",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="queue.propose_item",
                payload={
                    "title": "Fake source proposal",
                    "summary": "This should fail before a safe-output call is recorded.",
                    "source_ref": "receipt-fake-source",
                    "source_conversation_event_id": "conversation-event-fake",
                    "rationale": "The referenced source event does not exist.",
                    "urgency": "normal",
                    "suggested_owner": "product-manager",
                    "work_type": "slice",
                    "classification": "product_refinement",
                    "initiated_by": "sponsor:nicholas",
                },
            ),
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["safe_output_calls"] == 0
    assert snapshot["counts"]["queue_items"] == 0
    assert snapshot["counts"]["work_proposals"] == 0
