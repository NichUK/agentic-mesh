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


def test_role_dm_status_reply_creates_delivery_without_work_item(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-dm-1",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "private roadmap question",
        }
    )
    assert replayed.route_type == "role_direct_message"

    duplicate = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-dm-1",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "private roadmap question",
        }
    )
    assert duplicate.duplicate

    snapshot_before_reply = db.status_snapshot()
    assert snapshot_before_reply["counts"]["queue_items"] == 0
    assert snapshot_before_reply["counts"]["work_items"] == 0
    assert snapshot_before_reply["counts"]["role_assignments"] == 1
    assert snapshot_before_reply["conversation_events"][0]["body_preview"] == "[redacted private conversation]"
    assert snapshot_before_reply["external_event_receipts"][0]["payload"]["body"] == "[redacted private conversation]"
    assert snapshot_before_reply["external_event_receipts"][0]["payload"]["body_redacted"]

    role = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="product-manager-1",
        safe_outputs=ConnectorSafeOutputService(db, adapter=adapter),
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="status.reply",
                    payload={
                        "message": "The Product Manager view is that this should be shaped first.",
                        "conversation_id": replayed.conversation_id,
                        "destination_ref": "dm-nicholas-product",
                        "destination_type": "dm",
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
            title="Direct Teams conversation",
            summary="Human sent a direct message to Product Manager.",
            conversation_context=("private roadmap question",),
        )
    )
    assert receipt.terminal_tool == "status.reply"

    assignments = db.list_role_assignments()
    db.complete_role_assignment(assignments[0]["assignment_id"], role_instance_id="product-manager-1")

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["queue_items"] == 0
    assert snapshot["counts"]["work_items"] == 0
    assert snapshot["counts"]["safe_output_calls"] == 1
    assert snapshot["counts"]["delivery_records"] == 1
    assert snapshot["counts"]["role_assignments"] == 1
    assert snapshot["role_assignments"][0]["status"] == "completed"
    assert snapshot["conversation_events"][0]["body_preview"] == "[redacted private conversation]"
    assert snapshot["external_event_receipts"][0]["payload"]["body"] == "[redacted private conversation]"
    assert snapshot["delivery_statuses"] == {"sent": 1}
    delivery = snapshot["delivery_records"][0]
    assert delivery["purpose"] == "status.reply"
    assert delivery["destination_type"] == "dm"
    assert delivery["status"] == "sent"
