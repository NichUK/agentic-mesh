from pathlib import Path
import json
import sys

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputError
from agentic_mesh_v2.safe_outputs import SafeOutputService
from agentic_mesh_v2.server import V2StatusHandler
from agentic_mesh_v2.state_machine import TransitionRequest
from agentic_mesh_v2.worker_adapters import SafeOutputSubprocessWorker


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


class StaticPromptAssembler:
    def render(self, assignment: RoleAssignment):
        return type(
            "PromptRender",
            (),
            {
                "prompt_text": f"<prompt>{assignment.role_id}:{assignment.title}</prompt>",
                "component_manifest": {"components": ["static-test"]},
            },
        )()


class FailingPromptAssembler:
    def render(self, assignment: RoleAssignment):
        raise ValueError("prompt component missing")


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


def _blocked_work_db(tmp_path: Path) -> V2Database:
    db = _work_db(tmp_path)
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Product definition is ready.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering starts implementation.",
            owner="engineering",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="active",
            to_state="blocked",
            actor_role="engineering",
            reason="Deployment access is missing.",
            owner="release-manager",
            reason_class="deployment_access_missing",
            next_action="Release Manager must reopen after confirming access.",
            retryable=True,
        )
    )
    return db


def test_product_mark_ready_transitions_work_and_queues_engineering(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    try:
        db.create_run(
            run_id="run-product-mark-ready",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id="work-runtime-execution",
        )
        service = SafeOutputService(db)

        call_id = service.record(
            run_id="run-product-mark-ready",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="work_item.mark_ready",
                payload={
                    "work_item_id": "work-runtime-execution",
                    "reason": "Product shaping is complete and Engineering can implement.",
                    "source_documents": ["work-items/work-runtime-execution/020-product-definition.md"],
                    "target_outputs": ["implementation_change", "handoff.request"],
                },
            ),
        )

        work = db.get_work_item("work-runtime-execution")
        work_row = next(row for row in db.list_work_items() if row["work_item_id"] == "work-runtime-execution")
        assignments = db.list_role_assignments()
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert work.state == "ready"
    assert work_row["current_role"] == "engineering"
    assert next(row for row in calls if row["call_id"] == call_id)["terminal"] == 1
    assert len(assignments) == 1
    assert assignments[0]["role_id"] == "engineering"
    assert assignments[0]["assignment_type"] == "implementation"
    assert assignments[0]["work_item_id"] == "work-runtime-execution"
    assert assignments[0]["payload"]["safe_output_ref"] == call_id
    assert "implementation.record_change" in assignments[0]["payload"]["allowed_tools"]


