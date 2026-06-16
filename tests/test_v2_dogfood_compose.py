from pathlib import Path

import yaml

from agentic_mesh_v2.container_lifecycle import ComposeRoleLifecycleConfig
from agentic_mesh_v2.project_config import list_project_role_service_configs
from agentic_mesh_v2.project_config import load_role_container_lifecycle_config


PROJECT_FILE = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml")
V3_PROJECT_FILE = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v3.yaml")


def test_dogfood_compose_runs_status_server_with_project_file() -> None:
    compose = _load_dogfood_compose()
    service = compose["services"]["v2-runtime"]
    command = service["command"]

    assert "serve --host 0.0.0.0 --port 8080" in command
    assert "--project-file /mesh/project/agentic-mesh/project.yaml" in command
    assert service["environment"]["AGENTIC_MESH_PROJECT_FILE"] == "/mesh/project/agentic-mesh/project.yaml"
    assert service["environment"]["CODEX_HOME"] == "/mesh/worker-auth/codex"
    assert "/mesh/project" in "\n".join(service["volumes"])
    assert "/mesh/worker-auth/codex" in "\n".join(service["volumes"])


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


def test_dogfood_compose_runs_teams_ingress_service() -> None:
    compose = _load_dogfood_compose()
    runtime_service = compose["services"]["v2-runtime"]
    service = compose["services"]["v2-teams-ingress"]
    command = service["command"]

    assert service["image"] == runtime_service["image"]
    assert service["environment"] == runtime_service["environment"]
    assert service["volumes"] == runtime_service["volumes"]
    assert service["working_dir"] == runtime_service["working_dir"]
    assert "serve-teams-ingress --host 0.0.0.0 --port 3978" in command
    assert "--project-file /mesh/project/agentic-mesh/project.yaml" in command
    assert "--path /api/messages" in command
    assert service["ports"] == ["3978:3978"]
    assert service["depends_on"] == ["v2-runtime"]


def test_dogfood_compose_defines_v3_runtime_profile() -> None:
    compose = _load_dogfood_compose()
    service = compose["services"]["v3-runtime"]
    command = service["command"]

    assert service["profiles"] == ["v3"]
    assert service["depends_on"] == ["v3-nats"]
    assert "--project-config /mesh/project/agentic-mesh/project-v3.yaml" in command
    assert "python -m agentic_mesh_v3.cli" in command
    assert "serve --host 0.0.0.0 --port 8080" in command
    assert service["ports"] == ["${AGENTIC_MESH_V3_STATUS_PORT:-8101}:8080"]
    assert service["environment"]["AGENTIC_MESH_PROJECT_FILE"] == "/mesh/project/agentic-mesh/project-v3.yaml"
    assert service["environment"]["AGENTIC_MESH_STATE_ROOT"] == "/mesh/project/state/v3"
    assert "AGENTIC_MESH_ONEDRIVE_TOKEN" in service["environment"]
    assert "AGENTIC_MESH_ONEDRIVE_DRIVE_ID" in service["environment"]
    assert "AGENTIC_MESH_SPONSOR_TEAMS_USER_ID" in service["environment"]


def test_dogfood_compose_defines_v3_nats_profile() -> None:
    compose = _load_dogfood_compose()
    service = compose["services"]["v3-nats"]

    assert service["image"] == "nats:2.10-alpine"
    assert service["command"] == ["-js", "-sd", "/data"]
    assert service["profiles"] == ["v3", "v3-proof"]
    assert "${AGENTIC_MESH_NATS_STATE_HOST_PATH:-../../state/v3/nats}:/data" in service["volumes"]


def test_dogfood_compose_defines_v3_dogfood_proof_runner() -> None:
    compose = _load_dogfood_compose()
    service = compose["services"]["v3-dogfood-proof"]
    command = service["command"]

    assert service["profiles"] == ["v3-proof"]
    assert service["depends_on"] == ["v3-nats"]
    assert "python -m agentic_mesh_v3.cli" in command
    assert "--project-config /mesh/project/agentic-mesh/project-v3.yaml" in command
    assert "local-e2e-dogfood --deployment-target-id dogfood-compose" in command
    assert service["environment"]["AGENTIC_MESH_PROJECT_FILE"] == "/mesh/project/agentic-mesh/project-v3.yaml"


def test_linuxch_overlay_restarts_v3_runtime_and_mounts_docker_for_proof() -> None:
    overlay = _linuxch_overlay_text()

    assert "  v3-nats:\n    restart: unless-stopped" in overlay
    assert "  v3-runtime:\n    restart: unless-stopped" in overlay
    assert "  v3-dogfood-proof:" in overlay
    assert "/var/run/docker.sock:/var/run/docker.sock" in overlay


def test_linuxch_overlay_restarts_project_supervisor_service() -> None:
    overlay = _linuxch_overlay_text()

    assert "v2-supervisor:" in overlay
    assert "restart: unless-stopped" in overlay


def test_linuxch_overlay_restarts_teams_ingress_service() -> None:
    overlay = _linuxch_overlay_text()

    assert "v2-teams-ingress:" in overlay
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


def test_v3_dogfood_project_config_exists_for_compose_profile() -> None:
    assert V3_PROJECT_FILE.exists()


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
