from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.release import ComposeCommandResult
from agentic_mesh_v2.release import ComposeDeploymentTarget
from agentic_mesh_v2.release import ReleaseEvidence
from agentic_mesh_v2.release import ReleaseService
from agentic_mesh_v2.safe_output_mcp import SAFE_OUTPUT_MCP_TOOL
from agentic_mesh_v2.safe_output_mcp import handle_safe_output_mcp_request
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputError
from agentic_mesh_v2.safe_outputs import SafeOutputService
from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.state_machine import TransitionRequest


def test_release_manager_safe_outputs_record_no_deployment_and_close_work(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-no-deployment",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        approval_ref = _record_approved_release_decision(
            service,
            run_id="run-release-no-deployment",
        )

        service.record(
            run_id="run-release-no-deployment",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_no_deployment",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "release_id": "release-safe-output-no-deployment",
                    "reason": "Documentation-only release; no runtime activation required.",
                    "scope": "Close documentation-only release evidence.",
                    "rollback_plan": "No deployment was performed; reopen the work item if the evidence is wrong.",
                    "residual_risks": "None beyond accepting a no-deployment disposition.",
                    "approval_ref": approval_ref,
                    "commit_ref": "commit-safe-output",
                },
            ),
        )
        service.record(
            run_id="run-release-no-deployment",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.close",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "reason": "Sponsor approved no-deployment release closure.",
                },
                terminal=True,
            ),
        )

        work = db.get_work_item("work-release-safe-output")
        queue = db.get_queue_item("queue-release-safe-output")
        releases = db.list_releases()
    finally:
        db.close()

    assert work.state == "closed"
    assert queue["status"] == "closed"
    assert releases[0]["release_id"] == "release-safe-output-no-deployment"
    assert releases[0]["status"] == "no_deployment_disposition"
    assert releases[0]["deployment_result"].startswith("not_required:")


def test_release_manager_work_item_close_closes_no_deployment_work(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-work-item-close",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        approval_ref = _record_approved_release_decision(
            service,
            run_id="run-work-item-close",
        )

        service.record(
            run_id="run-work-item-close",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_no_deployment",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "release_id": "release-work-item-close",
                    "reason": "Documentation-only release; no runtime activation required.",
                    "scope": "Close through work_item.close with no-deployment evidence.",
                    "rollback_plan": "No deployment was performed; reopen the work item if the evidence is wrong.",
                    "residual_risks": "None beyond accepting a no-deployment disposition.",
                    "approval_ref": approval_ref,
                    "commit_ref": "commit-safe-output",
                },
            ),
        )
        call_id = service.record(
            run_id="run-work-item-close",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.close",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "reason": "Sponsor approved no-deployment release closure.",
                },
            ),
        )

        work = db.get_work_item("work-release-safe-output")
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert work.state == "closed"
    assert next(row for row in calls if row["call_id"] == call_id)["terminal"] == 1


def test_work_item_close_closes_already_released_work(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-work-item-close-released",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        approval_ref = _record_approved_release_decision(
            service,
            run_id="run-work-item-close-released",
        )
        service.record(
            run_id="run-work-item-close-released",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_no_deployment",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "release_id": "release-work-item-close-released",
                    "reason": "Documentation-only release; no runtime activation required.",
                    "scope": "Close already released work through work_item.close.",
                    "rollback_plan": "No deployment was performed; reopen the work item if the evidence is wrong.",
                    "residual_risks": "None beyond accepting a no-deployment disposition.",
                    "approval_ref": approval_ref,
                    "commit_ref": "commit-safe-output",
                },
            ),
        )
        db.transition_work_item(
            TransitionRequest(
                work_item_id="work-release-safe-output",
                from_state="release_review",
                to_state="released",
                actor_role="release-manager",
                reason="Release evidence accepted.",
            )
        )

        service.record(
            run_id="run-work-item-close-released",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="work_item.close",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "reason": "Sponsor approved already released work closure.",
                    "from_state": "release_review",
                },
            ),
        )
        work = db.get_work_item("work-release-safe-output")
    finally:
        db.close()

    assert work.state == "closed"


def test_work_item_close_rejects_missing_release_record_before_recording(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-work-item-close-no-release",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)

        with pytest.raises(SafeOutputError, match="requires a release record"):
            service.record(
                run_id="run-work-item-close-no-release",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="work_item.close",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "reason": "Sponsor approved closure.",
                    },
                ),
            )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert calls == []


