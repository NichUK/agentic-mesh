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
    assert config.inbox_consumer == "agentic-mesh-dev.product-manager.1"


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
    assert engineering_2.role_service_config.inbox_consumer == "agentic-mesh-dev.engineering.2"
    assert engineering_2.role_service_config.memory_db_path == tmp_path / "state" / "memory" / "agentic-mesh-dev.engineering.2.sqlite3"
    assert "Build safely." in (tmp_path / "agents" / "engineering" / "2" / "project.md").read_text(encoding="utf-8")
    assert "System rules." in (tmp_path / "agents" / "engineering" / "2" / "system.md").read_text(encoding="utf-8")
    product_tools = (tmp_path / "agents" / "product-manager" / "1" / "tools.md").read_text(encoding="utf-8")
    assert "Use approved tools." in product_tools
    assert "Role-Scoped Safe-Output Tool Catalog" in product_tools
    assert "`backlog.upsert`: allowed" in product_tools
    assert "Required fields: queue_item_id, title, summary, owner_role." in product_tools
    assert "Required fields: work_item_id, target_role, question." in product_tools
    assert "`release.deploy`: blocked" in product_tools


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
