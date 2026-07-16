from __future__ import annotations

import json
import re

import yaml

from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.config import V4RoleConfig
from agentic_mesh_v4.shared_fleet import generated_shared_fleet_plan
from agentic_mesh_v4.shared_fleet import stable_fleet_instance_id
from agentic_mesh_v4.shared_fleet import stable_fleet_service_name


OPS_ROLES = {"project-manager", "delivery-manager", "platform-engineer", "release-manager"}
DEV_ROLES = {"engineering"}
QA_ROLES = {"qa-engineer"}
# Full-authority roles that can produce or promote repository changes share the
# deployment's approved Git identity. The host key directory is mounted
# read-only and copied into the ephemeral container before Codex starts.
SSH_ROLES = OPS_ROLES | DEV_ROLES
DOCKER_SOCKET_ROLES = OPS_ROLES | DEV_ROLES
CODEX_CONFIG_ATOM = re.compile(r"^[A-Za-z0-9._-]+$")


def render_compose(project_config: V4ProjectConfig) -> str:
    shared_fleet_plan = generated_shared_fleet_plan(project_config)
    complete_shared_fleet = bool(project_config.shared_fleet.project_assignments)
    lines: list[str] = []
    if complete_shared_fleet:
        metadata = {
            "x-agentic-mesh-shared-fleet": {
                "activation_gate": shared_fleet_plan["activation_gate"],
                "assignment_allowlists": {
                    item["assignment_allowlist_id"]: item
                    for item in shared_fleet_plan["project_assignments"]
                },
                "enabled": shared_fleet_plan["enabled"],
                "runnable": shared_fleet_plan["runnable"],
            }
        }
        lines.extend(yaml.safe_dump(metadata, sort_keys=True).rstrip().splitlines())
    lines.extend([
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
        "    command: python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml serve --host 0.0.0.0 --port 8100 --document-root /documents",
        "    ports:",
        "      - \"${AGENTIC_MESH_V4_STATUS_PORT:-8100}:8100\"",
        "    environment:",
        "      AGENTIC_MESH_RUNTIME_VERSION: v4",
        "      AGENTIC_MESH_URL_ROOT: ${AGENTIC_MESH_URL_ROOT:-http://linuxch:8100}",
        "      AGENTIC_MESH_SYSTEM_ROOT: /mesh/system",
        "      AGENTIC_MESH_RUNTIME_SYSTEM_PATH: /mesh/system",
        "      AGENTIC_MESH_RUNTIME_WORKSPACE_PATH: /mesh/workspaces/agentic-mesh",
        "      AGENTIC_MESH_ENFORCE_RUNTIME_TOPOLOGY: 1",
        "      PYTHONPATH: /mesh/system/src",
        "      AGENTIC_MESH_DATABASE_HOST: ${AGENTIC_MESH_DATABASE_HOST:-agentic-mesh-postgres}",
        "      AGENTIC_MESH_DATABASE_PORT: ${AGENTIC_MESH_DATABASE_PORT:-5432}",
        "      AGENTIC_MESH_DATABASE_NAME: ${AGENTIC_MESH_DATABASE_NAME:-agentic_mesh_v4}",
        "      AGENTIC_MESH_DATABASE_USER: ${AGENTIC_MESH_DATABASE_USER:-agentic_mesh}",
        "      AGENTIC_MESH_DATABASE_PASSWORD_FILE: /mesh/project/state/secrets/postgres-password",
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
        f"    command: python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml dispatch-loop --project-id {project_config.project_id} --agent-config-root /mesh/project/state/v4/agent-configs --compose-file /mesh/project/deploy/compose/docker-compose.v4.yml --compose-file /mesh/project/deploy/compose/docker-compose.linuxch.yml --compose-project-name ${{COMPOSE_PROJECT_NAME:-agentic-mesh}} --compose-env-file /mesh/project/deploy/compose/.env --compose-working-directory /mesh/project/deploy/compose --active-turn-stale-seconds ${{AGENTIC_MESH_ACTIVE_TURN_STALE_SECONDS:-900}} --dispatch-workers ${{AGENTIC_MESH_DISPATCH_WORKERS:-8}} --wake",
        "    environment:",
        "      AGENTIC_MESH_RUNTIME_VERSION: v4",
        "      AGENTIC_MESH_URL_ROOT: ${AGENTIC_MESH_URL_ROOT:-http://linuxch:8100}",
        "      AGENTIC_MESH_SYSTEM_ROOT: /mesh/system",
        "      AGENTIC_MESH_RUNTIME_SYSTEM_PATH: /mesh/system",
        "      AGENTIC_MESH_RUNTIME_WORKSPACE_PATH: /mesh/workspaces/agentic-mesh",
        "      AGENTIC_MESH_ENFORCE_RUNTIME_TOPOLOGY: 1",
        "      AGENTIC_MESH_CODEX_WS_READ_TIMEOUT_SECONDS: ${AGENTIC_MESH_CODEX_WS_READ_TIMEOUT_SECONDS:-30}",
        "      PYTHONPATH: /mesh/system/src",
        "      AGENTIC_MESH_DATABASE_HOST: ${AGENTIC_MESH_DATABASE_HOST:-agentic-mesh-postgres}",
        "      AGENTIC_MESH_DATABASE_PORT: ${AGENTIC_MESH_DATABASE_PORT:-5432}",
        "      AGENTIC_MESH_DATABASE_NAME: ${AGENTIC_MESH_DATABASE_NAME:-agentic_mesh_v4}",
        "      AGENTIC_MESH_DATABASE_USER: ${AGENTIC_MESH_DATABASE_USER:-agentic_mesh}",
        "      AGENTIC_MESH_DATABASE_PASSWORD_FILE: /mesh/project/state/secrets/postgres-password",
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
        "    command: python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml watchdog-loop --compose-file /mesh/project/deploy/compose/docker-compose.v4.yml --compose-file /mesh/project/deploy/compose/docker-compose.linuxch.yml --compose-project-name ${COMPOSE_PROJECT_NAME:-agentic-mesh} --compose-env-file /mesh/project/deploy/compose/.env --compose-working-directory /mesh/project/deploy/compose --service runtime --service dispatcher --poll-interval-seconds ${AGENTIC_MESH_WATCHDOG_INTERVAL_SECONDS:-30}",
        "    environment:",
        "      AGENTIC_MESH_RUNTIME_VERSION: v4",
        "      AGENTIC_MESH_URL_ROOT: ${AGENTIC_MESH_URL_ROOT:-http://linuxch:8100}",
        "      AGENTIC_MESH_SYSTEM_ROOT: /mesh/system",
        "      AGENTIC_MESH_RUNTIME_SYSTEM_PATH: /mesh/system",
        "      AGENTIC_MESH_RUNTIME_WORKSPACE_PATH: /mesh/workspaces/agentic-mesh",
        "      AGENTIC_MESH_ENFORCE_RUNTIME_TOPOLOGY: 1",
        "      PYTHONPATH: /mesh/system/src",
        "      AGENTIC_MESH_DATABASE_HOST: ${AGENTIC_MESH_DATABASE_HOST:-agentic-mesh-postgres}",
        "      AGENTIC_MESH_DATABASE_PORT: ${AGENTIC_MESH_DATABASE_PORT:-5432}",
        "      AGENTIC_MESH_DATABASE_NAME: ${AGENTIC_MESH_DATABASE_NAME:-agentic_mesh_v4}",
        "      AGENTIC_MESH_DATABASE_USER: ${AGENTIC_MESH_DATABASE_USER:-agentic_mesh}",
        "      AGENTIC_MESH_DATABASE_PASSWORD_FILE: /mesh/project/state/secrets/postgres-password",
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
    ])
    for role in project_config.roles:
        max_instances = role.instances if complete_shared_fleet else 1
        for ordinal in range(1, max_instances + 1):
            lines.extend(
                _stable_role_service(
                    project_config=project_config,
                    role=role,
                    ordinal=ordinal,
                    assignment_refs=tuple(
                        item["assignment_allowlist_id"]
                        for item in shared_fleet_plan["project_assignments"]
                    ),
                )
                if complete_shared_fleet
                else _role_service(
                    project_config=project_config,
                    role=role,
                )
            )
            lines.append("")
    rendered = "\n".join(lines).rstrip() + "\n"
    validate_v4_compose(rendered)
    return rendered


