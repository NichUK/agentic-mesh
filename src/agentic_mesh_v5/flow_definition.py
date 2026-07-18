from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Mapping

from agentic_mesh_v5.package_resolver import ResolvedConfiguration


_ID = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")


class FlowDefinitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Condition:
    field: str
    values: tuple[str, ...]

    def matches(self, fields: Mapping[str, object]) -> bool:
        return fields.get(self.field) in self.values


@dataclass(frozen=True, slots=True)
class FlowAction:
    action_id: str
    kind: str
    role_id: str
    payload: Mapping[str, object]
    condition: Condition | None = None

    def applies(self, fields: Mapping[str, object]) -> bool:
        return self.condition is None or self.condition.matches(fields)


@dataclass(frozen=True, slots=True)
class FlowRoute:
    route_id: str
    outcome: str
    target_state: str
    target_role: str
    condition: Condition | None

    def matches(self, outcome: str, fields: Mapping[str, object]) -> bool:
        return self.outcome == outcome and (
            self.condition is None or self.condition.matches(fields)
        )


@dataclass(frozen=True, slots=True)
class FlowState:
    state_id: str
    owner_role: str
    purpose: str
    artifact: str
    consults: tuple[FlowAction, ...]
    gates: tuple[FlowAction, ...]
    informs: tuple[FlowAction, ...]
    routes: tuple[FlowRoute, ...]
    terminal: bool

    def obligations(self, fields: Mapping[str, object]) -> tuple[FlowAction, ...]:
        artifact = FlowAction(
            "artifact", "artifact", self.owner_role, {"path": self.artifact}
        )
        return (artifact,) + tuple(
            action
            for action in (*self.consults, *self.gates, *self.informs)
            if action.applies(fields)
        )

    def select_route(
        self, outcome: str, fields: Mapping[str, object]
    ) -> FlowRoute:
        matches = [route for route in self.routes if route.matches(outcome, fields)]
        if len(matches) != 1:
            raise FlowDefinitionError(
                f"state {self.state_id} requires exactly one route for outcome {outcome}"
            )
        return matches[0]


@dataclass(frozen=True, slots=True)
class FlowDefinition:
    flow_id: str
    leader_role: str
    entry_state: str
    terminal_states: tuple[str, ...]
    states: Mapping[str, FlowState]
    snapshot: Mapping[str, object]
    digest: str

    def state(self, state_id: str) -> FlowState:
        try:
            return self.states[state_id]
        except KeyError as exc:
            raise FlowDefinitionError(f"unknown flow state: {state_id}") from exc


def load_flow(configuration: ResolvedConfiguration) -> FlowDefinition:
    if not isinstance(configuration, ResolvedConfiguration):
        raise FlowDefinitionError("resolved configuration is required")
    value = configuration.settings.get("flow")
    if not isinstance(value, Mapping):
        raise FlowDefinitionError("resolved configuration has no flow object")
    snapshot = json.loads(json.dumps(value, sort_keys=True))
    return validate_flow(snapshot, digest=configuration.digest)


def validate_flow(value: Mapping[str, object], *, digest: str) -> FlowDefinition:
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise FlowDefinitionError("flow schema_version must be 1")
    flow_id = _identifier(value.get("flow_id"), "flow_id")
    leader = _identifier(value.get("leader_role"), "leader_role")
    entry = _identifier(value.get("entry_state"), "entry_state")
    terminal_values = _string_list(value.get("terminal_states"), "terminal_states")
    raw_states = value.get("states")
    if not isinstance(raw_states, Mapping) or not raw_states:
        raise FlowDefinitionError("flow states are required")
    states = {
        _identifier(state_id, "state_id"): _state(state_id, raw)
        for state_id, raw in raw_states.items()
    }
    if entry not in states:
        raise FlowDefinitionError("entry_state is unknown")
    if set(terminal_values) != {
        state_id for state_id, state in states.items() if state.terminal
    }:
        raise FlowDefinitionError("terminal_states do not match terminal flags")
    for state in states.values():
        if state.terminal and state.routes:
            raise FlowDefinitionError(f"terminal state {state.state_id} has routes")
        if not state.terminal and not state.routes:
            raise FlowDefinitionError(f"non-terminal state {state.state_id} is a dead end")
        for route in state.routes:
            target = states.get(route.target_state)
            if target is None:
                raise FlowDefinitionError(f"route {route.route_id} has unknown target")
            if target.owner_role != route.target_role:
                raise FlowDefinitionError(f"route {route.route_id} target owner disagrees")
        _reject_overlapping_routes(state)
    reachable = {entry}
    pending = [entry]
    while pending:
        for route in states[pending.pop()].routes:
            if route.target_state not in reachable:
                reachable.add(route.target_state)
                pending.append(route.target_state)
    if reachable != set(states):
        raise FlowDefinitionError("flow contains unreachable states")
    return FlowDefinition(
        flow_id,
        leader,
        entry,
        terminal_values,
        states,
        json.loads(json.dumps(value, sort_keys=True)),
        _digest(digest),
    )