def test_work_item_close_rejects_deployed_release_without_closure_evidence_before_recording(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-work-item-close-deployed-missing-evidence",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        ReleaseService(db).record_deployment(
            ReleaseEvidence(
                work_item_id="work-release-safe-output",
                release_id="release-work-item-close-deployed-missing-evidence",
                scope="Deployment record without closure package.",
                commit_ref="commit-safe-output",
                approval_ref="approval-safe-output",
                deployment_result="manual claim",
                smoke_result="passed",
                rollback_plan="Revert commit-safe-output.",
                residual_risks="Missing deployment run and release evidence links.",
            )
        )
        service = SafeOutputService(db)

        with pytest.raises(SafeOutputError, match="requires successful deployment and release evidence links"):
            service.record(
                run_id="run-work-item-close-deployed-missing-evidence",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="work_item.close",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "reason": "Sponsor approved closure.",
                    },
                ),
            )
        close_calls = [
            row
            for row in db.list_safe_output_calls()
            if row["tool_name"] == "work_item.close"
        ]
    finally:
        db.close()

    assert close_calls == []


def test_deferred_work_item_close_can_be_replayed_once(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-work-item-close-deferred",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        active_service = SafeOutputService(db)
        approval_ref = _record_approved_release_decision(
            active_service,
            run_id="run-work-item-close-deferred",
        )
        active_service.record(
            run_id="run-work-item-close-deferred",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_no_deployment",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "release_id": "release-work-item-close-deferred",
                    "reason": "Documentation-only release; no runtime activation required.",
                    "scope": "Close through deferred work_item.close replay.",
                    "rollback_plan": "No deployment was performed; reopen the work item if the evidence is wrong.",
                    "residual_risks": "None beyond accepting a no-deployment disposition.",
                    "approval_ref": approval_ref,
                    "commit_ref": "commit-safe-output",
                },
            ),
        )
        record_only_service = SafeOutputService(db, process_effects=False)
        call = SafeOutputCall(
            role_id="release-manager",
            tool_name="work_item.close",
            payload={
                "work_item_id": "work-release-safe-output",
                "reason": "Sponsor approved deferred no-deployment release closure.",
            },
        )
        call_id = record_only_service.record(run_id="run-work-item-close-deferred", call=call)

        active_service.process_recorded_call(
            call_id=call_id,
            run_id="run-work-item-close-deferred",
            call=call,
        )
        active_service.process_recorded_call(
            call_id=call_id,
            run_id="run-work-item-close-deferred",
            call=call,
        )
        work = db.get_work_item("work-release-safe-output")
        transition_events = [
            event
            for event in db.list_events("work-release-safe-output")
            if event["event_type"] == "work_item.transitioned"
        ]
    finally:
        db.close()

    assert work.state == "closed"
    assert sum(1 for event in transition_events if event["payload"]["to_state"] == "closed") == 1


def test_release_request_approval_safe_output_records_human_response_request(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-approval-request",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)

        call_id = service.record(
            run_id="run-release-approval-request",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.request_approval",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "question": "Approve release of work-release-safe-output?",
                    "gate_id": "release_decision_response",
                    "required_authority": "release_approver",
                },
                terminal=True,
            ),
        )
        service.process_recorded_call(
            call_id=call_id,
            run_id="run-release-approval-request",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.request_approval",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "question": "Approve release of work-release-safe-output?",
                    "gate_id": "release_decision_response",
                    "required_authority": "release_approver",
                },
                terminal=True,
            ),
        )

        requests = db.list_human_response_requests()
        work = db.get_work_item("work-release-safe-output")
    finally:
        db.close()

    assert work.state == "release_review"
    assert len(requests) == 1
    assert requests[0]["source_ref"] == call_id
    assert requests[0]["request_type"] == "release_approval"
    assert requests[0]["status"] == "awaiting_response"
    assert requests[0]["work_item_id"] == "work-release-safe-output"
    assert requests[0]["gate_id"] == "release_decision_response"
    assert requests[0]["required_authority"] == "release_approver"
    assert requests[0]["response_contract_id"] == "release-decision-v1"
    assert requests[0]["payload"]["card"]["request_id"] == requests[0]["request_id"]


