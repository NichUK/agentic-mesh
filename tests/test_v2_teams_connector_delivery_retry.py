from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputError


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
                "product-manager": "bot-product-manager",
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


def test_duplicate_outbound_send_reuses_delivery_without_new_attempt(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    first = adapter.send_message(
        source_ref="safe-output-call-1",
        destination_ref="dm-nicholas-product",
        destination_type="dm",
        purpose="status.reply",
        body="First reply.",
        role_id="product-manager",
    )
    duplicate = adapter.send_message(
        source_ref="safe-output-call-1",
        destination_ref="dm-nicholas-product",
        destination_type="dm",
        purpose="status.reply",
        body="First reply.",
        role_id="product-manager",
    )

    snapshot = db.status_snapshot()
    assert duplicate == first
    assert snapshot["counts"]["delivery_records"] == 1
    assert snapshot["counts"]["delivery_attempts"] == 1
    assert snapshot["delivery_statuses"] == {"sent": 1}
    assert snapshot["delivery_attempts"][0]["status"] == "sent"


def test_transient_failure_can_retry_to_sent_without_duplicate_success(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    delivery_id = adapter.send_message(
        source_ref="safe-output-call-2",
        destination_ref="dm-nicholas-product",
        destination_type="dm",
        purpose="status.reply",
        body="Retry me.",
        role_id="product-manager",
        outcome="failed_transient",
    )
    adapter.retry_delivery(delivery_id, outcome="sent")
    adapter.retry_delivery(delivery_id, outcome="sent")

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["delivery_records"] == 1
    assert snapshot["counts"]["delivery_attempts"] == 2
    assert snapshot["delivery_statuses"] == {"sent": 1}
    assert [attempt["status"] for attempt in reversed(snapshot["delivery_attempts"])] == [
        "failed_transient",
        "sent",
    ]
    assert snapshot["connector_attention_items"][0]["reason_class"] == "delivery_failed_transient"
    assert snapshot["connector_attention_items"][0]["retryable"] == 1


def test_permanent_failure_and_unknown_outcome_are_visible_for_operator_review(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    permanent = adapter.send_message(
        source_ref="safe-output-call-3",
        destination_ref="channel-project",
        destination_type="channel",
        purpose="status.reply",
        body="This will permanently fail.",
        role_id="product-manager",
        outcome="failed_permanent",
    )
    unknown = adapter.send_message(
        source_ref="safe-output-call-4",
        destination_ref="dm-nicholas-product",
        destination_type="dm",
        purpose="sponsor.ask_question",
        body="This outcome is unknown.",
        role_id="product-manager",
        outcome="unknown",
    )

    with pytest.raises(ValueError, match="not retryable"):
        adapter.retry_delivery(permanent)
    adapter.schedule_delivery_retry(unknown)

    snapshot = db.status_snapshot()
    assert snapshot["delivery_statuses"] == {
        "failed_permanent": 1,
        "retry_scheduled": 1,
    }
    attention = {item["reason_class"]: item for item in snapshot["connector_attention_items"]}
    assert attention["delivery_failed_permanent"]["retryable"] == 0
    assert "Correct connector configuration" in attention["delivery_failed_permanent"]["next_action"]
    assert attention["delivery_unknown"]["retryable"] == 1
    assert "avoid duplicate" in attention["delivery_unknown"]["next_action"]
    assert snapshot["counts"]["delivery_attempts"] == 2


def test_failed_status_reply_path_rejects_delivery_success_claims(tmp_path: Path) -> None:
    class FailingStatusReplyAdapter(LocalTeamsTestAdapter):
        def deliver_status_reply(self, *, call_id: str, role_id: str, payload: dict[str, object]) -> str:
            return self.send_message(
                source_ref=call_id,
                destination_ref=str(payload["destination_ref"]),
                destination_type=str(payload.get("destination_type") or "dm"),
                purpose="status.reply",
                body=str(payload["message"]),
                role_id=role_id,
                outcome="failed_transient",
            )

    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = FailingStatusReplyAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-failing-delivery",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )

    service.record(
        run_id="run-failing-delivery",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="status.reply",
            payload={
                "message": "I have a reply, but delivery is tracked separately.",
                "conversation_id": "conversation-1",
                "destination_ref": "dm-nicholas-product",
                "destination_type": "dm",
            },
            terminal=True,
        ),
    )
    with pytest.raises(SafeOutputError, match="connector delivery success"):
        service.record(
            run_id="run-failing-delivery",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="status.reply",
                payload={
                    "message": "I delivered the Teams reply successfully to the human.",
                    "conversation_id": "conversation-1",
                    "destination_ref": "dm-nicholas-product",
                    "destination_type": "dm",
                },
                terminal=True,
            ),
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["safe_output_calls"] == 1
    assert snapshot["counts"]["delivery_records"] == 1
    assert snapshot["delivery_statuses"] == {"failed_transient": 1}
    assert snapshot["connector_attention_items"][0]["reason_class"] == "delivery_failed_transient"
