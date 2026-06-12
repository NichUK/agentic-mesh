from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputService
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


def _work_db(tmp_path: Path) -> V2Database:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_queue_item(
        queue_item_id="queue-runtime-execution",
        title="Runtime execution",
        summary="Exercise downstream role execution.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-runtime-execution", actor_role="product-manager", reason="Ready.")
    db.promote_queue_item(
        queue_item_id="queue-runtime-execution",
        work_item_id="work-runtime-execution",
        owner_role="product-manager",
    )
    return db


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


def test_handoff_safe_output_creates_downstream_role_assignment_without_teams_delivery(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.create_role_assignment(
        assignment_id="assignment-product-handoff",
        role_id="product-manager",
        work_item_id="work-runtime-execution",
        source_ref="queue-runtime-execution",
        title="Shape runtime execution",
        summary="Product Manager should hand off implementation.",
        assignment_type="work_item_handoff",
        visibility_scope="project",
        payload={
            "source_documents": ["work-items/work-runtime-execution/020-product-definition.md"],
            "current_flow_state": "shaping",
        },
    )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="handoff.request",
                    payload={
                        "target_role": "engineering",
                        "reason": "Product definition accepted; implement the runtime execution slice.",
                        "current_flow_state": "ready",
                        "source_documents": [
                            "work-items/work-runtime-execution/020-product-definition.md",
                            "docs/engineering/v2-role-execution-implementation-log.md",
                        ],
                        "target_outputs": [
                            "implementation log",
                            "focused tests",
                        ],
                    },
                    terminal=True,
                )
            ]
        ),
    )

    receipt = service.run_next_assignment()

    assert receipt is not None
    snapshot = db.status_snapshot()
    assignments = {item["assignment_id"]: item for item in snapshot["role_assignments"]}
    product_assignment = assignments["assignment-product-handoff"]
    engineering_assignments = [
        item
        for item in snapshot["role_assignments"]
        if item["role_id"] == "engineering" and item["assignment_type"] == "role_handoff"
    ]
    assert product_assignment["status"] == "completed"
    assert len(engineering_assignments) == 1
    engineering_assignment = engineering_assignments[0]
    assert engineering_assignment["status"] == "queued"
    assert engineering_assignment["work_item_id"] == "work-runtime-execution"
    assert engineering_assignment["visibility_scope"] == "project"
    assert engineering_assignment["source_ref"].startswith("call-")
    assert engineering_assignment["payload"]["source_role"] == "product-manager"
    assert engineering_assignment["payload"]["route_tool"] == "handoff.request"
    assert engineering_assignment["payload"]["work_item_id"] == "work-runtime-execution"
    assert engineering_assignment["payload"]["current_flow_state"] == "ready"
    assert "implementation.record_change" in engineering_assignment["payload"]["allowed_tools"]
    assert "focused tests" in engineering_assignment["payload"]["target_outputs"]
    assert snapshot["counts"]["delivery_records"] == 0
    event_types = [event["event_type"] for event in db.list_events()]
    assert event_types.count("role_assignment.created") == 2


