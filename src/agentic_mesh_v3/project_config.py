from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class V3BrokerConfig:
    adapter: str
    stream: str = "agent-inbox"
    servers: str | None = None


@dataclass(frozen=True)
class V3DocumentLibraryConfig:
    adapter: str
    root: Path | None = None
    drive_id: str | None = None
    root_path: str = "/documents"


@dataclass(frozen=True)
class V3WorkerAuthConfig:
    credential: str | None = None


@dataclass(frozen=True)
class V3ReleaseDeploymentTargetConfig:
    target_id: str
    target_type: str
    command: tuple[str, ...] = ()
    working_directory: Path | None = None
    timeout_seconds: int = 300
    rollback_plan: str = "Re-run the previous known-good deployment target."
    reason: str | None = None


@dataclass(frozen=True)
class V3WorkerConfig:
    adapter: str | None = None
    command: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox_mode: str | None = None
    auth: V3WorkerAuthConfig = V3WorkerAuthConfig()


@dataclass(frozen=True)
class V3RoleMessagingIdentity:
    display_name: str | None = None
    mention_handle: str | None = None
    bot_id_ref: str | None = None
    secret_ref: str | None = None


@dataclass(frozen=True)
class V3TeamsConnectorConfig:
    adapter: str | None = None
    graph_base_url: str = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class V3RoleInstanceConfig:
    role_id: str
    template: str | None = None
    instances: int = 1
    worker: V3WorkerConfig = V3WorkerConfig()
    instructions: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    messaging_identity: V3RoleMessagingIdentity = V3RoleMessagingIdentity()


@dataclass(frozen=True)
class V3ProjectConfig:
    project_id: str
    broker: V3BrokerConfig
    document_library: V3DocumentLibraryConfig
    roles: tuple[V3RoleInstanceConfig, ...]
    release_deployment_targets: tuple[V3ReleaseDeploymentTargetConfig, ...] = ()
    teams_connector: V3TeamsConnectorConfig = V3TeamsConnectorConfig()


def load_project_config(path: Path) -> V3ProjectConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    project_id = _required(raw, "project_id")
    broker_raw = _mapping(raw.get("broker"))
    docs_raw = _mapping(raw.get("document_library"))
    roles_raw = _mapping(raw.get("roles"))
    release_targets_raw = _mapping(raw.get("release_deployment_targets"))
    teams_raw = _mapping(_mapping(raw.get("connectors")).get("teams"))
    return V3ProjectConfig(
        project_id=project_id,
        broker=V3BrokerConfig(
            adapter=str(broker_raw.get("adapter") or "nats-jetstream"),
            stream=str(broker_raw.get("stream") or "agent-inbox"),
            servers=_optional(broker_raw.get("servers")),
        ),
        document_library=V3DocumentLibraryConfig(
            adapter=str(docs_raw.get("adapter") or docs_raw.get("backend") or "onedrive"),
            root=Path(str(docs_raw["root"])) if docs_raw.get("root") else None,
            drive_id=_optional(docs_raw.get("drive_id")),
            root_path=str(docs_raw.get("root_path") or "/documents"),
        ),
        roles=tuple(_load_role(role_id, _mapping(role_raw), raw) for role_id, role_raw in sorted(roles_raw.items())),
        release_deployment_targets=tuple(
            _load_release_deployment_target(target_id, _mapping(target_raw))
            for target_id, target_raw in sorted(release_targets_raw.items())
        ),
        teams_connector=V3TeamsConnectorConfig(
            adapter=_optional(teams_raw.get("adapter")),
            graph_base_url=str(teams_raw.get("graph_base_url") or "https://graph.microsoft.com/v1.0"),
        ),
    )


def _required(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if value is None or str(value) == "":
        raise ValueError(f"{key} is required")
    return str(value)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _load_role(role_id: str, role_raw: dict[str, Any], project_raw: dict[str, Any]) -> V3RoleInstanceConfig:
    worker_raw = _mapping(role_raw.get("worker"))
    auth_raw = _mapping(worker_raw.get("auth"))
    return V3RoleInstanceConfig(
        role_id=role_id,
        template=_optional(role_raw.get("template")),
        instances=int(role_raw.get("instances") or 1),
        worker=V3WorkerConfig(
            adapter=_optional(worker_raw.get("adapter")),
            command=tuple(str(value) for value in _list(worker_raw.get("command"))),
            timeout_seconds=_optional_int(worker_raw.get("timeout_seconds")),
            model=_optional(worker_raw.get("model")),
            reasoning_effort=_optional(worker_raw.get("reasoning_effort")),
            sandbox_mode=_optional(worker_raw.get("sandbox_mode")),
            auth=V3WorkerAuthConfig(credential=_optional(auth_raw.get("credential"))),
        ),
        instructions=tuple(str(value) for value in _list(role_raw.get("instructions"))),
        write_paths=tuple(str(value) for value in _list(role_raw.get("write_paths"))),
        messaging_identity=_load_messaging_identity(role_id, project_raw),
    )


def _load_release_deployment_target(
    target_id: str,
    target_raw: dict[str, Any],
) -> V3ReleaseDeploymentTargetConfig:
    return V3ReleaseDeploymentTargetConfig(
        target_id=target_id,
        target_type=str(target_raw.get("type") or "command"),
        command=tuple(str(value) for value in _list(target_raw.get("command"))),
        working_directory=Path(str(target_raw["working_directory"])) if target_raw.get("working_directory") else None,
        timeout_seconds=int(target_raw.get("timeout_seconds") or 300),
        rollback_plan=str(
            target_raw.get("rollback_plan")
            or target_raw.get("rollback_summary")
            or "Re-run the previous known-good deployment target."
        ),
        reason=_optional(target_raw.get("reason") or target_raw.get("description")),
    )


def _load_messaging_identity(role_id: str, project_raw: dict[str, Any]) -> V3RoleMessagingIdentity:
    connectors = _mapping(project_raw.get("connectors"))
    teams = _mapping(connectors.get("teams"))
    role_bots = _mapping(teams.get("role_bots"))
    bot_raw = _mapping(role_bots.get(role_id))
    display_name = _optional(bot_raw.get("display_name"))
    mention_handle = f"@{display_name}" if display_name else None
    return V3RoleMessagingIdentity(
        display_name=display_name,
        mention_handle=mention_handle,
        bot_id_ref=_optional(bot_raw.get("bot_id_ref")),
        secret_ref=_optional(bot_raw.get("secret_ref")),
    )


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
