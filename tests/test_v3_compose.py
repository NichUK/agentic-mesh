from pathlib import Path

import yaml

from agentic_mesh_v3.compose import render_role_services_compose
from agentic_mesh_v3.compose import service_name_for_role
from agentic_mesh_v3.lifecycle import RoleContainerSpec


def test_render_role_services_compose_includes_role_service_command_and_mounts(tmp_path: Path) -> None:
    spec = RoleContainerSpec(
        role_instance_id="agentic-mesh-dev.product-manager.1",
        image="agentic-mesh-v3:local",
        source_repo=tmp_path / "source",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        agent_config_dir=tmp_path / "agents" / "product-manager" / "1",
        runtime_state_dir=tmp_path / "state",
        document_library_root=tmp_path / "documents",
        environment={"PROJECT_ID": "agentic-mesh-dev", "ROLE_ID": "product-manager"},
        target_repositories={"app": tmp_path / "app"},
    )

    compose = yaml.safe_load(render_role_services_compose([spec], network_name="mesh-test"))

    service = compose["services"]["agentic-mesh-dev-product-manager-1"]
    assert service["image"] == "agentic-mesh-v3:local"
    assert service["command"][0] == "agentic-mesh-v3"
    assert "run-agent-service" in service["command"]
    assert "--role-id" in service["command"]
    assert "product-manager" in service["command"]
    assert str(tmp_path / "documents") + ":/documents" in service["volumes"]
    assert str(tmp_path / "app") + ":/mesh/workspaces/app" in service["volumes"]
    assert service["environment"] == {
        "PROJECT_ID": "agentic-mesh-dev",
        "ROLE_ID": "product-manager",
    }
    assert service["networks"] == ["mesh-test"]
    assert compose["networks"]["mesh-test"]["name"] == "mesh-test"


def test_render_role_services_compose_can_include_nats_jetstream(tmp_path: Path) -> None:
    spec = RoleContainerSpec(
        role_instance_id="agentic-mesh-dev.product-manager.1",
        image="agentic-mesh-v3:local",
        source_repo=tmp_path / "source",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        agent_config_dir=tmp_path / "agents" / "product-manager" / "1",
        runtime_state_dir=tmp_path / "state",
        document_library_root=tmp_path / "documents",
        environment={"AGENTIC_MESH_BROKER_SERVERS": "nats://nats:4222"},
        target_repositories={},
    )

    compose = yaml.safe_load(render_role_services_compose([spec], include_nats=True))

    nats = compose["services"]["nats"]
    service = compose["services"]["agentic-mesh-dev-product-manager-1"]
    assert nats["image"] == "nats:2.10-alpine"
    assert nats["command"] == ["-js", "-m", "8222"]
    assert nats["ports"] == ["4222:4222", "8222:8222"]
    assert nats["networks"] == ["agentic-mesh"]
    assert service["depends_on"] == ["nats"]


def test_service_name_for_role_uses_compose_safe_name() -> None:
    assert service_name_for_role("agentic-mesh-dev.engineering.2") == "agentic-mesh-dev-engineering-2"
