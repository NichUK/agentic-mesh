from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.release import ComposeCommandResult
from agentic_mesh_v2.release import ComposeDeploymentTarget
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
                    "approval_ref": "approval-safe-output",
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
        releases = db.list_releases()
    finally:
        db.close()

    assert work.state == "closed"
    assert releases[0]["release_id"] == "release-safe-output-no-deployment"
    assert releases[0]["status"] == "no_deployment_disposition"
    assert releases[0]["deployment_result"].startswith("not_required:")


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
    finally:
        db.close()

    decisions = [row for row in evidence if row["evidence_type"] == "release_decision"]
    assert len(decisions) == 1
    assert decisions[0]["summary"] == (
        f"Release decision: request_changes; approval_ref: {request_id}; "
        "reason: Sponsor requested one more correction."
    )


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
                    "approval_ref": "approval-safe-output",
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
    assert receipt.safe_output_count == 2
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


class CliReleaseWorker:
    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        assert assignment.safe_output_transport is not None
        command = assignment.safe_output_transport["record_command"]
        _run_record_command(
            command,
            tool_name="release.record_no_deployment",
            payload={
                "work_item_id": "work-release-safe-output",
                "release_id": "release-safe-output-cli",
                "reason": "Documentation-only release; no runtime activation required.",
                "scope": "Close documentation-only release evidence.",
                "commit_ref": "commit-safe-output",
                "approval_ref": "approval-safe-output",
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
) -> None:
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
