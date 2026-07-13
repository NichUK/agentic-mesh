from __future__ import annotations

import os
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from agentic_mesh_v4.flow import validate_flow_conditions


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
    agent_network_id: str = "agentic-mesh-dev"
    instances: int = 1
    authority: str = "scoped"
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "high"
    plan_mode_reasoning_effort: str = "xhigh"
    show_raw_agent_reasoning: bool = True
    sandbox_mode: str = "workspace-write"
    approval_policy: str = "never"
    codex_port: int = 4700
    idle_timeout_seconds: int = 900
    instructions: tuple[str, ...] = ()

    @property
    def role_instance_id(self) -> str:
        return f"{self.agent_network_id}.{self.role_id}.1"

    @property
    def service_name(self) -> str:
        return f"{_service_project_id(self.agent_network_id)}-{self.role_id}-1"


@dataclass(frozen=True)
class V4ProjectConfig:
    project_id: str
    name: str
    goal: str
    roles: tuple[V4RoleConfig, ...]
    agent_network_id: str = "agentic-mesh-dev"
    document_root: str = "/documents"
    document_structure_policy: str = "togaf-sdlc-v1"
    document_accountabilities: dict[str, dict[str, Any]] | None = None
    flow: dict[str, Any] | None = None
    teams_public_endpoint: str | None = None
    teams_project_channel_id: str | None = None
    teams_project_team_id: str | None = None

    def role(self, role_id: str) -> V4RoleConfig:
        for role in self.roles:
            if role.role_id == role_id:
                return role
        raise KeyError(role_id)

    def accountabilities_for_role(self, role_id: str) -> dict[str, dict[str, Any]]:
        return {
            path: details
            for path, details in (self.document_accountabilities or {}).items()
            if details.get("owner_role") == role_id or role_id in details.get("contributing_roles", [])
        }


def load_project_config(path: str | Path) -> V4ProjectConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"project config must be a mapping: {config_path}")

    project_id = str(raw.get("project_id") or "agentic-mesh-dev")
    agent_network_id = str(raw.get("agent_network_id") or _nested(raw, ("agent_mesh", "id")) or "agentic-mesh-dev")
    worker_defaults = raw.get("worker_defaults")
    if worker_defaults is not None and not isinstance(worker_defaults, dict):
        raise ValueError("worker_defaults must be a mapping")
    roles = _roles_from_raw(
        raw.get("roles"),
        agent_network_id=agent_network_id,
        worker_defaults=worker_defaults or {},
    )
    flow = _resolve_flow(raw.get("flow"), config_path=config_path)
    if flow:
        validate_flow_conditions(flow)
    return V4ProjectConfig(
        project_id=project_id,
        agent_network_id=agent_network_id,
        name=str(raw.get("name") or raw.get("project_id") or "Agentic Mesh"),
        goal=_goal_description(raw.get("goal")),
        roles=roles,
        document_root=str(_nested(raw, ("document_library", "root_path")) or "/documents"),
        document_structure_policy=str(
            _nested(raw, ("document_library", "structure_policy")) or "togaf-sdlc-v1"
        ),
        document_accountabilities=_mapping_of_mappings(raw.get("document_accountabilities")),
        flow=flow,
        teams_public_endpoint=_expand_optional(_nested(raw, ("connectors", "teams", "ingress", "public_endpoint"))),
        teams_project_channel_id=_expand_optional(_nested(raw, ("connectors", "teams", "channels", "project", "id"))),
        teams_project_team_id=_expand_optional(_nested(raw, ("connectors", "teams", "team", "id"))),
    )


