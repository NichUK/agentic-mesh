from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputService


def test_implementation_and_test_evidence_safe_outputs_record_work_item_evidence(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-work-item-evidence",
            role_id="engineering",
            role_instance_id="test-project.engineering.1",
            work_item_id="work-evidence",
        )
        service = SafeOutputService(db)
        implementation_call_id = service.record(
            run_id="run-work-item-evidence",
            call=SafeOutputCall(
                role_id="engineering",
                tool_name="implementation.record_change",
                payload={
                    "work_item_id": "work-evidence",
                    "summary": "Implemented compact dashboard table layout.",
                },
            ),
        )
        db.create_run(
            run_id="run-test-evidence",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-evidence",
        )
        test_call_id = service.record(
            run_id="run-test-evidence",
            call=SafeOutputCall(
                role_id="qa-engineer",
                tool_name="test_evidence.record",
                payload={
                    "work_item_id": "work-evidence",
                    "summary": "BDD, unit, and dashboard regression checks passed.",
                },
            ),
        )
        evidence = db.list_work_item_evidence()
        snapshot = db.status_snapshot()
    finally:
        db.close()

    by_type = {row["evidence_type"]: row for row in evidence}
    assert by_type["implementation_change"]["safe_output_ref"] == implementation_call_id
    assert by_type["implementation_change"]["role_id"] == "engineering"
    assert by_type["implementation_change"]["summary"] == "Implemented compact dashboard table layout."
    assert by_type["test_evidence"]["safe_output_ref"] == test_call_id
    assert by_type["test_evidence"]["role_id"] == "qa-engineer"
    assert snapshot["counts"]["work_item_evidence"] == 2
    assert len(snapshot["work_item_evidence"]) == 2


def test_work_item_evidence_safe_output_is_idempotent_by_call_id(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-work-item-evidence-replay",
            role_id="engineering",
            role_instance_id="test-project.engineering.1",
            work_item_id="work-evidence",
        )
        service = SafeOutputService(db)
        call = SafeOutputCall(
            role_id="engineering",
            tool_name="implementation.record_change",
            payload={
                "work_item_id": "work-evidence",
                "summary": "Implemented compact dashboard table layout.",
            },
        )
        call_id = service.record(run_id="run-work-item-evidence-replay", call=call)
        service.process_recorded_call(
            call_id=call_id,
            run_id="run-work-item-evidence-replay",
            call=call,
        )
        evidence = db.list_work_item_evidence()
        event_types = [event["event_type"] for event in db.list_events()]
    finally:
        db.close()

    assert len(evidence) == 1
    assert evidence[0]["safe_output_ref"] == call_id
    assert event_types.count("work_item_evidence.recorded") == 1


def test_work_item_evidence_rejects_unknown_work_item_before_recording(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        db.create_run(
            run_id="run-work-item-evidence-missing",
            role_id="engineering",
            role_instance_id="test-project.engineering.1",
            work_item_id=None,
        )
        with pytest.raises(ValueError, match="unknown work item"):
            SafeOutputService(db).record(
                run_id="run-work-item-evidence-missing",
                call=SafeOutputCall(
                    role_id="engineering",
                    tool_name="implementation.record_change",
                    payload={
                        "work_item_id": "work-missing",
                        "summary": "This should not be recorded.",
                    },
                ),
            )
        calls = db.list_safe_output_calls_for_run("run-work-item-evidence-missing")
        evidence = db.list_work_item_evidence()
    finally:
        db.close()

    assert calls == []
    assert evidence == []


def _work_item(db: V2Database) -> None:
    db.create_queue_item(
        queue_item_id="queue-evidence",
        title="Work item evidence",
        summary="Record implementation and QA evidence from safe outputs.",
        owner_role="engineering",
    )
    db.mark_queue_ready("queue-evidence", actor_role="product-manager", reason="Ready.")
    db.promote_queue_item(
        queue_item_id="queue-evidence",
        work_item_id="work-evidence",
        owner_role="engineering",
    )
