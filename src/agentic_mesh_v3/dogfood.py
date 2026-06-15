from __future__ import annotations

import sys

from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.connectors import StakeholderMessage
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.deployment import CommandDeploymentTarget
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.tools import V3ToolService


def run_local_e2e_dogfood_slice(
    *,
    db: V3Database,
    document_library: DocumentLibraryAdapter,
    project_id: str = "agentic-mesh-dev",
) -> str:
    """Run one local V3 dogfood path with role-owned tool calls.

    This is a smoke harness for the V3 operating model. It deliberately records
    each role action through V3 tools so the runtime projection and document
    library show the same evidence a live mesh would produce.
    """

    queue_item_id = "queue-v3-local-e2e"
    work_item_id = "work-v3-local-e2e"
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "project.context"])
    teams = LocalTeamsBridge(broker)
    teams.route_inbound(
        StakeholderMessage(
            connector="teams",
            message_id="msg-v3-local-e2e",
            source_type="dm",
            sender_ref="sponsor",
            conversation_ref="dm:product-manager",
            text="Please run a tiny V3 dogfood release slice.",
        )
    )

    deploy_target = CommandDeploymentTarget(
        target_id="local-smoke",
        command=(sys.executable, "-c", "print('v3 local smoke deployed')"),
        rollback_plan="Re-run the previous known-good local command target.",
    )
    tools = V3ToolService(
        db,
        document_library=document_library,
        deployment_targets={"local-smoke": deploy_target},
    )

    product_governance = GovernanceContext.from_assignment(
        work_item_id=work_item_id,
        assignment=DEFAULT_SDLC_RACI.for_phase("requirements"),
        sponsor_decision_points=("product-signoff",),
        required_evidence=("work-items/work-v3-local-e2e/index.md",),
    )
    tools.call(
        role_instance_id=f"{project_id}.product-manager.1",
        tool_name="backlog.upsert",
        payload={
            "queue_item_id": queue_item_id,
            "title": "V3 local dogfood release",
            "summary": "Tiny V3 slice proving Teams intake, role tool calls, documentation, QA, deployment, release, and closure.",
            "status": "promoted",
            "owner_role": "product-manager",
            "linked_work_item_id": work_item_id,
            "source_ref": "teams:msg-v3-local-e2e",
        },
    )
    tools.call(
        role_instance_id=f"{project_id}.product-manager.1",
        tool_name="work_item.upsert",
        payload={
            "work_item_id": work_item_id,
            "queue_item_id": queue_item_id,
            "title": "V3 local dogfood release",
            "description": "Prove one local V3 end-to-end slice from intake to closure.",
            "state": "shaping",
            "owner_role": "product-manager",
            "current_phase": "requirements",
            "next_action": "Sponsor product sign-off.",
            "governance": product_governance.handoff_requirements(),
        },
    )
    tools.call(
        role_instance_id=f"{project_id}.product-manager.1",
        tool_name="approval.request",
        payload={
            "approval_id": "approval-v3-local-product",
            "work_item_id": work_item_id,
            "question": "Approve the tiny V3 local dogfood release scope?",
        },
    )
    _write_index(
        tools,
        role_instance_id=f"{project_id}.product-manager.1",
        work_item_id=work_item_id,
        status="shaping",
        owner_role="product-manager",
        governance_state="Product Manager requested sponsor sign-off.",
        next_action="Sponsor approval before implementation.",
    )

    tools.call(
        role_instance_id=f"{project_id}.engineering.1",
        tool_name="work_item.update_state",
        payload={
            "work_item_id": work_item_id,
            "state": "active",
            "owner_role": "engineering",
            "current_phase": "development",
            "next_action": "Engineering local smoke implementation complete; QA review required.",
        },
    )
    _write_index(
        tools,
        role_instance_id=f"{project_id}.engineering.1",
        work_item_id=work_item_id,
        status="active",
        owner_role="engineering",
        governance_state="Engineering implemented the local smoke path and consulted QA.",
        next_action="QA review.",
        decisions=("Engineering used the local command deployment target for smoke proof.",),
    )

    tools.call(
        role_instance_id=f"{project_id}.qa-engineer.1",
        tool_name="work_item.update_state",
        payload={
            "work_item_id": work_item_id,
            "state": "release_review",
            "owner_role": "release-manager",
            "current_phase": "deployment",
            "next_action": "Release Manager deploy local smoke target and close.",
        },
    )
    _write_index(
        tools,
        role_instance_id=f"{project_id}.qa-engineer.1",
        work_item_id=work_item_id,
        status="release_review",
        owner_role="release-manager",
        governance_state="QA accepted local smoke evidence and handed to Release Manager.",
        next_action="Deploy and close.",
        decisions=("QA accepted local smoke path for V3 dogfood harness.",),
    )

    tools.call(
        role_instance_id=f"{project_id}.release-manager.1",
        tool_name="release.deploy",
        payload={
            "release_id": "release-v3-local-e2e",
            "work_item_id": work_item_id,
            "target_id": "local-smoke",
            "scope": "Local V3 dogfood release smoke.",
            "residual_risks": "Live Teams, OneDrive, and NATS credentials still require environment-specific validation.",
        },
    )
    tools.call(
        role_instance_id=f"{project_id}.release-manager.1",
        tool_name="work_item.update_state",
        payload={
            "work_item_id": work_item_id,
            "state": "closed",
            "owner_role": "project-manager",
            "current_phase": "project-closure",
            "next_action": "Closed after local deployment smoke.",
        },
    )
    tools.call(
        role_instance_id=f"{project_id}.project-manager.1",
        tool_name="backlog.upsert",
        payload={
            "queue_item_id": queue_item_id,
            "title": "V3 local dogfood release",
            "summary": "Tiny V3 slice proving Teams intake, role tool calls, documentation, QA, deployment, release, and closure.",
            "status": "closed",
            "owner_role": "project-manager",
            "linked_work_item_id": work_item_id,
            "source_ref": "teams:msg-v3-local-e2e",
        },
    )
    _write_index(
        tools,
        role_instance_id=f"{project_id}.project-manager.1",
        work_item_id=work_item_id,
        status="closed",
        owner_role="project-manager",
        governance_state="Project Manager closed after release evidence and residual risks were recorded.",
        next_action="Review live-adapter follow-up slices.",
        decisions=("Release Manager deployed local-smoke target.", "Project Manager closed the local dogfood slice."),
        risks=("Live credentials and external connector validation remain environment-specific.",),
    )
    tools.call(
        role_instance_id=f"{project_id}.project-manager.1",
        tool_name="document.write_root_work_item_index",
        payload={},
    )
    return work_item_id


def _write_index(
    tools: V3ToolService,
    *,
    role_instance_id: str,
    work_item_id: str,
    status: str,
    owner_role: str,
    governance_state: str,
    next_action: str,
    decisions: tuple[str, ...] = (),
    risks: tuple[str, ...] = (),
) -> None:
    tools.call(
        role_instance_id=role_instance_id,
        tool_name="document.write_work_item_index",
        payload={
            "work_item_id": work_item_id,
            "title": "V3 local dogfood release",
            "status": status,
            "owner_role": owner_role,
            "raci_summary": "See sdlc-v3 RACI; role actions recorded through V3 tools.",
            "governance_state": governance_state,
            "next_action": next_action,
            "decisions": list(decisions),
            "risks": list(risks),
        },
    )
