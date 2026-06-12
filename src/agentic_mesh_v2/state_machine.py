from __future__ import annotations

from dataclasses import dataclass


WORK_ITEM_STATES: frozenset[str] = frozenset(
    {
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
)

TERMINAL_STATES: frozenset[str] = frozenset(
    {"closed", "canceled", "superseded", "failed_terminal"}
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"shaping", "ready", "canceled", "superseded"}),
    "shaping": frozenset(
        {"ready", "waiting_human", "waiting_agent", "blocked", "canceled", "superseded"}
    ),
    "ready": frozenset({"active", "waiting_agent", "canceled", "superseded"}),
    "active": frozenset(
        {
            "waiting_human",
            "waiting_agent",
            "waiting_external",
            "blocked",
            "recovering",
            "release_review",
            "superseded",
            "failed_terminal",
        }
    ),
    "waiting_human": frozenset({"shaping", "active", "release_review", "blocked", "canceled", "superseded"}),
    "waiting_agent": frozenset({"active", "blocked", "recovering", "canceled", "superseded"}),
    "waiting_external": frozenset({"active", "blocked", "recovering", "canceled", "superseded"}),
    "blocked": frozenset({"recovering", "waiting_human", "active", "canceled", "superseded"}),
    "recovering": frozenset({"active", "waiting_agent", "blocked", "superseded", "failed_terminal"}),
    "release_review": frozenset(
        {"deploying", "released", "waiting_human", "active", "blocked", "canceled", "superseded"}
    ),
    "deploying": frozenset({"released", "blocked", "recovering", "failed_terminal"}),
    "released": frozenset({"closed"}),
    "closed": frozenset(),
    "canceled": frozenset(),
    "superseded": frozenset(),
    "failed_terminal": frozenset(),
}

REQUIRED_ATTENTION_STATES: frozenset[str] = frozenset(
    {"waiting_human", "waiting_agent", "waiting_external", "blocked", "recovering"}
)


class StateTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class TransitionRequest:
    work_item_id: str
    from_state: str
    to_state: str
    actor_role: str
    reason: str
    owner: str | None = None
    reason_class: str | None = None
    next_action: str | None = None
    retryable: bool | None = None


def validate_transition(request: TransitionRequest) -> None:
    if request.from_state not in WORK_ITEM_STATES:
        raise StateTransitionError(f"unknown source state `{request.from_state}`")
    if request.to_state not in WORK_ITEM_STATES:
        raise StateTransitionError(f"unknown target state `{request.to_state}`")
    if request.from_state in TERMINAL_STATES:
        raise StateTransitionError(f"terminal state `{request.from_state}` cannot move")
    if request.to_state not in ALLOWED_TRANSITIONS[request.from_state]:
        raise StateTransitionError(
            f"invalid work item transition {request.from_state} -> {request.to_state}"
        )
    if request.to_state in REQUIRED_ATTENTION_STATES:
        missing = [
            name
            for name, value in {
                "owner": request.owner,
                "reason_class": request.reason_class,
                "next_action": request.next_action,
            }.items()
            if not isinstance(value, str) or not value.strip()
        ]
        if request.retryable is None:
            missing.append("retryable")
        if missing:
            raise StateTransitionError(
                f"{request.to_state} requires attention metadata: {', '.join(missing)}"
            )
