from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from agentic_mesh_v4.cli import _watchdog_services
from agentic_mesh_v4.compose import render_compose
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.lifecycle import ComposeLifecycle


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


def test_v4_agent_image_pins_codex_cli_required_by_configured_model() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "npm install -g @openai/codex@0.144.3" in dockerfile
    assert "@openai/codex@0.135.0" not in dockerfile
    assert " gh " in dockerfile


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
    watchdog = compose["services"]["watchdog"]

    assert "agentic_mesh_v4.cli" in runtime["command"]
    assert "serve --host 0.0.0.0 --port 8100" in runtime["command"]
    assert runtime["env_file"] == [{"path": ".env", "required": False}]
    assert runtime["ports"] == ["${AGENTIC_MESH_V4_STATUS_PORT:-8100}:8100"]
    assert runtime["environment"]["PYTHONPATH"] == "/mesh/system/src"
    assert runtime["environment"]["AGENTIC_MESH_ENFORCE_RUNTIME_TOPOLOGY"] == 1
    assert runtime["environment"]["AGENTIC_MESH_RUNTIME_SYSTEM_PATH"] == "/mesh/system"
    assert runtime["environment"]["AGENTIC_MESH_RUNTIME_WORKSPACE_PATH"] == "/mesh/workspaces/agentic-mesh"
    assert "/var/run/docker.sock:/var/run/docker.sock" in runtime["volumes"]

    assert "agentic_mesh_v4.cli" in dispatcher["command"]
    assert "dispatch-loop" in dispatcher["command"]
    assert "--project-id agentic-mesh-dev" in dispatcher["command"]
    assert dispatcher["env_file"] == [{"path": ".env", "required": False}]
    assert dispatcher["environment"]["PYTHONPATH"] == "/mesh/system/src"
    assert dispatcher["environment"]["AGENTIC_MESH_ENFORCE_RUNTIME_TOPOLOGY"] == 1
    assert dispatcher["environment"]["AGENTIC_MESH_CODEX_WS_READ_TIMEOUT_SECONDS"] == (
        "${AGENTIC_MESH_CODEX_WS_READ_TIMEOUT_SECONDS:-30}"
    )
    assert "--wake" in dispatcher["command"]
    assert "--active-turn-stale-seconds ${AGENTIC_MESH_ACTIVE_TURN_STALE_SECONDS:-900}" in dispatcher["command"]
    assert "--dispatch-workers ${AGENTIC_MESH_DISPATCH_WORKERS:-8}" in dispatcher["command"]
    assert "--compose-file /mesh/project/deploy/compose/docker-compose.v4.yml" in dispatcher["command"]
    assert "--compose-file /mesh/project/deploy/compose/docker-compose.linuxch.yml" in dispatcher["command"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in dispatcher["volumes"]

    assert "agentic_mesh_v4.cli" in watchdog["command"]
    assert "watchdog-loop" in watchdog["command"]
    assert watchdog["env_file"] == [{"path": ".env", "required": False}]
    assert watchdog["environment"]["PYTHONPATH"] == "/mesh/system/src"
    assert watchdog["environment"]["AGENTIC_MESH_ENFORCE_RUNTIME_TOPOLOGY"] == 1
    assert "--service runtime" in watchdog["command"]
    assert "--service dispatcher" in watchdog["command"]
    assert "--compose-file /mesh/project/deploy/compose/docker-compose.v4.yml" in watchdog["command"]
    assert "--compose-file /mesh/project/deploy/compose/docker-compose.linuxch.yml" in watchdog["command"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in watchdog["volumes"]


def test_dogfood_compose_defines_full_lazy_role_app_server_team() -> None:
    compose = _load_dogfood_compose()
    service_names = set(compose["services"])

    for role_id in V4_ROLE_IDS:
        service_name = f"agentic-mesh-dev-{role_id}-1"
        assert service_name in service_names
        service = compose["services"][service_name]
        assert service["profiles"] == ["roles"]
        assert service["cap_add"] == ["SYS_ADMIN"]
        assert service["security_opt"] == ["seccomp=unconfined", "apparmor=unconfined"]
        assert service["working_dir"] == "/mesh/agent-workspace"
        assert "codex -c model=gpt-5.6-sol" in service["command"]
        assert "-c model_reasoning_effort=high" in service["command"]
        assert "-c plan_mode_reasoning_effort=xhigh" in service["command"]
        assert "-c show_raw_agent_reasoning=false" in service["command"]
        assert "app-server" in service["command"]
        assert "mkdir -p /mesh/agent-workspace /documents/work-items" in service["command"]
        assert "chmod -R a+rwX /mesh/agent-workspace /documents" in service["command"]
        assert "for d in memories tmp sessions cache shell_snapshots" in service["command"]
        assert "chmod -R a+rwX /mesh/worker-auth/codex/$$d" in service["command"]
        assert "chmod -R a+rwX /mesh/worker-auth/codex " not in service["command"]
        assert "cp /mesh/agent/AGENTS.md /mesh/agent-workspace/AGENTS.md" in service["command"]
        assert "python -m agentic_mesh_v4.safe_output_proxy" in service["command"]
        assert f"--role-id {role_id}" in service["command"]
        assert "safe-output.sock" in service["command"]
        assert service["environment"]["AGENTIC_MESH_SAFE_OUTPUT_SOCKET"] == (
            "/mesh/agent-workspace/.agentic-mesh/safe-output.sock"
        )
        assert "--ws-auth capability-token" in service["command"]
        assert "--ws-token-file /mesh/agent/ws-token" in service["command"]
        assert service["environment"]["AGENTIC_MESH_ROLE_ID"] == role_id
        assert service["environment"]["AGENTIC_MESH_ROLE_INSTANCE_ID"] == f"agentic-mesh-dev.{role_id}.1"
        assert service["environment"]["PYTHONPATH"] == "/mesh/system/src"
        volumes = "\n".join(service["volumes"])
        assert f"/state/v4/agent-configs/{role_id}/1:/mesh/agent" in volumes
        assert f"/state/v4/agent-workspaces/{role_id}/1:/mesh/agent-workspace" in volumes
        assert ":/mesh/system:ro" in volumes
        assert ":/mesh/workspaces/agentic-mesh" in volumes


def test_linuxch_overlay_restarts_only_v4_runtime_services() -> None:
    overlay = _linuxch_overlay_text()

    assert "  runtime:\n    restart: unless-stopped" in overlay
    assert "  dispatcher:\n    restart: unless-stopped" in overlay
    assert "  watchdog:\n    restart: unless-stopped" in overlay
    assert overlay.count("PYTHONPATH: /mesh/system/src") >= len(V4_ROLE_IDS) + 3
    for role_id in V4_ROLE_IDS:
        assert f"  agentic-mesh-dev-{role_id}-1:" in overlay
    assert "  otel-collector:" in overlay
    assert "v3-nats:" not in overlay
    assert "v3-supervisor:" not in overlay
    assert "run-agent-service" not in overlay


def test_linuxch_deploy_script_preserves_v4_live_environment() -> None:
    script = Path("scripts/deploy-linuxch-compose.sh").read_text(encoding="utf-8")

    for name in [
        "AGENTIC_MESH_ONEDRIVE_TOKEN",
        "AGENTIC_MESH_ONEDRIVE_DRIVE_ID",
        "AGENTIC_MESH_GRAPH_CLIENT_ID",
        "AGENTIC_MESH_GRAPH_TENANT_ID",
        "AGENTIC_MESH_GRAPH_REFRESH_TOKEN",
        "AGENTIC_MESH_SPONSOR_TEAMS_USER_ID",
        "AGENTIC_MESH_TEAMS_TOKEN",
        "AGENTIC_MESH_TEAMS_PUBLIC_ENDPOINT",
        "AGENTIC_MESH_TEAMS_BOT_SERVICE_URL",
        "AGENTIC_MESH_TENANT_ID",
        "AGENTIC_MESH_PROJECT_TEAM_ID",
        "AGENTIC_MESH_PROJECT_CHANNEL_ID",
        "AGENTIC_MESH_SPONSOR_AAD_OBJECT_ID",
        "AGENTIC_MESH_V4_STATUS_PORT",
        "AGENTIC_MESH_GIT_SSH_HOST_PATH",
    ]:
        assert f"export {name}" in script
        assert f"{name}=${name}" in script
    assert "export AGENTIC_MESH_GRAPH_SCOPES" in script
    assert "AGENTIC_MESH_GRAPH_SCOPES='$AGENTIC_MESH_GRAPH_SCOPES'" in script
    assert "AGENTIC_MESH_V3_STATUS_PORT" not in script
    assert "AGENTIC_MESH_NATS_STATE_HOST_PATH" not in script


def test_dogfood_compose_env_example_lists_required_v4_live_inputs() -> None:
    env_example = Path("examples/projects/agentic-mesh-dev/deploy/compose/.env.example").read_text(
        encoding="utf-8"
    )

    for name in [
        "AGENTIC_MESH_ONEDRIVE_TOKEN",
        "AGENTIC_MESH_ONEDRIVE_DRIVE_ID",
        "AGENTIC_MESH_GRAPH_CLIENT_ID",
        "AGENTIC_MESH_GRAPH_TENANT_ID",
        "AGENTIC_MESH_GRAPH_REFRESH_TOKEN",
        "AGENTIC_MESH_GRAPH_SCOPES",
        "AGENTIC_MESH_SPONSOR_TEAMS_USER_ID",
        "AGENTIC_MESH_TEAMS_TOKEN",
        "AGENTIC_MESH_TEAMS_PUBLIC_ENDPOINT",
        "AGENTIC_MESH_TEAMS_BOT_SERVICE_URL",
        "AGENTIC_MESH_TENANT_ID",
        "AGENTIC_MESH_PROJECT_TEAM_ID",
        "AGENTIC_MESH_PROJECT_CHANNEL_ID",
        "AGENTIC_MESH_SPONSOR_AAD_OBJECT_ID",
        "AGENTIC_MESH_V4_STATUS_PORT",
        "AGENTIC_MESH_WATCHDOG_INTERVAL_SECONDS",
        "AGENTIC_MESH_ACTIVE_TURN_STALE_SECONDS",
        "AGENTIC_MESH_GIT_SSH_HOST_PATH",
    ]:
        assert f"{name}=" in env_example
    assert "AGENTIC_MESH_V3_STATUS_PORT" not in env_example
    assert "AGENTIC_MESH_NATS_STATE_HOST_PATH" not in env_example


def test_linuxch_release_script_defaults_to_v4_services() -> None:
    script = Path("scripts/release-linuxch-compose.sh").read_text(encoding="utf-8")

    assert "AGENTIC_MESH_SYSTEM_HOST_PATH=$(linuxch_host_path_or_default" in script
    assert "/home/nich/agentic-mesh" in script
    assert (
        "AGENTIC_MESH_WORKSPACE_HOST_PATH:=$AGENTIC_MESH_PROJECT_HOST_PATH/target-repos/agentic-mesh"
        not in script
    )
    assert "linuxch_host_path_or_default" in script
    assert "AGENTIC_MESH_COMPOSE_STAGE_DIR" in script
    assert "AGENTIC_MESH_RELEASE_SERVICES:=runtime dispatcher watchdog otel-collector" in script
    assert "AGENTIC_MESH_ROLE_SERVICES:=" in script
    assert script.index("render-compose") < script.index(
        "--profile build-image build base-agent-image ops-agent-image dev-agent-image qa-agent-image"
    )
    assert "Refusing to release: V4 compose points control-plane PYTHONPATH at a mutable project or workspace source tree." in script
    assert 'cp "$REPO_ROOT/examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml"' in script
    assert '"$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/compose/docker-compose.linuxch.yml"' in script
    assert "AGENTIC_MESH_RESTRICTED_SAFE_OUTPUT_CONFIG_HOST_PATH" in script
    assert "AGENTIC_MESH_GIT_SSH_HOST_PATH" in script
    assert "-o IdentitiesOnly=yes -i \"$AGENTIC_MESH_GIT_SSH_HOST_PATH/id_rsa\" -T git@github.com" in script
    assert "Refusing to release: the approved shared Git SSH identity cannot authenticate to GitHub." in script
    assert "restricted-safe-output-config.toml" in script
    assert "cp --remove-destination" in script
    assert 'grep -Eq "/mesh/(workspaces/agentic-mesh|project)/src"' in script
    assert "PYTHONPATH: /mesh/system/src" in script
    assert "--profile build-image build base-agent-image ops-agent-image dev-agent-image qa-agent-image" in script
    assert "--profile v4 stop $AGENTIC_MESH_RELEASE_SERVICES" in script
    assert "--profile v4 up -d --force-recreate --remove-orphans $AGENTIC_MESH_RELEASE_SERVICES" in script
    assert "--profile roles stop $AGENTIC_MESH_ROLE_SERVICES" in script
    assert "--profile roles rm -f $AGENTIC_MESH_ROLE_SERVICES" in script
    assert script.index("--profile roles rm -f $AGENTIC_MESH_ROLE_SERVICES") < script.index(
        "--profile v4 up -d --force-recreate --remove-orphans $AGENTIC_MESH_RELEASE_SERVICES"
    )
    assert 'if [ "$AGENTIC_MESH_MIN_WARM_ROLE_INSTANCES" != "0" ]; then' in script
    assert "--profile roles up -d --force-recreate $AGENTIC_MESH_ROLE_SERVICES" in script
    assert "project-v4.yaml" in script
    assert "state/v4/agent-configs" in script
    assert "agentic_mesh_v4.cli" in script
    for role_id in V4_ROLE_IDS:
        assert f"agentic-mesh-dev-{role_id}-1" in script
    assert "v3-nats" not in script
    assert "v3-supervisor" not in script


def test_linuxch_deploy_script_keeps_agent_workspace_separate_from_system_checkout() -> None:
    script = Path("scripts/deploy-linuxch-compose.sh").read_text(encoding="utf-8")
    env_example = Path("examples/projects/agentic-mesh-dev/deploy/compose/.env.example").read_text(
        encoding="utf-8"
    )

    assert (
        "AGENTIC_MESH_WORKSPACE_HOST_PATH=\"${AGENTIC_MESH_WORKSPACE_HOST_PATH:-$AGENTIC_MESH_PROJECT_HOST_PATH/target-repos/agentic-mesh}\""
        in script
    )
    assert "AGENTIC_MESH_ALLOW_WORKSPACE_EQUALS_SYSTEM" in script
    assert "reject_container_bind_path" in script
    assert "require_regular_file" in script
    assert "The deployment will not allow Docker to create a directory" in script
    assert "AGENTIC_MESH_RESTRICTED_SAFE_OUTPUT_CONFIG_HOST_PATH" in script
    assert "Refusing to deploy: staged V4 compose points control-plane PYTHONPATH at a mutable project or workspace source tree." in script
    assert "Refusing to deploy: effective V4 compose points control-plane PYTHONPATH at a mutable project or workspace source tree." in script
    assert '"$STAGE_DIR/docker-compose.yml" "$STAGE_DIR/docker-compose.linuxch.yml"' in script
    assert 'grep -Eq "PYTHONPATH:.*/mesh/(workspaces/agentic-mesh|project)/src"' in script
    assert "validate_effective_compose docker" in script
    assert "Regenerate compose from the system source before deploying." in script
    assert "which is an in-container path, not a Docker host bind path" in script
    assert "Refusing to deploy: AGENTIC_MESH_WORKSPACE_HOST_PATH resolves to AGENTIC_MESH_SYSTEM_HOST_PATH." in script
    assert (
        "AGENTIC_MESH_WORKSPACE_HOST_PATH=/home/nich/agentic-mesh-projects/agentic-mesh-dev/target-repos/agentic-mesh"
        in env_example
    )
    assert "AGENTIC_MESH_WORKSPACE_HOST_PATH=/home/nich/agentic-mesh\n" not in env_example


@pytest.mark.parametrize("source_kind", ["missing", "directory"])
def test_linuxch_deploy_fails_before_compose_for_non_file_restricted_config(
    tmp_path, source_kind
) -> None:
    project_root = tmp_path / "project"
    config_path = project_root / "deploy" / "codex" / "restricted-safe-output-config.toml"
    if source_kind == "directory":
        config_path.mkdir(parents=True)
    env = {
        **os.environ,
        "AGENTIC_MESH_PROJECT_HOST_PATH": str(project_root),
        "AGENTIC_MESH_COMPOSE_SRC": str(project_root / "deploy" / "compose"),
    }

    result = subprocess.run(
        ["sh", "scripts/deploy-linuxch-compose.sh", "up", "-d"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert "must exist as a regular file before Docker Compose runs" in result.stderr
    assert "will not allow Docker to create a directory" in result.stderr
    if source_kind == "missing":
        assert not config_path.exists()


def test_linuxch_release_checks_directory_config_before_runtime_work() -> None:
    script = Path("scripts/release-linuxch-compose.sh").read_text(encoding="utf-8")

    check = "restricted safe-output config target is a directory"
    assert check in script
    assert script.index(check) < script.index('cd "$REPO_ROOT"')


def test_v4_compose_lifecycle_env_file_overrides_container_environment(
    tmp_path, monkeypatch
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AGENTIC_MESH_PROJECT_HOST_PATH=/home/nich/agentic-mesh-projects/agentic-mesh-dev",
                "AGENTIC_MESH_SYSTEM_HOST_PATH=/home/nich/agentic-mesh",
                "AGENTIC_MESH_WORKSPACE_HOST_PATH=/home/nich/agentic-mesh-projects/agentic-mesh-dev/target-repos/agentic-mesh",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTIC_MESH_PROJECT_HOST_PATH", "/mesh/project")
    monkeypatch.setenv("AGENTIC_MESH_SYSTEM_HOST_PATH", "/mesh/system")
    monkeypatch.setenv("AGENTIC_MESH_WORKSPACE_HOST_PATH", "/mesh/workspaces/agentic-mesh")
    commands: list[list[str]] = []
    calls: list[dict[str, str]] = []

    def fake_run(*args, **kwargs):
        commands.append(args[0])
        calls.append(kwargs["env"])
        if args[0][-3:] == ["config", "--format", "json"]:
            return subprocess.CompletedProcess(
                args[0],
                0,
                '{"services":{"agentic-mesh-dev-project-manager-1":{"image":"agentic-mesh:base-agent","volumes":[]}}}',
                "",
            )
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ComposeLifecycle(
        compose_files=(compose_file,),
        env_file=env_file,
        working_directory=tmp_path,
    ).wake_service("agentic-mesh-dev-project-manager-1")

    assert calls
    assert commands[0][2:4] == ["--profile", "*"]
    assert commands[1][-4:] == [
        "up",
        "-d",
        "--no-deps",
        "agentic-mesh-dev-project-manager-1",
    ]
    assert "--no-recreate" not in commands[1]
    assert calls[0]["AGENTIC_MESH_PROJECT_HOST_PATH"] == (
        "/home/nich/agentic-mesh-projects/agentic-mesh-dev"
    )
    assert calls[0]["AGENTIC_MESH_SYSTEM_HOST_PATH"] == "/home/nich/agentic-mesh"
    assert calls[0]["AGENTIC_MESH_WORKSPACE_HOST_PATH"] == (
        "/home/nich/agentic-mesh-projects/agentic-mesh-dev/target-repos/agentic-mesh"
    )


def test_v4_compose_lifecycle_recreation_rejects_non_regular_required_bind(
    tmp_path, monkeypatch
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        commands.append(command)
        if command[-3:] == ["config", "--format", "json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "services": {
                            "agentic-mesh-dev-business-analyst-1": {
                                "image": "agentic-mesh:base-agent",
                                "volumes": [
                                    {
                                        "type": "bind",
                                        "source": "/host/missing-or-directory.toml",
                                        "target": "/etc/codex/config.toml",
                                        "bind": {"create_host_path": False},
                                    }
                                ],
                            }
                        }
                    }
                ),
                "",
            )
        if command[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(command, 1, "", "not a regular file")
        raise AssertionError(f"service creation must not run after failed bind preflight: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Required regular-file bind failed preflight"):
        ComposeLifecycle(
            compose_files=(compose_file,),
            working_directory=tmp_path,
        ).wake_service("agentic-mesh-dev-business-analyst-1")

    assert len(commands) == 2
    assert commands[1][:2] == ["docker", "run"]
    assert any(
        value.startswith("type=bind,source=/host/missing-or-directory.toml")
        for value in commands[1]
    )


def test_linuxch_overlay_marks_restricted_config_as_required_file_bind() -> None:
    overlay = _linuxch_overlay_text()
    required_roles = {
        "product-manager",
        "business-analyst",
        "research-analyst",
        "security-architect",
        "ux-designer",
        "qa-engineer",
    }

    assert "x-restricted-safe-output-config: &restricted-safe-output-config" in overlay
    assert "target: /etc/codex/config.toml" in overlay
    assert "create_host_path: false" in overlay
    assert overlay.count("- *restricted-safe-output-config") == len(required_roles)
    for role_id in required_roles:
        marker = f"  agentic-mesh-dev-{role_id}-1:"
        start = overlay.index(marker)
        next_service = overlay.find("\n  agentic-mesh-dev-", start + len(marker))
        section = overlay[start : next_service if next_service != -1 else None]
        assert "- *restricted-safe-output-config" in section


def test_v4_watchdog_restarts_missing_control_plane_services() -> None:
    class FakeLifecycle:
        def __init__(self) -> None:
            self.started: list[str] = []

        def is_service_running(self, service_name: str) -> bool:
            return service_name == "runtime"

        def wake_service(self, service_name: str) -> None:
            self.started.append(service_name)

    lifecycle = FakeLifecycle()

    checked = _watchdog_services(lifecycle=lifecycle, services=("runtime", "dispatcher"))  # type: ignore[arg-type]

    assert checked == [
        {"service": "runtime", "state": "running"},
        {"service": "dispatcher", "state": "restarted"},
    ]
    assert lifecycle.started == ["dispatcher"]


def test_v4_dogfood_project_config_exists_for_compose_profile() -> None:
    assert V4_PROJECT_FILE.exists()


def test_linuxch_overlay_is_valid_for_v4_profile() -> None:
    rendered_compose = render_compose(load_project_config(V4_PROJECT_FILE))
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "v4",
            "-f",
            "-",
            "-f",
            "examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml",
            "config",
            "--quiet",
        ],
        check=False,
        capture_output=True,
        text=True,
        input=rendered_compose,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def _load_dogfood_compose() -> dict[str, object]:
    compose = yaml.safe_load(render_compose(load_project_config(V4_PROJECT_FILE)))
    assert isinstance(compose, dict)
    return compose


def _linuxch_overlay_text() -> str:
    overlay_path = Path("examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml")
    return overlay_path.read_text(encoding="utf-8")