def test_release_record_decision_records_release_decision_evidence(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-decision",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        request_call_id = service.record(
            run_id="run-release-decision",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.request_approval",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "question": "Approve release of work-release-safe-output?",
                },
                terminal=True,
            ),
        )
        request_id = f"human-response-{hashlib.sha256(request_call_id.encode('utf-8')).hexdigest()[:16]}"
        db.complete_human_response_request(
            request_id=request_id,
            response_value="approve",
            responder_ref="sponsor",
        )

        decision_call_id = service.record(
            run_id="run-release-decision",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_decision",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "decision": "approved",
                    "approval_ref": request_id,
                    "reason": "Sponsor approved release after reviewing evidence.",
                },
                terminal=True,
            ),
        )
        service.process_recorded_call(
            call_id=decision_call_id,
            run_id="run-release-decision",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_decision",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "decision": "approved",
                    "approval_ref": request_id,
                    "reason": "Sponsor approved release after reviewing evidence.",
                },
                terminal=True,
            ),
        )
        db.transition_work_item(
            TransitionRequest(
                work_item_id="work-release-safe-output",
                from_state="release_review",
                to_state="released",
                actor_role="release-manager",
                reason="Release completed after decision evidence was recorded.",
            )
        )
        service.process_recorded_call(
            call_id=decision_call_id,
            run_id="run-release-decision",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_decision",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "decision": "approved",
                    "approval_ref": request_id,
                    "reason": "Sponsor approved release after reviewing evidence.",
                },
                terminal=True,
            ),
        )
        evidence = db.list_work_item_evidence()
        event_types = [event["event_type"] for event in db.list_events()]
        assignments = db.list_role_assignments()
        work = db.get_work_item("work-release-safe-output")
    finally:
        db.close()

    decisions = [row for row in evidence if row["evidence_type"] == "release_decision"]
    assert len(decisions) == 1
    assert decisions[0]["safe_output_ref"] == decision_call_id
    assert decisions[0]["role_id"] == "release-manager"
    assert decisions[0]["summary"] == (
        f"Release decision: approve; approval_ref: {request_id}; "
        "reason: Sponsor approved release after reviewing evidence."
    )
    assert event_types.count("work_item_evidence.recorded") == 1
    assert work.state == "released"
    assert [row for row in assignments if row["assignment_type"] == "release_rework"] == []


def test_release_record_decision_rejects_mismatched_approval_response(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-decision-mismatch",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        request_call_id = service.record(
            run_id="run-release-decision-mismatch",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.request_approval",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "question": "Approve release of work-release-safe-output?",
                },
                terminal=True,
            ),
        )
        request_id = f"human-response-{hashlib.sha256(request_call_id.encode('utf-8')).hexdigest()[:16]}"
        db.complete_human_response_request(
            request_id=request_id,
            response_value="reject",
            responder_ref="sponsor",
        )

        with pytest.raises(SafeOutputError, match="does not match"):
            service.record(
                run_id="run-release-decision-mismatch",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.record_decision",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "decision": "approve",
                        "approval_ref": request_id,
                        "reason": "This should not pass.",
                    },
                    terminal=True,
                ),
            )
        evidence = db.list_work_item_evidence()
    finally:
        db.close()

    assert [row for row in evidence if row["evidence_type"] == "release_decision"] == []


def test_release_record_decision_accepts_response_request_id_alias(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-decision-alias",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        request_call_id = service.record(
            run_id="run-release-decision-alias",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.request_approval",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "question": "Approve release of work-release-safe-output?",
                },
                terminal=True,
            ),
        )
        request_id = f"human-response-{hashlib.sha256(request_call_id.encode('utf-8')).hexdigest()[:16]}"
        db.complete_human_response_request(
            request_id=request_id,
            response_value="request changes",
            responder_ref="sponsor",
        )

        service.record(
            run_id="run-release-decision-alias",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_decision",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "decision": "changes_requested",
                    "response_request_id": request_id,
                    "reason": "Sponsor requested one more correction.",
                },
                terminal=True,
            ),
        )
        evidence = db.list_work_item_evidence()
        work = db.get_work_item("work-release-safe-output")
        assignments = db.list_role_assignments()
    finally:
        db.close()

    decisions = [row for row in evidence if row["evidence_type"] == "release_decision"]
    assert len(decisions) == 1
    assert decisions[0]["summary"] == (
        f"Release decision: request_changes; approval_ref: {request_id}; "
        "reason: Sponsor requested one more correction."
    )
    assert work.state == "active"
    assert len([row for row in assignments if row["assignment_type"] == "release_rework"]) == 1


def test_release_request_changes_routes_work_back_to_engineering(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-request-changes-route",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)

        call_id = service.record(
            run_id="run-release-request-changes-route",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_decision",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "decision": "request_changes",
                    "reason": "Sponsor needs clearer release notes before approval.",
                    "source_documents": ["work-items/work-release-safe-output/140-release-record.md"],
                    "target_outputs": ["updated release notes", "QA regression note"],
                },
                terminal=True,
            ),
        )
        service.process_recorded_call(
            call_id=call_id,
            run_id="run-release-request-changes-route",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_decision",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "decision": "request_changes",
                    "reason": "Sponsor needs clearer release notes before approval.",
                    "source_documents": ["work-items/work-release-safe-output/140-release-record.md"],
                    "target_outputs": ["updated release notes", "QA regression note"],
                },
                terminal=True,
            ),
        )
        work = db.get_work_item("work-release-safe-output")
        work_row = next(row for row in db.list_work_items() if row["work_item_id"] == "work-release-safe-output")
        evidence = db.list_work_item_evidence()
        assignments = db.list_role_assignments()
        transition_events = [
            event
            for event in db.list_events("work-release-safe-output")
            if event["event_type"] == "work_item.transitioned"
        ]
    finally:
        db.close()

    assert work.state == "active"
    assert work_row["current_role"] == "engineering"
    decisions = [row for row in evidence if row["evidence_type"] == "release_decision"]
    assert len(decisions) == 1
    rework_assignments = [row for row in assignments if row["assignment_type"] == "release_rework"]
    assert len(rework_assignments) == 1
    assert rework_assignments[0]["assignment_id"] == f"assignment-{call_id}-release-rework"
    assert rework_assignments[0]["role_id"] == "engineering"
    assert rework_assignments[0]["status"] == "queued"
    assert rework_assignments[0]["payload"]["safe_output_ref"] == call_id
    assert rework_assignments[0]["payload"]["release_decision"] == "request_changes"
    assert rework_assignments[0]["payload"]["source_documents"] == [
        "work-items/work-release-safe-output/140-release-record.md"
    ]
    assert rework_assignments[0]["payload"]["target_outputs"] == ["updated release notes", "QA regression note"]
    assert "implementation.record_change" in rework_assignments[0]["payload"]["allowed_tools"]
    assert sum(
        1
        for event in transition_events
        if event["payload"]["from_state"] == "release_review"
        and event["payload"]["to_state"] == "active"
    ) == 1