def test_queue_promote_creates_shaping_work_and_owner_assignment(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        db.create_queue_item(
            queue_item_id="queue-promote",
            title="Promote queued work",
            summary="Turn captured queue work into a shaping work item.",
            owner_role="product-manager",
        )
        db.create_run(
            run_id="run-queue-promote",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id=None,
        )
        service = SafeOutputService(db)
        call = SafeOutputCall(
            role_id="product-manager",
            tool_name="queue.promote",
            payload={"queue_item_id": "queue-promote", "reason": "Ready for product shaping."},
            terminal=True,
        )
        call_id = service.record(run_id="run-queue-promote", call=call)
        service.process_recorded_call(call_id=call_id, run_id="run-queue-promote", call=call)
        snapshot = db.status_snapshot()
    finally:
        db.close()

    queue_item = snapshot["queue_items"][0]
    work_item = snapshot["work_items"][0]
    assignments = snapshot["role_assignments"]
    assert queue_item["status"] == "promoted"
    assert queue_item["linked_work_item_id"] == work_item["work_item_id"]
    assert work_item["state"] == "shaping"
    assert work_item["owner_role"] == "product-manager"
    assert len(assignments) == 1
    assert assignments[0]["role_id"] == "product-manager"
    assert assignments[0]["work_item_id"] == work_item["work_item_id"]
    assert assignments[0]["assignment_type"] == "product_shaping"


def test_product_mark_ready_rejects_non_shaping_work_before_recording(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    try:
        db.transition_work_item(
            TransitionRequest(
                work_item_id="work-runtime-execution",
                from_state="shaping",
                to_state="ready",
                actor_role="product-manager",
                reason="Already ready.",
            )
        )
        db.create_run(
            run_id="run-product-mark-ready-reject",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id="work-runtime-execution",
        )
        service = SafeOutputService(db)

        with pytest.raises(SafeOutputError, match="requires work item state `shaping`"):
            service.record(
                run_id="run-product-mark-ready-reject",
                call=SafeOutputCall(
                    role_id="product-manager",
                    tool_name="work_item.mark_ready",
                    payload={
                        "work_item_id": "work-runtime-execution",
                        "reason": "Product shaping is complete.",
                    },
                ),
            )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert calls == []


def test_deferred_product_mark_ready_replay_is_idempotent(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    try:
        db.create_run(
            run_id="run-product-mark-ready-deferred",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id="work-runtime-execution",
        )
        call = SafeOutputCall(
            role_id="product-manager",
            tool_name="work_item.mark_ready",
            payload={
                "work_item_id": "work-runtime-execution",
                "reason": "Product shaping is complete and Engineering can implement.",
            },
        )
        call_id = SafeOutputService(db, process_effects=False).record(
            run_id="run-product-mark-ready-deferred",
            call=call,
        )
        active_service = SafeOutputService(db)

        active_service.process_recorded_call(
            call_id=call_id,
            run_id="run-product-mark-ready-deferred",
            call=call,
        )
        active_service.process_recorded_call(
            call_id=call_id,
            run_id="run-product-mark-ready-deferred",
            call=call,
        )
        work = db.get_work_item("work-runtime-execution")
        implementation_assignment = db.get_role_assignment("assignment-engineering-implementation")
        assignments = db.list_role_assignments()
        transition_events = [
            event
            for event in db.list_events("work-runtime-execution")
            if event["event_type"] == "work_item.transitioned"
        ]
    finally:
        db.close()

    assert work.state == "ready"
    assert len(assignments) == 1
    assert assignments[0]["assignment_id"] == f"assignment-{call_id}-work-item-ready"
    assert sum(1 for event in transition_events if event["payload"]["to_state"] == "ready") == 1


def test_distinct_deferred_product_mark_ready_cannot_duplicate_ready_assignment(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    try:
        db.create_run(
            run_id="run-product-mark-ready-deferred-duplicate",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id="work-runtime-execution",
        )
        first_call = SafeOutputCall(
            role_id="product-manager",
            tool_name="work_item.mark_ready",
            payload={
                "work_item_id": "work-runtime-execution",
                "reason": "Product shaping is complete and Engineering can implement.",
            },
        )
        second_call = SafeOutputCall(
            role_id="product-manager",
            tool_name="work_item.mark_ready",
            payload={
                "work_item_id": "work-runtime-execution",
                "reason": "Duplicate deferred readiness attempt.",
            },
        )
        record_only_service = SafeOutputService(db, process_effects=False)
        first_call_id = record_only_service.record(
            run_id="run-product-mark-ready-deferred-duplicate",
            call=first_call,
        )
        second_call_id = record_only_service.record(
            run_id="run-product-mark-ready-deferred-duplicate",
            call=second_call,
        )
        active_service = SafeOutputService(db)

        active_service.process_recorded_call(
            call_id=first_call_id,
            run_id="run-product-mark-ready-deferred-duplicate",
            call=first_call,
        )
        with pytest.raises(SafeOutputError, match="requires work item state `shaping`"):
            active_service.process_recorded_call(
                call_id=second_call_id,
                run_id="run-product-mark-ready-deferred-duplicate",
                call=second_call,
            )
        work = db.get_work_item("work-runtime-execution")
        assignments = db.list_role_assignments()
    finally:
        db.close()

    assert work.state == "ready"
    assert len(assignments) == 1
    assert assignments[0]["assignment_id"] == f"assignment-{first_call_id}-work-item-ready"


def test_engineering_claiming_implementation_assignment_activates_ready_work(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    try:
        db.transition_work_item(
            TransitionRequest(
                work_item_id="work-runtime-execution",
                from_state="shaping",
                to_state="ready",
                actor_role="product-manager",
                reason="Product definition is ready.",
                owner="engineering",
            )
        )
        db.create_role_assignment(
            assignment_id="assignment-engineering-implementation",
            role_id="engineering",
            work_item_id="work-runtime-execution",
            source_ref="call-product-ready",
            title="Implement runtime execution",
            summary="Implementation is ready to start.",
            assignment_type="implementation",
            visibility_scope="project",
            payload={},
        )
        service = RoleService(
            db=db,
            role_id="engineering",
            role_instance_id="test-project.engineering.1",
            worker=StaticWorker(
                [
                    SafeOutputCall(
                        role_id="engineering",
                        tool_name="implementation.record_change",
                        payload={
                            "work_item_id": "work-runtime-execution",
                            "summary": "Engineering started and recorded the implementation change.",
                        },
                    ),
                    SafeOutputCall(
                        role_id="engineering",
                        tool_name="handoff.request",
                        payload={
                            "target_role": "qa-engineer",
                            "work_item_id": "work-runtime-execution",
                            "reason": "Implementation is ready for QA.",
                        },
                        terminal=True,
                    ),
                ]
            ),
        )

        receipt = service.run_next_assignment()
        work = db.get_work_item("work-runtime-execution")
        implementation_assignment = db.get_role_assignment("assignment-engineering-implementation")
        assignments = db.list_role_assignments()
        transition_events = [
            event
            for event in db.list_events("work-runtime-execution")
            if event["event_type"] == "work_item.transitioned"
        ]
    finally:
        db.close()

    assert receipt is not None
    assert work.state == "active"
    assert implementation_assignment is not None
    assert implementation_assignment["status"] == "completed"
    assert implementation_assignment["terminal_tool"] == "handoff.request"
    assert any(row["role_id"] == "qa-engineer" and row["status"] == "queued" for row in assignments)
    assert any(
        event["payload"]["from_state"] == "ready"
        and event["payload"]["to_state"] == "active"
        and event["payload"]["actor_role"] == "engineering"
        for event in transition_events
    )


def test_engineering_implementation_assignment_is_idempotent_for_active_work(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    try:
        db.transition_work_item(
            TransitionRequest(
                work_item_id="work-runtime-execution",
                from_state="shaping",
                to_state="ready",
                actor_role="product-manager",
                reason="Product definition is ready.",
                owner="engineering",
            )
        )
        db.transition_work_item(
            TransitionRequest(
                work_item_id="work-runtime-execution",
                from_state="ready",
                to_state="active",
                actor_role="engineering",
                reason="Engineering already started.",
                owner="engineering",
            )
        )
        db.create_role_assignment(
            assignment_id="assignment-engineering-active",
            role_id="engineering",
            work_item_id="work-runtime-execution",
            source_ref="call-product-ready",
            title="Continue active implementation",
            summary="Implementation is already active.",
            assignment_type="implementation",
            visibility_scope="project",
            payload={},
        )
        service = RoleService(
            db=db,
            role_id="engineering",
            role_instance_id="test-project.engineering.1",
            worker=StaticWorker(
                [
                    SafeOutputCall(
                        role_id="engineering",
                        tool_name="status.complete",
                        payload={"message": "Active implementation assignment checked."},
                        terminal=True,
                    )
                ]
            ),
        )

        receipt = service.run_next_assignment()
        work = db.get_work_item("work-runtime-execution")
        transition_events = [
            event
            for event in db.list_events("work-runtime-execution")
            if event["event_type"] == "work_item.transitioned"
            and event["payload"]["from_state"] == "ready"
            and event["payload"]["to_state"] == "active"
        ]
    finally:
        db.close()

    assert receipt is not None
    assert work.state == "active"
    assert len(transition_events) == 1


def test_engineering_implementation_assignment_fails_for_non_ready_work(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    try:
        db.create_role_assignment(
            assignment_id="assignment-engineering-blocked",
            role_id="engineering",
            work_item_id="work-runtime-execution",
            source_ref="call-product-ready",
            title="Cannot start blocked work",
            summary="Implementation should not start from blocked.",
            assignment_type="implementation",
            visibility_scope="project",
            payload={},
        )
        worker = StaticWorker(
            [
                SafeOutputCall(
                    role_id="engineering",
                    tool_name="status.complete",
                    payload={"message": "Should not run."},
                    terminal=True,
                )
            ]
        )
        service = RoleService(
            db=db,
            role_id="engineering",
            role_instance_id="test-project.engineering.1",
            worker=worker,
        )

        with pytest.raises(ValueError, match="ready or active"):
            service.run_next_assignment()
        assignment = db.get_role_assignment("assignment-engineering-blocked")
        work = db.get_work_item("work-runtime-execution")
        runs = db.status_snapshot()["agent_runs"]
    finally:
        db.close()

    assert assignment is not None
    assert assignment["status"] == "failed"
    assert "ready or active" in assignment["failure_reason"]
    assert work.state == "blocked"
    assert runs[0]["status"] == "failed"
    assert worker.seen_assignment is None


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


def test_role_service_records_prompt_audit_and_passes_prompt_to_worker(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-prompt-audit",
        role_id="product-manager",
        source_ref="msg-prompt-audit",
        title="Prompt audit",
        summary="Record the full prompt before worker execution.",
        assignment_type="direct_conversation",
        visibility_scope="project",
        payload={},
    )
    worker = StaticWorker(
        [
            SafeOutputCall(
                role_id="product-manager",
                tool_name="status.complete",
                payload={"message": "Prompt audit complete."},
                terminal=True,
            )
        ]
    )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=worker,
        prompt_assembler=StaticPromptAssembler(),
    )

    receipt = service.run_next_assignment()

    assert receipt is not None
    assert worker.seen_assignment is not None
    assert worker.seen_assignment.generated_prompt == "<prompt>product-manager:Prompt audit</prompt>"
    prompts = db.list_agent_prompts()
    assert len(prompts) == 1
    assert prompts[0]["run_id"] == receipt.run_id
    assert prompts[0]["assignment_id"] == "assignment-prompt-audit"
    assert prompts[0]["prompt_text"] == "<prompt>product-manager:Prompt audit</prompt>"
    assert prompts[0]["component_manifest"] == {"components": ["static-test"]}
    assert db.status_snapshot()["counts"]["agent_prompts"] == 1
    event_types = [event["event_type"] for event in db.list_events()]
    assert "agent_prompt.recorded" in event_types


def test_role_service_collects_safe_outputs_recorded_by_worker_cli_transport(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-cli-transport",
        role_id="product-manager",
        source_ref="msg-cli-transport",
        title="CLI safe-output transport",
        summary="Worker should record safe output through the CLI transport.",
        assignment_type="direct_conversation",
        visibility_scope="project",
        payload={},
    )
    worker_script = (
        "import json, subprocess, sys; "
        "payload=json.load(sys.stdin); "
        "command=payload['safe_output_transport']['record_command']; "
        "subprocess.run(command + ["
        "'--tool-name','status.complete',"
        "'--payload-json',json.dumps({'message':'Recorded through CLI transport.'})"
        "], check=True, capture_output=True)"
    )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=SafeOutputSubprocessWorker((sys.executable, "-c", worker_script), timeout_seconds=10),
    )

    receipt = service.run_next_assignment()

    assert receipt is not None
    assert receipt.terminal_tool == "status.complete"
    assert receipt.safe_output_count == 1
    assignment = db.get_role_assignment("assignment-cli-transport")
    calls = db.list_safe_output_calls_for_run(receipt.run_id)
    assert assignment is not None
    assert assignment["status"] == "completed"
    assert calls[0]["tool_name"] == "status.complete"
    assert calls[0]["payload"]["message"] == "Recorded through CLI transport."
    assert calls[0]["terminal"] is True


def test_role_service_marks_run_failed_when_prompt_assembly_fails(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-prompt-failure",
        role_id="product-manager",
        source_ref="msg-prompt-failure",
        title="Prompt failure",
        summary="Prompt assembly should fail visibly.",
        assignment_type="direct_conversation",
        visibility_scope="project",
        payload={},
    )
    worker = StaticWorker(
        [
            SafeOutputCall(
                role_id="product-manager",
                tool_name="status.complete",
                payload={"message": "Should not run."},
                terminal=True,
            )
        ]
    )
    service = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        worker=worker,
        prompt_assembler=FailingPromptAssembler(),
    )

    with pytest.raises(ValueError, match="prompt component missing"):
        service.run_next_assignment()

    snapshot = db.status_snapshot()
    assert worker.seen_assignment is None
    assert snapshot["agent_runs"][0]["status"] == "failed"
    assert snapshot["counts"]["agent_prompts"] == 0
    assert snapshot["role_assignments"][0]["status"] == "failed"
    assert "prompt component missing" in snapshot["role_assignments"][0]["failure_reason"]


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


def test_report_blocked_marks_linked_work_item_blocked(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.create_role_assignment(
        assignment_id="assignment-work-blocked",
        role_id="product-manager",
        work_item_id="work-runtime-execution",
        source_ref="msg-work-blocked",
        title="Work blocker",
        summary="Exercise work item blocker propagation.",
        assignment_type="lifecycle_work",
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
                    tool_name="report.blocked",
                    payload={
                        "reason": "Architecture decision is missing.",
                        "owner": "solution-architect",
                        "next_action": "Provide ADR for the runtime boundary.",
                    },
                    terminal=True,
                )
            ]
        ),
    )

    receipt = service.run_next_assignment()

    snapshot = db.status_snapshot()
    assignment = snapshot["role_assignments"][0]
    work_item = snapshot["work_items"][0]
    assert receipt is not None
    assert assignment["status"] == "blocked"
    assert work_item["state"] == "blocked"
    assert work_item["current_role"] == "solution-architect"
    assert work_item["attention_owner"] == "solution-architect"
    assert work_item["reason_class"] == "role_reported_blocker"
    assert work_item["next_action"] == "Provide ADR for the runtime boundary."
    assert work_item["retryable"] is True


def test_report_blocked_without_work_item_stays_assignment_level(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_role_assignment(
        assignment_id="assignment-conversation-blocked",
        role_id="product-manager",
        source_ref="msg-conversation-blocked",
        title="Conversation blocker",
        summary="Exercise conversation-only blocker.",
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
                    tool_name="report.blocked",
                    payload={
                        "reason": "Sponsor context is missing.",
                        "owner": "sponsor",
                        "next_action": "Clarify the requested outcome.",
                    },
                    terminal=True,
                )
            ]
        ),
    )

    service.run_next_assignment()

    snapshot = db.status_snapshot()
    assert snapshot["role_assignments"][0]["status"] == "blocked"
    assert snapshot["counts"]["work_items"] == 0


