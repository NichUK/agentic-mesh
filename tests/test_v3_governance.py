from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import GovernanceInstructionSet
from agentic_mesh_v3.governance import RaciAssignment
from agentic_mesh_v3.governance import RaciMatrix


def test_default_raci_has_one_accountable_per_phase() -> None:
    DEFAULT_SDLC_RACI.validate()
    phases = {assignment.phase for assignment in DEFAULT_SDLC_RACI.assignments}
    assert "requirements" in phases
    assert "deployment" in phases
    assert DEFAULT_SDLC_RACI.for_phase("development").accountable == "engineering"


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
    assert "Ask the sponsor" in prompt_section
    assert "Write governance evidence" in prompt_section