def validate_v4_compose(rendered: str) -> None:
    forbidden = ("v3-nats", "v3-supervisor", "run-agent-service", "nats://", "agentic_mesh_v3.cli")
    found = [item for item in forbidden if item in rendered]
    if found:
        raise ValueError(f"V4 compose contains V3-only components: {', '.join(found)}")


def render_shared_fleet_binding_override(
    project_config: V4ProjectConfig,
    *,
    role_id: str,
    project_id: str,
    generation: int,
) -> str:
    """Render the only project-bearing layer used for a captured binding.

    The base shared-fleet service remains unbound and has no project resources.
    This override is intentionally generation-specific so a stale container or
    safe-output proxy cannot silently continue after an A -> B transition.
    """
    plan = generated_shared_fleet_plan(project_config)
    if not plan["runnable"]:
        raise ValueError("shared fleet binding override requires enabled runnable configuration")
    if generation < 1:
        raise ValueError("shared fleet binding generation must be positive")
    role = project_config.role(role_id)
    allowlists = {
        item["project_id"]: item for item in plan["project_assignments"]
    }
    assignment = allowlists.get(project_id)
    if assignment is None:
        raise ValueError(f"unknown shared-fleet project assignment: {project_id}")
    service_name = stable_fleet_service_name(
        fleet_id=project_config.shared_fleet.fleet_id,
        role_id=role_id,
    )
    instance_id = stable_fleet_instance_id(
        fleet_id=project_config.shared_fleet.fleet_id,
        role_id=role_id,
    )
    model = _codex_config_atom(role.model, field="model")
    reasoning = _codex_config_atom(role.reasoning_effort, field="reasoning_effort")
    plan_reasoning = _codex_config_atom(
        role.plan_mode_reasoning_effort, field="plan_mode_reasoning_effort"
    )
    raw_reasoning = "true" if role.show_raw_agent_reasoning else "false"
    app_server = (
        f"codex -c model={model} -c model_reasoning_effort={reasoning} "
        f"-c plan_mode_reasoning_effort={plan_reasoning} "
        f"-c show_raw_agent_reasoning={raw_reasoning} app-server "
        f"--listen ws://0.0.0.0:{role.codex_port} --ws-auth capability-token "
        f"--ws-token-file /mesh/project/state/v4/agent-configs/{role_id}/1/ws-token"
    )
    proxy = (
        "python -m agentic_mesh_v4.safe_output_proxy "
        "--socket /mesh/agent-workspace/.agentic-mesh/safe-output.sock "
        f"--role-id {role_id} --project-config /mesh/project/agentic-mesh/project-v4.yaml "
        f"--fleet-instance-id {instance_id} --binding-project-id {project_id} "
        f"--binding-generation {generation}"
    )
    command = (
        "mkdir -p /mesh/agent-workspace/.agentic-mesh; "
        "rm -f /mesh/agent-workspace/.agentic-mesh/safe-output.sock; "
        f"{proxy} & for i in $(seq 1 50); do "
        "[ -S /mesh/agent-workspace/.agentic-mesh/safe-output.sock ] && break; sleep 0.1; "
        "done; [ -S /mesh/agent-workspace/.agentic-mesh/safe-output.sock ] || exit 1; "
        f"exec {app_server}"
    )
    service = {
        "command": ["sh", "-lc", command],
        "env_file": [
            {
                "path": f"{assignment['project_root']}/deploy/compose/.env",
                "required": False,
            }
        ],
        "environment": {
            "AGENTIC_MESH_DATABASE_CREDENTIAL_REF": assignment["database_credential_ref"],
            "AGENTIC_MESH_DATABASE_PASSWORD_FILE": "/mesh/project/state/secrets/postgres-password",
            "AGENTIC_MESH_DATABASE_SCHEMA": assignment["database_schema"],
            "AGENTIC_MESH_PROJECT_ID": project_id,
            "AGENTIC_MESH_ROLE_ID": role_id,
            "AGENTIC_MESH_ROLE_INSTANCE_ID": instance_id,
            "AGENTIC_MESH_SAFE_OUTPUT_SOCKET": "/mesh/agent-workspace/.agentic-mesh/safe-output.sock",
            "AGENTIC_MESH_SHARED_FLEET_BINDING_GENERATION": str(generation),
            "AGENTIC_MESH_SHARED_FLEET_BINDING_STATE": "bound",
            "PGOPTIONS": f"-c search_path={assignment['database_schema']}",
        },
        "networks": assignment["networks"],
        "volumes": [
            {
                "type": "bind",
                "source": mount["source"],
                "target": mount["target"],
                "read_only": mount["read_only"],
            }
            for mount in assignment["mounts"]
        ],
    }
    return yaml.safe_dump(
        {
            "networks": {assignment["networks"][0]: {"external": True}},
            "services": {service_name: service},
        },
        sort_keys=True,
    )


