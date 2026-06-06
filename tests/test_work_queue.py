import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import InvalidTransitionError
from agentic_mesh.work_queue import SourceAnchor
from agentic_mesh.work_queue import WorkQueueError
from agentic_mesh.work_queue import validate_logical_id


def _anchor() -> SourceAnchor:
    return SourceAnchor(
        connector_type="teams",
        connector_id="teams-bot-listener",
        source_scope="all-agents",
        source_message_id="activity/raw-123",
        actor="sponsor-user-id",
        received_at="2026-06-04T10:00:00+00:00",
        display_label="Nich in all-agents",
        external_url="https://teams.example/raw-url",
    )


def _store(tmp_path: Path) -> tuple[FileWorkQueueStore, EventJournal]:
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    return FileWorkQueueStore(tmp_path / "state", "agentic-mesh-dev", journal), journal


def _capture(store: FileWorkQueueStore):
    return store.capture(
        title="Work Queue V0",
        summary="Create a first-class work queue.",
        owner_role="product-manager",
        source_anchor=_anchor(),
        recommended_work_item_type="slice",
        idempotency_key="teams:activity/raw-123",
    )


def test_source_anchor_default_summary_is_redacted() -> None:
    summary = _anchor().redacted_summary()

    assert summary["connector_type"] == "teams"
    assert summary["connector_id"] == "teams-bot-listener"
    assert summary["source_scope"] == "all-agents"
    assert summary["display_label"] == "Nich in all-agents"
    assert summary["source_anchor_ref"].startswith("source:")
    assert "activity/raw-123" not in json.dumps(summary)
    assert "sponsor-user-id" not in json.dumps(summary)
    assert "teams.example" not in json.dumps(summary)


def test_capture_is_idempotent_and_journaled(tmp_path: Path) -> None:
    store, journal = _store(tmp_path)

    first = _capture(store)
    replay = _capture(store)

    assert replay.queue_item_id == first.queue_item_id
    assert store.get(first.queue_item_id) == first
    assert len(store.list_items()) == 1
    event_types = [event["event_type"] for event in journal.read_all()]
    assert event_types == ["queue_item_created"]
    index_files = list((store.root / "index" / "source-keys").glob("*.json"))
    assert len(index_files) == 1
    assert "activity" not in index_files[0].name


def test_invalid_transition_does_not_mutate_updated_at(tmp_path: Path) -> None:
    store, journal = _store(tmp_path)
    item = _capture(store)

    with pytest.raises(InvalidTransitionError):
        store.transition(
            item.queue_item_id,
            "promoted",
            actor_role="product-manager",
            correlation_id="corr-invalid",
        )

    after = store.get(item.queue_item_id)
    assert after is not None
    assert after.status == "captured"
    assert after.updated_at == item.updated_at
    invalid_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "queue_item_invalid_transition"
    ]
    assert invalid_events[0]["attempted_status"] == "promoted"
    assert invalid_events[0]["correlation_id"] == "corr-invalid"


def test_readiness_authority_and_promotion_are_idempotent(tmp_path: Path) -> None:
    store, journal = _store(tmp_path)
    message_store = FileMessageStore(tmp_path / "state", "agentic-mesh-dev", journal)
    item = _capture(store)
    evidence = {
        "requester": "Nich",
        "outcome": "Visible project queue",
        "scope": "Local V0",
        "constraints": "Connector neutral",
        "priority_or_risk_signal": "High",
        "recommended_work_item_type": "slice",
    }

    with pytest.raises(WorkQueueError):
        store.mark_readiness(
            item.queue_item_id,
            actor_role="business-analyst",
            evidence=evidence,
        )
    ready = store.mark_readiness(
        item.queue_item_id,
        actor_role="product-manager",
        evidence=evidence,
    )
    assert ready.status == "ready_for_promotion"

    promotion = store.promote(
        item.queue_item_id,
        actor_role="promotion-service",
        message_store=message_store,
        target_role="business-analyst",
        lifecycle_state="business_analysis",
        work_item_id="work-queue-v0",
        work_item_type="slice",
    )
    replay = store.promote(
        item.queue_item_id,
        actor_role="promotion-service",
        message_store=message_store,
        target_role="business-analyst",
        lifecycle_state="business_analysis",
        work_item_id="work-queue-v0",
        work_item_type="slice",
    )

    assert replay == promotion
    assert message_store.pending_count("business-analyst") == 1
    message = message_store.claim_next("business-analyst", "test")
    assert message is not None
    assert message.payload["queue_item_id"] == item.queue_item_id
    assert message.payload["source_anchor"]["source_anchor_ref"].startswith("source:")


