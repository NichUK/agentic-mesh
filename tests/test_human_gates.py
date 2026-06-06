from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from agentic_mesh.config import load_mesh_config
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.human_gates import derive_human_gate_summary
from agentic_mesh.models import FlowGate


def _release_gate() -> FlowGate:
    return FlowGate(
        gate_id="release_decision_response",
        type="human_response",
        response_type="approve_not_approve",
        prompt="Record the final release decision for this work item.",
        requested_from="release-sponsor",
        channel="approvals",
        timeout="PT48H",
        on_timeout="escalate",
        completion_criteria={"accepted_values": ["approved"]},
    )


def test_request_store_reuses_active_request(tmp_path: Path) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    first, created = store.ensure_request(
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    second, reused_created = store.ensure_request(
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )

    assert created is True
    assert reused_created is False
    assert second.response_request_id == first.response_request_id
    assert len(store.read_by_work_item("work-abc123")) == 1


def test_request_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")

    with pytest.raises(ValueError):
        store.ensure_request(
            work_item_id="../escape",
            work_item_type="slice",
            lifecycle_state="release_review",
            gate=_release_gate(),
        )


def test_validate_response_completes_only_active_matching_request(tmp_path: Path) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    store.mark_enqueue_succeeded(
        request.response_request_id,
        connector_message_id="conn-msg-123",
    )

    accepted, reason, record = store.validate_response(
        project_id="agentic-mesh-dev",
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        response_type="approve_not_approve",
        response_request_id=request.response_request_id,
        responder="release-sponsor",
        response_value="approved",
    )

    assert accepted is True
    assert reason == "accepted"
    assert record is not None
    assert record.status == "completed"


def test_request_store_persists_decision_context_and_idempotent_duplicate(
    tmp_path: Path,
) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    store.record_decision_context(
        request.response_request_id,
        decision_context={
            "schema_version": "approval-decision-view-v1",
            "state": "requested",
            "response_request_id": request.response_request_id,
            "work_item_id": "work-abc123",
            "lifecycle_state": "release_review",
            "gate_id": "release_decision_response",
            "context_completeness": "complete",
        },
    )

    stored_context = store.read_decision_context(request.response_request_id)
    assert stored_context is not None
    assert stored_context["response_request_id"] == request.response_request_id

    accepted, reason, record = store.validate_response(
        project_id="agentic-mesh-dev",
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        response_type="approve_not_approve",
        response_request_id=request.response_request_id,
        responder="release-sponsor",
        response_value="approved",
    )
    assert accepted is True
    assert reason == "accepted"
    assert record is not None

    duplicate, duplicate_reason, duplicate_record = store.validate_response(
        project_id="agentic-mesh-dev",
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        response_type="approve_not_approve",
        response_request_id=request.response_request_id,
        responder="release-sponsor",
        response_value="approved",
    )

    assert duplicate is True
    assert duplicate_reason == "duplicate_same_value"
    assert duplicate_record is not None
    assert duplicate_record.completed_at == record.completed_at


def test_validate_response_rejects_terminal_value_conflict_without_mutation(
    tmp_path: Path,
) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    accepted, _, record = store.validate_response(
        project_id="agentic-mesh-dev",
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        response_type="approve_not_approve",
        response_request_id=request.response_request_id,
        responder="release-sponsor",
        response_value="approved",
    )
    assert accepted is True
    assert record is not None

    conflict, reason, conflict_record = store.validate_response(
        project_id="agentic-mesh-dev",
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        response_type="approve_not_approve",
        response_request_id=request.response_request_id,
        responder="release-sponsor",
        response_value="not_approved",
    )

    assert conflict is False
    assert reason == "terminal_request_conflict"
    assert conflict_record is not None
    assert conflict_record.status == "completed"
    assert conflict_record.response_value == "approved"
    assert conflict_record.completed_at == record.completed_at


def test_validate_response_rejects_mismatch_without_satisfying_gate(tmp_path: Path) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    store.mark_enqueue_succeeded(
        request.response_request_id,
        connector_message_id="conn-msg-123",
    )

    accepted, reason, record = store.validate_response(
        project_id="agentic-mesh-dev",
        work_item_id="work-different",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        response_type="approve_not_approve",
        response_request_id=request.response_request_id,
        responder="release-sponsor",
        response_value="approved",
    )

    assert accepted is False
    assert reason == "work_item_mismatch"
    assert record is not None
    assert record.status == "invalid_response"


def test_human_gate_summary_shows_future_release_gate(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())

    summary = derive_human_gate_summary(
        mesh_config=mesh_config,
        state_root=tmp_path,
        work_item_id="work-abc123",
        current={"status": "completed", "lifecycle_state": "implementation"},
    )

    assert summary["schema_version"] == "human-gate-summary-v0"
    assert summary["status"] == "not_yet_reached"
    assert summary["next_human_gate"]["gate_id"] == "release_decision_response"


def test_human_gate_summary_maps_request_failed(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-abc123",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    store.mark_failed(
        request.response_request_id,
        reason="approval_request_outbox_unavailable",
    )

    summary = derive_human_gate_summary(
        mesh_config=mesh_config,
        state_root=tmp_path,
        work_item_id="work-abc123",
        current={"status": "waiting_for_human_response", "lifecycle_state": "release_review"},
    )

    assert summary["status"] == "request_failed"
    assert summary["attention_reason"] == "approval_request_outbox_unavailable"


def test_human_gate_summary_derives_timed_out_request(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-timeout",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    store.mark_enqueue_succeeded(
        request.response_request_id,
        connector_message_id="conn-msg-123",
    )
    queued = store.read(request.response_request_id)
    assert queued is not None
    store._write_request(
        replace(
            queued,
            requested_at=(datetime.now(timezone.utc) - timedelta(hours=72)).isoformat(),
        )
    )

    summary = derive_human_gate_summary(
        mesh_config=mesh_config,
        state_root=tmp_path,
        work_item_id="work-timeout",
        current={"status": "waiting_for_human_response", "lifecycle_state": "release_review"},
    )

    assert summary["status"] == "timed_out"
    assert summary["timeout"] == "PT48H"
    assert summary["timeout_at"] is not None
    assert summary["on_timeout"] == "escalate"