def test_report_blocked_work_item_effect_is_replay_idempotent(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.create_run(
        run_id="run-work-blocker-replay",
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        work_item_id="work-runtime-execution",
    )
    service = SafeOutputService(db)
    call = SafeOutputCall(
        role_id="product-manager",
        tool_name="report.blocked",
        payload={
            "reason": "Architecture decision is missing.",
            "owner": "solution-architect",
            "next_action": "Provide ADR for the runtime boundary.",
        },
        terminal=True,
    )

    call_id = service.record(run_id="run-work-blocker-replay", call=call)
    service.process_recorded_call(call_id=call_id, run_id="run-work-blocker-replay", call=call)

    snapshot = db.status_snapshot()
    transition_events = [
        event
        for event in db.list_events("work-runtime-execution")
        if event["event_type"] == "work_item.transitioned"
        and event["payload"]["to_state"] == "blocked"
    ]
    assert snapshot["work_items"][0]["state"] == "blocked"
    assert len(transition_events) == 1


def test_report_blocked_replay_does_not_reblock_resolved_work_item(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.create_run(
        run_id="run-work-blocker-resolved-replay",
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        work_item_id="work-runtime-execution",
    )
    service = SafeOutputService(db)
    call = SafeOutputCall(
        role_id="product-manager",
        tool_name="report.blocked",
        payload={
            "reason": "Architecture decision is missing.",
            "owner": "solution-architect",
            "next_action": "Provide ADR for the runtime boundary.",
        },
        terminal=True,
    )
    call_id = service.record(run_id="run-work-blocker-resolved-replay", call=call)
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="blocked",
            to_state="active",
            actor_role="solution-architect",
            reason="Blocker resolved.",
            owner="product-manager",
        )
    )

    service.process_recorded_call(call_id=call_id, run_id="run-work-blocker-resolved-replay", call=call)

    snapshot = db.status_snapshot()
    blocked_events = [
        event
        for event in db.list_events("work-runtime-execution")
        if event["event_type"] == "work_item.transitioned"
        and event["payload"]["to_state"] == "blocked"
    ]
    evidence = [
        row
        for row in db.list_work_item_evidence()
        if row["evidence_type"] == "blocker_report"
    ]
    assert snapshot["work_items"][0]["state"] == "active"
    assert len(blocked_events) == 1
    assert len(evidence) == 1
    assert evidence[0]["safe_output_ref"] == call_id


