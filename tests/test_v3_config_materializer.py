from pathlib import Path
import json

from agentic_mesh_v3.config_materializer import build_role_instance_config
from agentic_mesh_v3.config_materializer import materialize_agent_config
from agentic_mesh_v3.config_materializer import materialize_project_agent_configs
from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.lifecycle import RoleContainerSpec
from agentic_mesh_v3.project_config import load_project_config


def test_materialize_agent_config_writes_mounted_files(tmp_path: Path) -> None:
    spec = RoleContainerSpec(
        role_instance_id="agentic-mesh-dev.project-manager.1",
        image="agentic-mesh:local",
        source_repo=tmp_path / "source",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        agent_config_dir=tmp_path / "agent",
        runtime_state_dir=tmp_path / "state",
        document_library_root=tmp_path / "documents",
        environment={"PROJECT_ID": "agentic-mesh-dev"},
    )

    written = materialize_agent_config(
        spec=spec,
        system_instructions="System rules.",
        role_prompt="You are Project Manager.",
        organisation_instructions="Org rules.",
        project_instructions="Project rules.",
        tool_instructions="Use MCP or CLI safe-output tools for durable effects.",
        raci=DEFAULT_SDLC_RACI,
    )

    assert {path.name for path in written} == {
        "role.md",
        "system.md",
        "organisation.md",
        "project.md",
        "tools.md",
        "raci.json",
        "container.json",
    }
    assert "Project Manager" in (tmp_path / "agent" / "role.md").read_text(encoding="utf-8")
    assert "System rules." in (tmp_path / "agent" / "system.md").read_text(encoding="utf-8")
    assert "safe-output tools" in (tmp_path / "agent" / "tools.md").read_text(encoding="utf-8")
    assert "requirements" in (tmp_path / "agent" / "raci.json").read_text(encoding="utf-8")
    container = json.loads((tmp_path / "agent" / "container.json").read_text(encoding="utf-8"))
    assert "tools.md" in json.dumps(container)
    assert container["command"][0] == "agentic-mesh-v3"
    assert "run-agent-service" in container["command"]


def test_build_role_instance_config_uses_materialized_prompt_paths(tmp_path: Path) -> None:
    config = build_role_instance_config(
        project_id="agentic-mesh-dev",
        role_id="product-manager",
        instance_id="1",
        agent_config_dir=tmp_path / "agent",
        runtime_state_dir=tmp_path / "state",
        inbox_stream="agent-inbox",
    )

    assert config.role_instance_id == "agentic-mesh-dev.product-manager.1"
    assert config.role_prompt_path == tmp_path / "agent" / "role.md"
    assert config.system_prompt_path == tmp_path / "agent" / "system.md"
    assert config.organisation_prompt_path == tmp_path / "agent" / "organisation.md"
    assert config.project_prompt_path == tmp_path / "agent" / "project.md"
    assert config.raci_path == tmp_path / "agent" / "raci.json"
    assert config.tools_prompt_path == tmp_path / "agent" / "tools.md"
    assert config.memory_db_path == tmp_path / "state" / "memory" / "agentic-mesh-dev.product-manager.1.sqlite3"
    assert config.inbox_stream == "agent-inbox"
    assert config.inbox_consumer == "product-manager.1"


