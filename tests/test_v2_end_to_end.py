from pathlib import Path

from agentic_mesh_v2.release import ComposeCommandResult
from agentic_mesh_v2.release import ComposeDeploymentTarget
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.release import ReleaseEvidence
from agentic_mesh_v2.release import ReleaseEvidenceLink
from agentic_mesh_v2.release import ReleaseService
from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.state_machine import TransitionRequest


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
                    tool_name="handoff.request",
                    payload={"target_role": "engineering", "reason": "Product definition approved."},
                    terminal=True,
                ),
            ]
        ),
    )
    receipt = pm.run_assignment(
        RoleAssignment(
            role_id="product-manager",
            role_instance_id="pm-1",
            work_item_id="work-1",
            title="Compact status table",
            summary="Make status dashboard rows compact and readable.",
        )
    )
    assert receipt.terminal_tool == "handoff.request"
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-1",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Product definition accepted.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-1",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering claimed work.",
        )
    )

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
                    payload={"target_role": "qa-engineer", "reason": "Implementation ready for QA."},
                    terminal=True,
                ),
            ]
        ),
    )
    engineering.run_assignment(
        RoleAssignment(
            role_id="engineering",
            role_instance_id="eng-1",
            work_item_id="work-1",
            title="Compact status table",
            summary="Make status dashboard rows compact and readable.",
        )
    )

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
                    tool_name="handoff.request",
                    payload={"target_role": "release-manager", "reason": "QA passed."},
                    terminal=True,
                ),
            ]
        ),
    )
    qa.run_assignment(
        RoleAssignment(
            role_id="qa-engineer",
            role_instance_id="qa-1",
            work_item_id="work-1",
            title="Compact status table",
            summary="Make status dashboard rows compact and readable.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-1",
            from_state="active",
            to_state="release_review",
            actor_role="qa-engineer",
            reason="QA passed.",
        )
    )

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

    def compose_runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ComposeCommandResult:
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
    release.deploy_compose_release(
        ReleaseEvidence(
            work_item_id="work-1",
            release_id="rel-1",
            scope="Compact status table.",
            commit_ref="abc123",
            approval_ref="approval-1",
            deployment_result="dogfood_compose passed",
            smoke_result="passed",
            rollback_plan="Revert abc123 and redeploy previous image.",
            residual_risks="None known.",
        ),
        target_id="target-compose-local",
        smoke_checks={
            "healthz": "passed",
            "status_json": "passed",
            "inbound_dm": "passed",
            "outbound_reply": "passed",
            "approval_response": "passed",
            "permission_failure": "passed",
        },
        evidence_links=(
            ReleaseEvidenceLink("work-items/work-1/020-product-definition.md", "product", "product-manager"),
            ReleaseEvidenceLink("work-items/work-1/040-architecture.md", "architecture", "solution-architect"),
            ReleaseEvidenceLink("work-items/work-1/050-security.md", "security", "security-architect"),
            ReleaseEvidenceLink("work-items/work-1/060-prompt-contract.md", "prompt", "prompt-engineer"),
            ReleaseEvidenceLink("work-items/work-1/100-implementation-log.md", "engineering", "engineering"),
            ReleaseEvidenceLink("work-items/work-1/110-quality-evidence.md", "qa", "qa-engineer"),
            ReleaseEvidenceLink("work-items/work-1/140-release-record.md", "release", "release-manager"),
        ),
    )
    release.close_released_work(
        work_item_id="work-1",
        from_state="release_review",
        actor_role="release-manager",
        reason="Sponsor approved release.",
    )

    assert db.get_work_item("work-1").state == "closed"
    event_types = [event["event_type"] for event in db.list_events()]
    assert "queue_item.created" in event_types
    assert "safe_output.recorded" in event_types
    assert "release.recorded" in event_types
    assert "deployment_run.recorded" in event_types
    assert "release_evidence.linked" in event_types
    assert event_types.count("work_item.transitioned") >= 4