def _role_service(*, project_config: V4ProjectConfig, role: V4RoleConfig) -> list[str]:
    role_id = role.role_id
    role_instance_id = project_config.role_instance_id(role_id)
    service_name = role.service_name
    port = role.codex_port
    model = _codex_config_atom(role.model, field="model")
    reasoning_effort = _codex_config_atom(role.reasoning_effort, field="reasoning_effort")
    plan_mode_reasoning_effort = _codex_config_atom(
        role.plan_mode_reasoning_effort,
        field="plan_mode_reasoning_effort",
    )
    raw_reasoning = "true" if role.show_raw_agent_reasoning else "false"
    app_server = (
        f"codex -c model={model} "
        f"-c model_reasoning_effort={reasoning_effort} "
        f"-c plan_mode_reasoning_effort={plan_mode_reasoning_effort} "
        f"-c show_raw_agent_reasoning={raw_reasoning} "
        f"app-server --listen ws://0.0.0.0:{port} --ws-auth capability-token --ws-token-file /mesh/agent/ws-token"
    )
    command = (
        "sh -lc 'mkdir -p /mesh/agent-workspace /documents/work-items; "
        "chmod -R a+rwX /mesh/agent-workspace /documents; "
        "mkdir -p /mesh/agent-workspace/.agentic-mesh; "
        "rm -f /mesh/agent-workspace/.agentic-mesh/safe-output.sock; "
        f"python -m agentic_mesh_v4.safe_output_proxy --socket /mesh/agent-workspace/.agentic-mesh/safe-output.sock --role-id {role_id} --project-config /mesh/project/agentic-mesh/project-v4.yaml & "
        "for i in $(seq 1 50); do [ -S /mesh/agent-workspace/.agentic-mesh/safe-output.sock ] && break; sleep 0.1; done; "
        "[ -S /mesh/agent-workspace/.agentic-mesh/safe-output.sock ] || { echo safe-output proxy failed to start >&2; exit 1; }; "
        "for d in memories tmp sessions cache shell_snapshots; do mkdir -p /mesh/worker-auth/codex/$$d; chmod -R a+rwX /mesh/worker-auth/codex/$$d; done; "
        "cp /mesh/agent/AGENTS.md /mesh/agent-workspace/AGENTS.md; "
        f"exec {app_server}'"
    )
    if role_id in SSH_ROLES:
        command = (
            "sh -lc 'mkdir -p /mesh/agent-workspace /documents/work-items; "
            "chmod -R a+rwX /mesh/agent-workspace /documents; "
            "mkdir -p /mesh/agent-workspace/.agentic-mesh; "
            "rm -f /mesh/agent-workspace/.agentic-mesh/safe-output.sock; "
            f"python -m agentic_mesh_v4.safe_output_proxy --socket /mesh/agent-workspace/.agentic-mesh/safe-output.sock --role-id {role_id} --project-config /mesh/project/agentic-mesh/project-v4.yaml & "
            "for i in $(seq 1 50); do [ -S /mesh/agent-workspace/.agentic-mesh/safe-output.sock ] && break; sleep 0.1; done; "
            "[ -S /mesh/agent-workspace/.agentic-mesh/safe-output.sock ] || { echo safe-output proxy failed to start >&2; exit 1; }; "
            "for d in memories tmp sessions cache shell_snapshots; do mkdir -p /mesh/worker-auth/codex/$$d; chmod -R a+rwX /mesh/worker-auth/codex/$$d; done; "
            "cp /mesh/agent/AGENTS.md /mesh/agent-workspace/AGENTS.md; "
            "mkdir -p /root/.ssh; "
            "if [ -d /mesh/home/.ssh ]; then cp -r /mesh/home/.ssh/. /root/.ssh/; fi; "
            "if [ -f /root/.ssh/config ]; then sed -i \"s#/mesh/home/.ssh#/root/.ssh#g\" /root/.ssh/config; fi; "
            "chmod 700 /root/.ssh; "
            "find /root/.ssh -type f -exec chmod 600 {} \\; 2>/dev/null || true; "
            f"exec {app_server}'"
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
        "    working_dir: /mesh/agent-workspace",
        f"    command: {command}",
        "    environment:",
        f"      AGENTIC_MESH_ROLE_ID: {role_id}",
        f"      AGENTIC_MESH_ROLE_INSTANCE_ID: {role_instance_id}",
        f"      AGENTIC_MESH_TOOL_PROFILE: {_tool_profile(role_id)}",
        "      AGENTIC_MESH_SYSTEM_ROOT: /mesh/system",
        "      PYTHONPATH: /mesh/system/src",
        "      CODEX_HOME: /mesh/worker-auth/codex",
        "      HOME: /mesh/home",
        "      AGENTIC_MESH_SAFE_OUTPUT_SOCKET: /mesh/agent-workspace/.agentic-mesh/safe-output.sock",
        "      AGENTIC_MESH_DATABASE_HOST: ${AGENTIC_MESH_DATABASE_HOST:-agentic-mesh-postgres}",
        "      AGENTIC_MESH_DATABASE_PORT: ${AGENTIC_MESH_DATABASE_PORT:-5432}",
        "      AGENTIC_MESH_DATABASE_NAME: ${AGENTIC_MESH_DATABASE_NAME:-agentic_mesh_v4}",
        "      AGENTIC_MESH_DATABASE_USER: ${AGENTIC_MESH_DATABASE_USER:-agentic_mesh}",
        "      AGENTIC_MESH_DATABASE_PASSWORD_FILE: /mesh/project/state/secrets/postgres-password",
        "    volumes:",
        f"      - ${{AGENTIC_MESH_PROJECT_HOST_PATH:-../..}}/state/v4/agent-configs/{role_id}/1:/mesh/agent",
        f"      - ${{AGENTIC_MESH_PROJECT_HOST_PATH:-../..}}/state/v4/agent-workspaces/{role_id}/1:/mesh/agent-workspace",
        "      - ${AGENTIC_MESH_DOCUMENTS_HOST_PATH:-../documents}:/documents",
        "      - ${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}:/mesh/project",
        "      - ${AGENTIC_MESH_SYSTEM_HOST_PATH:-../../../../..}:/mesh/system:ro",
        "      - ${AGENTIC_MESH_WORKSPACE_HOST_PATH:-../../../../..}:/mesh/workspaces/agentic-mesh",
        "      - ${AGENTIC_MESH_CODEX_HOME_HOST_PATH:-../../state/worker_mounts/codex-agentic-mesh-dev-team-home-q}:/mesh/worker-auth/codex",
    ]
    if role_id in DOCKER_SOCKET_ROLES:
        lines.append("      - /var/run/docker.sock:/var/run/docker.sock")
    if role_id in SSH_ROLES:
        lines.extend([
            "      - ${AGENTIC_MESH_PROJECT_ENV_FILE_HOST_PATH:-.env}:/mesh/home/.env:ro",
            "      - ${AGENTIC_MESH_GIT_SSH_HOST_PATH:-${AGENTIC_MESH_PROJECT_MANAGER_SSH_HOST_PATH:-../../state/worker_mounts/project-manager/.ssh}}:/mesh/home/.ssh:ro",
        ])
    return lines