def _roles_from_raw(
    raw_roles: object,
    *,
    agent_network_id: str,
    worker_defaults: dict[str, Any],
) -> tuple[V4RoleConfig, ...]:
    if not isinstance(raw_roles, dict) or not raw_roles:
        return tuple(
            _default_role(
                role_id,
                index,
                agent_network_id=agent_network_id,
                worker_defaults=worker_defaults,
            )
            for index, role_id in enumerate(DEFAULT_ROLE_IDS)
        )

    roles: list[V4RoleConfig] = []
    for index, role_id in enumerate(DEFAULT_ROLE_IDS):
        item = raw_roles.get(role_id)
        if not isinstance(item, dict):
            roles.append(
                _default_role(
                    role_id,
                    index,
                    agent_network_id=agent_network_id,
                    worker_defaults=worker_defaults,
                )
            )
            continue
        worker = dict(worker_defaults)
        if isinstance(item.get("worker"), dict):
            worker.update(item["worker"])
        roles.append(
            V4RoleConfig(
                agent_network_id=agent_network_id,
                role_id=role_id,
                display_name=_display_name(role_id),
                template=str(item.get("template") or role_id),
                instances=int(item.get("instances") or 1),
                authority="full" if role_id in FULL_ACCESS_ROLES else "scoped",
                model=str(worker.get("model") or "gpt-5.6-sol"),
                reasoning_effort=str(worker.get("reasoning_effort") or "high"),
                plan_mode_reasoning_effort=str(worker.get("plan_mode_reasoning_effort") or "xhigh"),
                show_raw_agent_reasoning=_bool_value(worker.get("show_raw_agent_reasoning"), default=True),
                sandbox_mode=_sandbox_mode(role_id, str(worker.get("sandbox_mode") or "")),
                approval_policy=_approval_policy(role_id, str(worker.get("approval_policy") or "")),
                codex_port=4700 + index,
                idle_timeout_seconds=int(item.get("idle_timeout_seconds") or 900),
                instructions=tuple(str(value) for value in item.get("instructions") or ()),
            )
        )
    return tuple(roles)


def _default_role(
    role_id: str,
    index: int,
    *,
    agent_network_id: str,
    worker_defaults: dict[str, Any],
) -> V4RoleConfig:
    return V4RoleConfig(
        agent_network_id=agent_network_id,
        role_id=role_id,
        display_name=_display_name(role_id),
        template=role_id,
        authority="full" if role_id in FULL_ACCESS_ROLES else "scoped",
        model=str(worker_defaults.get("model") or "gpt-5.6-sol"),
        reasoning_effort=str(worker_defaults.get("reasoning_effort") or "high"),
        plan_mode_reasoning_effort=str(worker_defaults.get("plan_mode_reasoning_effort") or "xhigh"),
        show_raw_agent_reasoning=_bool_value(worker_defaults.get("show_raw_agent_reasoning"), default=True),
        sandbox_mode=_sandbox_mode(role_id, str(worker_defaults.get("sandbox_mode") or "")),
        approval_policy=_approval_policy(role_id, str(worker_defaults.get("approval_policy") or "")),
        codex_port=4700 + index,
    )


def _sandbox_mode(role_id: str, configured: str) -> str:
    if configured:
        return configured
    if role_id in FULL_ACCESS_ROLES:
        return "danger-full-access"
    return "workspace-write"


def _approval_policy(role_id: str, configured: str) -> str:
    if configured:
        return configured
    return "never"


def _bool_value(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"expected a boolean value, got {value!r}")


def _display_name(role_id: str) -> str:
    acronyms = {"qa": "QA", "ux": "UX"}
    parts = []
    for part in role_id.split("-"):
        parts.append(acronyms.get(part, part.capitalize()))
    return " ".join(parts)


def _service_project_id(project_id: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in project_id.lower())
    return safe.strip("-_") or "agentic-mesh"


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


def _mapping_of_mappings(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): deepcopy(item)
        for key, item in value.items()
        if isinstance(item, dict)
    }


def _resolve_flow(value: object, *, config_path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    if "states" in value:
        return deepcopy(value)
    template = value.get("template")
    if not isinstance(template, str) or not template:
        return {}
    if not all(character.isalnum() or character == "-" for character in template):
        raise ValueError(f"invalid flow template: {template}")
    candidates = []
    system_root = os.environ.get("AGENTIC_MESH_SYSTEM_ROOT")
    if system_root:
        candidates.append(Path(system_root) / "config" / "flows" / f"{template}.yaml")
    candidates.extend(
        [
            Path(__file__).resolve().parents[2] / "config" / "flows" / f"{template}.yaml",
            config_path.parent / "flows" / f"{template}.yaml",
        ]
    )
    flow_path = next((candidate for candidate in candidates if candidate.exists()), None)
    if flow_path is None:
        searched = ", ".join(str(candidate) for candidate in candidates)
        raise ValueError(f"flow template {template!r} was not found; searched: {searched}")
    parsed = yaml.safe_load(flow_path.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"flow template must be a mapping: {flow_path}")
    overrides = value.get("overrides")
    return _deep_merge(parsed, overrides if isinstance(overrides, dict) else {})


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged
