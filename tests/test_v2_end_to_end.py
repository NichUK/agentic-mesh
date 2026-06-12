from pathlib import Path

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.release import ComposeCommandResult
from agentic_mesh_v2.release import ComposeDeploymentTarget
from agentic_mesh_v2.release import ReleaseService
from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputService


class StaticWorker:
    def __init__(self, calls: list[SafeOutputCall]) -> None:
        self.calls = calls

    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        return self.calls


def test_v2_one_real_slice_release_happy_path(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()

    db.create_queue_item(
        queue_item_id="queue-1",
        title="Compact status table",
        summary="Make status dashboard rows compact and readable.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-1", actor_role="product-manager", reason="Product can shape it.")
    work = db.promote_queue_item(
        queue_item_id="queue-1",
        work_item_id="work-1",
        owner_role="product-manager",
    )
    assert work.state == "shaping"
    db.create_role_assignment(
        assignment_id="assignment-product-1",
        role_id="product-manager",
        work_item_id="work-1",
        source_ref="queue-1",
        title="Shape compact status table",
        summary="Create product definition and mark the work ready.",
        assignment_type="product_shaping",
        visibility_scope="project",
        payload={},
    )

    pm = RoleService(
        db=db,
        role_id="product-manager",
        role_instance_id="pm-1",
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="document.propose_update",
                    payload={
                        "path": "work-items/work-1/020-product-definition.md",
                        "document_type": "product_definition",
                        "content": "Product evidence.",
                    },
                ),
                SafeOutputCall(
                    role_id="product-manager",
                    tool_name="work_item.mark_ready",
                    payload={
                        "work_item_id": "work-1",
                        "reason": "Product definition accepted; Engineering can implement.",
                        "source_documents": ["work-items/work-1/020-product-definition.md"],
                        "target_outputs": ["implementation_change", "test_evidence", "release"],
                    },
                    terminal=True,
                ),
            ]
        ),
    )
    pm_receipt = pm.run_next_assignment()
    assert pm_receipt is not None
    assert pm_receipt.terminal_tool == "work_item.mark_ready"
    assert db.get_work_item("work-1").state == "ready"

    engineering = RoleService(
        db=db,
        role_id="engineering",
        role_instance_id="eng-1",
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="engineering",
                    tool_name="implementation.record_change",
                    payload={"work_item_id": "work-1", "summary": "Implemented compact table."},
                ),
                SafeOutputCall(
                    role_id="engineering",
                    tool_name="handoff.request",
                    payload={
                        "work_item_id": "work-1",
                        "target_role": "qa-engineer",
                        "reason": "Implementation ready for QA.",
                    },
                    terminal=True,
                ),
            ]
        ),
    )
    eng_receipt = engineering.run_next_assignment()
    assert eng_receipt is not None
    assert eng_receipt.terminal_tool == "handoff.request"
    assert db.get_work_item("work-1").state == "active"

    qa = RoleService(
        db=db,
        role_id="qa-engineer",
        role_instance_id="qa-1",
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="qa-engineer",
                    tool_name="test_evidence.record",
                    payload={"work_item_id": "work-1", "summary": "BDD and unit evidence passed."},
                ),
                SafeOutputCall(
                    role_id="qa-engineer",
                    tool_name="quality.approve",
                    payload={"work_item_id": "work-1", "summary": "QA passed."},
                    terminal=True,
                ),
            ]
        ),
    )
    qa_receipt = qa.run_next_assignment()
    assert qa_receipt is not None
    assert qa_receipt.terminal_tool == "quality.approve"
    assert db.get_work_item("work-1").state == "release_review"

    adapter = LocalTeamsTestAdapter(db, _connector_config())
    adapter.install()
    conversation = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-release-approval-thread",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "Release approval discussion for compact status table.",
            "thread_ref": "thread-release-approval",
        }
    )
    release_approval = RoleService(
        db=db,
        role_id="release-manager",
        role_instance_id="rel-approval-1",
        safe_outputs=ConnectorSafeOutputService(db, adapter=adapter),
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.request_approval",
                    payload={
                        "work_item_id": "work-1",
                        "title": "Approve compact status table release",
                        "question": "Approve release of work-1?",
                        "conversation_id": conversation.conversation_id,
                        "destination_ref": "channel-project",
                        "destination_type": "channel",
                        "thread_ref": "thread-release-approval",
                        "required_authority": "release_approver",
                        "gate_id": "release_decision_response",
                    },
                    terminal=True,
                )
            ]
        ),
    )
    approval_receipt = release_approval.run_next_assignment()
    assert approval_receipt is not None
    assert approval_receipt.terminal_tool == "release.request_approval"
    request_id = str(db.list_human_response_requests()[0]["request_id"])
    adapter.submit_card_response(
        request_id=request_id,
        responder_ref="nicholas",
        response_value="approved",
        comment="Approved for release.",
    )

    compose_file = _compose_file(tmp_path)
    executed: list[list[str]] = []

    def compose_runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ComposeCommandResult:
        executed.append(command)
        return ComposeCommandResult(exit_code=0, stdout="started")

    release = ReleaseService(db, compose_runner=compose_runner)
    release.register_compose_target(
        ComposeDeploymentTarget(
            target_id="target-compose-local",
            project_id="agentic-mesh-dev",
            compose_files=(compose_file,),
            service_name="v2-runtime",
            external_base_url="http://linuxch:8100",
        )
    )
    release_manager = RoleService(
        db=db,
        role_id="release-manager",
        role_instance_id="rel-1",
        safe_outputs=SafeOutputService(db, release_service=release),
        worker=StaticWorker(
            [
                SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.record_decision",
                    payload={
                        "work_item_id": "work-1",
                        "decision": "approve",
                        "approval_ref": request_id,
                        "reason": "Sponsor approved release after reviewing evidence.",
                    },
                ),
                SafeOutputCall(
                    role_id="release-manager",
                    tool_name="release.deploy",
                    payload={
                        "work_item_id": "work-1",
                        "release_id": "rel-1",
                        "target_id": "target-compose-local",
                        "reason": "Deploy the approved compact status table slice.",
                        "scope": "Compact status table.",
                        "commit_ref": "abc123",
                        "approval_ref": request_id,
                        "rollback_plan": "Revert abc123 and redeploy previous image.",
                        "residual_risks": "None known.",
                        "smoke_checks": {
                            "healthz": "passed",
                            "status_json": "passed",
                            "inbound_dm": "passed",
                            "outbound_reply": "passed",
                            "approval_response": "passed",
                            "permission_failure": "passed",
                        },
                        "evidence_links": _release_evidence_links(),
                    },
                ),
                SafeOutputCall(
                    role_id="release-manager",
                    tool_name="work_item.close",
                    payload={"work_item_id": "work-1", "reason": "Sponsor approved release."},
                    terminal=True,
                ),
            ]
        ),
    )
    release_receipt = release_manager.run_next_assignment()

    assert release_receipt is not None
    assert release_receipt.terminal_tool == "work_item.close"
    assert executed
    assert db.get_work_item("work-1").state == "closed"
    snapshot = db.status_snapshot()
    evidence = db.list_work_item_evidence()
    event_types = [event["event_type"] for event in db.list_events()]
    assert snapshot["role_assignment_statuses"]["completed"] >= 4
    assert any(row["evidence_type"] == "implementation_change" for row in evidence)
    assert any(row["evidence_type"] == "test_evidence" for row in evidence)
    assert any(row["evidence_type"] == "release_decision" for row in evidence)
    assert snapshot["releases"][0]["status"] == "deployed"
    assert snapshot["deployment_runs"][0]["status"] == "succeeded"
    assert len(snapshot["release_evidence_links"]) == 7
    assert "queue_item.created" in event_types
    assert "safe_output.recorded" in event_types
    assert "human_response.responded" in event_types
    assert "release.recorded" in event_types
    assert "deployment_run.recorded" in event_types
    assert "release_evidence.linked" in event_types
    assert event_types.count("work_item.transitioned") >= 4


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


def _connector_config() -> ConnectorConfig:
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


def _release_evidence_links() -> list[dict[str, str]]:
    return [
        {"artifact_ref": "work-items/work-1/020-product-definition.md", "artifact_type": "product", "role_id": "product-manager"},
        {"artifact_ref": "work-items/work-1/040-architecture.md", "artifact_type": "architecture", "role_id": "solution-architect"},
        {"artifact_ref": "work-items/work-1/050-security.md", "artifact_type": "security", "role_id": "security-architect"},
        {"artifact_ref": "work-items/work-1/060-prompt-contract.md", "artifact_type": "prompt", "role_id": "prompt-engineer"},
        {"artifact_ref": "work-items/work-1/100-implementation-log.md", "artifact_type": "engineering", "role_id": "engineering"},
        {"artifact_ref": "work-items/work-1/110-quality-evidence.md", "artifact_type": "qa", "role_id": "qa-engineer"},
        {"artifact_ref": "work-items/work-1/140-release-record.md", "artifact_type": "release", "role_id": "release-manager"},
    ]