def test_report_blocked_accepts_explicit_work_item_target(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.create_run(
        run_id="run-work-blocker-explicit-target",
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        work_item_id=None,
    )

    SafeOutputService(db).record(
        run_id="run-work-blocker-explicit-target",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="report.blocked",
            payload={
                "work_item_id": "work-runtime-execution",
                "reason": "Architecture decision is missing.",
                "owner": "solution-architect",
                "next_action": "Provide ADR for the runtime boundary.",
            },
            terminal=True,
        ),
    )

    snapshot = db.status_snapshot()
    assert snapshot["work_items"][0]["state"] == "blocked"
    assert snapshot["work_items"][0]["attention_owner"] == "solution-architect"


def test_release_manager_reopen_blocked_work_item_routes_to_target_role(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-reopen",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    service = SafeOutputService(db)

    call_id = service.record(
        run_id="run-work-item-reopen",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="work_item.reopen",
            payload={
                "work_item_id": "work-runtime-execution",
                "target_role": "engineering",
                "reason": "Access confirmed; Engineering can continue.",
                "source_documents": ["work-items/work-runtime-execution/120-release-record.md"],
                "target_outputs": ["implementation update", "test rerun"],
            },
        ),
    )

    work_item = next(row for row in db.list_work_items() if row["work_item_id"] == "work-runtime-execution")
    assignments = [
        row
        for row in db.list_role_assignments()
        if row["source_ref"] == call_id and row["assignment_type"] == "work_item_reopen"
    ]
    calls = db.list_safe_output_calls_for_run("run-work-item-reopen")

    assert work_item["state"] == "active"
    assert work_item["current_role"] == "engineering"
    assert work_item["attention_owner"] is None
    assert len(assignments) == 1
    assert assignments[0]["role_id"] == "engineering"
    assert assignments[0]["status"] == "queued"
    assert assignments[0]["work_item_id"] == "work-runtime-execution"
    assert assignments[0]["payload"]["safe_output_ref"] == call_id
    assert assignments[0]["payload"]["previous_flow_state"] == "blocked"
    assert assignments[0]["payload"]["current_flow_state"] == "active"
    assert "implementation.record_change" in assignments[0]["payload"]["allowed_tools"]
    assert calls[0]["terminal"] is True


