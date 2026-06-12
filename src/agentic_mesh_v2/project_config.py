from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ProjectRoleServiceConfig:
    project_id: str
    role_id: str
    role_instance_id: str
    worker_config: dict[str, Any]


def load_role_worker_config(project_file: Path, *, role_id: str) -> dict[str, Any]:
    raw = _load_project_mapping(project_file)
    role = _role_mapping(raw, role_id=role_id)
    return _worker_config(project_file, role_id=role_id, role=role)


def load_role_hibernation_config(project_file: Path, *, role_id: str) -> dict[str, Any]:
    raw = _load_project_mapping(project_file)
    role = _role_mapping(raw, role_id=role_id)
    config: dict[str, Any] = {}
    project_hibernation = raw.get("hibernation")
    if isinstance(project_hibernation, dict):
        config.update(project_hibernation)
    role_hibernation = role.get("hibernation")
    if isinstance(role_hibernation, dict):
        config.update(role_hibernation)
    return config


def list_project_role_service_configs(project_file: Path) -> list[ProjectRoleServiceConfig]:
    raw = _load_project_mapping(project_file)
    project_id = raw.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project config requires project_id")
    roles = raw.get("roles")
    if not isinstance(roles, dict):
        raise ValueError("project config must define roles")

    configs: list[ProjectRoleServiceConfig] = []
    for role_id in sorted(str(key) for key in roles):
        role = roles.get(role_id)
        if not isinstance(role, dict):
            raise ValueError(f"role `{role_id}` config must be a mapping")
        instances = role.get("instances", 1)
        if isinstance(instances, bool) or not isinstance(instances, int) or instances < 1:
            raise ValueError(f"role `{role_id}` instances must be a positive integer")
        worker_config = _worker_config(project_file, role_id=role_id, role=role)
        for index in range(1, instances + 1):
            configs.append(
                ProjectRoleServiceConfig(
                    project_id=project_id,
                    role_id=role_id,
                    role_instance_id=f"{project_id}.{role_id}.{index}",
                    worker_config=dict(worker_config),
                )
            )
    return configs


def _load_project_mapping(project_file: Path) -> dict[str, Any]:
    with project_file.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("project config must be a mapping")
    return raw


def _role_mapping(raw: dict[str, Any], *, role_id: str) -> dict[str, Any]:
    roles = raw.get("roles")
    if not isinstance(roles, dict):
        raise ValueError("project config must define roles")
    role = roles.get(role_id)
    if not isinstance(role, dict):
        raise ValueError(f"project config does not define role `{role_id}`")
    return role


def _worker_config(project_file: Path, *, role_id: str, role: dict[str, Any]) -> dict[str, Any]:
    worker = role.get("worker")
    if not isinstance(worker, dict):
        raise ValueError(f"role `{role_id}` must define worker config")
    adapter = worker.get("adapter")
    if not isinstance(adapter, str) or not adapter.strip():
        raise ValueError(f"role `{role_id}` worker config requires adapter")

    config = dict(worker)
    if adapter == "safe-output-file":
        path = config.get("path")
        if isinstance(path, str) and path.strip():
            worker_path = Path(path)
            if not worker_path.is_absolute():
                worker_path = project_file.parent / worker_path
            config["path"] = str(worker_path)
    return config