def test_consult_safe_output_creates_consult_assignment_with_context_visibility(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.create_run(
        run_id="run-engineering-consult",
        role_id="engineering",
        role_instance_id="agentic-mesh-dev.engineering.1",
        work_item_id="work-runtime-execution",
    )

    call_id = SafeOutputService(db).record(
        run_id="run-engineering-consult",
        call=SafeOutputCall(
            role_id="engineering",
            tool_name="consult.request",
            payload={
                "target_role": "qa-engineer",
                "reason": "Review the failure-mode coverage before implementation continues.",
                "context_visibility": "project",
                "target_outputs": ["QA review comments"],
            },
            terminal=True,
        ),
    )

    assignment = db.list_role_assignments()[0]
    assert assignment["assignment_id"] == f"assignment-{call_id}"
    assert assignment["role_id"] == "qa-engineer"
    assert assignment["assignment_type"] == "role_consult"
    assert assignment["status"] == "queued"
    assert assignment["work_item_id"] == "work-runtime-execution"
    assert assignment["source_ref"] == call_id
    assert assignment["payload"]["source_run_id"] == "run-engineering-consult"
    assert assignment["payload"]["source_role"] == "engineering"
    assert assignment["payload"]["route_tool"] == "consult.request"
    assert assignment["payload"]["target_outputs"] == ["QA review comments"]
    assert "test_evidence.record" in assignment["payload"]["allowed_tools"]
    assert db.status_snapshot()["counts"]["delivery_records"] == 0


@pytest.mark.parametrize(
    ("tool_name", "payload", "expected_status"),
    [
        (
            "report.blocked",
            {
                "reason": "Required architecture decision is missing.",
                "owner": "solution-architect",
                "next_action": "Provide the missing decision record.",
            },
            "blocked",
        ),
        (
            "sponsor.ask_question",
            {
                "question": "Should this slice continue with the smaller scope?",
                "reason": "Scope decision is needed before implementation.",
            },
            "waiting_human",
        ),
        (
            "report.incomplete",
            {
                "reason": "The requested evidence could not be completed in this run.",
            },
            "incomplete",
        ),
        (
            "noop",
            {
                "reason": "No material specialist input is needed for this assignment.",
            },
            "completed",
        ),
    ],
)
def test_terminal_safe_outputs_map_to_visible_assignment_outcomes(
    tmp_path: Path,
    tool_name: str,
    payload: dict[str, str],
    expected_status: str,
) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-terminal-outcome",
        role_id="product-manager",
        source_ref="msg-terminal",
        title="Terminal outcome",
        summary="Exercise terminal outcome mapping.",
        assignment_type="direct_conversation",
        visibility_scope="project",
        payload={},
    )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name=tool_name,
                    payload=payload,
                    terminal=True,
                )
            ]
        ),
    )

    receipt = service.run_next_assignment()

    assert receipt is not None
    assignment = db.status_snapshot()["role_assignments"][0]
    assert assignment["status"] == expected_status
    assert assignment["terminal_tool"] == tool_name
    assert assignment["run_id"] == receipt.run_id
    assert db.status_snapshot()["role_assignment_statuses"] == {expected_status: 1}


def test_role_service_drains_available_assignments_and_records_idle_heartbeat(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    for index in range(2):
        db.create_role_assignment(
            assignment_id=f"assignment-drain-{index}",
            role_id="product-manager",
            source_ref=f"msg-drain-{index}",
            title=f"Drain assignment {index}",
            summary="Process in bounded drain loop.",
            assignment_type="direct_conversation",
            visibility_scope="project",
            payload={},
        )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="status.complete",
                    payload={"message": "Assignment processing complete."},
                    terminal=True,
                )
            ]
        ),
    )

    drain = service.drain_available_assignments(max_assignments=10)

    snapshot = db.status_snapshot()
    instance = snapshot["role_instance_statuses"][0]
    assert drain.status == "idle"
    assert drain.processed_count == 2
    assert len(drain.receipts) == 2
    assert snapshot["role_assignment_statuses"] == {"completed": 2}
    assert instance["status"] == "idle"
    assert instance["processed_count"] == 2
    assert instance["current_assignment_id"] is None
    assert instance["last_run_id"] == drain.receipts[-1].run_id
    assert instance["heartbeat_at"]


def test_role_service_drain_respects_max_assignments(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    for index in range(2):
        db.create_role_assignment(
            assignment_id=f"assignment-max-{index}",
            role_id="product-manager",
            source_ref=f"msg-max-{index}",
            title=f"Max assignment {index}",
            summary="Respect drain limit.",
            assignment_type="direct_conversation",
            visibility_scope="project",
            payload={},
        )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="status.complete",
                    payload={"message": "One assignment complete."},
                    terminal=True,
                )
            ]
        ),
    )

    drain = service.drain_available_assignments(max_assignments=1)

    assert drain.processed_count == 1
    assert db.status_snapshot()["role_assignment_statuses"] == {"completed": 1, "queued": 1}