def test_work_item_reopen_requires_blocked_state(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-reopen-not-blocked",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )

    with pytest.raises(SafeOutputError, match="requires work item state `blocked`"):
        SafeOutputService(db).record(
            run_id="run-work-item-reopen-not-blocked",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.reopen",
                payload={
                    "work_item_id": "work-runtime-execution",
                    "target_role": "engineering",
                    "reason": "Try to reopen work that is not blocked.",
                },
            ),
        )

    assert db.list_safe_output_calls_for_run("run-work-item-reopen-not-blocked") == []


def test_work_item_reopen_replay_does_not_unblock_later_blocker(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-reopen-replay",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    service = SafeOutputService(db)
    call = SafeOutputCall(
        role_id="release-manager",
        tool_name="work_item.reopen",
        payload={
            "work_item_id": "work-runtime-execution",
            "target_role": "engineering",
            "reason": "Access confirmed; Engineering can continue.",
        },
    )
    call_id = service.record(run_id="run-work-item-reopen-replay", call=call)
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="active",
            to_state="blocked",
            actor_role="engineering",
            reason="A new environment issue was found.",
            owner="platform-engineer",
            reason_class="environment_issue",
            next_action="Platform Engineer must repair the environment.",
            retryable=True,
        )
    )

    service.process_recorded_call(call_id=call_id, run_id="run-work-item-reopen-replay", call=call)

    work_item = next(row for row in db.list_work_items() if row["work_item_id"] == "work-runtime-execution")
    assignments = [
        row
        for row in db.list_role_assignments()
        if row["source_ref"] == call_id and row["assignment_type"] == "work_item_reopen"
    ]
    reopened_events = [
        event
        for event in db.list_events("work-runtime-execution")
        if event["event_type"] == "work_item.transitioned"
        and event["payload"]["to_state"] == "active"
        and event["payload"]["actor_role"] == "release-manager"
    ]

    assert work_item["state"] == "blocked"
    assert work_item["attention_owner"] == "platform-engineer"
    assert len(assignments) == 1
    assert len(reopened_events) == 1


