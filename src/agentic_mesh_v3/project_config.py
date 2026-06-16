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
class V3RoleInstanceConfig:
    role_id: str
    instances: int = 1


@dataclass(frozen=True)
class V3ProjectConfig:
    project_id: str
    broker: V3BrokerConfig
    document_library: V3DocumentLibraryConfig
    roles: tuple[V3RoleInstanceConfig, ...]


def load_project_config(path: Path) -> V3ProjectConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    project_id = _required(raw, "project_id")
    broker_raw = _mapping(raw.get("broker"))
    docs_raw = _mapping(raw.get("document_library"))
    roles_raw = _mapping(raw.get("roles"))
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
        roles=tuple(
            V3RoleInstanceConfig(role_id=role_id, instances=int(_mapping(role_raw).get("instances") or 1))
            for role_id, role_raw in sorted(roles_raw.items())
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
