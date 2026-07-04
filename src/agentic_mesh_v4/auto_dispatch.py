from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ACTIVE_WORK_STATES = {
    "implementation",
    "implementation_in_progress",
    "implementation_planned",
    "implementation_ready",
    "quality_review",
    "quality_review_required",
}
TERMINAL_WORK_STATES = {
    "cancelled",
    "canceled",
    "closed",
    "complete",
    "completed",
    "done",
    "released",
    "superseded",
}
HUMAN_WAIT_WORK_STATES = {
    "awaiting_approval",
    "awaiting_decision",
    "awaiting_human",
    "awaiting_review",
    "blocked_on_human",
    "human_review",
}

ATTENTION_NEEDED_OWNER = "project-manager"
STRUCTURED_DISPATCH_KEYS = ("dispatch_effect", "dispatch", "auto_dispatch")


@dataclass(frozen=True)
class WorkItemDispatchContext:
    work_item_id: str
    state: str
    owner_role: str | None = None
    next_action: str = ""
    work_item_type: str | None = None


@dataclass(frozen=True)
class DispatchTarget:
    target_role: str
    next_action: str
    target_state: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class DispatchResolution:
    status: str
    source: str
    targets: tuple[DispatchTarget, ...] = ()
    attention_owner: str | None = None
    attention_reason: str = ""
    evidence: dict[str, Any] | None = None

    @property
    def should_dispatch(self) -> bool:
        return self.status == "dispatch_ready" and bool(self.targets)


def resolve_auto_dispatch(
    *,
    payload: dict[str, Any],
    work_item: WorkItemDispatchContext,
    flow_edges: dict[str, DispatchTarget | str | dict[str, Any]] | None = None,
) -> DispatchResolution:
    """Resolve the Day 1 auto-dispatch contract without creating durable rows."""

    if _is_terminal_or_human_wait(work_item.state):
        return DispatchResolution(
            status="excluded",
            source="state_exclusion",
            evidence={
                "work_item_id": work_item.work_item_id,
                "state": work_item.state,
                "exclusion": "terminal" if work_item.state.casefold() in TERMINAL_WORK_STATES else "human_wait",
            },
        )

    explicit = _targets_from_structured_dispatch(payload, default_next_action=work_item.next_action)
    if explicit:
        return DispatchResolution(
            status="dispatch_ready",
            source="explicit_dispatch_effect",
            targets=explicit,
            evidence={"inference_order": _inference_order(), "work_item_id": work_item.work_item_id},
        )

    handoff_target = _target_from_handoff_contract(payload, default_next_action=work_item.next_action)
    if handoff_target is not None:
        return DispatchResolution(
            status="dispatch_ready",
            source="active_handoff_contract",
            targets=(handoff_target,),
            evidence={"inference_order": _inference_order(), "work_item_id": work_item.work_item_id},
        )

    if work_item.owner_role:
        return DispatchResolution(
            status="dispatch_ready",
            source="owner_role",
            targets=(
                DispatchTarget(
                    target_role=work_item.owner_role,
                    next_action=work_item.next_action,
                    target_state=work_item.state,
                    reason="work_item_owner_role",
                ),
            ),
            evidence={"inference_order": _inference_order(), "work_item_id": work_item.work_item_id},
        )

    flow_target = _target_from_flow_edge(
        state=work_item.state,
        flow_edges=flow_edges or {},
        default_next_action=work_item.next_action,
    )
    if flow_target is not None:
        return DispatchResolution(
            status="dispatch_ready",
            source="flow_yaml_edge",
            targets=(flow_target,),
            evidence={"inference_order": _inference_order(), "work_item_id": work_item.work_item_id},
        )

    return DispatchResolution(
        status="attention_needed",
        source="attention_needed",
        attention_owner=ATTENTION_NEEDED_OWNER,
        attention_reason="Auto-dispatch target is ambiguous; provide a structured dispatch effect, owner_role, or flow edge.",
        evidence={
            "inference_order": _inference_order(),
            "work_item_id": work_item.work_item_id,
            "state": work_item.state,
            "next_action": work_item.next_action,
            "next_action_used_for_target_inference": False,
        },
    )