def test_reopen_db_helper_is_guarded_by_existing_assignment(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    service = SafeOutputService(db)
    db.create_run(
        run_id="run-work-item-reopen-db-guard",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    call_id = service.record(
        run_id="run-work-item-reopen-db-guard",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="work_item.reopen",
            payload={
                "work_item_id": "work-runtime-execution",
                "target_role": "engineering",
                "reason": "Access confirmed; Engineering can continue.",
            },
        ),
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="active",
            to_state="blocked",
            actor_role="engineering",
            reason="A later blocker should remain active.",
            owner="platform-engineer",
            reason_class="environment_issue",
            next_action="Repair the later blocker.",
            retryable=True,
        )
    )

    db.reopen_work_item_with_assignment(
        request=TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="blocked",
            to_state="active",
            actor_role="release-manager",
            reason="Replay old reopen.",
            owner="engineering",
        ),
        assignment_id=f"assignment-{call_id}-work-item-reopen",
        role_id="engineering",
        source_ref=call_id,
        title="Replay old reopen",
        summary="Replay old reopen.",
        assignment_type="work_item_reopen",
        visibility_scope="project",
        payload={"safe_output_ref": call_id},
    )

    work_item = next(row for row in db.list_work_items() if row["work_item_id"] == "work-runtime-execution")
    reopened_events = [
        event
        for event in db.list_events("work-runtime-execution")
        if event["event_type"] == "work_item.transitioned"
        and event["payload"]["to_state"] == "active"
        and event["payload"]["actor_role"] == "release-manager"
    ]

    assert work_item["state"] == "blocked"
    assert work_item["attention_owner"] == "platform-engineer"
    assert len(reopened_events) == 1


def test_release_manager_supersede_blocked_work_item_records_terminal_evidence(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-supersede",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    service = SafeOutputService(db)

    call_id = service.record(
        run_id="run-work-item-supersede",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="work_item.supersede",
            payload={
                "work_item_id": "work-runtime-execution",
                "reason": "The dashboard slice has been replaced by a more complete redesign.",
                "replacement_ref": "work-dashboard-redesign-v2",
            },
        ),
    )

    work_item = next(row for row in db.list_work_items() if row["work_item_id"] == "work-runtime-execution")
    evidence = [
        row
        for row in db.list_work_item_evidence()
        if row["safe_output_ref"] == call_id and row["evidence_type"] == "work_item_superseded"
    ]
    calls = db.list_safe_output_calls_for_run("run-work-item-supersede")

    assert work_item["state"] == "superseded"
    assert work_item["current_role"] == "release-manager"
    assert work_item["attention_owner"] is None
    assert len(evidence) == 1
    assert evidence[0]["summary"].startswith("Superseded by work-dashboard-redesign-v2:")
    assert calls[0]["terminal"] is True