def _state(state_id: object, value: object) -> FlowState:
    if not isinstance(value, Mapping):
        raise FlowDefinitionError(f"state {state_id} must be an object")
    owner = _identifier(value.get("owner_role"), "owner_role")
    purpose = _required(value.get("purpose"), "purpose")
    artifact = _required(value.get("artifact"), "artifact")
    if "{work_item_id}" not in artifact or artifact.startswith(("/", "\\")):
        raise FlowDefinitionError(f"state {state_id} artifact path is invalid")
    consults = _actions(value.get("consults", []), "consult", owner)
    gates = _actions(value.get("gates", []), "gate", owner)
    informs = _actions(value.get("informs", []), "inform", owner)
    routes = _routes(value.get("routes", []))
    identifiers = ["artifact"]
    identifiers += [item.action_id for item in (*consults, *gates, *informs)]
    identifiers += [item.route_id for item in routes]
    if len(identifiers) != len(set(identifiers)):
        raise FlowDefinitionError(f"state {state_id} has duplicate action ids")
    terminal = value.get("terminal")
    if type(terminal) is not bool:
        raise FlowDefinitionError(f"state {state_id} terminal must be boolean")
    return FlowState(
        str(state_id), owner, purpose, artifact, consults, gates, informs, routes, terminal
    )


def _actions(value: object, kind: str, owner: str) -> tuple[FlowAction, ...]:
    if not isinstance(value, list):
        raise FlowDefinitionError(f"{kind}s must be an array")
    actions = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise FlowDefinitionError(f"{kind} must be an object")
        if kind == "gate":
            role_keys = [
                key for key in ("reviewer_role", "requested_from") if key in raw
            ]
            if len(role_keys) > 1:
                raise FlowDefinitionError(
                    "gate must use either reviewer_role or requested_from"
                )
            role_key = role_keys[0] if role_keys else "reviewer_role"
        else:
            role_key = "role"
        role = _identifier(raw.get(role_key, owner), role_key)
        action_id = _identifier(raw.get("id", f"{kind}-{index + 1}"), f"{kind}_id")
        payload = {key: item for key, item in raw.items() if key != "when"}
        actions.append(FlowAction(action_id, kind, role, payload, _condition(raw.get("when"))))
    return tuple(actions)


def _routes(value: object) -> tuple[FlowRoute, ...]:
    if not isinstance(value, list):
        raise FlowDefinitionError("routes must be an array")
    routes = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise FlowDefinitionError("route must be an object")
        routes.append(
            FlowRoute(
                _identifier(raw.get("id"), "route_id"),
                _identifier(raw.get("outcome"), "outcome"),
                _identifier(raw.get("target_state"), "target_state"),
                _identifier(raw.get("target_role"), "target_role"),
                _condition(raw.get("when")),
            )
        )
    return tuple(routes)


def _condition(value: object) -> Condition | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or len(value) != 1:
        raise FlowDefinitionError("condition must contain exactly one field")
    field, values = next(iter(value.items()))
    return Condition(_identifier(field, "condition field"), _string_list(values, "condition values"))


def _reject_overlapping_routes(state: FlowState) -> None:
    for index, route in enumerate(state.routes):
        for other in state.routes[index + 1 :]:
            if route.outcome != other.outcome:
                continue
            if _conditions_overlap(route.condition, other.condition):
                raise FlowDefinitionError(
                    f"state {state.state_id} has overlapping routes for outcome "
                    f"{route.outcome}: {route.route_id}, {other.route_id}"
                )


def _conditions_overlap(left: Condition | None, right: Condition | None) -> bool:
    if left is None or right is None:
        return True
    if left.field != right.field:
        return True
    return bool(set(left.values).intersection(right.values))


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise FlowDefinitionError(f"{label} must be a non-empty array")
    result = tuple(_identifier(item, label) for item in value)
    if len(result) != len(set(result)):
        raise FlowDefinitionError(f"{label} must be unique")
    return result


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise FlowDefinitionError(f"{label} is invalid")
    return value


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise FlowDefinitionError(f"{label} is invalid")
    return value.strip()


def _digest(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise FlowDefinitionError("flow digest is invalid")
    return value
