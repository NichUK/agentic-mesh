from pathlib import Path
import json
import yaml

from agentic_mesh.config import load_mesh_config


def test_project_schema_and_flow_template_files_exist() -> None:
    project = yaml.safe_load(
        (Path.cwd() / "examples" / "projects" / "agentic-mesh-dev.yaml").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (Path.cwd() / "config" / "schemas" / "project.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert project["flow"] == {"template": "sdlc"}
    assert project["workspace"]["default_repository"] == "agentic-mesh"
    assert schema["title"] == "Agentic Mesh Project Configuration"
    assert "workspace" in schema["required"]
    assert "flowConsult" in schema["$defs"]
    assert "sponsorInitiatedWork" in schema["$defs"]
    assert (Path.cwd() / "config" / "flows" / "sdlc.yaml").exists()
    assert (Path.cwd() / "config" / "schemas" / "response-types.schema.json").exists()


def test_loads_organization_defaults() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    assert mesh_config.organization.organization_id == "agentic-mesh-defaults"
    assert mesh_config.organization.global_language == "en-GB"
    assert mesh_config.organization.global_locale == "en-GB"
    assert mesh_config.organization.naming_defaults.brand_prefix == "AM"
    assert mesh_config.organization.naming_defaults.resource_namespace == "agentic-mesh"
    assert "work_item_id" in mesh_config.organization.handoff_defaults["required_fields"]
    assert "tracked work item" in mesh_config.organization.work_intake_defaults[
        "sponsor_initiated_work_rule"
    ]
    assert "configured consult routes" in mesh_config.organization.work_intake_defaults[
        "consult_route_rule"
    ]
    assert (
        mesh_config.organization.security_defaults["secrets_policy"]
        .lower()
        .startswith("secret values must never be committed")
    )


def test_loads_auth_methods_and_role_bindings() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    assert "codex_access_token" in mesh_config.auth_methods
    assert "claude_code_oauth_token" in mesh_config.auth_methods
    assert "microsoft_graph_app_certificate" in mesh_config.auth_methods

    product_auth = mesh_config.project.roles["product-manager"].worker.auth
    engineering_auth = mesh_config.project.roles["engineering"].worker.auth
    security_auth = mesh_config.project.roles["security-architect"].worker.auth
    ux_auth = mesh_config.project.roles["ux-designer"].worker.auth
    delivery_auth = mesh_config.project.roles["delivery-manager"].worker.auth

    assert product_auth is not None
    assert product_auth.method == "codex_access_token"
    assert product_auth.secret_ref == "codex-agentic-mesh-dev-product-manager-token"
    assert engineering_auth is not None
    assert engineering_auth.method == "codex_api_key"
    assert security_auth is not None
    assert security_auth.method == "claude_code_oauth_token"
    assert ux_auth is not None
    assert ux_auth.method == "codex_oauth_cache"
    assert ux_auth.mount_ref == "local-codex-ux-designer-home"
    assert delivery_auth is not None
    assert delivery_auth.method == "manual_human_no_auth"
    assert delivery_auth.secret_ref is None


def test_loads_response_type_templates() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    assert sorted(mesh_config.response_types) == [
        "approve_not_approve",
        "document_or_url",
        "document_reference",
        "money",
        "multiline_text",
        "number",
        "single_line_text",
        "url",
        "yes_no",
    ]
    approval = mesh_config.response_types["approve_not_approve"]
    assert approval.input_mode == "choice"
    assert approval.options[0]["value"] == "approved"
    assert mesh_config.response_types["money"].validation["required_fields"] == [
        "amount",
        "currency",
    ]


def test_loads_teams_connector_role_bot_mapping() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    teams = mesh_config.project.connectors["teams"]
    assert teams.adapter == "teams-bot-connector"
    assert teams.identity_model == "role_bots"
    assert teams.tenant_id == "564b667c-5b1a-4bbc-bb43-918b0b765a9b"
    assert teams.team_id == "37a58a51-c42b-417b-8733-608afda205fc"
    assert teams.team_name == "dev-team"
    assert teams.ingress is not None
    assert teams.ingress.public_endpoint == "https://vpn.nixnet.com/api/messages"
    assert teams.ingress.listen_port == 3978
    assert teams.channels["all-agents"].channel_id == (
        "19:f8Jv56Jp0J1svuc4x89d4yyj3X--8SQLiCrCPYrgJTA1@thread.tacv2"
    )
    assert teams.channels["approvals"].channel_id == (
        "19:0aee6a530977440a9e063eba2153bdbf@thread.tacv2"
    )
    assert set(teams.role_bots) == set(mesh_config.project.roles)
    assert teams.role_bots["engineering"].display_name == "AM-Engineering"
    assert teams.role_bots["engineering"].bot_id_ref == "teams-bot-engineering-app-id"
    assert teams.role_bots["engineering"].secret_ref == "teams-bot-engineering-secret"


def test_loads_project_roles_and_instances() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    assert mesh_config.project.workspace.root == "."
    assert mesh_config.project.workspace.default_repository == "agentic-mesh"
    assert mesh_config.project.workspace.repositories["agentic-mesh"].path == "."
    assert (
        mesh_config.project.workspace.repositories["agentic-mesh"].default_branch
        == "main"
    )
    assert sorted(mesh_config.project.roles) == [
        "business-analyst",
        "delivery-manager",
        "engineering",
        "enterprise-architect",
        "platform-engineer",
        "product-manager",
        "qa-engineer",
        "release-manager",
        "research-analyst",
        "security-architect",
        "solution-architect",
        "technical-writer",
        "ux-designer",
    ]
    assert sorted(mesh_config.instances) == [
        "agentic-mesh-dev.business-analyst.1",
        "agentic-mesh-dev.delivery-manager.1",
        "agentic-mesh-dev.engineering.1",
        "agentic-mesh-dev.engineering.2",
        "agentic-mesh-dev.enterprise-architect.1",
        "agentic-mesh-dev.platform-engineer.1",
        "agentic-mesh-dev.product-manager.1",
        "agentic-mesh-dev.qa-engineer.1",
        "agentic-mesh-dev.release-manager.1",
        "agentic-mesh-dev.research-analyst.1",
        "agentic-mesh-dev.security-architect.1",
        "agentic-mesh-dev.solution-architect.1",
        "agentic-mesh-dev.technical-writer.1",
        "agentic-mesh-dev.ux-designer.1",
    ]
    engineering = mesh_config.instances["agentic-mesh-dev.engineering.1"]
    assert engineering.template.role_id == "engineering"
    assert engineering.override.worker.adapter == "codex-cli"
    assert engineering.telemetry_service_name == "AM.dev-team.engineering.1"


def test_loads_project_sdlc_flow_overlay() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    flow = mesh_config.project.flow
    assert flow.flow_id == "agentic-mesh-sdlc-v0"
    assert flow.entry_state == "business_analysis"
    assert flow.work_item_types == ["slice", "feature", "spike"]
    assert flow.states["business_analysis"].owner_role == "business-analyst"
    assert (
        flow.states["business_analysis"].handoffs["completed"].target_state
        == "product_definition"
    )
    assert (
        flow.states["solution_design"].handoffs["completed"].target_role
        == "security-architect"
    )
    assert flow.sponsor_initiated_work is not None
    assert flow.sponsor_initiated_work.allow_from_any_state is True
    assert flow.sponsor_initiated_work.default_work_item_type == "spike"
    assert flow.sponsor_initiated_work.default_intake_state == "business_analysis"
    assert (
        flow.states["implementation"].consults["product_scope"].target_role
        == "product-manager"
    )
    assert (
        flow.states["release_review"].consults["security_context"].target_state
        == "security_review"
    )


def test_loads_document_accountabilities_and_gates() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    stories = mesh_config.project.document_accountabilities["docs/product/stories.md"]
    assert stories.owner_role == "product-manager"
    assert stories.can_edit_contributions is True
    assert stories.review_on_contribution is True
    assert "acceptance_criteria" in stories.required_sections
    assert "ux-designer" in stories.contributing_roles

    product_state = mesh_config.project.flow.states["product_definition"]
    assert product_state.gates[0].gate_id == "product_story_owner_review"
    assert product_state.gates[0].type == "document_owner_review"
    assert product_state.gates[0].required_documents == ["docs/product/stories.md"]
    assert product_state.gates[0].required_review_status == "approved"
    assert product_state.gates[0].reviewer_role == "product-manager"

    release_state = mesh_config.project.flow.states["release_review"]
    human_gate = release_state.gates[0]
    assert human_gate.gate_id == "release_decision_response"
    assert human_gate.type == "human_response"
    assert human_gate.response_type == "approve_not_approve"
    assert human_gate.requested_from == "release-sponsor"
    assert human_gate.channel == "approvals"
    assert human_gate.timeout == "PT48H"
    assert human_gate.on_timeout == "escalate"
    assert human_gate.completion_criteria["accepted_values"] == ["approved"]
