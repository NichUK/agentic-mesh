from __future__ import annotations

from pathlib import Path
import subprocess

import yaml


V4_PROJECT_FILE = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")
V4_ROLE_IDS = {
    "business-analyst",
    "delivery-manager",
    "enterprise-architect",
    "platform-engineer",
    "product-manager",
    "prompt-engineer",
    "project-manager",
    "research-analyst",
    "security-architect",
    "solution-architect",
    "engineering",
    "qa-engineer",
    "release-manager",
    "technical-writer",
    "ux-designer",
}


def test_dogfood_compose_does_not_define_legacy_runtime_services() -> None:
    compose = _load_dogfood_compose()

    for service_name in (
        "v2-runtime",
        "v2-teams-ingress",
        "v2-supervisor",
        "v3-runtime",
        "v3-nats",
        "v3-supervisor",
        "v3-dogfood-proof",
    ):
        assert service_name not in compose["services"]


def test_dogfood_compose_defines_v4_runtime_and_dispatcher() -> None:
    compose = _load_dogfood_compose()
    runtime = compose["services"]["runtime"]
    dispatcher = compose["services"]["dispatcher"]

    assert "agentic_mesh_v4.cli" in runtime["command"]
    assert "serve --host 0.0.0.0 --port 8100" in runtime["command"]
    assert runtime["ports"] == ["${AGENTIC_MESH_V4_STATUS_PORT:-8100}:8100"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in runtime["volumes"]

    assert "agentic_mesh_v4.cli" in dispatcher["command"]
    assert "dispatch-loop" in dispatcher["command"]
    assert "--wake" in dispatcher["command"]
    assert "--compose-file /mesh/project/deploy/compose/docker-compose.v4.yml" in dispatcher["command"]
    assert "--compose-file /mesh/project/deploy/compose/docker-compose.linuxch.yml" in dispatcher["command"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in dispatcher["volumes"]


def test_dogfood_compose_defines_full_lazy_role_app_server_team() -> None:
    compose = _load_dogfood_compose()
    service_names = set(compose["services"])

    for role_id in V4_ROLE_IDS:
        service_name = f"agentic-mesh-dev-{role_id}-1"
        assert service_name in service_names
        service = compose["services"][service_name]
        assert service["profiles"] == ["roles"]
        assert service["working_dir"] == "/mesh/agent"
        assert "codex app-server" in service["command"]
        assert "--ws-auth capability-token" in service["command"]
        assert "--ws-token-file /mesh/agent/ws-token" in service["command"]
        assert service["environment"]["AGENTIC_MESH_ROLE_ID"] == role_id
        assert service["environment"]["AGENTIC_MESH_ROLE_INSTANCE_ID"] == f"agentic-mesh-dev.{role_id}.1"
        assert f"/state/v4/agent-configs/{role_id}/1:/mesh/agent:ro" in "\n".join(service["volumes"])


def test_linuxch_overlay_restarts_only_v4_runtime_services() -> None:
    overlay = _linuxch_overlay_text()

    assert "  runtime:\n    restart: unless-stopped" in overlay
    assert "  dispatcher:\n    restart: unless-stopped" in overlay
    assert "  otel-collector:" in overlay
    assert "v3-nats:" not in overlay
    assert "v3-supervisor:" not in overlay
    assert "run-agent-service" not in overlay


def test_linuxch_deploy_script_preserves_v4_live_environment() -> None:
    script = Path("scripts/deploy-linuxch-compose.sh").read_text(encoding="utf-8")

    for name in [
        "AGENTIC_MESH_ONEDRIVE_TOKEN",
        "AGENTIC_MESH_ONEDRIVE_DRIVE_ID",
        "AGENTIC_MESH_SPONSOR_TEAMS_USER_ID",
        "AGENTIC_MESH_TEAMS_TOKEN",
        "AGENTIC_MESH_TEAMS_PUBLIC_ENDPOINT",
        "AGENTIC_MESH_TEAMS_BOT_SERVICE_URL",
        "AGENTIC_MESH_TENANT_ID",
        "AGENTIC_MESH_PROJECT_TEAM_ID",
        "AGENTIC_MESH_PROJECT_CHANNEL_ID",
        "AGENTIC_MESH_SPONSOR_AAD_OBJECT_ID",
        "AGENTIC_MESH_V4_STATUS_PORT",
    ]:
        assert f"export {name}" in script
        assert f"{name}=${name}" in script
    assert "AGENTIC_MESH_V3_STATUS_PORT" not in script
    assert "AGENTIC_MESH_NATS_STATE_HOST_PATH" not in script


def test_dogfood_compose_env_example_lists_required_v4_live_inputs() -> None:
    env_example = Path("examples/projects/agentic-mesh-dev/deploy/compose/.env.example").read_text(
        encoding="utf-8"
    )

    for name in [
        "AGENTIC_MESH_ONEDRIVE_TOKEN",
        "AGENTIC_MESH_ONEDRIVE_DRIVE_ID",
        "AGENTIC_MESH_SPONSOR_TEAMS_USER_ID",
        "AGENTIC_MESH_TEAMS_TOKEN",
        "AGENTIC_MESH_TEAMS_PUBLIC_ENDPOINT",
        "AGENTIC_MESH_TEAMS_BOT_SERVICE_URL",
        "AGENTIC_MESH_TENANT_ID",
        "AGENTIC_MESH_PROJECT_TEAM_ID",
        "AGENTIC_MESH_PROJECT_CHANNEL_ID",
        "AGENTIC_MESH_SPONSOR_AAD_OBJECT_ID",
        "AGENTIC_MESH_V4_STATUS_PORT",
    ]:
        assert f"{name}=" in env_example
    assert "AGENTIC_MESH_V3_STATUS_PORT" not in env_example
    assert "AGENTIC_MESH_NATS_STATE_HOST_PATH" not in env_example


def test_linuxch_release_script_defaults_to_v4_services() -> None:
    script = Path("scripts/release-linuxch-compose.sh").read_text(encoding="utf-8")

    assert "AGENTIC_MESH_RELEASE_SERVICES:=runtime dispatcher otel-collector" in script
    assert "AGENTIC_MESH_ROLE_SERVICES:=" in script
    assert "--profile build-image build runtime-image" in script
    assert "--profile v4 up -d --remove-orphans $AGENTIC_MESH_RELEASE_SERVICES" in script
    assert "--profile roles stop $AGENTIC_MESH_ROLE_SERVICES" in script
    assert "--profile roles rm -f $AGENTIC_MESH_ROLE_SERVICES" in script
    assert "project-v4.yaml" in script
    assert "state/v4/agent-configs" in script
    assert "agentic_mesh_v4.cli" in script
    for role_id in V4_ROLE_IDS:
        assert f"agentic-mesh-dev-{role_id}-1" in script
    assert "v3-nats" not in script
    assert "v3-supervisor" not in script


def test_v4_dogfood_project_config_exists_for_compose_profile() -> None:
    assert V4_PROJECT_FILE.exists()


def test_linuxch_overlay_is_valid_for_v4_profile() -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "v4",
            "-f",
            "examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml",
            "-f",
            "examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml",
            "config",
            "--quiet",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def _load_dogfood_compose() -> dict[str, object]:
    compose_path = Path("examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml")
    with compose_path.open("r", encoding="utf-8") as handle:
        compose = yaml.safe_load(handle)
    assert isinstance(compose, dict)
    return compose


def _linuxch_overlay_text() -> str:
    overlay_path = Path("examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml")
    return overlay_path.read_text(encoding="utf-8")
