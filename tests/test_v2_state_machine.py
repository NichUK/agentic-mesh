import pytest

from agentic_mesh_v2.state_machine import StateTransitionError
from agentic_mesh_v2.state_machine import TransitionRequest
from agentic_mesh_v2.state_machine import validate_transition


def test_attention_states_require_owner_reason_next_action_and_retryability() -> None:
    with pytest.raises(StateTransitionError, match="attention metadata"):
        validate_transition(
            TransitionRequest(
                work_item_id="work-1",
                from_state="active",
                to_state="blocked",
                actor_role="engineering",
                reason="Provider unavailable.",
            )
        )

    validate_transition(
        TransitionRequest(
            work_item_id="work-1",
            from_state="active",
            to_state="blocked",
            actor_role="engineering",
            reason="Provider unavailable.",
            owner="runtime",
            reason_class="provider_limit",
            next_action="Retry after capacity is restored.",
            retryable=True,
        )
    )


def test_terminal_states_do_not_move() -> None:
    with pytest.raises(StateTransitionError, match="terminal state"):
        validate_transition(
            TransitionRequest(
                work_item_id="work-1",
                from_state="closed",
                to_state="active",
                actor_role="release-manager",
                reason="Nope.",
            )
        )