def _stable_role_service(
    *,
    project_config: V4ProjectConfig,
    role: V4RoleConfig,
    ordinal: int,
    assignment_refs: tuple[str, ...],
) -> list[str]:
    fleet_id = project_config.shared_fleet.fleet_id
    role_id = role.role_id
    service_name = stable_fleet_service_name(fleet_id=fleet_id, role_id=role_id, ordinal=ordinal)
    instance_id = stable_fleet_instance_id(fleet_id=fleet_id, role_id=role_id, ordinal=ordinal)
    refs = json.dumps(sorted(assignment_refs), separators=(",", ":"))
    enabled = project_config.shared_fleet.enabled
    activation_gate = "activation_ready" if enabled else "stages_2_4_closed"
    lines = [
        f"  {service_name}:",
        f"    image: {_role_image(role_id)}",
    ]
    if enabled:
        # The physical service is deliberately useful but resource-free while
        # unbound.  A generation-checked lifecycle override supplies exactly
        # one project's command, mounts, credentials and network at bind time.
        lines.extend([
            "    restart: unless-stopped",
            "    command: [\"python\", \"-c\", \"import signal; signal.pause()\"]",
        ])
    else:
        lines.extend([
            "    profiles:",
            "      - shared-fleet-activation-closed",
            "    restart: \"no\"",
            "    deploy:",
            "      replicas: 0",
            "    command: [\"sh\", \"-lc\", \"echo 'shared fleet activation gate is closed' >&2; exit 78\"]",
        ])
    lines.extend([
        "    environment:",
        f"      AGENTIC_MESH_ROLE_ID: {role_id}",
        f"      AGENTIC_MESH_ROLE_INSTANCE_ID: {instance_id}",
        f"      AGENTIC_MESH_SHARED_FLEET_ENABLED: \"{1 if enabled else 0}\"",
        f"      AGENTIC_MESH_SHARED_FLEET_RUNNABLE: \"{1 if enabled else 0}\"",
        f"      AGENTIC_MESH_SHARED_FLEET_ACTIVATION_GATE: {activation_gate}",
        "      AGENTIC_MESH_SHARED_FLEET_BINDING_STATE: unbound",
        "      AGENTIC_MESH_SHARED_FLEET_BINDING_GENERATION: \"0\"",
        f"      AGENTIC_MESH_SHARED_FLEET_ASSIGNMENT_ALLOWLIST_REFS: '{refs}'",
        "    labels:",
        f"      agentic-mesh.shared-fleet.enabled: \"{'true' if enabled else 'false'}\"",
        f"      agentic-mesh.shared-fleet.runnable: \"{'true' if enabled else 'false'}\"",
        f"      agentic-mesh.shared-fleet.activation-gate: {activation_gate}",
    ])
    return lines


def _codex_config_atom(value: str, *, field: str) -> str:
    if not CODEX_CONFIG_ATOM.fullmatch(value):
        raise ValueError(f"invalid Codex {field}: {value!r}")
    return value


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