def test_work_item_supersede_replay_is_idempotent(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-supersede-replay",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    service = SafeOutputService(db)
    call = SafeOutputCall(
        role_id="release-manager",
        tool_name="work_item.supersede",
        payload={
            "work_item_id": "work-runtime-execution",
            "reason": "The dashboard slice has been replaced by a more complete redesign.",
            "replacement_ref": "work-dashboard-redesign-v2",
        },
    )

    call_id = service.record(run_id="run-work-item-supersede-replay", call=call)
    service.process_recorded_call(call_id=call_id, run_id="run-work-item-supersede-replay", call=call)

    superseded_events = [
        event
        for event in db.list_events("work-runtime-execution")
        if event["event_type"] == "work_item.transitioned"
        and event["payload"]["to_state"] == "superseded"
    ]
    evidence = [
        row
        for row in db.list_work_item_evidence()
        if row["safe_output_ref"] == call_id and row["evidence_type"] == "work_item_superseded"
    ]

    assert db.get_work_item("work-runtime-execution").state == "superseded"
    assert len(superseded_events) == 1
    assert len(evidence) == 1


def test_deferred_work_item_supersede_can_be_replayed(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-supersede-deferred",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    call = SafeOutputCall(
        role_id="release-manager",
        tool_name="work_item.supersede",
        payload={
            "work_item_id": "work-runtime-execution",
            "reason": "The dashboard slice has been replaced by a more complete redesign.",
            "replacement_ref": "work-dashboard-redesign-v2",
        },
    )
    call_id = SafeOutputService(db, process_effects=False).record(
        run_id="run-work-item-supersede-deferred",
        call=call,
    )

    assert db.get_work_item("work-runtime-execution").state == "blocked"

    SafeOutputService(db).process_recorded_call(
        call_id=call_id,
        run_id="run-work-item-supersede-deferred",
        call=call,
    )

    evidence = [
        row
        for row in db.list_work_item_evidence()
        if row["safe_output_ref"] == call_id and row["evidence_type"] == "work_item_superseded"
    ]
    assert db.get_work_item("work-runtime-execution").state == "superseded"
    assert len(evidence) == 1


def test_release_manager_override_blocker_routes_to_target_role_with_evidence(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-override-blocker",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    service = SafeOutputService(db)

    call_id = service.record(
        run_id="run-work-item-override-blocker",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="work_item.override_blocker",
            payload={
                "work_item_id": "work-runtime-execution",
                "target_role": "engineering",
                "reason": "Sponsor accepted the risk and authorized continuation.",
                "source_documents": ["work-items/work-runtime-execution/120-release-record.md"],
                "target_outputs": ["implementation update", "risk note"],
            },
        ),
    )

    work_item = next(row for row in db.list_work_items() if row["work_item_id"] == "work-runtime-execution")
    assignments = [
        row
        for row in db.list_role_assignments()
        if row["source_ref"] == call_id and row["assignment_type"] == "work_item_blocker_override"
    ]
    evidence = [
        row
        for row in db.list_work_item_evidence()
        if row["safe_output_ref"] == call_id and row["evidence_type"] == "blocker_override"
    ]
    calls = db.list_safe_output_calls_for_run("run-work-item-override-blocker")

    assert work_item["state"] == "active"
    assert work_item["current_role"] == "engineering"
    assert work_item["attention_owner"] is None
    assert len(assignments) == 1
    assert assignments[0]["role_id"] == "engineering"
    assert assignments[0]["status"] == "queued"
    assert assignments[0]["payload"]["route_tool"] == "work_item.override_blocker"
    assert "implementation.record_change" in assignments[0]["payload"]["allowed_tools"]
    assert len(evidence) == 1
    assert evidence[0]["summary"].startswith("Blocker override to engineering:")
    assert calls[0]["terminal"] is True


def test_work_item_override_blocker_requires_target_role(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-override-missing-target",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )

    with pytest.raises(SafeOutputError, match="missing required fields: target_role"):
        SafeOutputService(db).record(
            run_id="run-work-item-override-missing-target",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.override_blocker",
                payload={
                    "work_item_id": "work-runtime-execution",
                    "reason": "Override without an owner should be rejected.",
                },
            ),
        )

    assert db.list_safe_output_calls_for_run("run-work-item-override-missing-target") == []


def test_work_item_override_blocker_requires_blocked_state(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-override-not-blocked",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )

    with pytest.raises(SafeOutputError, match="requires work item state `blocked`"):
        SafeOutputService(db).record(
            run_id="run-work-item-override-not-blocked",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.override_blocker",
                payload={
                    "work_item_id": "work-runtime-execution",
                    "target_role": "engineering",
                    "reason": "Try to override a non-blocked item.",
                },
            ),
        )

    assert db.list_safe_output_calls_for_run("run-work-item-override-not-blocked") == []


def test_work_item_override_blocker_replay_does_not_unblock_later_blocker(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-override-replay",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    service = SafeOutputService(db)
    call = SafeOutputCall(
        role_id="release-manager",
        tool_name="work_item.override_blocker",
        payload={
            "work_item_id": "work-runtime-execution",
            "target_role": "engineering",
            "reason": "Sponsor accepted the risk and authorized continuation.",
        },
    )
    call_id = service.record(run_id="run-work-item-override-replay", call=call)
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="active",
            to_state="blocked",
            actor_role="engineering",
            reason="A later security blocker was found.",
            owner="security-architect",
            reason_class="security_blocker",
            next_action="Security Architect must assess the new blocker.",
            retryable=True,
        )
    )

    service.process_recorded_call(call_id=call_id, run_id="run-work-item-override-replay", call=call)

    work_item = next(row for row in db.list_work_items() if row["work_item_id"] == "work-runtime-execution")
    assignments = [
        row
        for row in db.list_role_assignments()
        if row["source_ref"] == call_id and row["assignment_type"] == "work_item_blocker_override"
    ]
    override_events = [
        event
        for event in db.list_events("work-runtime-execution")
        if event["event_type"] == "work_item.transitioned"
        and event["payload"]["to_state"] == "active"
        and event["payload"]["actor_role"] == "release-manager"
    ]

    assert work_item["state"] == "blocked"
    assert work_item["attention_owner"] == "security-architect"
    assert len(assignments) == 1
    assert len(override_events) == 1


