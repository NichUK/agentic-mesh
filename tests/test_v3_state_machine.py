import pytest

from agentic_mesh_v3.state_machine import StateTransition
from agentic_mesh_v3.state_machine import validate_state
from agentic_mesh_v3.state_machine import validate_transition


def test_state_machine_accepts_known_states() -> None:
    validate_state("queued")
    validate_state("release_review")
    validate_state("closed")


def test_state_machine_rejects_unknown_state() -> None:
    with pytest.raises(ValueError, match="unknown V3 work-item state"):
        validate_state("done-ish")


def test_state_machine_accepts_forward_transition() -> None:
    validate_transition(StateTransition("work-1", "shaping", "ready"))
    validate_transition(StateTransition("work-1", "waiting_human", "waiting_agent"))
    validate_transition(StateTransition("work-1", "waiting_agent", "deploying"))
    validate_transition(StateTransition("work-1", "waiting_agent", "closed"))
    validate_transition(StateTransition("work-1", "blocked", "release_review"))
    validate_transition(StateTransition("work-1", "blocked", "deploying"))
    validate_transition(StateTransition("work-1", "release_review", "deploying"))
    validate_transition(StateTransition("work-1", "release_review", "recovering"))
    validate_transition(StateTransition("work-1", "released", "waiting_agent"))
    validate_transition(StateTransition("work-1", "released", "closed"))


def test_state_machine_rejects_terminal_reopen_without_explicit_allowed_transition() -> None:
    with pytest.raises(ValueError, match="closed -> active"):
        validate_transition(StateTransition("work-1", "closed", "active"))