def test_release_request_changes_replay_repairs_missing_engineering_assignment(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-request-changes-replay",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        call = SafeOutputCall(
            role_id="release-manager",
            tool_name="release.record_decision",
            payload={
                "work_item_id": "work-release-safe-output",
                "decision": "request_changes",
                "reason": "Sponsor needs clearer rollback notes.",
            },
            terminal=True,
        )
        call_id = SafeOutputService(db, process_effects=False).record(
            run_id="run-release-request-changes-replay",
            call=call,
        )
        db.add_work_item_evidence(
            evidence_id=f"evidence-{call_id}",
            work_item_id="work-release-safe-output",
            evidence_type="release_decision",
            summary="Release decision: request_changes; reason: Sponsor needs clearer rollback notes.",
            role_id="release-manager",
            safe_output_ref=call_id,
        )
        db.transition_work_item(
            TransitionRequest(
                work_item_id="work-release-safe-output",
                from_state="release_review",
                to_state="active",
                actor_role="release-manager",
                reason="Simulate transition committed before assignment write failed.",
                owner="engineering",
            )
        )
        service = SafeOutputService(db)
        service.process_recorded_call(
            call_id=call_id,
            run_id="run-release-request-changes-replay",
            call=call,
        )
        service.process_recorded_call(
            call_id=call_id,
            run_id="run-release-request-changes-replay",
            call=call,
        )
        assignments = db.list_role_assignments()
        evidence = db.list_work_item_evidence()
    finally:
        db.close()

    assert len(assignments) == 1
    assert assignments[0]["assignment_id"] == f"assignment-{call_id}-release-rework"
    assert assignments[0]["role_id"] == "engineering"
    assert assignments[0]["assignment_type"] == "release_rework"
    assert len([row for row in evidence if row["evidence_type"] == "release_decision"]) == 1


