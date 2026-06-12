from __future__ import annotations

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.release import ReleaseEvidence
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


def run_demo_slice(db: V2Database) -> str:
    """Create one complete v2 slice with auditable queue, role, QA, and release records."""
    queue_id = "queue-v2-demo-slice"
    work_id = "work-v2-demo-slice"
    if _work_exists(db, work_id):
        return work_id

    db.create_queue_item(
        queue_item_id=queue_id,
        title="V2 runtime smoke slice",
        summary=(
            "Prove the v2 runtime can carry one small slice from queue capture "
            "through product shaping, implementation, QA, release evidence, and closure."
        ),
        owner_role="product-manager",
        source_kind="operator",
        source_ref="v2-cutover-smoke",
    )
    db.mark_queue_ready(queue_id, actor_role="product-manager", reason="Operator requested v2 cut-over smoke.")
    db.promote_queue_item(queue_item_id=queue_id, work_item_id=work_id, owner_role="product-manager")

    _run_role(
        db,
        role_id="product-manager",
        instance_id="agentic-mesh-dev.product-manager.1",
        work_id=work_id,
        calls=[
            SafeOutputCall(
                role_id="product-manager",
                tool_name="document.propose_update",
                payload={
                    "path": f"work-items/{work_id}/020-product-definition.md",
                    "document_type": "product_definition",
                    "content": "V2 smoke product definition with acceptance criteria and sponsor-visible release goal.",
                },
            ),
            SafeOutputCall(
                role_id="product-manager",
                tool_name="handoff.request",
                payload={"target_role": "engineering", "reason": "Product definition is shaped for smoke build."},
                terminal=True,
            ),
        ],
    )
    db.add_artifact(
        artifact_id="artifact-v2-demo-product",
        work_item_id=work_id,
        path=f"work-items/{work_id}/020-product-definition.md",
        document_type="product_definition",
        status="accepted",
        created_by_role="product-manager",
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id=work_id,
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Product shaping complete.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id=work_id,
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering claimed the smoke slice.",
        )
    )

    _run_role(
        db,
        role_id="engineering",
        instance_id="agentic-mesh-dev.engineering.1",
        work_id=work_id,
        calls=[
            SafeOutputCall(
                role_id="engineering",
                tool_name="implementation.record_change",
                payload={"work_item_id": work_id, "summary": "Implemented v2 runtime server and dashboard path."},
            ),
            SafeOutputCall(
                role_id="engineering",
                tool_name="handoff.request",
                payload={"target_role": "qa-engineer", "reason": "Implementation evidence is ready for QA."},
                terminal=True,
            ),
        ],
    )
    db.add_artifact(
        artifact_id="artifact-v2-demo-implementation",
        work_item_id=work_id,
        path=f"work-items/{work_id}/100-implementation-log.md",
        document_type="implementation_evidence",
        status="verified",
        created_by_role="engineering",
    )

    _run_role(
        db,
        role_id="qa-engineer",
        instance_id="agentic-mesh-dev.qa-engineer.1",
        work_id=work_id,
        calls=[
            SafeOutputCall(
                role_id="qa-engineer",
                tool_name="test_evidence.record",
                payload={"work_item_id": work_id, "summary": "V2 unit, import, and HTTP smoke checks passed."},
            ),
            SafeOutputCall(
                role_id="qa-engineer",
                tool_name="handoff.request",
                payload={"target_role": "release-manager", "reason": "QA evidence is accepted."},
                terminal=True,
            ),
        ],
    )
    db.add_artifact(
        artifact_id="artifact-v2-demo-quality",
        work_item_id=work_id,
        path=f"work-items/{work_id}/110-quality-evidence.md",
        document_type="quality_evidence",
        status="passed",
        created_by_role="qa-engineer",
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id=work_id,
            from_state="active",
            to_state="release_review",
            actor_role="qa-engineer",
            reason="QA passed the smoke slice.",
        )
    )

    ReleaseService(db).record_no_deployment(
        ReleaseEvidence(
            work_item_id=work_id,
            release_id="release-v2-demo-slice",
            scope="V2 runtime cut-over smoke deployment.",
            commit_ref="runtime-image:agentic-mesh:local",
            approval_ref="operator-cutover",
            rollback_plan="Switch compose back to the previous branch and rebuild agentic-mesh:local.",
            residual_risks="This is the v2 spine MVP, not the final long-running worker implementation.",
        ),
        reason="CLI demo creates an auditable closed slice without executing a deployment target.",
    )
    db.add_artifact(
        artifact_id="artifact-v2-demo-release",
        work_item_id=work_id,
        path=f"work-items/{work_id}/140-release-record.md",
        document_type="release_record",
        status="released",
        created_by_role="release-manager",
    )
    ReleaseService(db).close_released_work(
        work_item_id=work_id,
        from_state="release_review",
        actor_role="release-manager",
        reason="Operator approved v2 cut-over test release.",
    )
    return work_id


def _run_role(
    db: V2Database,
    *,
    role_id: str,
    instance_id: str,
    work_id: str,
    calls: list[SafeOutputCall],
) -> None:
    service = RoleService(
        db=db,
        role_id=role_id,
        role_instance_id=instance_id,
        worker=StaticWorker(calls),
    )
    service.run_assignment(
        RoleAssignment(
            role_id=role_id,
            role_instance_id=instance_id,
            work_item_id=work_id,
            title="V2 runtime smoke slice",
            summary="Exercise the v2 runtime state machine and evidence trail.",
        )
    )


def _work_exists(db: V2Database, work_id: str) -> bool:
    row = db.connection.execute(
        "SELECT 1 FROM work_items WHERE work_item_id = ?",
        (work_id,),
    ).fetchone()
    return row is not None
