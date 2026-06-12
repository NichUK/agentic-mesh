from pathlib import Path

import yaml


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


def _load_dogfood_compose() -> dict[str, object]:
    compose_path = Path("examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml")
    with compose_path.open("r", encoding="utf-8") as handle:
        compose = yaml.safe_load(handle)
    assert isinstance(compose, dict)
    return compose


def _linuxch_overlay_text() -> str:
    overlay_path = Path("examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml")
    return overlay_path.read_text(encoding="utf-8")