def test_release_request_changes_replay_does_not_reroute_after_rework_returns_to_release_review(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review_from_quality_approve(db)
        db.create_run(
            run_id="run-release-request-changes-reroute-guard",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        call = SafeOutputCall(
            role_id="release-manager",
            tool_name="release.record_decision",
            payload={
                "work_item_id": "work-release-safe-output",
                "decision": "request_changes",
                "reason": "Sponsor needs clearer release notes before approval.",
            },
            terminal=True,
        )
        call_id = service.record(run_id="run-release-request-changes-reroute-guard", call=call)
        db.transition_work_item(
            TransitionRequest(
                work_item_id="work-release-safe-output",
                from_state="active",
                to_state="release_review",
                actor_role="qa-engineer",
                reason="Rework completed and QA returned the item to release review.",
                owner="release-manager",
            )
        )
        service.process_recorded_call(
            call_id=call_id,
            run_id="run-release-request-changes-reroute-guard",
            call=call,
        )
        work = db.get_work_item("work-release-safe-output")
        work_row = next(row for row in db.list_work_items() if row["work_item_id"] == "work-release-safe-output")
        assignments = db.list_role_assignments()
        transition_events = [
            event
            for event in db.list_events("work-release-safe-output")
            if event["event_type"] == "work_item.transitioned"
        ]
    finally:
        db.close()

    assert work.state == "release_review"
    assert work_row["current_role"] == "release-manager"
    assert len([row for row in assignments if row["assignment_type"] == "release_review"]) == 1
    assert len([row for row in assignments if row["assignment_type"] == "release_rework"]) == 1
    assert sum(
        1
        for event in transition_events
        if event["payload"]["from_state"] == "release_review"
        and event["payload"]["to_state"] == "active"
    ) == 1


def test_release_record_decision_rejects_conflicting_approval_refs(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-decision-conflicting-refs",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        with pytest.raises(SafeOutputError, match="must match"):
            service.record(
                run_id="run-release-decision-conflicting-refs",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.record_decision",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "decision": "approve",
                        "approval_ref": "human-response-one",
                        "response_request_id": "human-response-two",
                    },
                    terminal=True,
                ),
            )
        evidence = db.list_work_item_evidence()
    finally:
        db.close()

    assert [row for row in evidence if row["evidence_type"] == "release_decision"] == []


def test_release_manager_safe_output_executes_compose_deployment(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    executed: list[list[str]] = []

    def compose_runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ComposeCommandResult:
        executed.append(command)
        return ComposeCommandResult(exit_code=0, stdout="started")

    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-deploy",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        release = ReleaseService(db, compose_runner=compose_runner)
        compose_file = _compose_file(tmp_path)
        release.register_compose_target(
            ComposeDeploymentTarget(
                target_id="target-compose-safe-output",
                project_id="test-project",
                compose_files=(compose_file,),
                service_name="v2-runtime",
                external_base_url="http://linuxch:8100",
            )
        )
        service = SafeOutputService(db, release_service=release)
        approval_ref = _record_approved_release_decision(
            service,
            run_id="run-release-deploy",
        )

        service.record(
            run_id="run-release-deploy",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.deploy",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "release_id": "release-safe-output-deploy",
                    "target_id": "target-compose-safe-output",
                    "reason": "Deploy the approved slice to the configured compose target.",
                    "scope": "Activate the safe-output release deployment path.",
                    "commit_ref": "commit-safe-output",
                    "approval_ref": approval_ref,
                    "rollback_plan": "Revert commit-safe-output and redeploy the previous image.",
                    "residual_risks": "Local compose target only.",
                    "smoke_checks": {
                        "healthz": "passed",
                        "status_json": "passed",
                    },
                    "evidence_links": _evidence_links(),
                },
            ),
        )
        service.record(
            run_id="run-release-deploy",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.close",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "reason": "Sponsor approved deployed release closure.",
                },
                terminal=True,
            ),
        )

        work = db.get_work_item("work-release-safe-output")
        snapshot = db.status_snapshot()
    finally:
        db.close()

    assert executed
    assert work.state == "closed"
    assert snapshot["releases"][0]["status"] == "deployed"
    assert snapshot["deployment_runs"][0]["status"] == "succeeded"
    assert snapshot["deployment_runs"][0]["target_id"] == "target-compose-safe-output"
    assert len(snapshot["release_evidence_links"]) == 7


def test_release_deploy_rejects_non_accepted_evidence_links_before_recording(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    executed: list[list[str]] = []

    def compose_runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ComposeCommandResult:
        executed.append(command)
        return ComposeCommandResult(exit_code=0, stdout="started")

    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-deploy-rejected-evidence",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        release = ReleaseService(db, compose_runner=compose_runner)
        release.register_compose_target(
            ComposeDeploymentTarget(
                target_id="target-compose-safe-output",
                project_id="test-project",
                compose_files=(_compose_file(tmp_path),),
                service_name="v2-runtime",
                external_base_url="http://linuxch:8100",
            )
        )
        service = SafeOutputService(db, release_service=release)
        approval_ref = _record_approved_release_decision(
            service,
            run_id="run-release-deploy-rejected-evidence",
        )
        evidence_links = _evidence_links()
        evidence_links[0]["status"] = "rejected"

        with pytest.raises(SafeOutputError, match="must have status `accepted`"):
            service.record(
                run_id="run-release-deploy-rejected-evidence",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.deploy",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "release_id": "release-safe-output-rejected-evidence",
                        "target_id": "target-compose-safe-output",
                        "reason": "Try to deploy with rejected evidence.",
                        "scope": "Rejected evidence should not deploy.",
                        "commit_ref": "commit-safe-output",
                        "approval_ref": approval_ref,
                        "rollback_plan": "Do not deploy until evidence is accepted.",
                        "residual_risks": "Evidence has not been accepted.",
                        "smoke_checks": {
                            "healthz": "passed",
                            "status_json": "passed",
                        },
                        "evidence_links": evidence_links,
                    },
                ),
            )
        deploy_calls = [
            row
            for row in db.list_safe_output_calls()
            if row["tool_name"] == "release.deploy"
        ]
    finally:
        db.close()

    assert executed == []
    assert deploy_calls == []


def test_release_manager_cli_transport_release_outputs_apply_once_in_role_service(tmp_path: Path) -> None:
    db_path = tmp_path / "v2.sqlite3"
    db = V2Database(db_path)
    try:
        db.migrate()
        _work_in_release_review(db)
        service = SafeOutputService(db)
        role = RoleService(
            db=db,
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            worker=CliReleaseWorker(),
            safe_outputs=service,
        )
        receipt = role.run_assignment(
            RoleAssignment(
                role_id="release-manager",
                role_instance_id="test-project.release-manager.1",
                work_item_id="work-release-safe-output",
                title="Release safe-output work",
                summary="Exercise CLI transport release effects.",
            ),
            run_id="run-release-cli-transport",
        )
        work = db.get_work_item("work-release-safe-output")
        releases = db.list_releases()
    finally:
        db.close()

    assert receipt.status == "completed"
    assert receipt.safe_output_count == 3
    assert work.state == "closed"
    assert len(releases) == 1


