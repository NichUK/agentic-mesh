from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentic_mesh_v2.connectors import ConnectorConfig


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


@dataclass(frozen=True)
class DocumentLibraryConfig:
    backend: str
    root: Path
    structure_policy: str


def load_teams_connector_config(project_file: Path, *, external_base_url: str | None = None) -> ConnectorConfig:
    raw = _load_project_mapping(project_file)
    project_id = _project_id_from_raw(raw)
    connectors = raw.get("connectors")
    if not isinstance(connectors, dict):
        raise ValueError("project config must define connectors for Teams ingress")
    teams = connectors.get("teams")
    if not isinstance(teams, dict):
        raise ValueError("project config must define connectors.teams for Teams ingress")
    if teams.get("adapter") != "teams-bot-connector":
        raise ValueError("Teams ingress requires connectors.teams.adapter `teams-bot-connector`")

    team = teams.get("team")
    if not isinstance(team, dict):
        raise ValueError("connectors.teams.team must be a mapping")
    channels = teams.get("channels")
    if not isinstance(channels, dict):
        raise ValueError("connectors.teams.channels must be a mapping")
    project_channel = channels.get("project")
    if not isinstance(project_channel, dict):
        raise ValueError("connectors.teams.channels.project must be a mapping")
    role_bots = teams.get("role_bots")
    if not isinstance(role_bots, dict) or not role_bots:
        raise ValueError("connectors.teams.role_bots must define at least one role bot")

    team_ref = _non_empty_string(team.get("id"), default=_non_empty_string(team.get("name"), default="project-team"))
    project_channel_ref = _non_empty_string(
        project_channel.get("id"),
        default=_non_empty_string(project_channel.get("name"), default="project"),
    )
    role_identities: dict[str, dict[str, object]] = {}
    identity_model = str(teams.get("identity_model") or "role_bots")
    normalized_identity_model = "separate_bot" if identity_model in {"role_bots", "role_instance_bots"} else "shared_gateway"
    for role_id, bot in role_bots.items():
        if not isinstance(role_id, str) or not role_id.strip():
            raise ValueError("connectors.teams.role_bots keys must be role ids")
        if not isinstance(bot, dict):
            raise ValueError(f"connectors.teams.role_bots.{role_id} must be a mapping")
        display_name = _non_empty_string(bot.get("display_name"), default=role_id.replace("-", " ").title())
        role_identities[role_id] = {
            "external_ref": _non_empty_string(bot.get("bot_id_ref"), default=f"bot-{role_id}"),
            "secret_ref": _non_empty_string(bot.get("secret_ref"), default=""),
            "display_name": display_name,
            "alias": role_id,
            "mention_handle": f"@{display_name}",
            "identity_model": normalized_identity_model,
            "enabled": bool(bot.get("enabled", True)),
        }

    channel_bindings: list[dict[str, object]] = []
    for channel_name, channel in channels.items():
        if channel_name == "project":
            continue
        if not isinstance(channel, dict):
            continue
        channel_ref = channel.get("id") or channel.get("name")
        if not isinstance(channel_ref, str) or not channel_ref.strip():
            continue
        channel_bindings.append(
            {
                "channel_ref": channel_ref.strip(),
                "scope_type": str(channel.get("scope_type") or "focused_work"),
                "display_name": str(channel.get("name") or channel_name),
                "visibility": str(channel.get("visibility") or "project"),
                "work_scope": channel.get("work_scope"),
                "private": bool(channel.get("private", False)),
            }
        )

    return ConnectorConfig.from_dict(
        {
            "connector_id": f"teams-{project_id}",
            "project_id": project_id,
            "connector_type": "teams",
            "display_name": f"{_non_empty_string(raw.get('name'), default=project_id)} Teams",
            "tenant_id": _non_empty_string(teams.get("tenant_id"), default=""),
            "project_team_ref": team_ref,
            "default_project_channel_ref": project_channel_ref,
            "external_base_url": external_base_url
            or _non_empty_string(
                (teams.get("ingress") or {}).get("public_endpoint") if isinstance(teams.get("ingress"), dict) else None,
                default="http://localhost:3978",
            ),
            "role_identities": role_identities,
            "channel_bindings": channel_bindings,
            "human_authorities": teams.get("human_authorities", {}),
            "people": teams.get("people", ()),
            "authority_groups": teams.get("authority_groups", {}),
            "retention": {
                "private_dm_days": 30,
                "project_channel_days": 90,
                "compacted_summary_days": 365,
                "delivery_record_days": 90,
                "idempotency_receipt_days": 30,
            },
            "team_wide_trigger": "@all-agents",
        }
    )


def load_role_worker_config(project_file: Path, *, role_id: str) -> dict[str, Any]:
    raw = _load_project_mapping(project_file)
    role = _role_mapping(raw, role_id=role_id)
    return _worker_config(project_file, role_id=role_id, role=role)


def load_project_id(project_file: Path) -> str:
    raw = _load_project_mapping(project_file)
    return _project_id_from_raw(raw)


def _project_id_from_raw(raw: dict[str, Any]) -> str:
    project_id = raw.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project config requires project_id")
    return project_id.strip()


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


def load_document_library_config(project_file: Path) -> DocumentLibraryConfig | None:
    raw = _load_project_mapping(project_file)
    config = raw.get("document_library")
    if not isinstance(config, dict):
        return None
    backend = _non_empty_string(config.get("backend"), default="filesystem")
    if backend not in {"filesystem", "git"}:
        raise ValueError(f"document_library backend `{backend}` is not supported yet")
    root_value = _non_empty_string(config.get("root"), default=".")
    root = Path(root_value)
    if not root.is_absolute():
        root = project_file.parent / root
    return DocumentLibraryConfig(
        backend=backend,
        root=root.resolve(strict=False),
        structure_policy=_non_empty_string(config.get("structure_policy"), default="togaf-sdlc-v1"),
    )


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
