from __future__ import annotations

from agentic_mesh_v4.config import V4ProjectConfig


OPS_ROLES = {"project-manager", "delivery-manager", "platform-engineer", "release-manager"}
DEV_ROLES = {"engineering"}
QA_ROLES = {"qa-engineer"}
SSH_ROLES = OPS_ROLES
DOCKER_SOCKET_ROLES = OPS_ROLES | DEV_ROLES


def render_compose(project_config: V4ProjectConfig) -> str:
    lines: list[str] = [
        "services:",
        "  base-agent-image:",
        "    image: ${AGENTIC_MESH_BASE_IMAGE_TAG:-agentic-mesh:base-agent}",
        "    build:",
        "      context: ${AGENTIC_MESH_RUNTIME_BUILD_CONTEXT:-../../../../..}",
        "      target: base-agent",
        "    profiles:",
        "      - build-image",
        "",
        "  ops-agent-image:",
        "    image: ${AGENTIC_MESH_OPS_IMAGE_TAG:-agentic-mesh:ops-agent}",
        "    build:",
        "      context: ${AGENTIC_MESH_RUNTIME_BUILD_CONTEXT:-../../../../..}",
        "      target: ops-agent",
        "    profiles:",
        "      - build-image",
        "",
        "  dev-agent-image:",
        "    image: ${AGENTIC_MESH_DEV_IMAGE_TAG:-agentic-mesh:dev-agent}",
        "    build:",
        "      context: ${AGENTIC_MESH_RUNTIME_BUILD_CONTEXT:-../../../../..}",
        "      target: dev-agent",
        "    profiles:",
        "      - build-image",
        "",
        "  qa-agent-image:",
        "    image: ${AGENTIC_MESH_QA_IMAGE_TAG:-agentic-mesh:qa-agent}",
        "    build:",
        "      context: ${AGENTIC_MESH_RUNTIME_BUILD_CONTEXT:-../../../../..}",
        "      target: qa-agent",
        "    profiles:",
        "      - build-image",
        "",
        "  runtime:",
        "    image: ${AGENTIC_MESH_OPS_IMAGE_TAG:-agentic-mesh:ops-agent}",
        "    env_file:",
        "      - path: .env",
        "        required: false",
        "    command: python -m agentic_mesh_v4.cli --db /mesh/project/state/v4/agentic-mesh-v4.sqlite3 --project-config /mesh/project/agentic-mesh/project-v4.yaml serve --host 0.0.0.0 --port 8100 --document-root /documents",
        "    ports:",
        "      - \"${AGENTIC_MESH_V4_STATUS_PORT:-8100}:8100\"",
        "    environment:",
        "      AGENTIC_MESH_RUNTIME_VERSION: v4",
        "      AGENTIC_MESH_URL_ROOT: ${AGENTIC_MESH_URL_ROOT:-http://linuxch:8100}",
        "      PYTHONPATH: /mesh/workspaces/agentic-mesh/src",
        "    volumes:",
        "      - ${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}:/mesh/system:ro",
        "      - ${AGENTIC_MESH_DOCUMENTS_HOST_PATH:-../documents}:/documents",
        "      - ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}:/mesh/project",
        "      - ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}:/mesh/workspaces/agentic-mesh",
        "      - /var/run/docker.sock:/var/run/docker.sock",
        "",
        "  dispatcher:",
        "    image: ${AGENTIC_MESH_OPS_IMAGE_TAG:-agentic-mesh:ops-agent}",
        "    env_file:",
        "      - path: .env",
        "        required: false",
        "    command: python -m agentic_mesh_v4.cli --db /mesh/project/state/v4/agentic-mesh-v4.sqlite3 --project-config /mesh/project/agentic-mesh/project-v4.yaml dispatch-loop --agent-config-root /mesh/project/state/v4/agent-configs --compose-file /mesh/project/deploy/compose/docker-compose.v4.yml --compose-file /mesh/project/deploy/compose/docker-compose.linuxch.yml --compose-project-name ${COMPOSE_PROJECT_NAME:-agentic-mesh} --compose-env-file /mesh/project/deploy/compose/.env --compose-working-directory /mesh/project/deploy/compose --active-turn-stale-seconds ${AGENTIC_MESH_ACTIVE_TURN_STALE_SECONDS:-900} --wake",
        "    environment:",
        "      AGENTIC_MESH_RUNTIME_VERSION: v4",
        "      AGENTIC_MESH_URL_ROOT: ${AGENTIC_MESH_URL_ROOT:-http://linuxch:8100}",
        "      PYTHONPATH: /mesh/workspaces/agentic-mesh/src",
        "      AGENTIC_MESH_SYSTEM_HOST_PATH: ${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}",
        "      AGENTIC_MESH_PROJECT_HOST_PATH: ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}",
        "      AGENTIC_MESH_WORKSPACE_HOST_PATH: ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}",
        "      AGENTIC_MESH_CODEX_HOME_HOST_PATH: ${AGENTIC_MESH_CODEX_HOME_HOST_PATH:-../../state/worker_mounts/codex-agentic-mesh-dev-team-home-q}",
        "      AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH: ${AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH:-../../../../../config/otel/collector.yaml}",
        "      AGENTIC_MESH_BASE_IMAGE_TAG: ${AGENTIC_MESH_BASE_IMAGE_TAG:-agentic-mesh:base-agent}",
        "      AGENTIC_MESH_OPS_IMAGE_TAG: ${AGENTIC_MESH_OPS_IMAGE_TAG:-agentic-mesh:ops-agent}",
        "      AGENTIC_MESH_DEV_IMAGE_TAG: ${AGENTIC_MESH_DEV_IMAGE_TAG:-agentic-mesh:dev-agent}",
        "      AGENTIC_MESH_QA_IMAGE_TAG: ${AGENTIC_MESH_QA_IMAGE_TAG:-agentic-mesh:qa-agent}",
        "      COMPOSE_PROJECT_NAME: ${COMPOSE_PROJECT_NAME:-agentic-mesh}",
        "    volumes:",
        "      - ${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}:/mesh/system:ro",
        "      - ${AGENTIC_MESH_DOCUMENTS_HOST_PATH:-../documents}:/documents",
        "      - ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}:/mesh/project",
        "      - ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}:/mesh/workspaces/agentic-mesh",
        "      - /var/run/docker.sock:/var/run/docker.sock",
        "",
        "  watchdog:",
        "    image: ${AGENTIC_MESH_OPS_IMAGE_TAG:-agentic-mesh:ops-agent}",
        "    env_file:",
        "      - path: .env",
        "        required: false",
        "    command: python -m agentic_mesh_v4.cli --db /mesh/project/state/v4/agentic-mesh-v4.sqlite3 --project-config /mesh/project/agentic-mesh/project-v4.yaml watchdog-loop --compose-file /mesh/project/deploy/compose/docker-compose.v4.yml --compose-file /mesh/project/deploy/compose/docker-compose.linuxch.yml --compose-project-name ${COMPOSE_PROJECT_NAME:-agentic-mesh} --compose-env-file /mesh/project/deploy/compose/.env --compose-working-directory /mesh/project/deploy/compose --service runtime --service dispatcher --poll-interval-seconds ${AGENTIC_MESH_WATCHDOG_INTERVAL_SECONDS:-30}",
        "    environment:",
        "      AGENTIC_MESH_RUNTIME_VERSION: v4",
        "      AGENTIC_MESH_URL_ROOT: ${AGENTIC_MESH_URL_ROOT:-http://linuxch:8100}",
        "      PYTHONPATH: /mesh/workspaces/agentic-mesh/src",
        "      AGENTIC_MESH_SYSTEM_HOST_PATH: ${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}",
        "      AGENTIC_MESH_PROJECT_HOST_PATH: ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}",
        "      AGENTIC_MESH_WORKSPACE_HOST_PATH: ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}",
        "      AGENTIC_MESH_CODEX_HOME_HOST_PATH: ${AGENTIC_MESH_CODEX_HOME_HOST_PATH:-../../state/worker_mounts/codex-agentic-mesh-dev-team-home-q}",
        "      AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH: ${AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH:-../../../../../config/otel/collector.yaml}",
        "      AGENTIC_MESH_BASE_IMAGE_TAG: ${AGENTIC_MESH_BASE_IMAGE_TAG:-agentic-mesh:base-agent}",
        "      AGENTIC_MESH_OPS_IMAGE_TAG: ${AGENTIC_MESH_OPS_IMAGE_TAG:-agentic-mesh:ops-agent}",
        "      AGENTIC_MESH_DEV_IMAGE_TAG: ${AGENTIC_MESH_DEV_IMAGE_TAG:-agentic-mesh:dev-agent}",
        "      AGENTIC_MESH_QA_IMAGE_TAG: ${AGENTIC_MESH_QA_IMAGE_TAG:-agentic-mesh:qa-agent}",
        "      COMPOSE_PROJECT_NAME: ${COMPOSE_PROJECT_NAME:-agentic-mesh}",
        "    volumes:",
        "      - ${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}:/mesh/system:ro",
        "      - ${AGENTIC_MESH_DOCUMENTS_HOST_PATH:-../documents}:/documents",
        "      - ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}:/mesh/project",
        "      - ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}:/mesh/workspaces/agentic-mesh",
        "      - /var/run/docker.sock:/var/run/docker.sock",
        "",
        "  otel-collector:",
        "    image: otel/opentelemetry-collector-contrib:0.101.0",
        "    command: [\"--config=/etc/otelcol/config.yaml\"]",
        "    volumes:",
        "      - ${AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH:-../../../../../config/otel/collector.yaml}:/etc/otelcol/config.yaml:ro",
        "    ports:",
        "      - \"4317:4317\"",
        "      - \"4318:4318\"",
        "",
    ]
    for role in project_config.roles:
        lines.extend(_role_service(role_id=role.role_id, service_name=role.service_name, port=role.codex_port))
        lines.append("")
    rendered = "\n".join(lines).rstrip() + "\n"
    validate_v4_compose(rendered)
    return rendered