def test_release_manager_mcp_transport_records_without_immediate_release_effects(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-mcp-transport",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )

        response = handle_safe_output_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "id": "release-mcp-1",
                "method": "tools/call",
                "params": {
                    "name": SAFE_OUTPUT_MCP_TOOL,
                    "arguments": {
                        "run_id": "run-release-mcp-transport",
                        "role_id": "release-manager",
                        "tool_name": "release.record_no_deployment",
                        "payload": {
                            "work_item_id": "work-release-safe-output",
                            "release_id": "release-safe-output-mcp",
                            "reason": "Documentation-only release; no runtime activation required.",
                            "scope": "Record MCP transport release intent.",
                            "commit_ref": "commit-safe-output",
                            "approval_ref": "approval-safe-output",
                            "rollback_plan": "No deployment was performed; reopen if needed.",
                            "residual_risks": "None beyond accepting a no-deployment disposition.",
                        },
                    },
                },
            },
        )
        calls = db.list_safe_output_calls()
        releases = db.list_releases()
        work = db.get_work_item("work-release-safe-output")
    finally:
        db.close()

    assert response is not None
    assert response["result"]["structuredContent"]["status"] == "ok"
    assert len(calls) == 1
    assert releases == []
    assert work.state == "release_review"


def test_release_deploy_safe_output_rejects_command_override(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-command-override",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        with pytest.raises(SafeOutputError, match="configured deployment target command"):
            service.record(
                run_id="run-release-command-override",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.deploy",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "release_id": "release-safe-output-override",
                        "target_id": "target-compose-safe-output",
                        "reason": "Try to override deployment.",
                        "scope": "Bad command override.",
                        "commit_ref": "commit-safe-output",
                        "approval_ref": "approval-safe-output",
                        "rollback_plan": "Do not deploy.",
                        "residual_risks": "Unsafe override.",
                        "command": ["python", "-c", "pass"],
                    },
                ),
            )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert calls == []


def test_release_deploy_safe_output_requires_approval_and_commit(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-deploy-no-provenance",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        with pytest.raises(SafeOutputError, match="missing required fields: approval_ref, commit_ref"):
            service.record(
                run_id="run-release-deploy-no-provenance",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.deploy",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "target_id": "target-compose-safe-output",
                        "reason": "Deploy without provenance.",
                        "scope": "Bad deploy.",
                        "rollback_plan": "Revert.",
                        "residual_risks": "Unknown.",
                    },
                ),
            )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert calls == []


def test_release_no_deployment_safe_output_requires_approval_and_commit(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-no-provenance",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        with pytest.raises(SafeOutputError, match="missing required fields: approval_ref, commit_ref"):
            service.record(
                run_id="run-release-no-provenance",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.record_no_deployment",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "reason": "No deployment.",
                        "scope": "No-deployment release.",
                        "rollback_plan": "No deployment performed.",
                        "residual_risks": "Unknown.",
                    },
                ),
            )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert calls == []


def test_release_activation_rejects_unrecorded_approval_ref(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-activation-unapproved",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        with pytest.raises(SafeOutputError, match="approved release decision"):
            service.record(
                run_id="run-release-activation-unapproved",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.record_no_deployment",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "release_id": "release-safe-output-unapproved",
                        "reason": "Try to close without a recorded release decision.",
                        "scope": "Bad no-deployment release.",
                        "rollback_plan": "No deployment performed.",
                        "residual_risks": "Unknown.",
                        "approval_ref": "approval-safe-output",
                        "commit_ref": "commit-safe-output",
                    },
                ),
            )
        releases = db.list_releases()
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert releases == []
    assert calls == []


def test_release_activation_rejects_rejected_release_decision(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-activation-rejected",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        decision_ref = service.record(
            run_id="run-release-activation-rejected",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_decision",
                payload={
                    "work_item_id": "work-release-safe-output",
                    "decision": "reject",
                    "reason": "Sponsor rejected release.",
                },
                terminal=True,
            ),
        )
        with pytest.raises(SafeOutputError, match="approved release decision"):
            service.record(
                run_id="run-release-activation-rejected",
                call=SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.record_no_deployment",
                    payload={
                        "work_item_id": "work-release-safe-output",
                        "release_id": "release-safe-output-rejected",
                        "reason": "Try to close after rejected release decision.",
                        "scope": "Bad no-deployment release.",
                        "rollback_plan": "No deployment performed.",
                        "residual_risks": "Sponsor rejected release.",
                        "approval_ref": decision_ref,
                        "commit_ref": "commit-safe-output",
                    },
                ),
            )
        releases = db.list_releases()
        work = db.get_work_item("work-release-safe-output")
        assignments = db.list_role_assignments()
    finally:
        db.close()

    assert releases == []
    assert work.state == "release_review"
    assert [row for row in assignments if row["assignment_type"] == "release_rework"] == []