def test_claim_sets_lease_and_terminal_completion_clears_it(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-lease-complete",
        role_id="product-manager",
        source_ref="msg-lease",
        title="Lease completion",
        summary="Lease should clear after terminal outcome.",
        assignment_type="direct_conversation",
        visibility_scope="project",
        payload={},
    )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        assignment_lease_seconds=120,
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="status.complete",
                    payload={"message": "Lease-clearing assignment complete."},
                    terminal=True,
                )
            ]
        ),
    )

    claimed = service.claim_next_assignment()
    assert claimed is not None
    claimed_row = db.get_role_assignment("assignment-lease-complete")
    assert claimed_row is not None
    assert claimed_row["status"] == "claimed"
    assert claimed_row["claimed_at"] is not None
    assert claimed_row["claim_expires_at"] is not None
    assert service.refresh_assignment_lease("assignment-lease-complete") is True

    receipt = service.run_assignment(claimed)
    db.complete_role_assignment(
        "assignment-lease-complete",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        run_id=receipt.run_id,
        terminal_tool=receipt.terminal_tool,
        status="completed",
    )

    completed = db.get_role_assignment("assignment-lease-complete")
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["claim_expires_at"] is None


def test_lease_refresh_requires_claiming_instance_and_failure_clears_it(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-lease-fail",
        role_id="product-manager",
        source_ref="msg-lease-fail",
        title="Lease failure",
        summary="Lease should be scoped and clear after failure.",
        assignment_type="direct_conversation",
        visibility_scope="project",
        payload={},
    )
    assert db.claim_role_assignment(
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        lease_seconds=120,
    )

    assert not db.refresh_role_assignment_lease(
        assignment_id="assignment-lease-fail",
        role_instance_id="agentic-mesh-dev.product-manager.2",
        lease_seconds=120,
    )
    db.fail_role_assignment(
        "assignment-lease-fail",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        run_id="run-failed",
        reason="Worker failed.",
    )

    failed = db.get_role_assignment("assignment-lease-fail")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["claim_expires_at"] is None


def test_recover_stale_claimed_assignment_returns_it_to_queue(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-stale",
        role_id="product-manager",
        source_ref="msg-stale",
        title="Stale assignment",
        summary="Recover stale claimed work.",
        assignment_type="direct_conversation",
        visibility_scope="project",
        payload={},
    )
    assert db.claim_role_assignment(
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        lease_seconds=60,
    )
    with db.connection:
        db.connection.execute(
            """
            UPDATE role_assignments
            SET claim_expires_at = '2000-01-01 00:00:00'
            WHERE assignment_id = 'assignment-stale'
            """
        )

    recovered = db.recover_stale_role_assignments(reason="Role instance heartbeat expired.")

    assert len(recovered) == 1
    recovered_row = db.get_role_assignment("assignment-stale")
    assert recovered_row is not None
    assert recovered_row["status"] == "queued"
    assert recovered_row["role_instance_id"] is None
    assert recovered_row["claimed_at"] is None
    assert recovered_row["claim_expires_at"] is None
    assert recovered_row["recovery_count"] == 1
    assert recovered_row["failure_reason"] == "Role instance heartbeat expired."
    event_types = [event["event_type"] for event in db.list_events()]
    assert "role_assignment.recovered" in event_types

    reclaimed = db.claim_role_assignment(
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.2",
        lease_seconds=60,
    )
    assert reclaimed is not None
    assert reclaimed["status"] == "claimed"
    assert reclaimed["failure_reason"] is None
    assert reclaimed["recovery_count"] == 1


def test_recovery_ignores_unexpired_claims_and_dashboard_shows_lease(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-fresh",
        role_id="product-manager",
        source_ref="msg-fresh",
        title="Fresh lease assignment",
        summary="Fresh leases should not recover.",
        assignment_type="direct_conversation",
        visibility_scope="project",
        payload={},
    )
    assert db.claim_role_assignment(
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        lease_seconds=3600,
    )

    recovered = db.recover_stale_role_assignments(reason="Recovery scan.")
    snapshot = db.status_snapshot()
    html = _render(snapshot)

    assert recovered == []
    row = snapshot["role_assignments"][0]
    assert row["status"] == "claimed"
    assert row["claim_expires_at"] is not None
    assert row["recovery_count"] == 0
    assert "Lease / Recoveries" in html
    assert "Fresh lease assignment" in html


