from pathlib import Path

import yaml


V3_PROJECT_FILE = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v3.yaml")
V3_LIVE_ROLE_IDS = {
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


def test_dogfood_compose_does_not_define_v2_runtime_services() -> None:
    compose = _load_dogfood_compose()

    assert "v2-runtime" not in compose["services"]
    assert "v2-teams-ingress" not in compose["services"]
    assert "v2-supervisor" not in compose["services"]


def test_dogfood_compose_defines_v3_runtime_profile() -> None:
    compose = _load_dogfood_compose()
    service = compose["services"]["v3-runtime"]
    command = service["command"]

    assert service["profiles"] == ["v3"]
    assert service["depends_on"] == ["v3-nats"]
    assert "--project-config /mesh/project/agentic-mesh/project-v3.yaml" in command
    assert "preflight-live --check-broker && exec" in command
    assert "python -m agentic_mesh_v3.cli" in command
    assert "serve --host 0.0.0.0 --port 8080" in command
    assert service["ports"] == ["${AGENTIC_MESH_V3_STATUS_PORT:-8100}:8080"]
    assert service["environment"]["AGENTIC_MESH_PROJECT_FILE"] == "/mesh/project/agentic-mesh/project-v3.yaml"
    assert service["environment"]["AGENTIC_MESH_STATE_ROOT"] == "/mesh/project/state/v3"
    assert service["environment"]["AGENTIC_MESH_SYSTEM_HOST_PATH"] == "${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}"
    assert service["environment"]["AGENTIC_MESH_PROJECT_HOST_PATH"] == "${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}"
    assert service["environment"]["AGENTIC_MESH_WORKSPACE_HOST_PATH"] == "${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}"
    assert (
        service["environment"]["AGENTIC_MESH_CODEX_HOME_HOST_PATH"]
        == "${AGENTIC_MESH_CODEX_HOME_HOST_PATH:-../../state/worker_mounts/codex-agentic-mesh-dev-team-home-q}"
    )
    assert "AGENTIC_MESH_ONEDRIVE_TOKEN" in service["environment"]
    assert "AGENTIC_MESH_ONEDRIVE_DRIVE_ID" in service["environment"]
    assert "AGENTIC_MESH_SPONSOR_TEAMS_USER_ID" in service["environment"]
    assert "AGENTIC_MESH_TENANT_ID" in service["environment"]
    assert "AGENTIC_MESH_TEAMS_PUBLIC_ENDPOINT" in service["environment"]
    assert "AGENTIC_MESH_TEAMS_BOT_SERVICE_URL" in service["environment"]
    assert "AGENTIC_MESH_PROJECT_TEAM_ID" in service["environment"]
    assert "AGENTIC_MESH_PROJECT_CHANNEL_ID" in service["environment"]
    assert "AGENTIC_MESH_SPONSOR_AAD_OBJECT_ID" in service["environment"]


def test_dogfood_compose_defines_v3_nats_profile() -> None:
    compose = _load_dogfood_compose()
    service = compose["services"]["v3-nats"]

    assert service["image"] == "nats:2.10-alpine"
    assert service["command"] == ["-js", "-sd", "/data"]
    assert service["profiles"] == ["v3", "v3-proof"]
    assert "${AGENTIC_MESH_NATS_STATE_HOST_PATH:-../../state/v3/nats}:/data" in service["volumes"]


def test_dogfood_compose_runs_v3_supervisor_with_broker_wake() -> None:
    compose = _load_dogfood_compose()
    runtime_service = compose["services"]["v3-runtime"]
    service = compose["services"]["v3-supervisor"]
    command = service["command"]

    assert service["image"] == runtime_service["image"]
    assert service["environment"] == runtime_service["environment"]
    assert service["volumes"] == runtime_service["volumes"]
    assert service["working_dir"] == runtime_service["working_dir"]
    assert service["depends_on"] == ["v3-nats", "v3-runtime"]
    assert service["profiles"] == ["v3"]
    assert "run-project-supervisor-service" in command
    assert "--continuous" in command
    assert "--refresh-inbox-from-broker" in command
    assert "--execute" in command
    assert "--compose-file /mesh/project/deploy/compose/docker-compose.yml" in command
    assert "--compose-file /mesh/project/deploy/compose/docker-compose.linuxch.yml" in command
    assert "--compose-project-name ${COMPOSE_PROJECT_NAME:-agentic-mesh}" in command


def test_dogfood_compose_defines_v3_dogfood_proof_runner() -> None:
    compose = _load_dogfood_compose()
    service = compose["services"]["v3-dogfood-proof"]
    command = service["command"]

    assert service["profiles"] == ["v3-proof"]
    assert service["depends_on"] == ["v3-nats"]
    assert "python -m agentic_mesh_v3.cli" in command
    assert "--project-config /mesh/project/agentic-mesh/project-v3.yaml" in command
    assert "agent-e2e-dogfood" in command
    assert "--runtime-state-dir /mesh/project/state/v3/agent-service-dogfood" in command
    assert "--deployment-target-id dogfood-compose" in command
    assert "--broker-stream agent-inbox-proof" in command
    assert service["environment"]["AGENTIC_MESH_PROJECT_FILE"] == "/mesh/project/agentic-mesh/project-v3.yaml"


def test_linuxch_overlay_restarts_v3_runtime_and_mounts_docker_for_proof() -> None:
    overlay = _linuxch_overlay_text()

    assert "  v3-nats:\n    restart: unless-stopped" in overlay
    assert "  v3-runtime:\n    restart: unless-stopped" in overlay
    assert "  v3-supervisor:" in overlay
    assert "  v3-dogfood-proof:" in overlay
    assert "/var/run/docker.sock:/var/run/docker.sock" in overlay


def test_linuxch_deploy_script_preserves_v3_live_environment() -> None:
    script = Path("scripts/deploy-linuxch-compose.sh").read_text(encoding="utf-8")

    for name in [
        "AGENTIC_MESH_ONEDRIVE_TOKEN",
        "AGENTIC_MESH_ONEDRIVE_DRIVE_ID",
        "AGENTIC_MESH_SPONSOR_TEAMS_USER_ID",
        "AGENTIC_MESH_TEAMS_TOKEN",
        "AGENTIC_MESH_TEAMS_SENDER_USER_ID",
        "AGENTIC_MESH_TEAMS_PUBLIC_ENDPOINT",
        "AGENTIC_MESH_TEAMS_BOT_SERVICE_URL",
        "AGENTIC_MESH_TENANT_ID",
        "AGENTIC_MESH_PROJECT_TEAM_ID",
        "AGENTIC_MESH_PROJECT_CHANNEL_ID",
        "AGENTIC_MESH_SPONSOR_AAD_OBJECT_ID",
        "AGENTIC_MESH_V3_STATUS_PORT",
        "AGENTIC_MESH_NATS_STATE_HOST_PATH",
    ]:
        assert f"export {name}" in script
        assert f"{name}=${name}" in script
    assert 'chmod 600 "$STAGE_DIR/.env"' in script
    assert 'AGENTIC_MESH_V3_STATUS_PORT="${AGENTIC_MESH_V3_STATUS_PORT:-8100}"' in script
    assert "AGENTIC_MESH_FORCE_V3_STATUS_PORT" in script


def test_dogfood_compose_env_example_lists_required_v3_live_inputs() -> None:
    env_example = Path("examples/projects/agentic-mesh-dev/deploy/compose/.env.example").read_text(
        encoding="utf-8"
    )

    for name in [
        "AGENTIC_MESH_ONEDRIVE_TOKEN",
        "AGENTIC_MESH_ONEDRIVE_DRIVE_ID",
        "AGENTIC_MESH_SPONSOR_TEAMS_USER_ID",
        "AGENTIC_MESH_TEAMS_TOKEN",
        "AGENTIC_MESH_TEAMS_SENDER_USER_ID",
        "AGENTIC_MESH_TEAMS_PUBLIC_ENDPOINT",
        "AGENTIC_MESH_TEAMS_BOT_SERVICE_URL",
        "AGENTIC_MESH_TENANT_ID",
        "AGENTIC_MESH_PROJECT_TEAM_ID",
        "AGENTIC_MESH_PROJECT_CHANNEL_ID",
        "AGENTIC_MESH_SPONSOR_AAD_OBJECT_ID",
        "AGENTIC_MESH_V3_STATUS_PORT",
        "AGENTIC_MESH_NATS_STATE_HOST_PATH",
    ]:
        assert f"{name}=" in env_example
    assert "AGENTIC_MESH_V3_STATUS_PORT=8100" in env_example


def test_linuxch_v3_project_install_script_cleans_up_graph_token_file() -> None:
    script = Path("scripts/install-linuxch-v3-project.ps1").read_text(encoding="utf-8")

    assert "agentic_mesh_v3.project_install_cli" in script
    assert 'trap cleanup EXIT' in script
    assert 'rm -f "`$token_file"' in script
    assert 'printf "{\\"access_token\\":\\"%s\\"}" "`$AGENTIC_MESH_TEAMS_TOKEN"' in script
    assert 'chmod 600 "`$token_file"' in script


def test_linuxch_graph_env_refresh_helper_requests_required_scopes_and_updates_remote_env() -> None:
    script = Path("scripts/update-linuxch-v3-graph-env.ps1").read_text(encoding="utf-8")

    for scope in [
        "https://graph.microsoft.com/Files.ReadWrite.All",
        "https://graph.microsoft.com/Chat.Create",
        "https://graph.microsoft.com/Chat.ReadWrite",
        "https://graph.microsoft.com/ChatMessage.Send",
        "https://graph.microsoft.com/ChannelMessage.Send",
    ]:
        assert scope in script
    for name in [
        "AGENTIC_MESH_ONEDRIVE_TOKEN",
        "AGENTIC_MESH_TEAMS_TOKEN",
        "AGENTIC_MESH_ONEDRIVE_DRIVE_ID",
        "AGENTIC_MESH_SPONSOR_TEAMS_USER_ID",
        "AGENTIC_MESH_TEAMS_SENDER_USER_ID",
        "AGENTIC_MESH_GRAPH_CLIENT_ID",
        "AGENTIC_MESH_GRAPH_TENANT_ID",
    ]:
        assert name in script
    assert "oauth2/v2.0/devicecode" in script
    assert "oauth2/v2.0/token" in script
    assert script.count("-ErrorAction Stop") >= 2
    assert "ErrorDetails.Message" in script
    assert '$remoteScript = $remoteScript -replace "`r", ""' in script
    assert 'authorization_pending")' in script
    assert 'slow_down")' in script
    assert "--use-device-code" not in script
    assert "SkipDeviceLogin" in script
    assert "os.chmod(tmp_path, 0o600)" in script
    assert "Write-Host $token" not in script
    assert "Write-Output $token" not in script


def test_linuxch_release_script_defaults_to_v3_preflight_and_services() -> None:
    script = Path("scripts/release-linuxch-compose.sh").read_text(encoding="utf-8")

    assert 'AGENTIC_MESH_V3_STATUS_PORT:=8100' in script
    assert 'export AGENTIC_MESH_FORCE_V3_STATUS_PORT="$AGENTIC_MESH_V3_STATUS_PORT"' in script
    assert "AGENTIC_MESH_RELEASE_SERVICES:=v3-nats v3-runtime v3-supervisor otel-collector" in script
    assert "AGENTIC_MESH_ROLE_SERVICES:=" in script
    assert "AGENTIC_MESH_SUPERVISOR_SERVICE:=v3-supervisor" in script
    for role_id in V3_LIVE_ROLE_IDS:
        assert f"agentic-mesh-dev-{role_id}-1" in script
    assert "AGENTIC_MESH_STOP_ROLE_SERVICES_ON_RELEASE:=1" in script
    assert 'AGENTIC_MESH_REMOVE_LEGACY_V2_CONTAINERS:=1' in script
    assert "agentic-mesh-v2-runtime-1" in script
    assert "agentic-mesh-agentic-mesh-dev-business-analyst-1-1" in script
    assert 'docker ps -q --filter "name=agentic-mesh-agentic-mesh-dev-"' not in script
    assert "mkdir -p" in script and "AGENTIC_MESH_NATS_STATE_HOST_PATH" in script
    assert "--profile v3 up -d v3-nats" in script
    assert "--profile v3 run --rm --no-deps v3-runtime" in script
    assert "preflight-live" in script
    assert "--check-broker" in script
    assert "materialize-agent-configs" in script
    assert "--agent-config-root /mesh/project/state/v3/agent-configs" in script
    assert "--profile v3 up -d --remove-orphans $AGENTIC_MESH_RELEASE_SERVICES" in script
    assert "--profile v3 stop $AGENTIC_MESH_ROLE_SERVICES" in script
    assert "--profile v3 rm -f $AGENTIC_MESH_ROLE_SERVICES" in script
    assert script.index("--profile v3 stop $AGENTIC_MESH_ROLE_SERVICES") < script.index(
        "--profile v3 rm -f $AGENTIC_MESH_ROLE_SERVICES"
    )
    assert '--profile v3 stop "$AGENTIC_MESH_SUPERVISOR_SERVICE"' in script
    assert script.index('--profile v3 stop "$AGENTIC_MESH_SUPERVISOR_SERVICE"') < script.index(
        "--profile v3 up -d v3-nats"
    )


def test_v3_dogfood_project_config_uses_compose_nats_service_name() -> None:
    project_config = V3_PROJECT_FILE.read_text(encoding="utf-8")

    assert "servers: nats://v3-nats:4222" in project_config
    assert "servers: nats://nats:4222" not in project_config


def test_linuxch_overlay_does_not_restart_v2_supervisor_service() -> None:
    overlay = _linuxch_overlay_text()

    assert "v2-supervisor:" not in overlay


def test_linuxch_overlay_does_not_restart_v2_teams_ingress_service() -> None:
    overlay = _linuxch_overlay_text()

    assert "v2-teams-ingress:" not in overlay


def test_dogfood_compose_uses_v3_project_configuration_by_default() -> None:
    compose = _load_dogfood_compose()
    runtime_service = compose["services"]["v3-runtime"]

    assert runtime_service["environment"]["AGENTIC_MESH_PROJECT_FILE"] == "/mesh/project/agentic-mesh/project-v3.yaml"
    assert runtime_service["environment"]["AGENTIC_MESH_STATE_ROOT"] == "/mesh/project/state/v3"


def test_v3_dogfood_project_config_exists_for_compose_profile() -> None:
    assert V3_PROJECT_FILE.exists()


def test_dogfood_compose_defines_full_lazy_role_service_team() -> None:
    compose = _load_dogfood_compose()
    service_names = set(compose["services"])

    for role_id in V3_LIVE_ROLE_IDS:
        assert f"agentic-mesh-dev-{role_id}-1" in service_names
    assert "agentic-mesh-dev-engineering-2" not in service_names


def test_dogfood_compose_runs_v3_live_role_services() -> None:
    compose = _load_dogfood_compose()
    runtime_service = compose["services"]["v3-runtime"]

    for role_id in sorted(V3_LIVE_ROLE_IDS):
        service_name = f"agentic-mesh-dev-{role_id}-1"
        service = compose["services"][service_name]
        command = service["command"]

        assert service["image"] == runtime_service["image"]
        assert service["environment"] == runtime_service["environment"]
        assert service["volumes"] == runtime_service["volumes"]
        assert service["working_dir"] == runtime_service["working_dir"]
        assert service["depends_on"] == ["v3-nats", "v3-runtime"]
        assert service["profiles"] == ["v3"]
        assert service["restart"] == "unless-stopped"
        assert "python -m agentic_mesh_v3.cli" in command
        assert "run-agent-service" in command
        assert "--project-config /mesh/project/agentic-mesh/project-v3.yaml" in command
        assert f"--role-id {role_id}" in command
        assert f"--agent-config-dir /mesh/project/state/v3/agent-configs/{role_id}/1" in command


def test_linuxch_overlay_restarts_every_role_service() -> None:
    overlay = _linuxch_overlay_text()

    for role_id in V3_LIVE_ROLE_IDS:
        service_name = f"agentic-mesh-dev-{role_id}-1"
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
