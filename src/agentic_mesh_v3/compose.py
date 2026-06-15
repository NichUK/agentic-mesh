from __future__ import annotations

from typing import Iterable

import yaml

from agentic_mesh_v3.lifecycle import RoleContainerSpec
from agentic_mesh_v3.lifecycle import service_name_for_role


def render_role_services_compose(
    specs: Iterable[RoleContainerSpec],
    *,
    network_name: str = "agentic-mesh",
) -> str:
    """Render Docker Compose YAML for configured V3 role-agent services."""

    services: dict[str, object] = {}
    for spec in specs:
        service_name = service_name_for_role(spec.role_instance_id)
        services[service_name] = {
            "image": spec.image,
            "command": spec.service_command(),
            "environment": dict(sorted(spec.environment.items())),
            "volumes": _volume_list(spec),
            "networks": [network_name],
            "restart": "unless-stopped",
        }
    compose = {
        "services": services,
        "networks": {
            network_name: {
                "name": network_name,
            }
        },
    }
    return yaml.safe_dump(compose, sort_keys=True)

def _volume_list(spec: RoleContainerSpec) -> list[str]:
    return [f"{host}:{container}" for host, container in sorted(spec.volume_mounts().items())]
