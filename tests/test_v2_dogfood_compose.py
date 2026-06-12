from pathlib import Path

import yaml

from agentic_mesh_v2.container_lifecycle import ComposeRoleLifecycleConfig
from agentic_mesh_v2.project_config import list_project_role_service_configs
from agentic_mesh_v2.project_config import load_role_container_lifecycle_config


PROJECT_FILE = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml")


def test_dogfood_compose_runs_status_server_with_project_file() -> None:
    compose = _load_dogfood_compose()
    service = compose["services"]["v2-runtime"]
    command = service["command"]

    assert "serve --host 0.0.0.0 --port 8080" in command
    assert "--project-file /mesh/project/agentic-mesh/project.yaml" in command
    assert service["environment"]["AGENTIC_MESH_PROJECT_FILE"] == "/mesh/project/agentic-mesh/project.yaml"
    assert "/mesh/project" in "\n".join(service["volumes"])


def test_dogfood_compose_runs_project_supervisor_service() -> None:
    compose = _load_dogfood_compose()
    runtime_service = compose["services"]["v2-runtime"]
    service = compose["services"]["v2-supervisor"]
    command = service["command"]

    assert service["image"] == runtime_service["image"]
    assert service["environment"] == runtime_service["environment"]
    assert service["volumes"] == runtime_service["volumes"]
    assert service["working_dir"] == runtime_service["working_dir"]
    assert service["environment"]["AGENTIC_MESH_STATE_ROOT"] == "/mesh/project/state/v2"
    assert "run-project-supervisor-service" in command
    assert "--project-file /mesh/project/agentic-mesh/project.yaml" in command
    assert "--continuous" in command
    assert "--execute" in command
    assert "--poll-seconds ${AGENTIC_MESH_SUPERVISOR_POLL_SECONDS:-5}" in command
    assert service["depends_on"] == ["v2-runtime"]


def test_linuxch_overlay_restarts_project_supervisor_service() -> None:
    overlay = _linuxch_overlay_text()

    assert "v2-supervisor:" in overlay
    assert "restart: unless-stopped" in overlay


def test_dogfood_project_config_defines_role_container_lifecycle() -> None:
    config = load_role_container_lifecycle_config(PROJECT_FILE, role_id="product-manager")
    lifecycle = ComposeRoleLifecycleConfig.from_mapping(config)

    service_name = lifecycle.service_name(
        project_id="agentic-mesh-dev",
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
    )

    assert service_name == "agentic-mesh-dev-product-manager-1"
    assert lifecycle.working_directory == PROJECT_FILE.parent / "../deploy/compose"
    assert [path.name for path in lifecycle.compose_files] == [
        "docker-compose.yml",
        "docker-compose.linuxch.yml",
    ]


def test_dogfood_compose_runs_one_role_service_per_project_role_instance() -> None:
    compose = _load_dogfood_compose()
    runtime_service = compose["services"]["v2-runtime"]

    for role_config in list_project_role_service_configs(PROJECT_FILE):
        lifecycle = ComposeRoleLifecycleConfig.from_mapping(
            load_role_container_lifecycle_config(PROJECT_FILE, role_id=role_config.role_id)
        )
        service_name = lifecycle.service_name(
            project_id=role_config.project_id,
            role_id=role_config.role_id,
            role_instance_id=role_config.role_instance_id,
        )
        service = compose["services"][service_name]
        command = service["command"]

        assert service["image"] == runtime_service["image"]
        assert service["environment"] == runtime_service["environment"]
        assert service["volumes"] == runtime_service["volumes"]
        assert service["working_dir"] == runtime_service["working_dir"]
        assert service["depends_on"] == ["v2-runtime"]
        assert "run-role-service-loop" in command
        assert f"--role-id {role_config.role_id}" in command
        assert f"--role-instance-id {role_config.role_instance_id}" in command
        assert "--continuous" in command
        assert "--poll-seconds ${AGENTIC_MESH_ROLE_POLL_SECONDS:-5}" in command
        assert "--worker-timeout-seconds" not in command


def test_linuxch_overlay_restarts_every_role_service() -> None:
    overlay = _linuxch_overlay_text()

    for role_config in list_project_role_service_configs(PROJECT_FILE):
        lifecycle = ComposeRoleLifecycleConfig.from_mapping(
            load_role_container_lifecycle_config(PROJECT_FILE, role_id=role_config.role_id)
        )
        service_name = lifecycle.service_name(
            project_id=role_config.project_id,
            role_id=role_config.role_id,
            role_instance_id=role_config.role_instance_id,
        )
        assert f"  {service_name}:\n    restart: unless-stopped" in overlay


def _load_dogfood_compose() -> dict[str, object]:
    compose_path = Path("examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml")
    with compose_path.open("r", encoding="utf-8") as handle:
        compose = yaml.safe_load(handle)
    assert isinstance(compose, dict)
    return compose


def _linuxch_overlay_text() -> str:
    overlay_path = Path("examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml")
    return overlay_path.read_text(encoding="utf-8")
