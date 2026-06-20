from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_ROLE_IDS: tuple[str, ...] = (
    "project-manager",
    "delivery-manager",
    "product-manager",
    "business-analyst",
    "research-analyst",
    "enterprise-architect",
    "solution-architect",
    "security-architect",
    "ux-designer",
    "engineering",
    "qa-engineer",
    "platform-engineer",
    "release-manager",
    "technical-writer",
    "prompt-engineer",
)


FULL_ACCESS_ROLES: frozenset[str] = frozenset(
    {
        "project-manager",
        "delivery-manager",
        "release-manager",
        "engineering",
        "platform-engineer",
    }
)


@dataclass(frozen=True)
class V4RoleConfig:
    role_id: str
    display_name: str
    template: str
    instances: int = 1
    authority: str = "scoped"
    model: str = "gpt-5.5"
    reasoning_effort: str = "high"
    sandbox_mode: str = "workspace-write"
    codex_port: int = 4700
    idle_timeout_seconds: int = 900
    instructions: tuple[str, ...] = ()

    @property
    def role_instance_id(self) -> str:
        return f"{self.role_id}.1"

    @property
    def service_name(self) -> str:
        return f"agentic-mesh-dev-{self.role_id}-1"


@dataclass(frozen=True)
class V4ProjectConfig:
    project_id: str
    name: str
    goal: str
    roles: tuple[V4RoleConfig, ...]
    document_root: str = "/documents"
    teams_public_endpoint: str | None = None
    teams_project_channel_id: str | None = None
    teams_project_team_id: str | None = None

    def role(self, role_id: str) -> V4RoleConfig:
        for role in self.roles:
            if role.role_id == role_id:
                return role
        raise KeyError(role_id)


def load_project_config(path: str | Path) -> V4ProjectConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"project config must be a mapping: {config_path}")

    roles = _roles_from_raw(raw.get("roles"))
    return V4ProjectConfig(
        project_id=str(raw.get("project_id") or "agentic-mesh-dev"),
        name=str(raw.get("name") or raw.get("project_id") or "Agentic Mesh"),
        goal=_goal_description(raw.get("goal")),
        roles=roles,
        document_root=str(_nested(raw, ("document_library", "root_path")) or "/documents"),
        teams_public_endpoint=_expand_optional(_nested(raw, ("connectors", "teams", "ingress", "public_endpoint"))),
        teams_project_channel_id=_expand_optional(_nested(raw, ("connectors", "teams", "channels", "project", "id"))),
        teams_project_team_id=_expand_optional(_nested(raw, ("connectors", "teams", "team", "id"))),
    )


def _roles_from_raw(raw_roles: object) -> tuple[V4RoleConfig, ...]:
    if not isinstance(raw_roles, dict) or not raw_roles:
        return tuple(_default_role(role_id, index) for index, role_id in enumerate(DEFAULT_ROLE_IDS))

    roles: list[V4RoleConfig] = []
    for index, role_id in enumerate(DEFAULT_ROLE_IDS):
        item = raw_roles.get(role_id)
        if not isinstance(item, dict):
            roles.append(_default_role(role_id, index))
            continue
        worker = item.get("worker") if isinstance(item.get("worker"), dict) else {}
        roles.append(
            V4RoleConfig(
                role_id=role_id,
                display_name=_display_name(role_id),
                template=str(item.get("template") or role_id),
                instances=int(item.get("instances") or 1),
                authority="full" if role_id in FULL_ACCESS_ROLES else "scoped",
                model=str(worker.get("model") or "gpt-5.5"),
                reasoning_effort=str(worker.get("reasoning_effort") or "high"),
                sandbox_mode=_sandbox_mode(role_id, str(worker.get("sandbox_mode") or "")),
                codex_port=4700 + index,
                idle_timeout_seconds=int(item.get("idle_timeout_seconds") or 900),
                instructions=tuple(str(value) for value in item.get("instructions") or ()),
            )
        )
    return tuple(roles)


def _default_role(role_id: str, index: int) -> V4RoleConfig:
    return V4RoleConfig(
        role_id=role_id,
        display_name=_display_name(role_id),
        template=role_id,
        authority="full" if role_id in FULL_ACCESS_ROLES else "scoped",
        sandbox_mode=_sandbox_mode(role_id, ""),
        codex_port=4700 + index,
    )


def _sandbox_mode(role_id: str, configured: str) -> str:
    if configured:
        return configured
    if role_id in FULL_ACCESS_ROLES:
        return "danger-full-access"
    return "workspace-write"


def _display_name(role_id: str) -> str:
    acronyms = {"qa": "QA", "ux": "UX"}
    parts = []
    for part in role_id.split("-"):
        parts.append(acronyms.get(part, part.capitalize()))
    return " ".join(parts)


def _goal_description(raw_goal: object) -> str:
    if isinstance(raw_goal, dict):
        return str(raw_goal.get("description") or "")
    return str(raw_goal or "")


def _nested(raw: dict[str, Any], path: tuple[str, ...]) -> object | None:
    current: object = raw
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _expand_optional(value: object | None) -> str | None:
    if value is None:
        return None
    expanded = os.path.expandvars(str(value))
    return expanded or None
