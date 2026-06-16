from pathlib import Path

from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import GovernanceInstructionSet
from agentic_mesh_v3.governance import RaciAssignment
from agentic_mesh_v3.governance import RaciMatrix
from agentic_mesh_v3.governance import evaluate_governance_checklist
from agentic_mesh_v3.governance import load_raci_matrix_from_flow


def test_default_raci_has_one_accountable_per_phase() -> None:
    DEFAULT_SDLC_RACI.validate()
    phases = {assignment.phase for assignment in DEFAULT_SDLC_RACI.assignments}
    assert "requirements" in phases
    assert "deployment" in phases
    development = DEFAULT_SDLC_RACI.for_phase("development")
    assert development.accountable == "engineering"
    assert "prompt-engineer" not in development.consulted


def test_raci_rejects_duplicate_phase() -> None:
    matrix = RaciMatrix(
        assignments=(
            RaciAssignment(phase="requirements", accountable="project-manager", responsible=("business-analyst",)),
            RaciAssignment(phase="requirements", accountable="product-manager", responsible=("business-analyst",)),
        )
    )

    try:
        matrix.validate()
    except ValueError as exc:
        assert "duplicate RACI phase" in str(exc)
    else:
        raise AssertionError("duplicate phase should fail validation")


def test_load_raci_matrix_from_flow_reads_configured_flow() -> None:
    matrix = load_raci_matrix_from_flow(Path("config/flows/sdlc-v3.yaml"))

    matrix.validate()
    deployment = matrix.for_phase("deployment")
    assert deployment.accountable == "release-manager"
    assert deployment.responsible == ("platform-engineer", "engineering")
    assert "qa-engineer" in deployment.consulted
    assert "project-manager" in deployment.informed


def test_governance_context_exports_handoff_requirements() -> None:
    assignment = DEFAULT_SDLC_RACI.for_phase("requirements")
    context = GovernanceContext.from_assignment(
        work_item_id="work-123",
        assignment=assignment,
        sponsor_decision_points=("scope-signoff",),
        required_evidence=("020-requirements.md",),
    )

    requirements = context.handoff_requirements()

    assert requirements["work_item_id"] == "work-123"
    assert requirements["accountable_role"] == "project-manager"
    assert "product-manager" in requirements["consulted_roles"]
    assert "020-requirements.md" in requirements["required_evidence"]


def test_governance_prompt_rules_are_explicit_about_consultation() -> None:
    prompt_section = GovernanceInstructionSet().as_prompt_section()

    assert "<governance-instructions>" in prompt_section
    assert "Consult every role marked C" in prompt_section
    assert "`consult.request`" in prompt_section
    assert "`informed.update`" in prompt_section
    assert "`stakeholder.ask_question`" in prompt_section
    assert "`governance.record_exception`" in prompt_section
    assert "`document.write_work_item_index`" in prompt_section
    assert "Consult Prompt Engineer with `consult.request` when a slice changes prompt components" in prompt_section
    assert "Ask the sponsor" in prompt_section
    assert "Write governance evidence" in prompt_section


def test_governance_checklist_flags_missing_required_evidence() -> None:
    context = GovernanceContext(
        work_item_id="work-123",
        phase="development",
        accountable_role="engineering",
        responsible_roles=("engineering",),
        consulted_roles=("product-manager", "qa-engineer"),
        informed_roles=("project-manager",),
        sponsor_decision_points=("product-signoff",),
    )

    checklist = evaluate_governance_checklist(context)

    assert checklist.missing_consultations == ("product-manager", "qa-engineer")
    assert checklist.missing_informed_updates == ("project-manager",)
    assert checklist.pending_sponsor_decisions == ("product-signoff",)
    assert checklist.is_satisfied is False
    assert "Missing consultation evidence for `product-manager`" in checklist.as_prompt_section()


def test_governance_checklist_uses_recorded_tool_evidence() -> None:
    context = GovernanceContext(
        work_item_id="work-123",
        phase="deployment",
        accountable_role="release-manager",
        responsible_roles=("platform-engineer",),
        consulted_roles=("qa-engineer",),
        informed_roles=("project-manager",),
        sponsor_decision_points=("release-approval",),
    )

    checklist = evaluate_governance_checklist(
        context,
        governance_records=(
            {
                "record_type": "consult.request",
                "target_ref": "qa-engineer",
                "status": "requested",
            },
            {
                "record_type": "informed.update",
                "target_ref": "project-manager",
                "status": "sent",
            },
        ),
        approvals=(
            {
                "approval_id": "approval-1",
                "question": "release-approval for work-123",
                "status": "approved",
            },
        ),
    )

    assert checklist.is_satisfied is True
    assert "currently satisfied" in checklist.as_prompt_section()


def test_governance_checklist_respects_recorded_exceptions() -> None:
    context = GovernanceContext(
        work_item_id="work-123",
        phase="system-design",
        accountable_role="solution-architect",
        responsible_roles=("solution-architect",),
        consulted_roles=("security-architect",),
        informed_roles=("stakeholders",),
    )

    checklist = evaluate_governance_checklist(
        context,
        governance_records=(
            {
                "record_type": "governance.record_exception",
                "target_ref": "security-architect",
                "summary": "Security consultation deferred because scope is documentation-only.",
                "status": "exception_recorded",
            },
        ),
    )

    assert checklist.missing_consultations == ()
    assert checklist.missing_informed_updates == ("stakeholders",)
    assert checklist.recorded_exceptions == (
        "Security consultation deferred because scope is documentation-only.",
    )
