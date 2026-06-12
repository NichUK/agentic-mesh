from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.server import V2StatusHandler


class StaticWorker:
    def __init__(self, calls: list[SafeOutputCall]) -> None:
        self.calls = calls
        self.seen_assignment: RoleAssignment | None = None

    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        self.seen_assignment = assignment
        return self.calls


class EmptyWorker:
    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        return []


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
            "human_authorities": {"nicholas": ["sponsor"]},
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


def _render(snapshot: dict[str, object]) -> str:
    handler = object.__new__(V2StatusHandler)
    handler._snapshot = lambda: snapshot  # type: ignore[method-assign]
    return handler._render_status()


def test_role_service_claims_connector_assignment_and_completes_with_safe_output(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-role-claim-1",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "Please give me a product status update.",
        }
    )
    assignment_before = db.list_role_assignments()[0]
    assert assignment_before["status"] == "queued"

    worker = StaticWorker(
        [
            SafeOutputCall(
                role_id="product-manager",
                tool_name="status.reply",
                payload={
                    "message": "Product status: the connector release story is complete.",
                    "conversation_id": replayed.conversation_id,
                    "destination_ref": "dm-nicholas-product",
                    "destination_type": "dm",
                },
                terminal=True,
            )
        ]
    )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        safe_outputs=ConnectorSafeOutputService(db, adapter=adapter),
        worker=worker,
    )

    receipt = service.run_next_assignment()

    assert receipt is not None
    assert receipt.assignment_id == assignment_before["assignment_id"]
    assert receipt.terminal_tool == "status.reply"
    assert worker.seen_assignment is not None
    assert worker.seen_assignment.assignment_id == assignment_before["assignment_id"]
    assert worker.seen_assignment.assignment_type == "direct_conversation"
    assert worker.seen_assignment.visibility_scope == "private"
    assert worker.seen_assignment.payload is not None
    assert worker.seen_assignment.payload["conversation_event_id"]
    assert service.run_next_assignment() is None

    snapshot = db.status_snapshot()
    assignment_after = snapshot["role_assignments"][0]
    assert snapshot["role_assignment_statuses"] == {"completed": 1}
    assert assignment_after["status"] == "completed"
    assert assignment_after["role_instance_id"] == "agentic-mesh-dev.product-manager.1"
    assert assignment_after["run_id"] == receipt.run_id
    assert assignment_after["terminal_tool"] == "status.reply"
    assert snapshot["counts"]["safe_output_calls"] == 1
    assert snapshot["delivery_statuses"] == {"sent": 1}
    event_types = [event["event_type"] for event in db.list_events()]
    assert "role_assignment.claimed" in event_types
    assert "role_assignment.completed" in event_types


def test_role_service_does_not_claim_assignments_for_other_roles(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-engineering-1",
        role_id="engineering",
        source_ref="queue-1",
        title="Engineering work",
        summary="Engineering should own this.",
        assignment_type="work_item_handoff",
        visibility_scope="project",
        payload={"work_item_id": "work-1"},
        work_item_id=None,
    )

    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=StaticWorker([]),
    )

    assert service.run_next_assignment() is None
    assert db.list_role_assignments()[0]["status"] == "queued"


def test_role_service_marks_claimed_assignment_failed_when_worker_emits_no_terminal_output(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-product-1",
        role_id="product-manager",
        source_ref="msg-1",
        title="Product conversation",
        summary="Product Manager should answer.",
        assignment_type="direct_conversation",
        visibility_scope="private",
        payload={"conversation_id": "conversation-1"},
    )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=EmptyWorker(),
    )

    with pytest.raises(ValueError, match="did not emit any safe-output"):
        service.run_next_assignment()

    snapshot = db.status_snapshot()
    assignment = snapshot["role_assignments"][0]
    assert snapshot["role_assignment_statuses"] == {"failed": 1}
    assert assignment["status"] == "failed"
    assert assignment["run_id"].startswith("run-")
    assert "did not emit any safe-output" in assignment["failure_reason"]
    assert snapshot["agent_runs"][0]["status"] == "failed"
    assert snapshot["agent_runs"][0]["run_id"] == assignment["run_id"]
    event_types = [event["event_type"] for event in db.list_events()]
    assert "role_assignment.claimed" in event_types
    assert "role_assignment.failed" in event_types


def test_status_dashboard_shows_role_assignment_terminal_state(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-product-dashboard",
        role_id="product-manager",
        source_ref="msg-dashboard",
        title="Dashboard visible assignment",
        summary="Show assignment state in the dashboard.",
        assignment_type="direct_conversation",
        visibility_scope="private",
        payload={},
    )
    db.claim_role_assignment(
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
    )
    db.complete_role_assignment(
        "assignment-product-dashboard",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        run_id="run-dashboard",
        terminal_tool="status.reply",
    )

    snapshot = db.status_snapshot()
    html = _render(snapshot)

    assert snapshot["role_assignment_statuses"] == {"completed": 1}
    assert "Role Assignments" in html
    assert "Dashboard visible assignment" in html
    assert "status.reply" in html
    assert "run-dashboard" in html
