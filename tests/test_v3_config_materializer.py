from pathlib import Path

from agentic_mesh_v3.config_materializer import materialize_agent_config
from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.lifecycle import RoleContainerSpec


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
        role_prompt="You are Project Manager.",
        organisation_instructions="Org rules.",
        project_instructions="Project rules.",
        tool_instructions="Use MCP or CLI safe-output tools for durable effects.",
        raci=DEFAULT_SDLC_RACI,
    )

    assert {path.name for path in written} == {
        "role.md",
        "organisation.md",
        "project.md",
        "tools.md",
        "raci.json",
        "container.json",
    }
    assert "Project Manager" in (tmp_path / "agent" / "role.md").read_text(encoding="utf-8")
    assert "safe-output tools" in (tmp_path / "agent" / "tools.md").read_text(encoding="utf-8")
    assert "requirements" in (tmp_path / "agent" / "raci.json").read_text(encoding="utf-8")
    assert "tools.md" in (tmp_path / "agent" / "container.json").read_text(encoding="utf-8")
