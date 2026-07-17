from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping, Sequence

from agentic_mesh_v5.package_resolver import ContentProvenance
from agentic_mesh_v5.package_resolver import PackageReference
from agentic_mesh_v5.package_resolver import PackageResolutionError
from agentic_mesh_v5.package_resolver import resolve_packages


class PromptRenderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RenderedRoleStatePrompt:
    text: str
    digest: str
    packages: tuple[str, ...]
    provenance: tuple[ContentProvenance, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "digest": self.digest,
            "packages": list(self.packages),
            "provenance": [source.to_dict() for source in self.provenance],
            "text": self.text,
        }


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PromptRenderError(f"effective configuration has no valid {label} object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromptRenderError(f"{label} must be a non-empty string")
    return value.strip()


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PromptRenderError(f"{label} must be a string list")
    return tuple(item.strip() for item in value)


def _bullets(values: Sequence[str], empty: str = "None") -> list[str]:
    return [f"- {value}" for value in values] or [f"- {empty}"]


def _condition(value: object) -> str:
    if value is None:
        return "always"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _consult_lines(value: object) -> list[str]:
    if not isinstance(value, list):
        raise PromptRenderError("state consults must be a list")
    lines: list[str] = []
    for consult in value:
        item = _object(consult, "consult")
        role = _string(item.get("role"), "consult role")
        purpose = _string(item.get("purpose"), "consult purpose")
        lines.append(f"- {role}: {purpose} (when: {_condition(item.get('when'))})")
    return lines or ["- None"]


def _gate_lines(value: object) -> list[str]:
    if not isinstance(value, list):
        raise PromptRenderError("state gates must be a list")
    lines: list[str] = []
    for gate in value:
        item = _object(gate, "gate")
        gate_id = _string(item.get("id"), "gate id")
        gate_type = _string(item.get("type"), "gate type")
        reviewer = item.get("reviewer_role", item.get("requested_from"))
        lines.append(
            f"- {gate_id}: {gate_type}; reviewer={_string(reviewer, 'gate reviewer')}; "
            f"when={_condition(item.get('when'))}"
        )
    return lines or ["- None"]


def _route_lines(value: object) -> list[str]:
    if not isinstance(value, list):
        raise PromptRenderError("state routes must be a list")
    lines: list[str] = []
    for route in value:
        item = _object(route, "route")
        route_id = _string(item.get("id"), "route id")
        outcome = _string(item.get("outcome"), "route outcome")
        target_state = _string(item.get("target_state"), "route target_state")
        target_role = _string(item.get("target_role"), "route target_role")
        lines.append(
            f"- {route_id}: on {outcome}, hand off to {target_role} in {target_state}; "
            f"when={_condition(item.get('when'))}"
        )
    return lines or ["- None"]


def render_role_state_prompt(
    config_root: Path,
    role_reference: str,
    flow_reference: str,
    state_id: str,
) -> RenderedRoleStatePrompt:
    try:
        role_package = PackageReference.parse(role_reference)
        flow_package = PackageReference.parse(flow_reference)
        resolved = resolve_packages(config_root, [role_reference, flow_reference])
    except PackageResolutionError as exc:
        raise PromptRenderError(str(exc)) from exc
    if role_package.kind != "role" or flow_package.kind != "flow":
        raise PromptRenderError("role_reference and flow_reference must select role and flow packages")

    role = _object(resolved.settings.get("role"), "role")
    flow = _object(resolved.settings.get("flow"), "flow")
    role_id = _string(role.get("role_id"), "role_id")
    flow_id = _string(flow.get("flow_id"), "flow_id")
    if role_id != role_package.package_id or flow_id != flow_package.package_id:
        raise PromptRenderError("effective role or flow identity does not match its package")
    states = _object(flow.get("states"), "flow states")
    if state_id not in states:
        raise PromptRenderError(f"unknown flow state: {state_id}")
    state = _object(states[state_id], f"flow state {state_id}")
    rights = _object(role.get("decision_rights"), "role decision_rights")
    owner_role = _string(state.get("owner_role"), "state owner_role")
    terminal = state.get("terminal")
    if not isinstance(terminal, bool):
        raise PromptRenderError("state terminal must be boolean")

    shared_text = [section.text.strip() for section in resolved.text_sections if section.text.strip()]
    sources = [
        f"- {source.package} :: {source.path} :: sha256:{source.sha256}"
        for source in resolved.provenance
    ]
    lines = [
        "# Agentic Mesh effective role prompt",
        "",
        f"Configuration digest: {resolved.digest}",
        f"Role package: {role_reference}",
        f"Flow package: {flow_reference}",
        "",
        "## Shared instructions",
        "",
        *(shared_text or ["No shared text instructions were supplied."]),
        "",
        "## Role",
        "",
        f"Role: {_string(role.get('display_name'), 'display_name')} ({role_id})",
        f"Class: {_string(role.get('role_class'), 'role_class')}",
        f"Purpose: {_string(role.get('purpose'), 'role purpose')}",
        f"Memory scope: {_string(role.get('memory_scope'), 'memory_scope')}",
        "",
        "Accountabilities:",
        *_bullets(_strings(role.get("accountabilities"), "role accountabilities")),
        "",
        "Owns:",
        *_bullets(_strings(rights.get("owns"), "decision_rights.owns")),
        "",
        "Must not:",
        *_bullets(_strings(rights.get("must_not"), "decision_rights.must_not")),
        "",
        "Role instructions:",
        *_bullets(_strings(role.get("instructions"), "role instructions")),
        "",
        "Documentation:",
        *_bullets(_strings(role.get("documentation"), "role documentation")),
        "",
        "## Current SDLC state",
        "",
        f"Flow: {flow_id}",
        f"State: {state_id}",
        f"Owner: {owner_role}",
        f"This role owns the state: {'yes' if role_id == owner_role else 'no'}",
        f"Purpose: {_string(state.get('purpose'), 'state purpose')}",
        f"Artifact: {_string(state.get('artifact'), 'state artifact')}",
        f"Terminal: {'yes' if terminal else 'no'}",
        "",
        "Required consultations:",
        *_consult_lines(state.get("consults")),
        "",
        "Gates:",
        *_gate_lines(state.get("gates")),
        "",
        "Routes and handoffs:",
        *_route_lines(state.get("routes")),
        "",
        "## Configuration provenance",
        "",
        *sources,
        "",
    ]
    return RenderedRoleStatePrompt(
        text="\n".join(lines),
        digest=resolved.digest,
        packages=resolved.packages,
        provenance=resolved.provenance,
    )
