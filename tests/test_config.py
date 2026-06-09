from pathlib import Path
import json
import yaml

import pytest

from agentic_mesh.config import ConfigError
from agentic_mesh.config import _control_plane_policy_from_dict
from agentic_mesh.config import _role_template_from_dict
from agentic_mesh.config import load_mesh_config


def test_project_schema_and_flow_template_files_exist() -> None:
    readme = (Path.cwd() / "README.md").read_text(encoding="utf-8")
    project = yaml.safe_load(
        (
            Path.cwd()
            / "examples"
            / "projects"
            / "agentic-mesh-dev"
            / "agentic-mesh"
            / "project.yaml"
        ).read_text(encoding="utf-8")
    )
    schema = json.loads(
        (Path.cwd() / "config" / "schemas" / "project.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert "docs/configuration/project-configuration.md" in readme
    assert "docs/architecture/role-charters.md" in readme
    assert "Standards-Informed Roles And Flows" in readme
    assert project["flow"] == {"template": "sdlc"}
    assert project["workspace"]["default_repository"] == "agentic-mesh"
    assert schema["title"] == "Agentic Mesh Project Configuration"
    assert "workspace" in schema["required"]
    assert "authCredential" in schema["$defs"]
    assert "documentLibrary" in schema["$defs"]
    assert "roleMemory" in schema["$defs"]
    assert "projectMesh" in schema["$defs"]
    assert "flowConsult" in schema["$defs"]
    assert "sponsorInitiatedWork" in schema["$defs"]
    assert "notificationPolicy" in schema["$defs"]
    assert "controlPlanePolicy" in schema["$defs"]
    assert "projectGateway" in schema["$defs"]
    assert "gateways" in schema["properties"]
    control_plane_schema = schema["$defs"]["controlPlanePolicy"]["properties"]
    assert control_plane_schema["binding_mode"]["default"] == "local_private"
    assert control_plane_schema["api_enabled"]["default"] is False
    assert control_plane_schema["mcp_enabled"]["default"] is False
    reasoning_effort_schema = schema["$defs"]["worker"]["properties"]["reasoning_effort"]
    assert reasoning_effort_schema["default"] == "medium"
    assert "high" in reasoning_effort_schema["enum"]
    sandbox_mode_schema = schema["$defs"]["worker"]["properties"]["sandbox_mode"]
    assert sandbox_mode_schema["default"] == "workspace-write"
    assert "danger-full-access" in sandbox_mode_schema["enum"]
    assert (Path.cwd() / "config" / "flows" / "sdlc.yaml").exists()
    assert (Path.cwd() / "config" / "schemas" / "response-types.schema.json").exists()
    role_schema = json.loads(
        (Path.cwd() / "config" / "schemas" / "role-template.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert role_schema["title"] == "Agentic Mesh Role Template"
    assert "coreWorkflow" in role_schema["$defs"]
    assert "standardsReference" in role_schema["$defs"]


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
    assert mesh_config.project.auth_credentials

    product_auth = mesh_config.project.roles["product-manager"].worker.auth
    engineering_auth = mesh_config.project.roles["engineering"].worker.auth
    security_auth = mesh_config.project.roles["security-architect"].worker.auth
    ux_auth = mesh_config.project.roles["ux-designer"].worker.auth
    delivery_auth = mesh_config.project.roles["delivery-manager"].worker.auth

    assert product_auth is not None
    assert product_auth.credential_ref is not None
    assert product_auth.method in {"codex_access_token", "codex_oauth_cache"}
    assert product_auth.secret_ref is not None or product_auth.mount_ref is not None
    assert engineering_auth is not None
    assert engineering_auth.credential_ref is not None
    assert engineering_auth.method in {"codex_access_token", "codex_oauth_cache"}
    assert engineering_auth.secret_ref is not None or engineering_auth.mount_ref is not None
    assert security_auth is not None
    assert security_auth.method in {"codex_access_token", "codex_oauth_cache"}
    assert security_auth.secret_ref is not None or security_auth.mount_ref is not None
    assert ux_auth is not None
    assert ux_auth.credential_ref is not None
    assert ux_auth.method in {"codex_access_token", "codex_oauth_cache"}
    assert ux_auth.secret_ref is not None or ux_auth.mount_ref is not None
    assert delivery_auth is not None
    assert delivery_auth.credential_ref is not None
    assert delivery_auth.method in {"codex_access_token", "codex_oauth_cache"}
    assert delivery_auth.secret_ref is not None or delivery_auth.mount_ref is not None


def test_auth_credentials_can_be_reused_across_roles() -> None:
    mesh_config = load_mesh_config(
        Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
    )

    product_auth = mesh_config.project.roles["product-manager"].worker.auth
    engineering_auth = mesh_config.project.roles["engineering"].worker.auth

    assert product_auth is not None
    assert engineering_auth is not None
    assert product_auth.credential_ref == "codex-example-shared-api-key"
    assert engineering_auth.credential_ref == "codex-example-shared-api-key"
    assert product_auth.method == "codex_api_key"
    assert engineering_auth.secret_ref == "codex-example-shared-api-key"


def test_worker_reasoning_effort_defaults_to_medium_when_omitted() -> None:
    mesh_config = load_mesh_config(
        Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
    )

    assert mesh_config.project.goal.description == ""
    assert mesh_config.project.goal.success_measures == []
    assert (
        mesh_config.project.roles["product-manager"].worker.reasoning_effort
        == "medium"
    )
    assert (
        mesh_config.project.roles["product-manager"].worker.sandbox_mode
        == "workspace-write"
    )
    assert (
        mesh_config.project.roles["engineering"].worker.reasoning_effort
        == "medium"
    )


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
    gateway = mesh_config.project.gateways["agentic-mesh"]
    assert gateway.no_delivery_work is True
    assert gateway.raw_retention_mode == "reference_only"
    assert gateway.default_owner_role == "delivery-manager"
    assert gateway.teams is not None
    assert gateway.teams.connector == "teams"
    assert gateway.teams.dm_enabled is True
    assert gateway.teams.intake_channels == ["all-agents"]
    assert gateway.teams.process_role_channels_by_default is False
    assert gateway.teams.bot.display_name == "AM-Agentic Mesh"
    assert gateway.teams.bot.bot_id_ref == "teams-bot-agentic-mesh-app-id"
    notification_policy = mesh_config.project.notification_policy
    assert notification_policy.schema_version == "notification-policy-v0"
    assert notification_policy.defaults["routine_lifecycle_events"] == "dashboard_only"
    assert notification_policy.surfaces["intake"].route == "all-agents"
    assert notification_policy.surfaces["approvals"].route == "approvals"
    assert notification_policy.surfaces["status_fallback"].label == "project status fallback"
    assert (
        notification_policy.event_overrides["lifecycle.handoff_requested"].visibility
        == "dashboard_only"
    )
    control_plane = mesh_config.project.control_plane
    assert control_plane.schema_version == "control-plane-policy-v0"
    assert control_plane.binding_mode == "local_private"
    assert control_plane.api_enabled is False
    assert control_plane.mcp_enabled is False
    assert control_plane.remote_promotion_enabled is False
    assert control_plane.support_read_enabled is False


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("api_enabled", "false"),
        ("api_enabled", "no"),
        ("api_enabled", 0),
        ("api_enabled", None),
        ("mcp_enabled", "false"),
        ("mcp_enabled", "no"),
        ("mcp_enabled", 0),
        ("mcp_enabled", None),
        ("remote_promotion_enabled", "false"),
        ("remote_promotion_enabled", "no"),
        ("remote_promotion_enabled", 0),
        ("remote_promotion_enabled", None),
        ("support_read_enabled", "false"),
        ("support_read_enabled", "no"),
        ("support_read_enabled", 0),
        ("support_read_enabled", None),
    ],
)
def test_control_plane_policy_rejects_non_boolean_flags(
    field_name: str,
    bad_value: object,
) -> None:
    with pytest.raises(ConfigError, match=f"control_plane.{field_name} must be a boolean"):
        _control_plane_policy_from_dict(
            {
                "control_plane": {
                    "binding_mode": "remote_enabled",
                    field_name: bad_value,
                }
            }
        )


def test_loads_project_roles_and_instances() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    assert mesh_config.project.workspace.root == "examples/projects/agentic-mesh-dev"
    assert mesh_config.project.workspace.default_repository == "agentic-mesh"
    assert mesh_config.project.workspace.repositories["agentic-mesh"].path == "../../.."
    assert (
        mesh_config.project.workspace.repositories["agentic-mesh"].default_branch
        == "develop"
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
    assert engineering.template.role_profile.startswith("Act as a senior engineer")
    assert "Implementation approach" in " ".join(
        engineering.template.decision_rights["owns"]
    )
    assert engineering.template.core_workflows[0]["workflow_id"] == (
        "plan-implementation"
    )
    assert any(
        reference["name"] == "NIST SSDF"
        for reference in engineering.template.standards_references
    )
    assert "Codebase patterns" in " ".join(engineering.template.memory_focus)
    assert "document-library.read" in engineering.template.default_tools
    assert "bdd.scenarios.read" in engineering.template.default_tools
    assert engineering.override.worker.adapter == "codex-cli"
    assert engineering.override.worker.reasoning_effort == "high"
    assert engineering.override.worker.sandbox_mode == "danger-full-access"
    assert engineering.telemetry_service_name == "AM.dev-team.engineering.1"
    assert mesh_config.project.document_library.backend == "git"
    assert mesh_config.project.document_library.root == "../../.."
    assert (
        mesh_config.project.document_library.index_path
        == "docs/00-index/document-library-manifest.json"
    )
    assert mesh_config.project.goal.description.startswith("Build Agentic Mesh")
    assert "pluggable connectors" in mesh_config.project.goal.description
    assert any(
        "smallest appropriate set of roles" in measure
        for measure in mesh_config.project.goal.success_measures
    )
    assert any(
        "clarifying questions" in measure
        for measure in mesh_config.project.goal.success_measures
    )
    assert any(
        "Do not create documents just to record failure" in constraint
        for constraint in mesh_config.project.goal.constraints
    )
    assert mesh_config.project.goal.guidance
    assert mesh_config.project.role_memory.enabled is True
    assert mesh_config.project.role_memory.provenance_required is True
    assert sorted(mesh_config.project.meshes) == ["governance", "sdlc"]
    assert "enterprise-architect" in mesh_config.project.meshes["sdlc"].roles
    assert "bdd.scenarios.write" in mesh_config.role_templates[
        "qa-engineer"
    ].default_tools
    assert "flow.visualize" in mesh_config.role_templates[
        "delivery-manager"
    ].default_tools


def test_all_starter_role_templates_have_expanded_charters() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    for role_id, template in mesh_config.role_templates.items():
        assert template.role_profile, role_id
        assert template.accountabilities, role_id
        assert template.decision_rights.get("owns"), role_id
        assert template.boundaries, role_id
        assert template.collaboration_style, role_id
        assert template.quality_bar, role_id
        assert template.memory_focus, role_id
        assert template.core_workflows, role_id
        assert template.standards_references, role_id
        assert template.anti_patterns, role_id


def test_minimal_legacy_role_template_defaults_expanded_fields() -> None:
    template = _role_template_from_dict(
        {
            "role_id": "legacy-role",
            "version": 1,
            "purpose": "Legacy role.",
            "standing_instructions": [],
            "default_tools": [],
            "documentation_obligations": [],
            "handoff_targets": [],
        },
        Path("legacy-role.yaml"),
    )

    assert template.role_profile == ""
    assert template.accountabilities == []
    assert template.decision_rights == {}
    assert template.core_workflows == []


def test_role_template_rejects_malformed_decision_rights() -> None:
    with pytest.raises(ConfigError, match="decision_rights.owns"):
        _role_template_from_dict(
            {
                "role_id": "bad-role",
                "version": 1,
                "purpose": "Bad role.",
                "standing_instructions": [],
                "default_tools": [],
                "documentation_obligations": [],
                "handoff_targets": [],
                "decision_rights": {"owns": "not-a-list"},
            },
            Path("bad-role.yaml"),
        )


def test_role_template_rejects_malformed_core_workflow() -> None:
    with pytest.raises(ConfigError, match="core_workflows\\[0\\]"):
        _role_template_from_dict(
            {
                "role_id": "bad-role",
                "version": 1,
                "purpose": "Bad role.",
                "standing_instructions": [],
                "default_tools": [],
                "documentation_obligations": [],
                "handoff_targets": [],
                "core_workflows": [{"workflow_id": "missing-fields"}],
            },
            Path("bad-role.yaml"),
        )


def test_loads_project_sdlc_flow_overlay() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    flow = mesh_config.project.flow
    assert flow.flow_id == "agentic-mesh-sdlc-v0"
    assert flow.entry_state == "business_analysis"
    assert flow.work_item_types == ["slice", "subslice", "feature", "spike"]
    assert flow.states["business_analysis"].owner_role == "business-analyst"
    assert (
        flow.states["business_analysis"].handoffs["completed"].target_state
        == "product_definition"
    )
    assert flow.states["solution_design"].handoffs["completed"].target_role == (
        "security-architect"
    )
    assert flow.sponsor_initiated_work is not None
    assert flow.sponsor_initiated_work.allow_from_any_state is True
    assert flow.sponsor_initiated_work.default_work_item_type == "spike"
    assert flow.sponsor_initiated_work.default_intake_state == "business_analysis"
    assert (
        flow.states["implementation_planning"].handoffs["completed"].target_state
        == "quality_planning"
    )
    assert (
        flow.states["quality_planning"].handoffs["completed"].target_state
        == "implementation"
    )
    assert (
        flow.states["quality_planning"].gates[0].type
        == "plan_review"
    )
    assert "engineering" in flow.states["quality_planning"].gates[0].affected_roles
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
    assert product_state.gates[1].gate_id == "product_definition_sponsor_signoff"
    assert product_state.gates[1].type == "human_response"
    assert product_state.gates[1].response_type == "approve_not_approve"
    assert product_state.gates[1].requested_from == "sponsor"
    assert product_state.gates[1].channel == "approvals"
    assert product_state.gates[1].completion_criteria["accepted_values"] == [
        "approved"
    ]
    assert product_state.artifact_path == (
        "work-items/{work_item_id}/20-product-definition.md"
    )
    assert product_state.gates[0].required_documents == [
        "work-items/{work_item_id}/20-product-definition.md"
    ]
    assert product_state.gates[0].required_review_status == "approved"
    assert product_state.gates[0].reviewer_role == "product-manager"

    implementation_plan_state = mesh_config.project.flow.states[
        "implementation_planning"
    ]
    assert implementation_plan_state.artifact_path == (
        "work-items/{work_item_id}/80-implementation-plan.md"
    )

    quality_plan_state = mesh_config.project.flow.states["quality_planning"]
    assert quality_plan_state.artifact_path == (
        "work-items/{work_item_id}/90-quality-plan.md"
    )

    release_state = mesh_config.project.flow.states["release_review"]
    human_gate = release_state.gates[0]
    assert human_gate.gate_id == "release_decision_response"
    assert human_gate.type == "human_response"
    assert human_gate.response_type == "approve_not_approve"
    assert human_gate.requested_from == "release-sponsor"
    assert human_gate.channel == "approvals"
    assert "rollback plan" in human_gate.prompt
    assert "no-staging disposition" in human_gate.prompt
    assert human_gate.timeout == "PT48H"
    assert human_gate.on_timeout == "escalate"
    assert human_gate.completion_criteria["accepted_values"] == ["approved"]