def requires_dispatch_path(*, state: str, next_action: str | None, owner_role: str | None) -> bool:
    normalized_state = state.casefold()
    if normalized_state in TERMINAL_WORK_STATES or normalized_state in HUMAN_WAIT_WORK_STATES:
        return False
    if owner_role is None:
        return False
    if normalized_state in ACTIVE_WORK_STATES:
        return True
    return looks_actionable_next_action(next_action)


def looks_actionable_next_action(next_action: str | None) -> bool:
    if next_action is None:
        return False
    text = next_action.casefold()
    actionable_terms = (
        "add ",
        "build",
        "create",
        "dispatch",
        "handoff",
        "implement",
        "investigate",
        "plan",
        "review",
        "run ",
        "test",
        "validate",
        "verify",
    )
    return any(term in text for term in actionable_terms)


def _targets_from_structured_dispatch(payload: dict[str, Any], *, default_next_action: str) -> tuple[DispatchTarget, ...]:
    effect = _first_mapping(payload.get(key) for key in STRUCTURED_DISPATCH_KEYS)
    if effect is None:
        return ()
    raw_targets = _sequence(effect.get("targets") or effect.get("handoffs"))
    if not raw_targets and any(key in effect for key in ("target_role", "to_role", "role")):
        raw_targets = [effect]
    targets = tuple(
        target
        for target in (
            _target_from_mapping(item, default_next_action=default_next_action)
            for item in raw_targets
        )
        if target is not None
    )
    return _dedupe_targets(targets)


def _target_from_handoff_contract(payload: dict[str, Any], *, default_next_action: str) -> DispatchTarget | None:
    target_role = _string(payload.get("to_role"))
    if target_role is None:
        return None
    if _string(payload.get("work_item_id")) is None:
        return None
    if _string(payload.get("handoff_id")) is None and not {"from_role", "to_role"}.issubset(payload):
        return None
    return DispatchTarget(
        target_role=target_role,
        next_action=str(payload.get("next_action") or default_next_action),
        target_state=_string(payload.get("state")),
        reason=str(payload.get("reason") or "handoff_contract"),
    )


def _target_from_flow_edge(
    *,
    state: str,
    flow_edges: dict[str, DispatchTarget | str | dict[str, Any]],
    default_next_action: str,
) -> DispatchTarget | None:
    raw = flow_edges.get(state) or flow_edges.get(state.casefold())
    if isinstance(raw, DispatchTarget):
        return raw
    if isinstance(raw, str) and raw:
        return DispatchTarget(target_role=raw, next_action=default_next_action, target_state=state, reason="flow_yaml_edge")
    if isinstance(raw, dict):
        return _target_from_mapping(raw, default_next_action=default_next_action)
    return None


def _target_from_mapping(value: object, *, default_next_action: str) -> DispatchTarget | None:
    item = _mapping(value)
    if item is None:
        return None
    target_role = _string(item.get("target_role") or item.get("to_role") or item.get("role"))
    if target_role is None:
        return None
    return DispatchTarget(
        target_role=target_role,
        next_action=str(item.get("next_action") or item.get("required_next_action") or default_next_action),
        target_state=_string(item.get("target_state") or item.get("state")),
        reason=str(item.get("reason") or "structured_dispatch_effect"),
    )


def _dedupe_targets(targets: tuple[DispatchTarget, ...]) -> tuple[DispatchTarget, ...]:
    seen: set[tuple[str, str | None]] = set()
    deduped: list[DispatchTarget] = []
    for target in targets:
        key = (target.target_role, target.target_state)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return tuple(deduped)


def _is_terminal_or_human_wait(state: str) -> bool:
    normalized = state.casefold()
    return normalized in TERMINAL_WORK_STATES or normalized in HUMAN_WAIT_WORK_STATES


def _inference_order() -> tuple[str, ...]:
    return (
        "explicit_dispatch_effect",
        "active_handoff_contract",
        "owner_role",
        "flow_yaml_edge",
        "attention_needed",
    )


def _mapping(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _sequence(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _first_mapping(values: object) -> dict[str, Any] | None:
    for value in values:
        mapped = _mapping(value)
        if mapped is not None:
            return mapped
    return None