def validate_v4_compose(rendered: str) -> None:
    forbidden = ("v3-nats", "v3-supervisor", "run-agent-service", "nats://", "agentic_mesh_v3.cli")
    found = [item for item in forbidden if item in rendered]
    if found:
        raise ValueError(f"V4 compose contains V3-only components: {', '.join(found)}")


def _role_service(*, role_id: str, service_name: str, port: int) -> list[str]:
    command = f"codex app-server --listen ws://0.0.0.0:{port} --ws-auth capability-token --ws-token-file /mesh/agent/ws-token"
    if role_id in SSH_ROLES:
        command = (
            "sh -lc 'mkdir -p /root/.ssh; "
            "if [ -d /mesh/home/.ssh ]; then cp -r /mesh/home/.ssh/. /root/.ssh/; fi; "
            "if [ -f /root/.ssh/config ]; then sed -i \"s#/mesh/home/.ssh#/root/.ssh#g\" /root/.ssh/config; fi; "
            "chmod 700 /root/.ssh; "
            "find /root/.ssh -type f -exec chmod 600 {} \\; 2>/dev/null || true; "
            f"exec codex app-server --listen ws://0.0.0.0:{port} --ws-auth capability-token --ws-token-file /mesh/agent/ws-token'"
        )
    lines = [
        f"  {service_name}:",
        f"    image: {_role_image(role_id)}",
        "    env_file:",
        "      - path: .env",
        "        required: false",
        "    profiles:",
        "      - roles",
        "    cap_add:",
        "      - SYS_ADMIN",
        "    security_opt:",
        "      - seccomp=unconfined",
        "      - apparmor=unconfined",
        "    working_dir: /mesh/agent",
        f"    command: {command}",
        "    environment:",
        f"      AGENTIC_MESH_ROLE_ID: {role_id}",
        f"      AGENTIC_MESH_ROLE_INSTANCE_ID: agentic-mesh-dev.{role_id}.1",
        f"      AGENTIC_MESH_TOOL_PROFILE: {_tool_profile(role_id)}",
        "      PYTHONPATH: /mesh/workspaces/agentic-mesh/src",
        "      CODEX_HOME: /mesh/worker-auth/codex",
        "      HOME: /mesh/home",
        "    volumes:",
        f"      - ${{AGENTIC_MESH_PROJECT_HOST_PATH:-../..}}/state/v4/agent-configs/{role_id}/1:/mesh/agent",
        "      - ${AGENTIC_MESH_DOCUMENTS_HOST_PATH:-../documents}:/documents",
        "      - ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}:/mesh/project",
        "      - ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}:/mesh/workspaces/agentic-mesh",
        "      - ${AGENTIC_MESH_CODEX_HOME_HOST_PATH:-../../state/worker_mounts/codex-agentic-mesh-dev-team-home-q}:/mesh/worker-auth/codex",
    ]
    if role_id in DOCKER_SOCKET_ROLES:
        lines.append("      - /var/run/docker.sock:/var/run/docker.sock")
    if role_id in SSH_ROLES:
        lines.extend([
            "      - ${AGENTIC_MESH_PROJECT_ENV_FILE_HOST_PATH:-.env}:/mesh/home/.env:ro",
            "      - ${AGENTIC_MESH_PROJECT_MANAGER_SSH_HOST_PATH:-../../state/worker_mounts/project-manager/.ssh}:/mesh/home/.ssh:ro",
        ])
    return lines


def _role_image(role_id: str) -> str:
    if role_id in OPS_ROLES:
        return "${AGENTIC_MESH_OPS_IMAGE_TAG:-agentic-mesh:ops-agent}"
    if role_id in DEV_ROLES:
        return "${AGENTIC_MESH_DEV_IMAGE_TAG:-agentic-mesh:dev-agent}"
    if role_id in QA_ROLES:
        return "${AGENTIC_MESH_QA_IMAGE_TAG:-agentic-mesh:qa-agent}"
    return "${AGENTIC_MESH_BASE_IMAGE_TAG:-agentic-mesh:base-agent}"


def _tool_profile(role_id: str) -> str:
    if role_id in OPS_ROLES:
        return "ops-agent"
    if role_id in DEV_ROLES:
        return "dev-agent"
    if role_id in QA_ROLES:
        return "qa-agent"
    return "base-agent"