def test_override_blocker_db_helper_is_guarded_by_existing_records(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-override-db-guard",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    call_id = SafeOutputService(db).record(
        run_id="run-work-item-override-db-guard",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="work_item.override_blocker",
            payload={
                "work_item_id": "work-runtime-execution",
                "target_role": "engineering",
                "reason": "Sponsor accepted the risk and authorized continuation.",
            },
        ),
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="active",
            to_state="blocked",
            actor_role="engineering",
            reason="A later blocker should remain active.",
            owner="security-architect",
            reason_class="security_blocker",
            next_action="Security Architect must assess the new blocker.",
            retryable=True,
        )
    )

    db.override_blocker_with_assignment_and_evidence(
        request=TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="blocked",
            to_state="active",
            actor_role="release-manager",
            reason="Replay old override.",
            owner="engineering",
        ),
        assignment_id=f"assignment-{call_id}-blocker-override",
        role_id="engineering",
        source_ref=call_id,
        title="Replay old override",
        summary="Replay old override.",
        assignment_type="work_item_blocker_override",
        visibility_scope="project",
        payload={"safe_output_ref": call_id},
        evidence_id=f"evidence-{call_id}",
        evidence_type="blocker_override",
        evidence_summary="Replay old override.",
        evidence_role_id="release-manager",
        safe_output_ref=call_id,
    )

    work_item = next(row for row in db.list_work_items() if row["work_item_id"] == "work-runtime-execution")
    override_events = [
        event
        for event in db.list_events("work-runtime-execution")
        if event["event_type"] == "work_item.transitioned"
        and event["payload"]["to_state"] == "active"
        and event["payload"]["actor_role"] == "release-manager"
    ]

    assert work_item["state"] == "blocked"
    assert work_item["attention_owner"] == "security-architect"
    assert len(override_events) == 1


def test_deferred_work_item_override_blocker_can_be_replayed(tmp_path: Path) -> None:
    db = _blocked_work_db(tmp_path)
    db.create_run(
        run_id="run-work-item-override-deferred",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )
    call = SafeOutputCall(
        role_id="release-manager",
        tool_name="work_item.override_blocker",
        payload={
            "work_item_id": "work-runtime-execution",
            "target_role": "engineering",
            "reason": "Sponsor accepted the risk and authorized continuation.",
        },
    )
    call_id = SafeOutputService(db, process_effects=False).record(
        run_id="run-work-item-override-deferred",
        call=call,
    )

    assert db.get_work_item("work-runtime-execution").state == "blocked"

    SafeOutputService(db).process_recorded_call(
        call_id=call_id,
        run_id="run-work-item-override-deferred",
        call=call,
    )

    evidence = [
        row
        for row in db.list_work_item_evidence()
        if row["safe_output_ref"] == call_id and row["evidence_type"] == "blocker_override"
    ]
    assert db.get_work_item("work-runtime-execution").state == "active"
    assert db.get_role_assignment(f"assignment-{call_id}-blocker-override") is not None
    assert len(evidence) == 1


def test_work_item_supersede_rejects_deploying_state(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Ready.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering complete.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="active",
            to_state="release_review",
            actor_role="qa-engineer",
            reason="QA passed.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="release_review",
            to_state="deploying",
            actor_role="release-manager",
            reason="Deployment started.",
        )
    )
    db.create_run(
        run_id="run-work-item-supersede-deploying",
        role_id="release-manager",
        role_instance_id="agentic-mesh-dev.release-manager.1",
        work_item_id="work-runtime-execution",
    )

    with pytest.raises(SafeOutputError, match="cannot supersede work item state `deploying`"):
        SafeOutputService(db).record(
            run_id="run-work-item-supersede-deploying",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.supersede",
                payload={
                    "work_item_id": "work-runtime-execution",
                    "reason": "Do not hide an active deployment.",
                    "replacement_ref": "work-dashboard-redesign-v2",
                },
            ),
        )

    assert db.list_safe_output_calls_for_run("run-work-item-supersede-deploying") == []


def test_report_blocked_rejects_invalid_explicit_work_item_before_recording(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_run(
        run_id="run-work-blocker-invalid-target",
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        work_item_id=None,
    )

    with pytest.raises(ValueError, match="unknown work item"):
        SafeOutputService(db).record(
            run_id="run-work-blocker-invalid-target",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="report.blocked",
                payload={
                    "work_item_id": "work-missing",
                    "reason": "Architecture decision is missing.",
                    "owner": "solution-architect",
                    "next_action": "Provide ADR for the runtime boundary.",
                },
                terminal=True,
            ),
        )

    assert db.list_safe_output_calls_for_run("run-work-blocker-invalid-target") == []


def test_report_blocked_rejects_disallowed_work_item_state_before_recording(tmp_path: Path) -> None:
    db = _work_db(tmp_path)
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Ready.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering complete.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="active",
            to_state="release_review",
            actor_role="qa-engineer",
            reason="QA passed.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-runtime-execution",
            from_state="release_review",
            to_state="released",
            actor_role="release-manager",
            reason="Release accepted.",
        )
    )
    db.create_run(
        run_id="run-work-blocker-released",
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        work_item_id="work-runtime-execution",
    )

    with pytest.raises(SafeOutputError, match="cannot block work item state `released`"):
        SafeOutputService(db).record(
            run_id="run-work-blocker-released",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="report.blocked",
                payload={
                    "reason": "This should not split assignment and work item state.",
                    "owner": "solution-architect",
                    "next_action": "No action.",
                },
                terminal=True,
            ),
        )

    assert db.list_safe_output_calls_for_run("run-work-blocker-released") == []
    assert db.status_snapshot()["work_items"][0]["state"] == "released"


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