def test_release_activation_rejects_deferred_unrecorded_approval_ref(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        db.create_run(
            run_id="run-release-deferred-unapproved",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        record_only = SafeOutputService(db, process_effects=False)
        call = SafeOutputCall(
            role_id="release-manager",
            tool_name="release.record_no_deployment",
            payload={
                "work_item_id": "work-release-safe-output",
                "release_id": "release-safe-output-deferred-unapproved",
                "reason": "Try to close without a recorded release decision.",
                "scope": "Bad deferred no-deployment release.",
                "rollback_plan": "No deployment performed.",
                "residual_risks": "Unknown.",
                "approval_ref": "approval-safe-output",
                "commit_ref": "commit-safe-output",
            },
        )
        call_id = record_only.record(run_id="run-release-deferred-unapproved", call=call)
        with pytest.raises(SafeOutputError, match="approved release decision"):
            SafeOutputService(db).process_recorded_call(
                call_id=call_id,
                run_id="run-release-deferred-unapproved",
                call=call,
            )
        releases = db.list_releases()
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert len(calls) == 1
    assert releases == []


def test_release_activation_rejects_deferred_wrong_work_item_decision_ref(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_in_release_review(db)
        _other_work_in_release_review(db)
        db.create_run(
            run_id="run-release-deferred-wrong-work",
            role_id="release-manager",
            role_instance_id="test-project.release-manager.1",
            work_item_id="work-release-safe-output",
        )
        service = SafeOutputService(db)
        wrong_work_decision_ref = service.record(
            run_id="run-release-deferred-wrong-work",
            call=SafeOutputCall(
                role_id="release-manager",
                tool_name="release.record_decision",
                payload={
                    "work_item_id": "work-other-release-safe-output",
                    "decision": "approve",
                    "reason": "Sponsor approved a different work item.",
                },
                terminal=True,
            ),
        )
        record_only = SafeOutputService(db, process_effects=False)
        call = SafeOutputCall(
            role_id="release-manager",
            tool_name="release.record_no_deployment",
            payload={
                "work_item_id": "work-release-safe-output",
                "release_id": "release-safe-output-deferred-wrong-work",
                "reason": "Try to close with a decision for another work item.",
                "scope": "Bad deferred no-deployment release.",
                "rollback_plan": "No deployment performed.",
                "residual_risks": "Wrong work item approval.",
                "approval_ref": wrong_work_decision_ref,
                "commit_ref": "commit-safe-output",
            },
        )
        call_id = record_only.record(run_id="run-release-deferred-wrong-work", call=call)
        with pytest.raises(SafeOutputError, match="approved release decision"):
            service.process_recorded_call(
                call_id=call_id,
                run_id="run-release-deferred-wrong-work",
                call=call,
            )
        releases = db.list_releases()
    finally:
        db.close()

    assert releases == []


class CliReleaseWorker:
    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        assert assignment.safe_output_transport is not None
        command = assignment.safe_output_transport["record_command"]
        approval_ref = _run_record_command(
            command,
            tool_name="release.record_decision",
            payload={
                "work_item_id": "work-release-safe-output",
                "decision": "approve",
                "reason": "Sponsor approved no-deployment release closure.",
            },
        )
        _run_record_command(
            command,
            tool_name="release.record_no_deployment",
            payload={
                "work_item_id": "work-release-safe-output",
                "release_id": "release-safe-output-cli",
                "reason": "Documentation-only release; no runtime activation required.",
                "scope": "Close documentation-only release evidence.",
                "commit_ref": "commit-safe-output",
                "approval_ref": approval_ref,
                "rollback_plan": "No deployment was performed; reopen the work item if the evidence is wrong.",
                "residual_risks": "None beyond accepting a no-deployment disposition.",
            },
        )
        _run_record_command(
            command,
            tool_name="release.close",
            payload={
                "work_item_id": "work-release-safe-output",
                "reason": "Sponsor approved no-deployment release closure.",
            },
            terminal=True,
        )
        return []


def _run_record_command(
    command: list[str],
    *,
    tool_name: str,
    payload: dict[str, object],
    terminal: bool = False,
) -> str:
    full_command = [
        *command,
        "--tool-name",
        tool_name,
        "--payload-json",
        json.dumps(payload),
    ]
    if terminal:
        full_command.append("--terminal")
    completed = subprocess.run(full_command, capture_output=True, check=False, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return str(json.loads(completed.stdout)["call_id"])


def _record_approved_release_decision(service: SafeOutputService, *, run_id: str) -> str:
    return service.record(
        run_id=run_id,
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="release.record_decision",
            payload={
                "work_item_id": "work-release-safe-output",
                "decision": "approve",
                "reason": "Sponsor approved release after reviewing evidence.",
            },
            terminal=True,
        ),
    )


def _work_in_release_review(db: V2Database) -> None:
    db.create_queue_item(
        queue_item_id="queue-release-safe-output",
        title="Release safe-output work",
        summary="Exercise release-manager safe-output authority.",
        owner_role="release-manager",
    )
    db.mark_queue_ready("queue-release-safe-output", actor_role="product-manager", reason="Ready.")
    db.promote_queue_item(
        queue_item_id="queue-release-safe-output",
        work_item_id="work-release-safe-output",
        owner_role="release-manager",
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-safe-output",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Product ready.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-safe-output",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering complete.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-safe-output",
            from_state="active",
            to_state="release_review",
            actor_role="qa-engineer",
            reason="QA passed.",
        )
    )


def _work_in_release_review_from_quality_approve(db: V2Database) -> None:
    db.create_queue_item(
        queue_item_id="queue-release-safe-output",
        title="Release safe-output work",
        summary="Exercise release-manager safe-output authority.",
        owner_role="engineering",
    )
    db.mark_queue_ready("queue-release-safe-output", actor_role="product-manager", reason="Ready.")
    db.promote_queue_item(
        queue_item_id="queue-release-safe-output",
        work_item_id="work-release-safe-output",
        owner_role="engineering",
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-safe-output",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Product ready.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-safe-output",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering complete.",
        )
    )
    db.create_run(
        run_id="run-release-safe-output-qa-approval",
        role_id="qa-engineer",
        role_instance_id="test-project.qa-engineer.1",
        work_item_id="work-release-safe-output",
    )
    service = SafeOutputService(db)
    service.record(
        run_id="run-release-safe-output-qa-approval",
        call=SafeOutputCall(
            role_id="qa-engineer",
            tool_name="test_evidence.record",
            payload={"work_item_id": "work-release-safe-output", "summary": "Release-safe-output QA evidence passed."},
        ),
    )
    service.record(
        run_id="run-release-safe-output-qa-approval",
        call=SafeOutputCall(
            role_id="qa-engineer",
            tool_name="quality.approve",
            payload={"work_item_id": "work-release-safe-output", "summary": "QA approved release review."},
            terminal=True,
        ),
    )


def _other_work_in_release_review(db: V2Database) -> None:
    db.create_queue_item(
        queue_item_id="queue-other-release-safe-output",
        title="Other release safe-output work",
        summary="Exercise approval isolation between work items.",
        owner_role="release-manager",
    )
    db.mark_queue_ready("queue-other-release-safe-output", actor_role="product-manager", reason="Ready.")
    db.promote_queue_item(
        queue_item_id="queue-other-release-safe-output",
        work_item_id="work-other-release-safe-output",
        owner_role="release-manager",
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-other-release-safe-output",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Product ready.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-other-release-safe-output",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering complete.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-other-release-safe-output",
            from_state="active",
            to_state="release_review",
            actor_role="qa-engineer",
            reason="QA passed.",
        )
    )