def test_materialize_project_agent_configs_writes_each_role_instance(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
broker:
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
  root_path: /documents
  structure_policy: togaf-sdlc-v1
target_repositories:
  app:
    type: git
    path: ../app
    default_branch: develop
release_deployment_targets:
  dogfood-compose:
    type: command
    command:
      - ./scripts/release-compose.sh
    working_directory: ../runtime
    timeout_seconds: 900
    rollback_summary: Re-run the previous compose deployment.
  planning-only:
    type: no-deployment
    description: Planning and analysis artifacts only.
stakeholder_contacts:
  sponsor:
    display_name: Nicholas Overend
    connector: teams
    target_ref: chat:sponsor-chat
    importance: high
roles:
  product-manager:
    template: product-manager
    instances: 1
    instructions:
      - Shape product scope.
  engineering:
    template: engineering
    instances: 2
    instructions:
      - Build safely.
""",
        encoding="utf-8",
    )
    role_templates = tmp_path / "roles"
    role_templates.mkdir()
    (role_templates / "product-manager.yaml").write_text(_role_template("product-manager"), encoding="utf-8")
    (role_templates / "engineering.yaml").write_text(_role_template("engineering"), encoding="utf-8")

    materialized = materialize_project_agent_configs(
        project_config=load_project_config(project_file),
        image="agentic-mesh-v3:local",
        source_repo=tmp_path / "source",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        agent_config_root=tmp_path / "agents",
        runtime_state_dir=tmp_path / "state",
        document_library_root=tmp_path / "documents",
        role_templates_dir=role_templates,
        system_instructions="System rules.",
        organisation_instructions="Org rules.",
        raci=DEFAULT_SDLC_RACI,
        tool_instructions="Use approved tools.",
    )

    assert [item.container_spec.role_instance_id for item in materialized] == [
        "agentic-mesh-dev.engineering.1",
        "agentic-mesh-dev.engineering.2",
        "agentic-mesh-dev.product-manager.1",
    ]
    engineering_2 = materialized[1]
    assert engineering_2.container_spec.agent_config_dir == tmp_path / "agents" / "engineering" / "2"
    assert engineering_2.container_spec.target_repositories == {
        "app": (project_file.parent / "../app").resolve(strict=False)
    }
    assert engineering_2.role_service_config.inbox_consumer == "engineering.2"
    assert engineering_2.role_service_config.memory_db_path == tmp_path / "state" / "memory" / "agentic-mesh-dev.engineering.2.sqlite3"
    engineering_project = (tmp_path / "agents" / "engineering" / "2" / "project.md").read_text(encoding="utf-8")
    assert "Build safely." in engineering_project
    assert "Documentation framework: `togaf-sdlc-v1`" in engineering_project
    assert "Document library root path: `/documents`" in engineering_project
    assert "/documents/work-items/{work_item_id}" in engineering_project
    assert "/documents/work-items/index.md" in engineering_project
    assert "Stakeholder Contacts" in engineering_project
    assert "`sponsor` (Nicholas Overend): connector `teams`, target_ref `chat:sponsor-chat`." in engineering_project
    assert "Default importance: `high`" in engineering_project
    assert "Release Deployment Targets" in engineering_project
    assert "`dogfood-compose`: type `command`." in engineering_project
    assert "Command: `./scripts/release-compose.sh`" in engineering_project
    assert "Working directory: `" in engineering_project
    assert "Timeout seconds: `900`" in engineering_project
    assert "Rollback plan: Re-run the previous compose deployment." in engineering_project
    assert "`planning-only`: type `no-deployment`." in engineering_project
    assert "No-deployment reason/description: Planning and analysis artifacts only." in engineering_project
    assert "System rules." in (tmp_path / "agents" / "engineering" / "2" / "system.md").read_text(encoding="utf-8")
    container = json.loads((tmp_path / "agents" / "engineering" / "2" / "container.json").read_text(encoding="utf-8"))
    assert container["mounts"][str((project_file.parent / "../app").resolve(strict=False))] == "/mesh/workspaces/app"
    assert container["target_repositories"] == {"app": "/mesh/workspaces/app"}
    assert container["environment"]["AGENTIC_MESH_DB"] == "/mesh/state/agentic-mesh-v3.sqlite3"
    assert container["environment"]["AGENTIC_MESH_PROJECT_ID"] == "agentic-mesh-dev"
    assert container["environment"]["AGENTIC_MESH_PROJECT_CONFIG"] == "/mesh/project/agentic-mesh/project.yaml"
    assert container["environment"]["AGENTIC_MESH_ROLE_INSTANCE_ID"] == "agentic-mesh-dev.engineering.2"
    product_tools = (tmp_path / "agents" / "product-manager" / "1" / "tools.md").read_text(encoding="utf-8")
    assert "Use approved tools." in product_tools
    assert "Role-Scoped Safe-Output Tool Catalog" in product_tools
    assert "python -m agentic_mesh_v3.cli" in product_tools
    assert "--role-instance-id \"<exact value from <role-instance>>\"" in product_tools
    assert "A valid run must record at least one allowed DO tool and at least one allowed REPLY tool" in product_tools
    assert "`backlog.upsert`: allowed" in product_tools
    assert "`backlog.upsert`: allowed; categories: DO." in product_tools
    assert "Required fields: queue_item_id, title, summary, owner_role." in product_tools
    assert "Required fields: work_item_id, target_role, question." in product_tools
    assert "`status.reply`: allowed terminal; categories: REPLY." in product_tools
    assert "`blocker.raise`: allowed; categories: DO, REPLY." in product_tools
    assert "`release.deploy`: blocked" in product_tools
    assert "`release.deploy`: blocked; categories: DO." in product_tools
    assert "Document Framework Catalog" in product_tools
    assert "Selected framework: `togaf-sdlc-v1`." in product_tools
    assert "`product_definition`: Product definition; path: `work-items/{work_item_id}/020-product-definition.md`." in product_tools
    assert "`implementation_log`: Implementation log; path: `work-items/{work_item_id}/100-implementation-log.md`." in product_tools
    assert "`artifact`: Generic artifact; path: flexible supporting artifact." in product_tools


def test_materialized_project_manager_prompt_includes_operator_guidance(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
broker:
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
  root_path: /documents
  structure_policy: togaf-sdlc-v1
roles:
  project-manager:
    template: project-manager
    instances: 1
""",
        encoding="utf-8",
    )

    materialize_project_agent_configs(
        project_config=load_project_config(project_file),
        image="agentic-mesh-v3:local",
        source_repo=tmp_path / "source",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        agent_config_root=tmp_path / "agents",
        runtime_state_dir=tmp_path / "state",
        document_library_root=tmp_path / "documents",
        role_templates_dir=Path("config/roles"),
        system_instructions="System rules.",
        organisation_instructions="Org rules.",
        raci=DEFAULT_SDLC_RACI,
        tool_instructions="Use approved tools.",
    )

    role_prompt = (tmp_path / "agents" / "project-manager" / "1" / "role.md").read_text(encoding="utf-8")

    assert "diagnose-and-route-mesh-operation" in role_prompt
    assert "agent.delegate" in role_prompt
    assert "runtime.broker.inspect" in role_prompt
    assert "runtime.sweep.request" in role_prompt
    assert "After every debug or operator-style request" in role_prompt


def _role_template(role_id: str) -> str:
    return f"""
role_id: {role_id}
purpose: Test role.
role_profile: Act as a specialist role for tests.
accountabilities:
  - Do the role work.
decision_rights:
  owns:
    - Own role decisions.
  advises:
    - Advise related roles.
  escalates:
    - Escalate blockers.
boundaries:
  - Stay inside role authority.
collaboration_style:
  - Be concise.
quality_bar:
  - Evidence is recorded.
memory_focus:
  - Useful recurring context.
core_workflows:
  - workflow_id: test-workflow
    trigger: Test trigger.
    inputs:
      - Input
    outputs:
      - Output
    artifacts:
      - documents/work-items/{{work_item_id}}/index.md
standards_references:
  - name: Test Standard
    url: docs/test.md
    applies_to: Tests
anti_patterns:
  - Pretending work happened.
standing_instructions:
  - Use tools honestly.
"""
