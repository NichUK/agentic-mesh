from __future__ import annotations

from agentic_mesh_v4.completion_gate import evaluate_work_continuity


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, *, work_item, continuation=None, decision=None):
        self.work_item = work_item
        self.continuation = continuation
        self.decision = decision

    def execute(self, sql, _params):
        if "FROM work_items" in sql:
            return _Result(self.work_item)
        if "FROM decision_records" in sql:
            return _Result(self.decision)
        if "FROM handoffs" in sql:
            return _Result(self.continuation)
        raise AssertionError(sql)


class _Database:
    def __init__(self, *, work_item, continuation=None, decision=None):
        self.connection = _Connection(
            work_item=work_item,
            continuation=continuation,
            decision=decision,
        )

    def list_safe_output_calls(self, **_kwargs):
        return []


def _evaluate(db):
    return evaluate_work_continuity(
        db=db,
        role_instance_id="project.project-manager.1",
        message_id="current-message",
        turn_id="turn-1",
        work_item_id="work-1",
    )


def test_nonterminal_milestone_without_handoff_fails_completion() -> None:
    result = _evaluate(
        _Database(
            work_item={
                "work_item_id": "work-1",
                "state": "stage1_complete",
                "owner_role": "project-manager",
                "next_action": "Promote the accepted branch.",
            }
        )
    )

    assert result.state == "completed_with_missing_output"
    assert result.missing_predicates[0]["predicate"] == "continuation_recorded"


def test_nonterminal_work_with_queued_handoff_can_complete_turn() -> None:
    result = _evaluate(
        _Database(
            work_item={
                "work_item_id": "work-1",
                "state": "promotion",
                "owner_role": "engineering",
                "next_action": "Push the accepted branch.",
            },
            continuation={
                "handoff_id": "handoff-1",
                "to_role": "engineering",
                "message_id": "queued-message",
                "state": "queued",
            },
        )
    )

    assert result.state == "completed"
    assert result.observed_outputs["work_continuity"][0]["continuity"] == "handoff_queued"


def test_terminal_work_can_complete_without_handoff() -> None:
    result = _evaluate(
        _Database(
            work_item={
                "work_item_id": "work-1",
                "state": "completed",
                "owner_role": "project-manager",
                "next_action": "No further action required.",
            }
        )
    )

    assert result.state == "completed"


def test_human_wait_requires_delivered_sponsor_decision() -> None:
    work_item = {
        "work_item_id": "work-1",
        "state": "blocked_on_human",
        "owner_role": "project-manager",
        "next_action": "Await sponsor decision.",
    }
    missing = _evaluate(_Database(work_item=work_item))
    delivered = _evaluate(
        _Database(
            work_item=work_item,
            decision={
                "decision_id": "decision-1",
                "delivery_id": "delivery-1",
                "activity_id": "teams-activity-1",
            },
        )
    )

    assert missing.state == "completed_with_missing_output"
    assert delivered.state == "completed"
    assert delivered.observed_outputs["work_continuity"][0]["continuity"] == "human_notified"