def _compose_file(tmp_path: Path) -> Path:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  v2-runtime:
    image: agentic-mesh:local
    command: python -m agentic_mesh_v2.cli --db /mesh/project/state/v2.sqlite3 serve
    environment:
      AGENTIC_MESH_PROJECT_FILE: /mesh/project/agentic-mesh/project.yaml
    volumes:
      - ./project:/mesh/project
""".strip(),
        encoding="utf-8",
    )
    return compose_file


def _evidence_links() -> list[dict[str, str]]:
    return [
        {"artifact_ref": "work-items/work-release-safe-output/020-product-definition.md", "artifact_type": "product", "role_id": "product-manager"},
        {"artifact_ref": "work-items/work-release-safe-output/040-architecture.md", "artifact_type": "architecture", "role_id": "solution-architect"},
        {"artifact_ref": "work-items/work-release-safe-output/050-security.md", "artifact_type": "security", "role_id": "security-architect"},
        {"artifact_ref": "work-items/work-release-safe-output/060-prompt-contract.md", "artifact_type": "prompt", "role_id": "prompt-engineer"},
        {"artifact_ref": "work-items/work-release-safe-output/100-implementation-log.md", "artifact_type": "engineering", "role_id": "engineering"},
        {"artifact_ref": "work-items/work-release-safe-output/110-quality-evidence.md", "artifact_type": "qa", "role_id": "qa-engineer"},
        {"artifact_ref": "work-items/work-release-safe-output/140-release-record.md", "artifact_type": "release", "role_id": "release-manager"},
    ]
