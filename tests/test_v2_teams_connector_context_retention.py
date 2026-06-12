from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.context import ContextRetentionService
from agentic_mesh_v2.context import ContextSummaryRequest
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
            },
            "channel_bindings": [
                {
                    "channel_ref": "channel-feature-dashboard",
                    "scope_type": "feature",
                    "display_name": "Feature - Dashboard",
                    "visibility": "project",
                    "work_scope": "work-dashboard",
                    "private": False,
                }
            ],
            "human_authorities": {
                "nicholas": ["sponsor", "operator"],
            },
            "retention": {
                "private_dm_days": 14,
                "project_channel_days": 90,
                "focus_channel_days": 45,
                "compacted_summary_days": 730,
                "delivery_record_days": 60,
                "idempotency_receipt_days": 30,
            },
            "team_wide_trigger": "@all-agents",
        }
    )


def _setup(tmp_path: Path) -> tuple[V2Database, LocalTeamsTestAdapter, ContextRetentionService]:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    config = _config()
    adapter = LocalTeamsTestAdapter(db, config)
    adapter.install()
    return db, adapter, ContextRetentionService(db, config)


def test_retention_policy_keys_are_configurable_for_context_classes(tmp_path: Path) -> None:
    db, adapter, service = _setup(tmp_path)

    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-private",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "Private retention class.",
        }
    )
    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-project",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "Project retention class.",
        }
    )
    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-focus",
            "conversation_ref": "channel-feature-dashboard",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "Focus retention class.",
        }
    )

    events = {event["payload"]["message_id"]: event for event in db.list_conversation_events()}
    assert service.retention_days("focus_channel_days") == 45
    assert service.retention_key_for_conversation_event(events["msg-private"]) == "private_dm_days"
    assert service.retention_key_for_conversation_event(events["msg-project"]) == "project_channel_days"
    assert service.retention_key_for_conversation_event(events["msg-focus"]) == "focus_channel_days"
    assert service.retention_key_for_receipt() == "idempotency_receipt_days"
    assert service.retention_key_for_delivery() == "delivery_record_days"
    assert service.retention_key_for_summary() == "compacted_summary_days"


def test_project_context_compaction_preserves_source_and_durable_refs(tmp_path: Path) -> None:
    db, adapter, service = _setup(tmp_path)
    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-decision",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "Decision: use compact dashboard rows for daily status.",
        }
    )
    event_id = db.list_conversation_events()[0]["conversation_event_id"]

    summary = service.compact_events(
        ContextSummaryRequest(
            summary_id="summary-project-decision",
            source_event_ids=(event_id,),
            summary="Decision: status pages should favor compact dashboard rows for daily monitoring.",
            classification="decision",
            visibility_scope="project",
            created_by_role="product-manager",
            durable_refs=("decision:dashboard-density", "document:docs/product/status-dashboard.md"),
            target_ref="work-dashboard",
        )
    )

    snapshot = db.status_snapshot()
    assert summary["conversation_id"] == replayed.conversation_id
    assert summary["visibility_scope"] == "project"
    assert summary["retention_key"] == "compacted_summary_days"
    assert summary["source_refs"] == [event_id]
    assert summary["durable_refs"] == ["decision:dashboard-density", "document:docs/product/status-dashboard.md"]
    assert snapshot["counts"]["context_summaries"] == 1
    assert snapshot["context_summaries"][0]["classification"] == "decision"


