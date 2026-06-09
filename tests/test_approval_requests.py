from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from agentic_mesh.approval_requests import ApprovalStatusService
from agentic_mesh.config import load_mesh_config
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import FlowGate
from agentic_mesh.storage import FileConnectorOutbox


def _mesh_config():
    return load_mesh_config(Path.cwd())


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


def _service(tmp_path: Path) -> ApprovalStatusService:
    return ApprovalStatusService(mesh_config=_mesh_config(), state_root=tmp_path)


def test_pending_request_serializes_safe_canonical_status(tmp_path: Path) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, created = store.ensure_request(
        work_item_id="work-approval",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    assert created is True
    store.mark_enqueue_succeeded(
        request.response_request_id,
        connector_message_id="conn-msg-1",
    )

    dto = _service(tmp_path).status_for_work_item(
        "work-approval",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
    )
    payload = dto.to_dict()

    assert payload["approval_request_status"] == "pending"
    assert payload["approval_request_status_label"] == "Waiting for approval"
    assert payload["approval_request_id"] == request.response_request_id
    assert payload["response_request_id"] == request.response_request_id
    assert payload["notification_attempt_summary"]["status"] == "queued"
    assert "support_refs" not in json.dumps(payload)
    assert "tenant_id" not in json.dumps(payload)
    assert "service_url" not in json.dumps(payload)


def test_status_service_reports_not_yet_reached_for_future_gate(tmp_path: Path) -> None:
    dto = _service(tmp_path).status_for_work_item(
        "work-future",
        lifecycle_state="implementation",
        gate_id="release_decision_response",
    )

    assert dto.approval_request_status == "not_yet_reached"
    assert dto.approval_request_status_label == "Approval not reached yet"
    assert dto.approval_request_id is None


def test_status_service_derives_timed_out_from_configured_timeout(tmp_path: Path) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-timeout",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    store.mark_enqueue_succeeded(
        request.response_request_id,
        connector_message_id="conn-msg-1",
    )
    queued = store.read(request.response_request_id)
    assert queued is not None
    store._write_request(
        replace(
            queued,
            requested_at=(datetime.now(timezone.utc) - timedelta(hours=72)).isoformat(),
        )
    )

    dto = _service(tmp_path).status_for_work_item(
        "work-timeout",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
    )

    assert dto.approval_request_status == "timed_out"
    assert dto.timeout == "PT48H"
    assert dto.timeout_at is not None
    assert dto.on_timeout == "escalate"


def test_reconcile_dry_run_stale_waiting_is_non_mutating(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = service.reconcile(
        work_item_id="work-stale",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        dry_run=True,
        current_status="waiting_for_human_response",
    )

    payload = result.to_dict()
    assert payload["dry_run"] is True
    assert payload["mutation_performed"] is False
    assert payload["before"]["approval_request_status"] == "stale_waiting"
    assert payload["proposed_after"]["approval_request_status"] == "stale_waiting"
    assert "Dry run only - no approval records" in payload["message"]
    assert FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev").read_all() == []


def test_reconcile_execute_requires_actor_and_reason(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="Actor and reason are required"):
        service.reconcile(
            work_item_id="work-stale",
            lifecycle_state="release_review",
            gate_id="release_decision_response",
            dry_run=False,
            current_status="waiting_for_human_response",
        )


def test_resend_refuses_terminal_status_without_writing_attempt(tmp_path: Path) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-approved",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )
    store.mark_enqueue_succeeded(
        request.response_request_id,
        connector_message_id="conn-msg-1",
    )
    accepted, _, _ = store.validate_response(
        project_id="agentic-mesh-dev",
        work_item_id="work-approved",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        response_type="approve_not_approve",
        response_request_id=request.response_request_id,
        responder="release-sponsor",
        response_value="approved",
    )
    assert accepted is True

    with pytest.raises(ValueError, match="Cannot resend terminal"):
        _service(tmp_path).resend(
            work_item_id="work-approved",
            lifecycle_state="release_review",
            gate_id="release_decision_response",
            actor="operator",
            reason="Do not duplicate terminal approvals.",
        )

    record = store.read(request.response_request_id)
    assert record is not None
    assert len(record.notification_attempts) == 1


def test_supersede_does_not_write_approval_outcome(tmp_path: Path) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-supersede",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )

    dto = _service(tmp_path).mark_superseded(
        work_item_id="work-supersede",
        lifecycle_state="release_review",
        gate_id="release_decision_response",
        actor="operator",
        reason="Approval was replaced by a newer gate attempt.",
    )

    assert dto.approval_request_status == "superseded"
    record = store.read(request.response_request_id)
    assert record is not None
    assert record.response_value is None
    assert record.active is False


def test_approval_resend_cli_enqueues_one_connector_message(tmp_path: Path) -> None:
    store = FileHumanGateRequestStore(tmp_path, "agentic-mesh-dev")
    request, _ = store.ensure_request(
        work_item_id="work-resend",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=_release_gate(),
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "--config-root",
            str(Path.cwd()),
            "--state-root",
            str(tmp_path),
            "approval-resend",
            "--work-item-id",
            "work-resend",
            "--lifecycle-state",
            "release_review",
            "--gate-id",
            "release_decision_response",
            "--actor",
            "operator",
            "--reason",
            "Recover the missing approval card.",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["approval_request_status"] == "pending"
    assert payload["approval_request_id"] == request.response_request_id
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    outbox = FileConnectorOutbox(tmp_path, "agentic-mesh-dev", journal)
    assert outbox.pending_count("approvals") == 1
    updated = store.read(request.response_request_id)
    assert updated is not None
    assert len(updated.notification_attempts) == 2
    assert updated.notification_attempts[-1]["status"] == "queued"
