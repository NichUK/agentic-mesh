from __future__ import annotations

from agentic_mesh_v4.config import V4ProjectConfig


def render_compose(project_config: V4ProjectConfig) -> str:
    lines: list[str] = [
        "services:",
        "  runtime-image:",
        "    image: ${AGENTIC_MESH_IMAGE_TAG:-agentic-mesh:local}",
        "    build:",
        "      context: ${AGENTIC_MESH_RUNTIME_BUILD_CONTEXT:-../../../../..}",
        "    profiles:",
        "      - build-image",
        "",
        "  runtime:",
        "    image: ${AGENTIC_MESH_IMAGE_TAG:-agentic-mesh:local}",
        "    env_file:",
        "      - path: .env",
        "        required: false",
        "    command: python -m agentic_mesh_v4.cli --db /mesh/project/state/v4/agentic-mesh-v4.sqlite3 --project-config /mesh/project/agentic-mesh/project-v4.yaml serve --host 0.0.0.0 --port 8100",
        "    ports:",
        "      - \"${AGENTIC_MESH_V4_STATUS_PORT:-8100}:8100\"",
        "    environment:",
        "      AGENTIC_MESH_RUNTIME_VERSION: v4",
        "      AGENTIC_MESH_URL_ROOT: ${AGENTIC_MESH_URL_ROOT:-http://linuxch:8100}",
        "    volumes:",
        "      - ${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}:/mesh/system:ro",
        "      - ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}:/mesh/project",
        "      - ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}:/mesh/workspaces/agentic-mesh",
        "      - /var/run/docker.sock:/var/run/docker.sock",
        "",
        "  dispatcher:",
        "    image: ${AGENTIC_MESH_IMAGE_TAG:-agentic-mesh:local}",
        "    env_file:",
        "      - path: .env",
        "        required: false",
        "    command: python -m agentic_mesh_v4.cli --db /mesh/project/state/v4/agentic-mesh-v4.sqlite3 --project-config /mesh/project/agentic-mesh/project-v4.yaml dispatch-loop --agent-config-root /mesh/project/state/v4/agent-configs --compose-file /mesh/project/deploy/compose/docker-compose.v4.yml --compose-file /mesh/project/deploy/compose/docker-compose.linuxch.yml --compose-project-name ${COMPOSE_PROJECT_NAME:-agentic-mesh} --compose-env-file /mesh/project/deploy/compose/.env --compose-working-directory /mesh/project/deploy/compose --wake",
        "    environment:",
        "      AGENTIC_MESH_RUNTIME_VERSION: v4",
        "      AGENTIC_MESH_URL_ROOT: ${AGENTIC_MESH_URL_ROOT:-http://linuxch:8100}",
        "      AGENTIC_MESH_SYSTEM_HOST_PATH: ${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}",
        "      AGENTIC_MESH_PROJECT_HOST_PATH: ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}",
        "      AGENTIC_MESH_WORKSPACE_HOST_PATH: ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}",
        "      AGENTIC_MESH_CODEX_HOME_HOST_PATH: ${AGENTIC_MESH_CODEX_HOME_HOST_PATH:-../../state/worker_mounts/codex-agentic-mesh-dev-team-home-q}",
        "      AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH: ${AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH:-../../../../../config/otel/collector.yaml}",
        "      AGENTIC_MESH_IMAGE_TAG: ${AGENTIC_MESH_IMAGE_TAG:-agentic-mesh:local}",
        "      COMPOSE_PROJECT_NAME: ${COMPOSE_PROJECT_NAME:-agentic-mesh}",
        "    volumes:",
        "      - ${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}:/mesh/system:ro",
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
    return [
        f"  {service_name}:",
        "    image: ${AGENTIC_MESH_IMAGE_TAG:-agentic-mesh:local}",
        "    profiles:",
        "      - roles",
        "    working_dir: /mesh/agent",
        f"    command: codex app-server --listen ws://0.0.0.0:{port} --ws-auth capability-token --ws-token-file /mesh/agent/ws-token",
        "    environment:",
        f"      AGENTIC_MESH_ROLE_ID: {role_id}",
        f"      AGENTIC_MESH_ROLE_INSTANCE_ID: agentic-mesh-dev.{role_id}.1",
        "      CODEX_HOME: /mesh/worker-auth/codex",
        "    volumes:",
        f"      - ${{AGENTIC_MESH_PROJECT_HOST_PATH:-../..}}/state/v4/agent-configs/{role_id}/1:/mesh/agent:ro",
        "      - ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}:/mesh/project",
        "      - ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}:/mesh/workspaces/agentic-mesh",
        "      - ${AGENTIC_MESH_CODEX_HOME_HOST_PATH:-../../state/worker_mounts/codex-agentic-mesh-dev-team-home-q}:/mesh/worker-auth/codex",
        "      - /var/run/docker.sock:/var/run/docker.sock",
    ]
