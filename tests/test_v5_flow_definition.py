from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path

import pytest

from agentic_mesh_v5.flow_definition import FlowDefinitionError
from agentic_mesh_v5.flow_definition import load_flow
from agentic_mesh_v5.flow_definition import validate_flow
from agentic_mesh_v5.package_resolver import resolve_packages


CONFIG = Path(
    os.environ.get(
        "AGENTIC_MESH_CONFIG_REPOSITORY",
        Path(__file__).resolve().parents[2] / "agentic-mesh-config",
    )
)


def _flow_value():
    return {
        "schema_version": 1,
        "flow_id": "sdlc",
        "leader_role": "project-manager",
        "entry_state": "business_analysis",
        "terminal_states": ["release_review"],
        "states": {
            "business_analysis": {
                "owner_role": "business-analyst",
                "purpose": "Clarify the need.",
                "artifact": "projects/{project_id}/work/{work_item_id}/010-analysis.md",
                "consults": [],
                "gates": [],
                "routes": [
                    {
                        "id": "analysis-completed",
                        "outcome": "completed",
                        "target_state": "experience_design",
                        "target_role": "ux-designer",
                    }
                ],
                "terminal": False,
            },
            "experience_design": {
                "owner_role": "ux-designer",
                "purpose": "Design the experience.",
                "artifact": "projects/{project_id}/work/{work_item_id}/020-experience.md",
                "consults": [],
                "gates": [],
                "routes": [
                    {
                        "id": "experience-no-material",
                        "outcome": "completed",
                        "target_state": "solution_design",
                        "target_role": "solution-architect",
                        "when": {"architecture_impact": ["no-material"]},
                    },
                    {
                        "id": "experience-material",
                        "outcome": "completed",
                        "target_state": "enterprise_alignment",
                        "target_role": "enterprise-architect",
                        "when": {"architecture_impact": ["material"]},
                    },
                ],
                "terminal": False,
            },
            "enterprise_alignment": {
                "owner_role": "enterprise-architect",
                "purpose": "Align the material change.",
                "artifact": "projects/{project_id}/work/{work_item_id}/030-alignment.md",
                "consults": [],
                "gates": [],
                "routes": [
                    {
                        "id": "alignment-completed",
                        "outcome": "completed",
                        "target_state": "solution_design",
                        "target_role": "solution-architect",
                    }
                ],
                "terminal": False,
            },
            "solution_design": {
                "owner_role": "solution-architect",
                "purpose": "Define the solution.",
                "artifact": "projects/{project_id}/work/{work_item_id}/050-solution-design.md",
                "consults": [
                    {
                        "id": "enterprise_architecture_conformance_review",
                        "role": "enterprise-architect",
                        "when": {"architecture_impact": ["material"]},
                    }
                ],
                "gates": [],
                "routes": [
                    {
                        "id": "solution-completed",
                        "outcome": "completed",
                        "target_state": "release_review",
                        "target_role": "release-manager",
                    }
                ],
                "terminal": False,
            },
            "release_review": {
                "owner_role": "release-manager",
                "purpose": "Close the delivery.",
                "artifact": "projects/{project_id}/work/{work_item_id}/090-release.md",
                "consults": [],
                "gates": [],
                "routes": [],
                "terminal": True,
            },
        },
    }


@pytest.fixture
def flow():
    return validate_flow(_flow_value(), digest="a" * 64)


def test_sdlc_definition_loads_with_accountable_states(flow):
    assert flow.flow_id == "sdlc"
    assert flow.entry_state == "business_analysis"
    assert len(flow.states) == 5
    assert all(state.owner_role for state in flow.states.values())
    assert flow.state("release_review").terminal is True
    assert flow.state("release_review").routes == ()


def test_conditional_architecture_route_is_exact(flow):
    state = flow.state("experience_design")
    assert state.select_route("completed", {"architecture_impact": "no-material"}).target_state == (
        "solution_design"
    )
    assert state.select_route("completed", {"architecture_impact": "material"}).target_state == (
        "enterprise_alignment"
    )
    with pytest.raises(FlowDefinitionError, match="exactly one route"):
        state.select_route("completed", {})


@pytest.mark.parametrize("mutation", ["unknown", "dead_end", "owner", "unreachable"])
def test_invalid_graphs_are_rejected(flow, mutation):
    value = deepcopy(flow.snapshot)
    if mutation == "unknown":
        value["states"]["business_analysis"]["routes"][0]["target_state"] = "missing"
    elif mutation == "dead_end":
        value["states"]["business_analysis"]["routes"] = []
    elif mutation == "owner":
        value["states"]["business_analysis"]["routes"][0]["target_role"] = "engineering"
    else:
        value["states"]["business_analysis"]["routes"][0]["target_state"] = (
            "release_review"
        )
        value["states"]["business_analysis"]["routes"][0]["target_role"] = "release-manager"
    with pytest.raises(FlowDefinitionError):
        validate_flow(value, digest=flow.digest)


def test_ambiguous_routes_fail_activation(flow):
    value = deepcopy(flow.snapshot)
    route = deepcopy(value["states"]["business_analysis"]["routes"][0])
    route["id"] = "duplicate-match"
    value["states"]["business_analysis"]["routes"].append(route)
    with pytest.raises(FlowDefinitionError, match="overlapping routes"):
        validate_flow(value, digest="a" * 64)


def test_state_obligations_are_conditionally_materialized(flow):
    state = flow.state("solution_design")
    no_material = state.obligations({"architecture_impact": "no-material"})
    material = state.obligations({"architecture_impact": "material"})
    assert no_material[0].payload["path"].endswith("050-solution-design.md")
    assert not [item for item in no_material if item.action_id == "enterprise_architecture_conformance_review"]
    assert [item for item in material if item.action_id == "enterprise_architecture_conformance_review"]


def test_released_external_sdlc_package_loads_when_available():
    package = CONFIG / "packages" / "flow" / "sdlc" / "0.1.0" / "package.json"
    if not package.exists():
        pytest.skip("external configuration repository is not available")
    flow = load_flow(resolve_packages(CONFIG, ["flow/sdlc@0.1.0"]))
    assert len(flow.states) == 14
    assert flow.state("release_review").terminal is True