def test_promoted_queue_item_closes_when_lifecycle_work_completed(
    tmp_path: Path,
) -> None:
    store, journal = _store(tmp_path)
    message_store = FileMessageStore(tmp_path / "state", "agentic-mesh-dev", journal)
    item = _capture(store)
    evidence = {
        "requester": "Nich",
        "outcome": "Visible project queue",
        "scope": "Local V0",
        "constraints": "Connector neutral",
        "priority_or_risk_signal": "High",
        "recommended_work_item_type": "slice",
    }
    store.mark_readiness(
        item.queue_item_id,
        actor_role="product-manager",
        evidence=evidence,
    )
    store.promote(
        item.queue_item_id,
        actor_role="promotion-service",
        message_store=message_store,
        target_role="business-analyst",
        lifecycle_state="business_analysis",
        work_item_id="work-queue-v0",
        work_item_type="slice",
    )
    claimed = message_store.claim_next("business-analyst", "test")
    assert claimed is not None
    message_store.complete(claimed, "completed")

    closed = store.reconcile_promoted_closures(message_store=message_store)

    assert [item.queue_item_id for item in closed] == [item.queue_item_id]
    assert store.get(item.queue_item_id).status == "closed"
    assert [event["event_type"] for event in journal.read_all()][-2:] == [
        "queue_item_closed",
        "queue_item_closed_by_reconciliation",
    ]


def test_promoted_queue_item_stays_active_when_lifecycle_work_is_pending(
    tmp_path: Path,
) -> None:
    store, journal = _store(tmp_path)
    message_store = FileMessageStore(tmp_path / "state", "agentic-mesh-dev", journal)
    item = _capture(store)
    evidence = {
        "requester": "Nich",
        "outcome": "Visible project queue",
        "scope": "Local V0",
        "constraints": "Connector neutral",
        "priority_or_risk_signal": "High",
        "recommended_work_item_type": "slice",
    }
    store.mark_readiness(
        item.queue_item_id,
        actor_role="product-manager",
        evidence=evidence,
    )
    store.promote(
        item.queue_item_id,
        actor_role="promotion-service",
        message_store=message_store,
        target_role="business-analyst",
        lifecycle_state="business_analysis",
        work_item_id="work-queue-v0",
        work_item_type="slice",
    )
    journal.append(
        "work_completed",
        project_id="agentic-mesh-dev",
        work_item_id="work-queue-v0",
        work_item_type="slice",
        role_id="business-analyst",
        lifecycle_state="business_analysis",
        message_id="msg-old",
        status="completed",
    )

    closed = store.reconcile_promoted_closures(message_store=message_store)

    assert closed == []
    assert store.get(item.queue_item_id).status == "promoted"


def test_captured_spike_can_be_promoted_to_intake_role_without_readiness(
    tmp_path: Path,
) -> None:
    store, journal = _store(tmp_path)
    message_store = FileMessageStore(tmp_path / "state", "agentic-mesh-dev", journal)
    item = store.capture(
        title="Operational Recovery Spike",
        summary="Investigate smart retry and blocker observability.",
        owner_role="product-manager",
        source_anchor=_anchor(),
        recommended_work_item_type="spike",
        idempotency_key="teams:activity/spike",
    )

    with pytest.raises(WorkQueueError) as wrong_role:
        store.promote(
            item.queue_item_id,
            actor_role="promotion-service",
            message_store=message_store,
            target_role="product-manager",
            lifecycle_state="product_planning",
            work_item_id="work-wrong-role",
            work_item_type="spike",
        )
    assert "business-analyst" in str(wrong_role.value)

    promotion = store.promote(
        item.queue_item_id,
        actor_role="promotion-service",
        message_store=message_store,
        target_role="business-analyst",
        lifecycle_state="business_analysis",
        work_item_id="work-operational-recovery-spike",
        work_item_type="spike",
        message_type="sdlc.intake",
    )

    assert promotion.target_role == "business-analyst"
    assert store.get(item.queue_item_id).status == "promoted"
    message = message_store.claim_next("business-analyst", "test")
    assert message is not None
    assert message.payload["queue_promotion_kind"] == "intake"
    assert message.payload["queue_status_at_promotion"] == "captured"