def test_private_dm_compaction_stays_private_until_explicit_promotion(tmp_path: Path) -> None:
    db, adapter, service = _setup(tmp_path)
    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-private-promotion",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "PRIVATE_COMPACTION_SENTINEL turn this into tracked work.",
        }
    )
    event = db.list_conversation_events()[0]

    with pytest.raises(ValueError, match="explicit promotion"):
        service.compact_events(
            ContextSummaryRequest(
                summary_id="summary-invalid-public-private",
                source_event_ids=(event["conversation_event_id"],),
                summary="This private context must not become shared yet.",
                classification="requirement",
                visibility_scope="project",
                created_by_role="product-manager",
            )
        )

    private_summary = service.compact_events(
        ContextSummaryRequest(
            summary_id="summary-private",
            source_event_ids=(event["conversation_event_id"],),
            summary="PRIVATE_COMPACT_SUMMARY_SENTINEL sponsor privately asked to turn dashboard feedback into tracked work.",
            classification="conversation_summary",
            visibility_scope="private",
            created_by_role="product-manager",
        )
    )
    assert private_summary["visibility_scope"] == "private"
    assert private_summary["retention_key"] == "private_dm_days"
    snapshot_after_private = db.status_snapshot()
    private_snapshot_summary = {
        item["summary_id"]: item for item in snapshot_after_private["context_summaries"]
    }["summary-private"]
    assert private_snapshot_summary["summary"] == "[redacted private conversation]"
    assert private_snapshot_summary["summary_redacted"] is True
    assert "PRIVATE_COMPACT_SUMMARY_SENTINEL" not in str(snapshot_after_private)

    db.create_run(
        run_id="run-private-promotion",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )
    ConnectorSafeOutputService(db, adapter=adapter).record(
        run_id="run-private-promotion",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="queue.propose_item",
            payload={
                "title": "Track dashboard feedback",
                "summary": "Promote the private dashboard feedback into a tracked product refinement.",
                "source_ref": replayed.receipt_id,
                "source_conversation_id": replayed.conversation_id,
                "source_conversation_event_id": event["conversation_event_id"],
                "rationale": "The sponsor explicitly asked for tracked work.",
                "urgency": "normal",
                "suggested_owner": "product-manager",
                "work_type": "slice",
                "classification": "product_refinement",
                "initiated_by": "sponsor:nicholas",
            },
        ),
    )
    promoted_summary = service.compact_events(
        ContextSummaryRequest(
            summary_id="summary-promoted-private",
            source_event_ids=(event["conversation_event_id"],),
            summary="Promoted private dashboard feedback is now represented by a tracked queue item.",
            classification="requirement",
            visibility_scope="project",
            created_by_role="product-manager",
            durable_refs=(db.list_queue_items()[0]["queue_item_id"],),
        )
    )

    snapshot = db.status_snapshot()
    assert promoted_summary["visibility_scope"] == "project"
    assert "PRIVATE_COMPACTION_SENTINEL" not in str(snapshot)


def test_durable_decision_compaction_requires_durable_reference_or_target(tmp_path: Path) -> None:
    db, adapter, service = _setup(tmp_path)
    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-decision-without-target",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "Decision: this must not remain only in context summary.",
        }
    )
    event_id = db.list_conversation_events()[0]["conversation_event_id"]

    with pytest.raises(ValueError, match="durable reference or target"):
        service.compact_events(
            ContextSummaryRequest(
                summary_id="summary-decision-without-target",
                source_event_ids=(event_id,),
                summary="Decision: this must not remain only in context summary.",
                classification="decision",
                visibility_scope="project",
                created_by_role="product-manager",
            )
        )

    assert db.status_snapshot()["counts"]["context_summaries"] == 0


def test_retention_expiry_preserves_hashes_and_scrubs_raw_payloads(tmp_path: Path) -> None:
    db, adapter, service = _setup(tmp_path)
    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-expire",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "RAW_EXPIRY_SENTINEL project history should expire later.",
        }
    )
    event = db.list_conversation_events()[0]
    receipt = db.list_external_event_receipts()[0]
    delivery_id = adapter.send_message(
        source_ref="source-expire",
        destination_ref="channel-project",
        destination_type="channel",
        purpose="status.reply",
        body="RAW_EXPIRY_SENTINEL delivery payload",
        role_id="product-manager",
    )

    event_expiry = service.expire_conversation_event_raw(event["conversation_event_id"])
    receipt_expiry = service.expire_external_receipt_raw(receipt["receipt_id"])
    delivery_expiry = service.expire_delivery_record_raw(delivery_id)
    repeat_event_expiry = service.expire_conversation_event_raw(event["conversation_event_id"])

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["retention_expiry_records"] == 3
    expiries = {item["source_table"]: item for item in snapshot["retention_expiry_records"]}
    assert expiries["conversation_events"]["expiry_id"] == event_expiry
    assert repeat_event_expiry == event_expiry
    assert expiries["conversation_events"]["retention_key"] == "project_channel_days"
    assert len(expiries["conversation_events"]["content_sha256"]) == 64
    assert expiries["external_event_receipts"]["expiry_id"] == receipt_expiry
    assert expiries["external_event_receipts"]["retention_key"] == "idempotency_receipt_days"
    assert expiries["delivery_records"]["expiry_id"] == delivery_expiry
    assert expiries["delivery_records"]["retention_key"] == "delivery_record_days"
    expired_event = snapshot["conversation_events"][0]
    expired_receipt = snapshot["external_event_receipts"][0]
    expired_delivery = snapshot["delivery_records"][0]
    assert expired_event["body_preview"] == "[expired raw content]"
    assert expired_event["payload"]["raw_content_expired"] is True
    assert expired_event["payload"]["content_sha256"] == expiries["conversation_events"]["content_sha256"]
    assert expired_receipt["payload"]["raw_content_expired"] is True
    assert expired_delivery["payload"]["raw_content_expired"] is True
    assert "RAW_EXPIRY_SENTINEL" not in str(snapshot)
