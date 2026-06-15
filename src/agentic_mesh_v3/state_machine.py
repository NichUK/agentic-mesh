from __future__ import annotations

from dataclasses import dataclass


WORK_ITEM_STATES = {
    "queued",
    "shaping",
    "ready",
    "active",
    "waiting_human",
    "waiting_agent",
    "waiting_external",
    "blocked",
    "recovering",
    "release_review",
    "deploying",
    "released",
    "closed",
    "canceled",
    "superseded",
    "failed_terminal",
}

TERMINAL_STATES = {"closed", "canceled", "superseded", "failed_terminal"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"shaping", "ready", "active", "canceled", "superseded"},
    "shaping": {"ready", "waiting_human", "waiting_agent", "blocked", "canceled", "superseded"},
    "ready": {"active", "waiting_agent", "blocked", "canceled", "superseded"},
    "active": {
        "waiting_human",
        "waiting_agent",
        "waiting_external",
        "blocked",
        "recovering",
        "release_review",
        "canceled",
        "superseded",
    },
    "waiting_human": {"shaping", "ready", "active", "release_review", "blocked", "canceled", "superseded"},
    "waiting_agent": {"active", "blocked", "recovering", "release_review", "canceled", "superseded"},
    "waiting_external": {"active", "blocked", "recovering", "release_review", "canceled", "superseded"},
    "blocked": {"recovering", "waiting_human", "waiting_agent", "active", "canceled", "superseded"},
    "recovering": {"active", "waiting_agent", "blocked", "release_review", "failed_terminal"},
    "release_review": {"waiting_human", "deploying", "released", "blocked", "active", "canceled", "superseded"},
    "deploying": {"released", "blocked", "recovering", "failed_terminal"},
    "released": {"closed", "active", "failed_terminal"},
    "closed": set(),
    "canceled": set(),
    "superseded": set(),
    "failed_terminal": set(),
}


@dataclass(frozen=True)
class StateTransition:
    work_item_id: str
    from_state: str | None
    to_state: str
    reason: str = ""


def validate_state(state: str) -> None:
    if state not in WORK_ITEM_STATES:
        raise ValueError(f"unknown V3 work-item state `{state}`")


def validate_transition(transition: StateTransition) -> None:
    validate_state(transition.to_state)
    if transition.from_state is None:
        return
    validate_state(transition.from_state)
    if transition.from_state == transition.to_state:
        return
    if transition.to_state not in ALLOWED_TRANSITIONS[transition.from_state]:
        raise ValueError(
            f"invalid V3 work-item transition for {transition.work_item_id}: "
            f"{transition.from_state} -> {transition.to_state}"
        )
