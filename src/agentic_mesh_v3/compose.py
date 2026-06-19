from __future__ import annotations

from typing import Iterable

import yaml

from agentic_mesh_v3.lifecycle import RoleContainerSpec
from agentic_mesh_v3.lifecycle import service_name_for_role


def render_role_services_compose(
    specs: Iterable[RoleContainerSpec],
    *,
    network_name: str = "agentic-mesh",
    include_nats: bool = False,
    nats_service_name: str = "nats",
    nats_image: str = "nats:2.10-alpine",
    nats_ports: tuple[str, ...] = ("4222:4222", "8222:8222"),
    include_supervisor: bool = False,
    supervisor_service_name: str = "v3-supervisor",
    supervisor_image: str | None = None,
    supervisor_poll_seconds: float = 30.0,
    supervisor_compose_file: str | None = None,
    supervisor_execute: bool = False,
    supervisor_mount_docker_socket: bool = False,
) -> str:
    """Render Docker Compose YAML for configured V3 role-agent services."""

    spec_list = list(specs)
    if include_supervisor and not spec_list and supervisor_image is None:
        raise ValueError("supervisor_image is required when rendering a supervisor without role specs")
    services: dict[str, object] = {}
    if include_nats:
        services[nats_service_name] = {
            "image": nats_image,
            "command": ["-js", "-m", "8222"],
            "ports": list(nats_ports),
            "networks": [network_name],
            "restart": "unless-stopped",
        }
    for spec in spec_list:
        service_name = service_name_for_role(spec.role_instance_id)
        service = {
            "image": spec.image,
            "command": spec.service_command(),
            "environment": dict(sorted(spec.environment.items())),
            "volumes": _volume_list(spec),
            "networks": [network_name],
            "restart": "no",
        }
        if include_nats:
            service["depends_on"] = [nats_service_name]
        services[service_name] = service
    if include_supervisor:
        template = spec_list[0] if spec_list else None
        service = {
            "image": supervisor_image or template.image,  # type: ignore[union-attr]
            "command": _supervisor_command(
                poll_seconds=supervisor_poll_seconds,
                compose_file=supervisor_compose_file,
                execute=supervisor_execute,
            ),
            "environment": dict(sorted(template.environment.items())) if template else {},  # type: ignore[union-attr]
            "volumes": _supervisor_volumes(template, supervisor_mount_docker_socket),
            "networks": [network_name],
            "restart": "unless-stopped",
        }
        if include_nats:
            service["depends_on"] = [nats_service_name]
        services[supervisor_service_name] = service
    compose = {
        "services": services,
        "networks": {
            network_name: {
                "name": network_name,
            }
        },
    }
    return yaml.safe_dump(compose, sort_keys=True)


def _supervisor_command(
    *,
    poll_seconds: float,
    compose_file: str | None,
    execute: bool,
) -> list[str]:
    command = [
        "agentic-mesh-v3",
        "--db",
        "/mesh/state/agentic-mesh-v3.sqlite3",
        "--project-config",
        "/mesh/project/agentic-mesh/project.yaml",
        "run-project-supervisor-service",
        "--continuous",
        "--poll-seconds",
        _format_seconds(poll_seconds),
        "--publish-sweep-to-project-manager",
        "--refresh-inbox-from-broker",
    ]
    if compose_file:
        command.extend(["--compose-file", compose_file])
    if execute:
        command.append("--execute")
    return command


def _supervisor_volumes(
    template: RoleContainerSpec | None,
    mount_docker_socket: bool,
) -> list[str]:
    volumes = _volume_list(template) if template else []
    if mount_docker_socket:
        volumes.append("/var/run/docker.sock:/var/run/docker.sock")
    return volumes


def _volume_list(spec: RoleContainerSpec) -> list[str]:
    return [f"{host}:{container}" for host, container in sorted(spec.volume_mounts().items())]


def _format_seconds(value: float) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)
