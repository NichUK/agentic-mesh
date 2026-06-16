from __future__ import annotations

from datetime import datetime
from datetime import timezone

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.documents import WorkItemIndex
from agentic_mesh_v3.documents import write_root_work_item_index
from agentic_mesh_v3.documents import write_work_item_index
from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.reporting import AgentStatus


def run_demo_slice(
    db: V3Database,
    *,
    project_id: str = "agentic-mesh-dev",
    document_library: DocumentLibraryAdapter | None = None,
) -> None:
    """Create one completed V3 dogfood-style slice in the read model."""

    queue_item_id = "queue-v3-demo-slice"
    work_item_id = "work-v3-demo-slice"
    governance = GovernanceContext.from_assignment(
        work_item_id=work_item_id,
        assignment=DEFAULT_SDLC_RACI.for_phase("deployment"),
        sponsor_decision_points=("release-approval",),
        required_evidence=("work-items/work-v3-demo-slice/index.md", "release-record"),
    )
    db.upsert_backlog_item(
        queue_item_id=queue_item_id,
        title="V3 runtime smoke slice",
        summary="Prove the V3 read model can show queue, work, agents, artifacts, release, and closure.",
        status="closed",
        owner_role="project-manager",
        linked_work_item_id=work_item_id,
        source_ref="demo",
    )
    db.upsert_work_item(
        work_item_id=work_item_id,
        queue_item_id=queue_item_id,
        title="V3 runtime smoke slice",
        description="One local proof that V3 reporting and governance projections can close a slice.",
        state="closed",
        owner_role="project-manager",
        current_phase="project-closure",
        next_action="Closed after local smoke evidence.",
        governance=governance.handoff_requirements(),
    )
    now = datetime.now(timezone.utc).isoformat()
    for role in ("project-manager", "product-manager", "engineering", "qa-engineer", "release-manager"):
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id=f"{project_id}.{role}.1",
                container_state="running",
                heartbeat_at=now,
                current_work=None,
                inbox_depth=0,
            )
        )
    db.add_artifact(
        artifact_id="artifact-v3-demo-index",
        work_item_id=work_item_id,
        filename="index.md",
        title="Work item index",
        relative_path="work-items/work-v3-demo-slice/index.md",
        document_type="work_item_index",
        status="published",
        created_by_role="project-manager",
    )
    db.record_release(
        release_id="release-v3-demo-slice",
        work_item_id=work_item_id,
        status="released",
        scope="Local V3 smoke read model only.",
        deployment_result="No deployment required for read-model demo seed.",
        rollback_plan="Delete demo seed rows from the local V3 database.",
        residual_risks="Real connector and deployment adapters remain follow-up slices.",
        version_ref="demo-seed:no-code-change",
        approval_ref="demo-seed:local",
        smoke_evidence="Read-model demo seed renders status and work-item pages.",
        closure_state="closed",
    )
    if document_library is not None:
        index = WorkItemIndex(
            work_item_id=work_item_id,
            title="V3 runtime smoke slice",
            status="closed",
            owner_role="project-manager",
            raci_summary="project-manager A/R, delivery-manager R, release-manager C",
            governance_state="Demo closure recorded with release evidence.",
            decisions=("V3 local smoke can use a no-deployment disposition.",),
            risks=("Real connector and deployment adapters remain follow-up slices.",),
            next_action="Continue with real Teams, OneDrive, NATS, and deployment adapters.",
        )
        write_work_item_index(document_library, index)
        write_root_work_item_index(document_library, [index])
