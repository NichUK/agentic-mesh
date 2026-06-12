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


@dataclass(frozen=True)
class RoleMemoryConfig:
    enabled: bool
    backend: str
    config_root: Path
    role_config_path: Path
    memory_path: Path
    memory_filename: str


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


def load_role_container_lifecycle_config(project_file: Path, *, role_id: str) -> dict[str, Any]:
    raw = _load_project_mapping(project_file)
    role = _role_mapping(raw, role_id=role_id)
    config: dict[str, Any] = {}
    project_lifecycle = raw.get("container_lifecycle")
    if isinstance(project_lifecycle, dict):
        config.update(project_lifecycle)
    role_lifecycle = role.get("container_lifecycle")
    if isinstance(role_lifecycle, dict):
        config.update(role_lifecycle)
    if "working_directory" in config and isinstance(config["working_directory"], str):
        working_directory = Path(config["working_directory"])
        if not working_directory.is_absolute():
            config["working_directory"] = str(project_file.parent / working_directory)
    if "compose_files" in config and isinstance(config["compose_files"], list):
        compose_files: list[str] = []
        for index, item in enumerate(config["compose_files"]):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"container_lifecycle compose_files item {index} must be a non-empty string")
            compose_file = Path(item)
            if not compose_file.is_absolute():
                compose_file = project_file.parent / compose_file
            compose_files.append(str(compose_file))
        config["compose_files"] = compose_files
    return config


def load_role_memory_config(project_file: Path, *, role_id: str) -> RoleMemoryConfig:
    raw = _load_project_mapping(project_file)
    project_root = _project_root(project_file)
    project_memory = raw.get("role_memory")
    if not isinstance(project_memory, dict) or project_memory.get("enabled") is False:
        config_root = project_root / "agentic-mesh" / "roles"
        memory_filename = "MEMORY.md"
        role_config_path = config_root / role_id / "role.yaml"
        return RoleMemoryConfig(
            enabled=False,
            backend="filesystem",
            config_root=config_root,
            role_config_path=role_config_path,
            memory_path=role_config_path.parent / memory_filename,
            memory_filename=memory_filename,
        )

    backend = str(project_memory.get("backend") or "filesystem")
    if backend != "filesystem":
        raise ValueError(f"role_memory backend `{backend}` is not supported yet")
    memory_filename = _non_empty_string(project_memory.get("memory_filename"), default="MEMORY.md")
    _validate_relative_file_name(memory_filename, field="role_memory.memory_filename")
    config_root = _contained_project_path(
        project_root,
        project_file,
        _non_empty_string(project_memory.get("config_root"), default="agentic-mesh/roles"),
        field="role_memory.config_root",
    )
    role_dir = _contained_child_path(config_root, role_id, field="role_id")
    _ensure_contained(role_dir, project_root, field="role role directory")
    role_config_path = role_dir / "role.yaml"
    memory_path = _contained_child_path(role_dir, memory_filename, field="role_memory.memory_filename")
    _ensure_contained(memory_path, project_root, field="role memory path")
    if role_config_path.exists():
        with role_config_path.open("r", encoding="utf-8") as handle:
            role_config = yaml.safe_load(handle)
        if isinstance(role_config, dict):
            role_memory = role_config.get("memory")
            if isinstance(role_memory, dict):
                role_memory_file = role_memory.get("file")
                if isinstance(role_memory_file, str) and role_memory_file.strip():
                    _validate_relative_file_name(role_memory_file, field="role.memory.file")
                    memory_path = _contained_child_path(
                        role_config_path.parent,
                        role_memory_file,
                        field="role.memory.file",
                    )
                    _ensure_contained(memory_path, project_root, field="role memory path")
    return RoleMemoryConfig(
        enabled=True,
        backend=backend,
        config_root=config_root,
        role_config_path=role_config_path,
        memory_path=memory_path,
        memory_filename=memory_filename,
    )


def load_role_memory_context(project_file: Path, *, role_id: str) -> tuple[str, ...]:
    config = load_role_memory_config(project_file, role_id=role_id)
    if not config.enabled or not config.memory_path.exists():
        return ()
    text = config.memory_path.read_text(encoding="utf-8").strip()
    if not text:
        return ()
    return (f"Role memory file: {config.memory_path}\n{text}",)


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


def _project_root(project_file: Path) -> Path:
    project_file = Path(project_file)
    if project_file.parent.name == "agentic-mesh":
        return project_file.parent.parent
    return project_file.parent


def _contained_project_path(project_root: Path, project_file: Path, value: str, *, field: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        candidate = path
    else:
        candidate = _project_root(project_file) / path
    return _ensure_contained(candidate, project_root, field=field)


def _contained_child_path(root: Path, value: str, *, field: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise ValueError(f"{field} must be relative to the project role folder")
    return _ensure_contained(root / path, root, field=field)


def _ensure_contained(path: Path, root: Path, *, field: str) -> Path:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{field} must resolve inside project root `{resolved_root}`") from exc
    return resolved_path


def _validate_relative_file_name(value: str, *, field: str) -> None:
    path = Path(value)
    if path.is_absolute() or path.name != value:
        raise ValueError(f"{field} must be a single relative file name")


def _non_empty_string(value: object, *, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default