def test_support_mode_and_purge_are_audited_without_raw_paths(tmp_path: Path) -> None:
    store, journal = _store(tmp_path)
    item = store.capture(
        title="Raw retention",
        summary="Synthetic payload",
        owner_role="business-analyst",
        source_anchor=_anchor(),
        recommended_work_item_type="spike",
        raw_payload={"secret": "synthetic"},
        retain_raw_payload=True,
    )

    with pytest.raises(WorkQueueError):
        store.support_read(item.queue_item_id, actor="", reason="", correlation_id="")

    support = store.support_read(
        item.queue_item_id,
        actor="operator",
        reason="synthetic test",
        correlation_id="corr-support",
    )
    dry_run = store.purge_raw(queue_item_id=item.queue_item_id, dry_run=True)
    executed = store.purge_raw(
        queue_item_id=item.queue_item_id,
        dry_run=False,
        actor="operator",
        reason="synthetic cleanup",
        correlation_id="corr-purge",
    )

    assert support["raw_ref_count"] == 1
    assert "/" not in support["raw_refs"][0]
    assert dry_run["raw_file_count"] == 1
    assert executed["raw_file_count"] == 1
    after = store.get(item.queue_item_id)
    assert after is not None
    assert after.raw_refs == []
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "queue_item_support_accessed" in event_types
    assert "queue_item_purge_dry_run" in event_types
    assert "queue_item_purged" in event_types


def test_logical_id_validation_rejects_path_values() -> None:
    for value in ["", "../queue", "queue/item", "file:queue", "https://queue"]:
        with pytest.raises(WorkQueueError):
            validate_logical_id(value, field_name="queue_item_id")


def test_queue_telemetry_uses_redacted_attributes(tmp_path: Path) -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        store, _ = _store(tmp_path)
        item = _capture(store)
        store.transition(item.queue_item_id, "triaging", actor_role="product-manager")
    finally:
        telemetry.set_test_sink(None)

    queue_logs = [
        log for log in sink.logs if str(log.get("event_type", "")).startswith("queue_item")
    ]
    assert queue_logs
    rendered = json.dumps(queue_logs)
    assert "activity/raw-123" not in rendered
    assert "sponsor-user-id" not in rendered
    assert "teams.example" not in rendered
    assert any(
        metric["name"] == "agentic_mesh.work_queue.depth"
        for metric in sink.metrics
    )


def test_cli_queue_status_is_redacted(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    capture = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "--config-root",
            ".",
            "--state-root",
            str(state_root),
            "work-queue",
            "capture",
            "--title",
            "CLI queue",
            "--summary",
            "Capture through CLI.",
            "--owner-role",
            "business-analyst",
            "--source-message-id",
            "raw/source/123",
            "--actor",
            "raw-user-id",
            "--external-url",
            "https://teams.example/raw",
        ],
        cwd=Path.cwd(),
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(capture.stdout)
    listing = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "--config-root",
            ".",
            "--state-root",
            str(state_root),
            "work-queue",
            "show",
            "--queue-item-id",
            payload["queue_item_id"],
        ],
        cwd=Path.cwd(),
        check=True,
        text=True,
        capture_output=True,
    )

    rendered = listing.stdout
    assert "CLI queue" in rendered
    assert "raw/source/123" not in rendered
    assert "raw-user-id" not in rendered
    assert "teams.example" not in rendered


def test_cli_support_mode_requires_context_without_traceback(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    capture = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "--config-root",
            ".",
            "--state-root",
            str(state_root),
            "work-queue",
            "capture",
            "--title",
            "Support queue",
            "--summary",
            "Capture through CLI.",
            "--owner-role",
            "business-analyst",
        ],
        cwd=Path.cwd(),
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(capture.stdout)

    denied = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "--config-root",
            ".",
            "--state-root",
            str(state_root),
            "work-queue",
            "show",
            "--queue-item-id",
            payload["queue_item_id"],
            "--support",
        ],
        cwd=Path.cwd(),
        check=False,
        text=True,
        capture_output=True,
    )

    assert denied.returncode == 1
    assert denied.stdout == ""
    assert "Traceback" not in denied.stderr
    assert json.loads(denied.stderr)["error"] == (
        "support mode requires actor, reason, and correlation id"
    )


def test_cli_support_mode_allowed_is_audited_and_redacted(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    capture = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "--config-root",
            ".",
            "--state-root",
            str(state_root),
            "work-queue",
            "capture",
            "--title",
            "Support queue",
            "--summary",
            "Capture through CLI.",
            "--owner-role",
            "business-analyst",
            "--raw-payload",
            '{"secret":"synthetic"}',
            "--retain-raw-payload",
        ],
        cwd=Path.cwd(),
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(capture.stdout)

    allowed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "--config-root",
            ".",
            "--state-root",
            str(state_root),
            "work-queue",
            "show",
            "--queue-item-id",
            payload["queue_item_id"],
            "--support",
            "--actor",
            "operator",
            "--reason",
            "synthetic support evidence",
            "--correlation-id",
            "corr-support-cli",
        ],
        cwd=Path.cwd(),
        check=True,
        text=True,
        capture_output=True,
    )

    support_payload = json.loads(allowed.stdout)
    assert support_payload["raw_ref_count"] == 1
    assert "/" not in support_payload["raw_refs"][0]
    assert "synthetic" not in allowed.stdout
    assert "Traceback" not in allowed.stderr
