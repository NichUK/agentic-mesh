from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputError
from agentic_mesh_v2.safe_outputs import SafeOutputService
from agentic_mesh_v2.state_machine import TransitionRequest


def test_quality_approve_requires_test_evidence_and_moves_to_release_review(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _active_work_item(db)
        db.create_run(
            run_id="run-quality-approve",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-quality",
        )
        service = SafeOutputService(db)
        service.record(
            run_id="run-quality-approve",
            call=SafeOutputCall(
                role_id="qa-engineer",
                tool_name="test_evidence.record",
                payload={"work_item_id": "work-quality", "summary": "BDD and regression tests passed."},
            ),
        )
        approve_call = SafeOutputCall(
            role_id="qa-engineer",
            tool_name="quality.approve",
            payload={"work_item_id": "work-quality", "summary": "QA approved release readiness."},
        )
        call_id = service.record(run_id="run-quality-approve", call=approve_call)
        service.process_recorded_call(call_id=call_id, run_id="run-quality-approve", call=approve_call)
        work_item = db.list_work_items()[0]
        assignments = db.list_role_assignments()
        calls = db.list_safe_output_calls_for_run("run-quality-approve")
        transition_events = [
            event
            for event in db.list_events("work-quality")
            if event["event_type"] == "work_item.transitioned"
        ]
    finally:
        db.close()

    assert work_item["state"] == "release_review"
    assert work_item["owner_role"] == "engineering"
    assert work_item["current_role"] == "release-manager"
    assert len(assignments) == 1
    assert assignments[0]["role_id"] == "release-manager"
    assert assignments[0]["work_item_id"] == "work-quality"
    assert assignments[0]["assignment_type"] == "release_review"
    assert assignments[0]["status"] == "queued"
    assert assignments[0]["payload"]["safe_output_ref"] == call_id
    assert "release.request_approval" in assignments[0]["payload"]["allowed_tools"]
    approve_row = next(call for call in calls if call["tool_name"] == "quality.approve")
    assert approve_row["terminal"] is True
    assert sum(1 for event in transition_events if event["payload"]["to_state"] == "release_review") == 1


def test_quality_approve_without_test_evidence_fails_before_recording(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _active_work_item(db)
        db.create_run(
            run_id="run-quality-approve-no-evidence",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-quality",
        )
        with pytest.raises(SafeOutputError, match="requires existing test_evidence"):
            SafeOutputService(db).record(
                run_id="run-quality-approve-no-evidence",
                call=SafeOutputCall(
                    role_id="qa-engineer",
                    tool_name="quality.approve",
                    payload={"work_item_id": "work-quality", "summary": "QA approved without evidence."},
                ),
            )
        calls = db.list_safe_output_calls_for_run("run-quality-approve-no-evidence")
    finally:
        db.close()

    assert calls == []


def test_quality_approve_replay_repairs_missing_release_assignment(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _active_work_item(db)
        db.create_run(
            run_id="run-quality-replay-repair",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-quality",
        )
        service = SafeOutputService(db)
        service.record(
            run_id="run-quality-replay-repair",
            call=SafeOutputCall(
                role_id="qa-engineer",
                tool_name="test_evidence.record",
                payload={"work_item_id": "work-quality", "summary": "Replay repair QA evidence passed."},
            ),
        )
        approve_call = SafeOutputCall(
            role_id="qa-engineer",
            tool_name="quality.approve",
            payload={"work_item_id": "work-quality", "summary": "QA approved before assignment failure."},
        )
        call_id = SafeOutputService(db, process_effects=False).record(
            run_id="run-quality-replay-repair",
            call=approve_call,
        )
        db.transition_work_item(
            TransitionRequest(
                work_item_id="work-quality",
                from_state="active",
                to_state="release_review",
                actor_role="qa-engineer",
                reason="Simulate transition committed before assignment write failed.",
                owner="release-manager",
            )
        )
        service.process_recorded_call(
            call_id=call_id,
            run_id="run-quality-replay-repair",
            call=approve_call,
        )
        service.process_recorded_call(
            call_id=call_id,
            run_id="run-quality-replay-repair",
            call=approve_call,
        )
        assignments = db.list_role_assignments()
        transition_events = [
            event
            for event in db.list_events("work-quality")
            if event["event_type"] == "work_item.transitioned"
        ]
    finally:
        db.close()

    assert len(assignments) == 1
    assert assignments[0]["role_id"] == "release-manager"
    assert assignments[0]["assignment_type"] == "release_review"
    assert assignments[0]["payload"]["safe_output_ref"] == call_id
    assert sum(1 for event in transition_events if event["payload"]["to_state"] == "release_review") == 1


def test_quality_request_changes_moves_to_waiting_agent_with_engineering_attention(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _active_work_item(db)
        db.create_run(
            run_id="run-quality-changes",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-quality",
        )
        change_call = SafeOutputCall(
            role_id="qa-engineer",
            tool_name="quality.request_changes",
            payload={
                "work_item_id": "work-quality",
                "reason": "Dashboard rows wrap incorrectly on mobile.",
            },
        )
        call_id = SafeOutputService(db).record(run_id="run-quality-changes", call=change_call)
        SafeOutputService(db).process_recorded_call(
            call_id=call_id,
            run_id="run-quality-changes",
            call=change_call,
        )
        work_item = db.list_work_items()[0]
        calls = db.list_safe_output_calls_for_run("run-quality-changes")
    finally:
        db.close()

    assert calls[0]["terminal"] is True
    assert work_item["state"] == "waiting_agent"
    assert work_item["current_role"] == "engineering"
    assert work_item["attention_owner"] == "engineering"
    assert work_item["reason_class"] == "quality_changes_requested"
    assert work_item["retryable"] is True
    assert "wrap incorrectly" in work_item["next_action"]


def test_quality_decision_rejects_non_active_work_before_recording(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _ready_work_item(db)
        db.create_run(
            run_id="run-quality-wrong-state",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-quality",
        )
        with pytest.raises(SafeOutputError, match="requires work item state `active`"):
            SafeOutputService(db).record(
                run_id="run-quality-wrong-state",
                call=SafeOutputCall(
                    role_id="qa-engineer",
                    tool_name="quality.request_changes",
                    payload={"work_item_id": "work-quality", "reason": "Not ready for QA yet."},
                ),
            )
        calls = db.list_safe_output_calls_for_run("run-quality-wrong-state")
    finally:
        db.close()

    assert calls == []


def test_connector_backed_quality_decision_runs_core_runtime_effects(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _active_work_item(db)
        adapter = LocalTeamsTestAdapter(db, _connector_config())
        adapter.install()
        service = ConnectorSafeOutputService(db, adapter=adapter)
        db.create_run(
            run_id="run-connector-quality",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-quality",
        )
        service.record(
            run_id="run-connector-quality",
            call=SafeOutputCall(
                role_id="qa-engineer",
                tool_name="test_evidence.record",
                payload={"work_item_id": "work-quality", "summary": "Connector-backed QA evidence passed."},
            ),
        )
        service.record(
            run_id="run-connector-quality",
            call=SafeOutputCall(
                role_id="qa-engineer",
                tool_name="quality.approve",
                payload={"work_item_id": "work-quality", "summary": "Connector-backed QA approved."},
            ),
        )
        work_item = db.list_work_items()[0]
        evidence = db.list_work_item_evidence()
        assignments = db.list_role_assignments()
    finally:
        db.close()

    assert work_item["state"] == "release_review"
    assert work_item["current_role"] == "release-manager"
    assert any(row["evidence_type"] == "test_evidence" for row in evidence)
    assert len(assignments) == 1
    assert assignments[0]["role_id"] == "release-manager"
    assert assignments[0]["assignment_type"] == "release_review"


def _connector_config() -> ConnectorConfig:
    return ConnectorConfig.from_dict(
        {
            "connector_id": "teams-test",
            "project_id": "test-project",
            "connector_type": "teams",
            "display_name": "Test Teams",
            "project_team_ref": "team-test",
            "default_project_channel_ref": "channel-test",
            "external_base_url": "http://linuxch:8100",
            "role_identities": {
                "qa-engineer": {
                    "external_ref": "bot-qa",
                    "display_name": "AM-QA Engineer",
                    "alias": "qa-engineer",
                    "mention_handle": "@AM-QA Engineer",
                    "identity_model": "separate_bot",
                    "enabled": True,
                }
            },
            "human_authorities": {"sponsor": ["sponsor"]},
            "retention": {
                "private_dm_days": 30,
                "project_channel_days": 90,
                "compacted_summary_days": 365,
                "delivery_record_days": 90,
                "idempotency_receipt_days": 30,
            },
            "team_wide_trigger": "@all-agents",
        }
    )


def _ready_work_item(db: V2Database) -> None:
    db.create_queue_item(
        queue_item_id="queue-quality",
        title="Quality decision",
        summary="Exercise QA decision safe outputs.",
        owner_role="engineering",
    )
    db.mark_queue_ready("queue-quality", actor_role="product-manager", reason="Ready.")
    db.promote_queue_item(
        queue_item_id="queue-quality",
        work_item_id="work-quality",
        owner_role="engineering",
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-quality",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Ready for implementation.",
        )
    )


def _active_work_item(db: V2Database) -> None:
    _ready_work_item(db)
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-quality",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering implementation started.",
        )
    )
