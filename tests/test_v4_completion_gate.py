from __future__ import annotations

from typing import Any

from agentic_mesh_v4.completion_gate import CompletionContract
from agentic_mesh_v4.completion_gate import CompletionPredicate
from agentic_mesh_v4.completion_gate import evaluate_completion_contract


WORK_ITEM_ID = "work-qos-pb-02-ado-428"


class _EmptyConnection:
    def execute(self, _sql: str, _params: tuple[object, ...]) -> list[dict[str, Any]]:
        return []


class _SafeOutputDb:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls
        self.connection = _EmptyConnection()

    def list_safe_output_calls(self, **filters: object) -> list[dict[str, Any]]:
        return [
            item
            for item in self.calls
            if all(value is None or item.get(key) == value for key, value in filters.items())
        ]


def _handoff_contract() -> CompletionContract:
    return CompletionContract(
        required=(
            CompletionPredicate(
                predicate="handoff_recorded",
                work_item_id=WORK_ITEM_ID,
                tool_name="handoff.require",
                next_action="Return the probe result.",
            ),
        ),
        source="message_payload",
    )


def test_completion_observes_message_correlated_cli_handoff_across_role_instance_metadata() -> None:
    db = _SafeOutputDb(
        [
            {
                "call_id": "call-exact-artifact",
                "role_instance_id": "example-project.project-manager.2",
                "tool_name": "document.link_artifact",
                "message_id": "message-1",
                "turn_id": "turn-1",
                "work_item_id": WORK_ITEM_ID,
            },
            {
                "call_id": "call-cli-handoff",
                "role_instance_id": "example-project.project-manager.1",
                "tool_name": "handoff.require",
                "message_id": "message-1",
                "turn_id": None,
                "work_item_id": WORK_ITEM_ID,
            },
        ]
    )

    result = evaluate_completion_contract(
        db=db,
        contract=_handoff_contract(),
        role_instance_id="example-project.project-manager.2",
        message_id="message-1",
        turn_id="turn-1",
    )

    assert result.state == "completed"
    assert [item["call_id"] for item in result.observed_outputs["safe_output_calls"]] == [
        "call-exact-artifact",
        "call-cli-handoff",
    ]


def test_completion_does_not_observe_cli_handoff_from_another_message() -> None:
    db = _SafeOutputDb(
        [
            {
                "call_id": "call-exact-artifact",
                "role_instance_id": "example-project.project-manager.2",
                "tool_name": "document.link_artifact",
                "message_id": "message-1",
                "turn_id": "turn-1",
                "work_item_id": WORK_ITEM_ID,
            },
            {
                "call_id": "call-unrelated-handoff",
                "role_instance_id": "example-project.project-manager.1",
                "tool_name": "handoff.require",
                "message_id": "message-older",
                "turn_id": None,
                "work_item_id": WORK_ITEM_ID,
            },
        ]
    )

    result = evaluate_completion_contract(
        db=db,
        contract=_handoff_contract(),
        role_instance_id="example-project.project-manager.2",
        message_id="message-1",
        turn_id="turn-1",
    )

    assert result.state == "completed_with_missing_output"
    assert result.observed_outputs["safe_output_calls"] == [
        {
            "call_id": "call-exact-artifact",
            "tool_name": "document.link_artifact",
            "message_id": "message-1",
            "turn_id": "turn-1",
            "work_item_id": WORK_ITEM_ID,
        }
    ]


def test_completion_does_not_observe_message_correlated_handoff_from_another_role() -> None:
    db = _SafeOutputDb(
        [
            {
                "call_id": "call-other-role-handoff",
                "role_instance_id": "example-project.release-manager.1",
                "tool_name": "handoff.require",
                "message_id": "message-1",
                "turn_id": None,
                "work_item_id": WORK_ITEM_ID,
            },
        ]
    )

    result = evaluate_completion_contract(
        db=db,
        contract=_handoff_contract(),
        role_instance_id="example-project.project-manager.2",
        message_id="message-1",
        turn_id="turn-1",
    )

    assert result.state == "completed_with_missing_output"
