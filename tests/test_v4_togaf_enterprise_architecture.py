from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from agentic_mesh_v4.agent_config import materialize_agent_configs
from agentic_mesh_v4.cli import main
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.enterprise_architecture import materialize_enterprise_architecture_portfolio
from agentic_mesh_v4.flow import FlowConditionError
from agentic_mesh_v4.flow import eligible_routes
from agentic_mesh_v4.flow import render_flow_mermaid
from agentic_mesh_v4.flow import validate_flow_conditions
from agentic_mesh_v4.reporting import render_work_item
from v4_postgres import make_v4_db_url


PROJECT_CONFIG = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")
ROLE_SCHEMA = Path("config/schemas/role-template.schema.json")
PROJECT_SCHEMA = Path("config/schemas/project.schema.json")
ARCHITECTURE_SCHEMA = Path("config/schemas/architecture-impact.schema.json")


def test_all_role_templates_validate_after_togaf_upgrade() -> None:
    schema = json.loads(ROLE_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    for path in sorted(Path("config/roles").glob("*.yaml")):
        role = yaml.safe_load(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(role), key=lambda error: list(error.path))
        assert not errors, f"{path}: {errors}"


def test_architecture_impact_schema_requires_material_domains_and_exception_reference() -> None:
    schema = json.loads(ARCHITECTURE_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    valid = {
        "architecture_impact": "material",
        "architecture_impact_rationale": "Changes shared identity and integration boundaries.",
        "architecture_domains": ["application", "security"],
        "architecture_conformance": "exception",
        "architecture_conformance_rationale": "Sponsor accepted the time-bound exception.",
        "architecture_conformance_decision_ref": "decision-1",
    }

    assert not list(validator.iter_errors(valid))
    invalid = dict(valid, architecture_domains=[], architecture_conformance_decision_ref=None)
    assert list(validator.iter_errors(invalid))


def test_project_schema_rejects_malformed_or_executable_flow_conditions() -> None:
    schema = json.loads(PROJECT_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema["$defs"]["flowCondition"])

    assert not list(
        validator.iter_errors({"field": "architecture_impact", "in": ["material", "uncertain"]})
    )
    assert list(validator.iter_errors({"expression": "system('unsafe')"}))
    assert list(validator.iter_errors({"field": "architecture_impact", "in": ["maybe"]}))


def test_flow_uses_deterministic_proportional_architecture_routes() -> None:
    config = load_project_config(PROJECT_CONFIG)
    validate_flow_conditions(config.flow or {})
    handoffs = config.flow["states"]["experience_design"]["handoffs"]

    none_routes = eligible_routes(handoffs, context={"architecture_impact": "none"})
    material_routes = eligible_routes(handoffs, context={"architecture_impact": "material"})
    uncertain_routes = eligible_routes(handoffs, context={"architecture_impact": "uncertain"})

    assert set(none_routes) == {"no_material_enterprise_impact"}
    assert none_routes["no_material_enterprise_impact"]["target_role"] == "solution-architect"
    assert set(material_routes) == {"enterprise_review_required"}
    assert material_routes["enterprise_review_required"]["target_role"] == "enterprise-architect"
    assert set(uncertain_routes) == {"enterprise_review_required"}
    mermaid = render_flow_mermaid(config.flow)
    assert "architecture impact in none" in mermaid
    assert "architecture impact in material, uncertain" in mermaid
    assert "enterprise architecture conformance review" in mermaid


def test_flow_rejects_arbitrary_executable_conditions() -> None:
    flow = {
        "states": {
            "product_definition": {
                "handoffs": {
                    "bad": {
                        "target_role": "enterprise-architect",
                        "target_state": "enterprise_alignment",
                        "when": {"expression": "run_user_code()"},
                    }
                }
            }
        }
    }

    with pytest.raises(FlowConditionError, match="exactly 'field' and 'in'"):
        validate_flow_conditions(flow)


def test_enterprise_architect_prompt_contains_charter_portfolio_flow_and_project_context(tmp_path: Path) -> None:
    config = load_project_config(PROJECT_CONFIG)
    materialize_agent_configs(
        project_config=config,
        output_root=tmp_path / "agents",
        role_templates_dir=Path("config/roles"),
    )

    prompt = (tmp_path / "agents" / "enterprise-architect" / "1" / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert "### Core Workflows" in prompt
    assert "maintain-enterprise-architecture-portfolio" in prompt
    assert "### Standards References" in prompt
    assert "Government Digital and Data Enterprise Architect" in prompt
    assert "Keep the Agentic Mesh enterprise architecture portfolio aligned" in prompt
    assert "`020-architecture/enterprise/040-target-operating-model.md`: accountable owner" in prompt
    assert "`enterprise_alignment`: Accountable/Responsible owner" in prompt
    assert "Gate reviewer for `enterprise_architecture_conformance_review`" in prompt


def test_related_role_prompts_include_architecture_contribution_obligations(tmp_path: Path) -> None:
    config = load_project_config(PROJECT_CONFIG)
    materialize_agent_configs(
        project_config=config,
        output_root=tmp_path / "agents",
        role_templates_dir=Path("config/roles"),
    )

    product = (tmp_path / "agents" / "product-manager" / "1" / "AGENTS.md").read_text(encoding="utf-8")
    solution = (tmp_path / "agents" / "solution-architect" / "1" / "AGENTS.md").read_text(encoding="utf-8")
    release = (tmp_path / "agents" / "release-manager" / "1" / "AGENTS.md").read_text(encoding="utf-8")
    assert "architecture-impact" in product
    assert "architecture impact" in product.casefold()
    assert "enterprise constraints" in solution.casefold()
    assert "architecture conformance" in release.casefold()


def test_enterprise_architecture_portfolio_is_numbered_indexed_and_preserves_content(tmp_path: Path) -> None:
    config = load_project_config(PROJECT_CONFIG)
    result = materialize_enterprise_architecture_portfolio(
        project_config=config,
        document_root=tmp_path / "documents",
    )
    enterprise_root = tmp_path / "documents" / "020-architecture" / "enterprise"
    paths = sorted(path.name for path in enterprise_root.glob("*.md"))

    assert paths[0] == "000-index.md"
    assert paths[-1] == "160-architecture-decision-register.md"
    assert "040-target-operating-model.md" in paths
    assert len(paths) == 17
    assert "040-target-operating-model.md" in (enterprise_root / "000-index.md").read_text(encoding="utf-8")
    domain_text = (enterprise_root / "060-data-architecture.md").read_text(encoding="utf-8")
    assert "This scaffold contains no inferred architecture" in domain_text
    assert domain_text.count("Initial assessment is pending") == 1
    assert result.created

    principles = enterprise_root / "010-architecture-principles.md"
    principles.write_text("# Sponsor-owned principles\n", encoding="utf-8")
    second = materialize_enterprise_architecture_portfolio(
        project_config=config,
        document_root=tmp_path / "documents",
    )
    assert principles.read_text(encoding="utf-8") == "# Sponsor-owned principles\n"
    assert "020-architecture/enterprise/010-architecture-principles.md" in second.preserved

    manifest = json.loads(
        (tmp_path / "documents" / "000-index" / "document-library-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    target_operating_model = next(
        item for item in manifest["documents"] if item["path"].endswith("040-target-operating-model.md")
    )
    assert target_operating_model["owner_role"] == "enterprise-architect"
    assert "business-analyst" in target_operating_model["contributing_roles"]
    flow_diagram = (tmp_path / "documents" / "000-index" / "sdlc-flow.mmd").read_text(encoding="utf-8")
    assert "architecture impact in material, uncertain" in flow_diagram


def test_portfolio_cli_materializes_without_opening_runtime_database(tmp_path: Path, capsys) -> None:
    root = tmp_path / "documents"
    main(
        [
            "--project-config",
            str(PROJECT_CONFIG),
            "materialize-enterprise-architecture",
            "--document-root",
            str(root),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["created"]
    assert output["manifest_path"] == "000-index/document-library-manifest.json"
    assert (root / "020-architecture" / "enterprise" / "040-target-operating-model.md").exists()


def test_work_item_page_shows_architecture_governance() -> None:
    page = render_work_item(
        "work-1",
        [
            {
                "work_item_id": "work-1",
                "title": "Identity integration",
                "state": "enterprise_alignment",
                "owner_role": "enterprise-architect",
                "next_action": "Update target architecture.",
                "architecture_impact": "material",
                "architecture_impact_rationale": "Shared identity boundary changes.",
                "architecture_domains_json": '["application", "security"]',
                "architecture_reviewer_role": "product-manager",
                "architecture_conformance": "pending",
                "architecture_conformance_rationale": "",
            }
        ],
        [
            {
                "record_type": "impact",
                "status": "material",
                "actor_role": "product-manager",
                "rationale": "Shared identity boundary changes.",
                "decision_ref": "",
                "created_at": "2026-07-10T10:00:00Z",
            }
        ],
    )

    assert "Architecture impact" in page
    assert "material" in page
    assert "application, security" in page
    assert "Architecture Governance History" in page


def test_postgres_architecture_guards_proportional_flow_and_release(tmp_path: Path) -> None:
    db = V4Database(make_v4_db_url())
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-material",
            title="Identity integration",
            state="product_definition",
            owner_role="product-manager",
            next_action="Assess architecture impact.",
        )
        with pytest.raises(ValueError, match="architecture impact"):
            db.upsert_work_item(
                work_item_id="work-material",
                state="experience_design",
                owner_role="ux-designer",
            )

        with pytest.raises(ValueError, match="unsupported architecture domains"):
            db.record_architecture_impact(
                work_item_id="work-material",
                classification="material",
                rationale="Invalid domain must not enter the durable record.",
                affected_domains=["arbitrary-executable-domain"],
                actor_role="product-manager",
            )

        db.record_architecture_impact(
            work_item_id="work-material",
            classification="material",
            rationale="Changes the shared identity boundary.",
            affected_domains=["application", "security"],
            actor_role="product-manager",
        )
        db.upsert_work_item(
            work_item_id="work-material",
            state="experience_design",
            owner_role="ux-designer",
        )
        with pytest.raises(ValueError, match="enterprise_alignment"):
            db.upsert_work_item(
                work_item_id="work-material",
                state="solution_design",
                owner_role="solution-architect",
            )

        db.upsert_work_item(
            work_item_id="work-material",
            state="enterprise_alignment",
            owner_role="enterprise-architect",
        )
        db.upsert_work_item(
            work_item_id="work-material",
            state="solution_design",
            owner_role="solution-architect",
        )
        with pytest.raises(ValueError, match="approved conformance"):
            db.upsert_work_item(
                work_item_id="work-material",
                state="implementation_planning",
                owner_role="engineering",
            )

        db.record_architecture_conformance(
            work_item_id="work-material",
            status="approved",
            rationale="Solution conforms to the enterprise identity requirements.",
            actor_role="enterprise-architect",
        )
        db.upsert_work_item(
            work_item_id="work-material",
            state="implementation_planning",
            owner_role="engineering",
        )
        item = db.connection.execute(
            "SELECT * FROM work_items WHERE work_item_id=?",
            ("work-material",),
        ).fetchone()
        assert item["architecture_impact"] == "material"
        assert item["architecture_conformance"] == "approved"
    finally:
        db.close()