def test_recovery_can_be_scoped_to_one_role(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    for role_id in ("product-manager", "engineering"):
        db.create_role_assignment(
            assignment_id=f"assignment-stale-{role_id}",
            role_id=role_id,
            source_ref=f"msg-stale-{role_id}",
            title=f"Stale {role_id} assignment",
            summary="Recover only the requested role.",
            assignment_type="work_item_handoff",
            visibility_scope="project",
            payload={},
        )
        assert db.claim_role_assignment(
            role_id=role_id,
            role_instance_id=f"agentic-mesh-dev.{role_id}.1",
            lease_seconds=60,
        )
    with db.connection:
        db.connection.execute(
            """
            UPDATE role_assignments
            SET claim_expires_at = '2000-01-01 00:00:00'
            """
        )

    recovered = db.recover_stale_role_assignments(
        role_id="product-manager",
        reason="Product Manager recovery scan.",
    )

    assert [row["assignment_id"] for row in recovered] == ["assignment-stale-product-manager"]
    product = db.get_role_assignment("assignment-stale-product-manager")
    engineering = db.get_role_assignment("assignment-stale-engineering")
    assert product is not None
    assert engineering is not None
    assert product["status"] == "queued"
    assert engineering["status"] == "claimed"


def test_role_service_tick_recovers_stale_role_assignment_then_processes_it(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    for role_id in ("product-manager", "engineering"):
        db.create_role_assignment(
            assignment_id=f"assignment-tick-{role_id}",
            role_id=role_id,
            source_ref=f"msg-tick-{role_id}",
            title=f"Tick {role_id} assignment",
            summary="Exercise role service maintenance tick.",
            assignment_type="work_item_handoff",
            visibility_scope="project",
            payload={},
        )
        assert db.claim_role_assignment(
            role_id=role_id,
            role_instance_id=f"agentic-mesh-dev.{role_id}.old",
            lease_seconds=60,
        )
    with db.connection:
        db.connection.execute(
            """
            UPDATE role_assignments
            SET claim_expires_at = '2000-01-01 00:00:00'
            """
        )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="status.complete",
                    payload={"message": "Recovered assignment processed."},
                    terminal=True,
                )
            ]
        ),
    )

    tick = service.run_service_tick(max_recoveries=10, max_assignments=10)

    product = db.get_role_assignment("assignment-tick-product-manager")
    engineering = db.get_role_assignment("assignment-tick-engineering")
    instance = db.status_snapshot()["role_instance_statuses"][0]
    assert product is not None
    assert engineering is not None
    assert tick.status == "idle"
    assert tick.recovered_count == 1
    assert tick.processed_count == 1
    assert len(tick.receipts) == 1
    assert product["status"] == "completed"
    assert product["role_instance_id"] == "agentic-mesh-dev.product-manager.1"
    assert product["recovery_count"] == 1
    assert product["failure_reason"] is None
    assert engineering["status"] == "claimed"
    assert engineering["role_instance_id"] == "agentic-mesh-dev.engineering.old"
    assert instance["status"] == "idle"
    assert instance["processed_count"] == 1
    assert "recovered 1 stale assignments and processed 1 assignments" in instance["detail"]


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


def test_status_dashboard_shows_role_instance_state(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.update_role_instance_status(
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        status="idle",
        last_run_id="run-visible",
        processed_count=3,
        detail="Waiting for work.",
    )

    snapshot = db.status_snapshot()
    html = _render(snapshot)

    assert snapshot["counts"]["role_instance_statuses"] == 1
    assert "Role Instances" in html
    assert "agentic-mesh-dev.product-manager.1" in html
    assert "Waiting for work." in html
    assert "run-visible" in html
