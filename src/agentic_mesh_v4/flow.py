from __future__ import annotations

from typing import Any


CONDITION_FIELDS = {
    "architecture_impact": {"not_assessed", "none", "material", "uncertain"},
    "architecture_conformance": {"not_required", "pending", "approved", "changes_requested", "exception"},
}
ARCHITECTURE_DOMAINS = {
    "business",
    "data",
    "application",
    "integration",
    "technology",
    "security",
    "compliance",
    "governance",
    "operating-model",
    "commercial",
    "cross-project",
    "roadmap",
}


class FlowConditionError(ValueError):
    pass


def condition_matches(condition: object, *, context: dict[str, Any]) -> bool:
    if condition is None:
        return True
    if not isinstance(condition, dict) or set(condition) != {"field", "in"}:
        raise FlowConditionError("flow condition must contain exactly 'field' and 'in'")
    field = condition.get("field")
    values = condition.get("in")
    if field not in CONDITION_FIELDS:
        raise FlowConditionError(f"unsupported flow condition field: {field}")
    if not isinstance(values, list) or not values:
        raise FlowConditionError("flow condition 'in' must be a non-empty list")
    invalid = {str(value) for value in values} - CONDITION_FIELDS[str(field)]
    if invalid:
        raise FlowConditionError(f"unsupported values for {field}: {sorted(invalid)}")
    return str(context.get(str(field), "")) in {str(value) for value in values}


def eligible_routes(
    routes: object,
    *,
    context: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    if not isinstance(routes, dict):
        return {}
    eligible: dict[str, dict[str, Any]] = {}
    for route_id, route in routes.items():
        if not isinstance(route, dict):
            continue
        if condition_matches(route.get("when"), context=context):
            eligible[str(route_id)] = route
    return eligible


def resolve_handoff(
    flow: dict[str, Any],
    *,
    state: str,
    outcome: str,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    states = flow.get("states")
    if not isinstance(states, dict):
        return None
    state_config = states.get(state)
    if not isinstance(state_config, dict):
        return None
    return eligible_routes(state_config.get("handoffs"), context=context).get(outcome)


def validate_flow_conditions(flow: dict[str, Any]) -> None:
    states = flow.get("states")
    if not isinstance(states, dict):
        raise FlowConditionError("flow states must be a mapping")
    for state_id, state in states.items():
        if not isinstance(state, dict):
            raise FlowConditionError(f"flow state must be a mapping: {state_id}")
        for collection_name in ("consults", "handoffs", "informs"):
            routes = state.get(collection_name) or {}
            if not isinstance(routes, dict):
                raise FlowConditionError(f"{state_id}.{collection_name} must be a mapping")
            for route_id, route in routes.items():
                if not isinstance(route, dict):
                    raise FlowConditionError(f"{state_id}.{collection_name}.{route_id} must be a mapping")
                condition_matches(route.get("when"), context={})
        for gate in state.get("gates") or []:
            if not isinstance(gate, dict):
                raise FlowConditionError(f"{state_id}.gates entries must be mappings")
            condition_matches(gate.get("when"), context={})


def render_flow_mermaid(flow: dict[str, Any]) -> str:
    validate_flow_conditions(flow)
    states = flow.get("states") or {}
    lines = ["flowchart LR"]
    entry_state = str(flow.get("entry_state") or "")
    if entry_state:
        lines.append(f'  start(["Start"]) --> {_mermaid_id(entry_state)}')
    for state_id, state in states.items():
        if not isinstance(state, dict):
            continue
        state_node = _mermaid_id(str(state_id))
        owner = str(state.get("owner_role") or "unowned")
        lines.append(f'  {state_node}["{_mermaid_label(str(state_id))}<br/>{_mermaid_label(owner)}"]')
        for gate in state.get("gates") or []:
            if not isinstance(gate, dict):
                continue
            gate_id = f"{state_node}_{_mermaid_id(str(gate.get('gate_id') or 'gate'))}"
            condition = _condition_label(gate.get("when"))
            label = f"{gate.get('gate_id', 'gate')}<br/>{gate.get('type', 'gate')}{condition}"
            lines.append(f'  {gate_id}{{"{_mermaid_label(label)}"}}')
            lines.append(f"  {state_node} --> {gate_id}")
        for outcome, handoff in (state.get("handoffs") or {}).items():
            if not isinstance(handoff, dict):
                continue
            target = _mermaid_id(str(handoff.get("target_state") or ""))
            label = f"{outcome}{_condition_label(handoff.get('when'))}"
            lines.append(f'  {state_node} -->|"{_mermaid_label(label)}"| {target}')
        for consult_id, consult in (state.get("consults") or {}).items():
            if not isinstance(consult, dict):
                continue
            target = _mermaid_id(str(consult.get("target_state") or ""))
            label = f"consult: {consult_id}{_condition_label(consult.get('when'))}"
            lines.append(f'  {state_node} -. "{_mermaid_label(label)}" .-> {target}')
        for inform_id, inform in (state.get("informs") or {}).items():
            if not isinstance(inform, dict):
                continue
            target = _mermaid_id(str(inform.get("target_state") or state_id))
            label = f"inform {inform.get('target_role', inform_id)}{_condition_label(inform.get('when'))}"
            lines.append(f'  {state_node} -. "{_mermaid_label(label)}" .-> {target}')
    return "\n".join(lines) + "\n"


def _condition_label(condition: object) -> str:
    if not isinstance(condition, dict):
        return ""
    field = str(condition.get("field") or "")
    values = condition.get("in") or []
    return f" [{field} in {', '.join(str(value) for value in values)}]"


def _mermaid_id(value: str) -> str:
    identifier = "".join(character if character.isalnum() else "_" for character in value)
    return identifier or "unknown"


def _mermaid_label(value: str) -> str:
    return value.replace("_", " ").replace('"', "'")
