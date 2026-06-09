import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_mesh import telemetry
from agentic_mesh.external_actions import ACTION_CAPTURE_WORK
from agentic_mesh.external_actions import ACTION_MARK_READY
from agentic_mesh.external_actions import ACTION_PROMOTE
from agentic_mesh.external_actions import ACTION_SHOW_STATUS
from agentic_mesh.external_actions import ACTION_TRANSITION
from agentic_mesh.external_actions import OUTCOME_ACTION_DENIED
from agentic_mesh.external_actions import OUTCOME_CAPTURED
from agentic_mesh.external_actions import OUTCOME_DUPLICATE
from agentic_mesh.external_actions import ExternalActionPolicy
from agentic_mesh.external_actions import ExternalActionRequest
from agentic_mesh.external_actions import ExternalActionService
from agentic_mesh.external_actions import ExternalActionTarget
from agentic_mesh.external_actions import ExternalActor
from agentic_mesh.external_actions import FileExternalActionStore
from agentic_mesh.external_actions import QueueReceiptNotificationService
from agentic_mesh.external_actions import derive_idempotency_key
from agentic_mesh.external_actions import status_location_for
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import NotificationPolicyConfig
from agentic_mesh.models import NotificationSurfaceConfig
from agentic_mesh.notifications import FileNotificationAttemptStore
from agentic_mesh.notifications import FileSourceRouteStore
from agentic_mesh.notifications import SourceRouteRecord
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import SourceAnchor
from agentic_mesh.work_queue import WorkQueueError


def _anchor() -> SourceAnchor:
    return SourceAnchor(
        connector_type="teams",
        connector_id="teams-bot-listener",
        source_scope="all-agents",
        source_message_id="activity/raw-123",
        actor="sponsor-user-id",
        received_at="2026-06-05T10:00:00+00:00",
        display_label="Sponsor <script> in all-agents",
        external_url="https://teams.example/raw-url",
        extensions={"channel_id": "raw-channel"},
    )


def _service(
    tmp_path: Path,
    *,
    allow_cli_promotion: bool = False,
) -> tuple[ExternalActionService, FileWorkQueueStore, EventJournal, FileMessageStore]:
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    work_queue = FileWorkQueueStore(tmp_path / "state", "agentic-mesh-dev", journal)
    message_store = FileMessageStore(tmp_path / "state", "agentic-mesh-dev", journal)
    connector_outbox = FileConnectorOutbox(
        tmp_path / "state",
        "agentic-mesh-dev",
        journal,
    )
    notification_policy = NotificationPolicyConfig(
        surfaces={
            "status_fallback": NotificationSurfaceConfig(
                surface_id="status_fallback",
                connector="teams",
                route="all-agents",
                label="project status fallback",
            )
        }
    )
    service = ExternalActionService(
        project_id="agentic-mesh-dev",
        work_queue=work_queue,
        store=FileExternalActionStore(tmp_path / "state", "agentic-mesh-dev"),
        journal=journal,
        policy=ExternalActionPolicy(allow_cli_promotion=allow_cli_promotion),
        message_store=message_store,
        notification_service=QueueReceiptNotificationService(
            project_id="agentic-mesh-dev",
            policy=notification_policy,
            work_queue=work_queue,
            attempt_store=FileNotificationAttemptStore(
                tmp_path / "state",
                "agentic-mesh-dev",
            ),
            connector_outbox=connector_outbox,
            source_route_store=FileSourceRouteStore(
                tmp_path / "state",
                "agentic-mesh-dev",
            ),
        ),
    )
    return service, work_queue, journal, message_store


def _capture_request(*, key: str = "source-key") -> ExternalActionRequest:
    return ExternalActionRequest(
        action_type=ACTION_CAPTURE_WORK,
        source_type="cli",
        source_anchor=_anchor(),
        actor=ExternalActor(actor_type="local_operator", display_label="operator"),
        payload={
            "title": "Unified intake <b>slice</b>",
            "summary": "Capture through shared contracts.",
            "owner_role": "product-manager",
            "recommended_work_item_type": "slice",
        },
        idempotency_key=key,
        correlation_id="corr-external-capture",
    )


def test_contract_serializers_redact_connector_private_metadata() -> None:
    request = _capture_request()
    safe = request.to_safe_dict()
    rendered = json.dumps(safe)

    assert safe["schema_version"] == "external-action-v0"
    assert safe["action_type"] == "capture_work"
    assert safe["source_anchor_summary"]["source_anchor_ref"].startswith("source:")
    assert "activity/raw-123" not in rendered
    assert "sponsor-user-id" not in rendered
    assert "teams.example" not in rendered
    assert "raw-channel" not in rendered
    assert "<script>" not in rendered


def test_status_location_uses_relative_routes_without_external_url() -> None:
    location = status_location_for("queue_item", "queue-safe").to_safe_dict()

    assert location["schema_version"] == "status-location-v0"
    assert location["relative_path"] == "/work-queue/queue-safe"
    assert location["json_path"] == "/work-queue.json"
    assert location["external_url"] is None
    assert "/tmp" not in json.dumps(location)


def test_capture_records_receipt_and_replay_does_not_duplicate(tmp_path: Path) -> None:
    service, work_queue, journal, _ = _service(tmp_path)

    first = service.execute(_capture_request())
    replay = service.execute(_capture_request())

    assert first.outcome == OUTCOME_CAPTURED
    assert replay.outcome == OUTCOME_DUPLICATE
    assert replay.target.queue_item_id == first.target.queue_item_id
    assert len(work_queue.list_items()) == 1
    receipts, errors = service.store.list_receipts()
    assert len(receipts) == 1
    assert errors == []
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "external_action_received" in event_types
    assert "external_action_receipt_recorded" in event_types
    assert "external_action_replay_detected" in event_types


def test_capture_records_fallback_notification_before_outbox_message(
    tmp_path: Path,
) -> None:
    service, work_queue, journal, _ = _service(tmp_path)

    receipt = service.execute(_capture_request())

    item = work_queue.get(receipt.target.queue_item_id)
    assert item is not None
    assert receipt.notification_state == "queued"
    assert item.notification is not None
    assert item.notification.status == "queued"
    assert item.notification.event_kind == "queue.captured"
    assert item.notification.selected_surface_key == "status_fallback"
    assert item.notification.fallback_reason == "source_route_missing"
    outbox_files = list(
        (tmp_path / "state").glob(
            "projects/agentic-mesh-dev/connector_outbox/all-agents/pending/*.json"
        )
    )
    assert len(outbox_files) == 1
    outbox_payload = json.loads(outbox_files[0].read_text(encoding="utf-8"))
    assert outbox_payload["type"] == "notification.event"
    assert outbox_payload["payload"]["event_kind"] == "queue.captured"
    assert outbox_payload["payload"]["notification_attempt_id"] == item.notification.attempt_id
    event_types = [event["event_type"] for event in journal.read_all()]
    assert event_types.index("queue_item_notification_queued") < event_types.index(
        "connector_message_queued"
    )


def test_replay_returns_existing_receipt_without_duplicate_notification(
    tmp_path: Path,
) -> None:
    service, _, _, _ = _service(tmp_path)

    first = service.execute(_capture_request())
    replay = service.execute(_capture_request())

    outbox_files = list(
        (tmp_path / "state").glob(
            "projects/agentic-mesh-dev/connector_outbox/all-agents/pending/*.json"
        )
    )
    assert first.notification_state == "queued"
    assert replay.outcome == OUTCOME_DUPLICATE
    assert replay.notification_state == "queued"
    assert len(outbox_files) == 1


def test_readiness_default_is_dashboard_only_and_blocked_notifies(
    tmp_path: Path,
) -> None:
    service, work_queue, _, _ = _service(tmp_path)
    captured = service.execute(_capture_request())
    evidence = {
        "requester": "Sponsor",
        "outcome": "Shared external receipts",
        "scope": "Local contract slice",
        "constraints": "Connector neutral",
        "priority_or_risk_signal": "High",
        "recommended_work_item_type": "slice",
    }

    ready = service.execute(
        ExternalActionRequest(
            action_type=ACTION_MARK_READY,
            source_type="cli",
            source_anchor=_anchor(),
            actor=ExternalActor(
                actor_type="local_operator",
                display_label="product-manager",
                role_id="product-manager",
            ),
            target=ExternalActionTarget(queue_item_id=captured.target.queue_item_id),
            payload={"evidence": evidence},
            correlation_id="corr-ready",
        )
    )
    blocker_capture = service.execute(_capture_request(key="blocker-key"))
    blocked = service.execute(
        ExternalActionRequest(
            action_type=ACTION_TRANSITION,
            source_type="cli",
            source_anchor=_anchor(),
            actor=ExternalActor(
                actor_type="local_operator",
                display_label="product-manager",
                role_id="product-manager",
            ),
            target=ExternalActionTarget(queue_item_id=blocker_capture.target.queue_item_id),
            payload={"status": "blocked"},
            reason="Needs sponsor input <script>",
            correlation_id="corr-blocked",
        )
    )

    assert ready.notification_state == "dashboard_only"
    assert blocked.notification_state == "queued"
    item = work_queue.get(blocker_capture.target.queue_item_id)
    assert item is not None
    assert item.notification is not None
    assert item.notification.event_kind == "queue.blocked"
    assert item.notification.reason == "source_route_missing"


def test_source_route_is_preferred_before_fallback(tmp_path: Path) -> None:
    service, work_queue, _, _ = _service(tmp_path)
    source_routes = FileSourceRouteStore(tmp_path / "state", "agentic-mesh-dev")
    source_routes.write(
        SourceRouteRecord(
            source_anchor_ref=_anchor().source_anchor_ref(),
            connector_type="teams",
            connector_id="teams",
            route_label="request thread",
            route_kind="source_thread",
            raw_route={"route": "intake"},
            capabilities=("notification_event",),
        )
    )

    receipt = service.execute(_capture_request(key="source-route-key"))

    item = work_queue.get(receipt.target.queue_item_id)
    assert item is not None
    assert item.notification is not None
    assert item.notification.selected_surface_key == "source_thread"
    assert item.notification.route_kind == "source_thread"
    assert item.notification.fallback_reason is None
    source_outbox = list(
        (tmp_path / "state").glob(
            "projects/agentic-mesh-dev/connector_outbox/intake/pending/*.json"
        )
    )
    assert len(source_outbox) == 1


def test_default_policy_denies_cli_promotion_without_mutation(tmp_path: Path) -> None:
    service, work_queue, _, message_store = _service(tmp_path)
    captured = service.execute(_capture_request())
    request = ExternalActionRequest(
        action_type=ACTION_PROMOTE,
        source_type="cli",
        source_anchor=_anchor(),
        actor=ExternalActor(
            actor_type="local_operator",
            display_label="promotion-service",
            role_id="promotion-service",
        ),
        target=ExternalActionTarget(queue_item_id=captured.target.queue_item_id),
        payload={
            "target_role": "business-analyst",
            "lifecycle_state": "business_analysis",
        },
        reason="synthetic promotion attempt",
        correlation_id="corr-promote-denied",
    )

    denied = service.execute(request)

    assert denied.outcome == OUTCOME_ACTION_DENIED
    assert denied.safe_reason == "promotion_denied_by_default"
    after = work_queue.get(captured.target.queue_item_id)
    assert after is not None
    assert after.status == "captured"
    assert message_store.pending_count("business-analyst") == 0


def test_promotion_failure_records_safe_notification_state(tmp_path: Path) -> None:
    service, work_queue, _, message_store = _service(tmp_path, allow_cli_promotion=True)
    captured = service.execute(_capture_request())
    request = ExternalActionRequest(
        action_type=ACTION_PROMOTE,
        source_type="cli",
        source_anchor=_anchor(),
        actor=ExternalActor(
            actor_type="local_operator",
            display_label="promotion-service",
            role_id="promotion-service",
        ),
        target=ExternalActionTarget(queue_item_id=captured.target.queue_item_id),
        payload={
            "target_role": "business-analyst",
            "lifecycle_state": "business_analysis",
        },
        reason="synthetic failed promotion",
        correlation_id="corr-promote-failed",
    )

    failed = service.execute(request)

    assert failed.outcome == "action_failed"
    assert failed.notification_state == "queued"
    assert failed.extra["notification"]["event_kind"] == "queue.promotion_failed"
    after = work_queue.get(captured.target.queue_item_id)
    assert after is not None
    assert after.status == "captured"
    assert message_store.pending_count("business-analyst") == 0


def test_explicit_promotion_authorization_preserves_work_queue_idempotency(
    tmp_path: Path,
) -> None:
    service, work_queue, _, message_store = _service(tmp_path, allow_cli_promotion=True)
    captured = service.execute(_capture_request())
    evidence = {
        "requester": "Sponsor",
        "outcome": "Shared external receipts",
        "scope": "Local contract slice",
        "constraints": "Connector neutral",
        "priority_or_risk_signal": "High",
        "recommended_work_item_type": "slice",
    }
    work_queue.mark_readiness(
        captured.target.queue_item_id,
        actor_role="product-manager",
        evidence=evidence,
    )
    request = ExternalActionRequest(
        action_type=ACTION_PROMOTE,
        source_type="cli",
        source_anchor=_anchor(),
        actor=ExternalActor(
            actor_type="local_operator",
            display_label="product-manager",
            role_id="product-manager",
        ),
        target=ExternalActionTarget(queue_item_id=captured.target.queue_item_id),
        payload={
            "target_role": "business-analyst",
            "lifecycle_state": "business_analysis",
            "work_item_id": "work-uei",
            "work_item_type": "slice",
        },
        reason="approved local test promotion",
        idempotency_key="promotion-key",
        correlation_id="corr-promote-allowed",
    )

    first = service.execute(request)
    replay = service.execute(request)

    assert first.outcome == "action_recorded"
    assert replay.outcome == OUTCOME_DUPLICATE
    assert first.target.work_item_id == "work-uei"
    assert first.notification_state == "queued"
    assert first.extra["notification"]["event_kind"] == "queue.promoted"
    assert replay.notification_state == "queued"
    assert message_store.pending_count("business-analyst") == 1
    outbox_files = list(
        (tmp_path / "state").glob(
            "projects/agentic-mesh-dev/connector_outbox/all-agents/pending/*.json"
        )
    )
    # One capture notification plus one promotion notification; replay sends none.
    assert len(outbox_files) == 2


def test_storage_rejects_path_ids_and_isolates_corrupt_receipts(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path)
    request = _capture_request()
    receipt = service.execute(request)
    bad_request = ExternalActionRequest(
        action_type=ACTION_SHOW_STATUS,
        source_type="cli",
        source_anchor=_anchor(),
        actor=ExternalActor(actor_type="local_operator", display_label="operator"),
        target=ExternalActionTarget(queue_item_id=receipt.target.queue_item_id),
        action_id="../escape",
    )

    with pytest.raises(WorkQueueError):
        service.store.record(request=bad_request, receipt=receipt)

    corrupt = service.store.receipts_dir / "receipt-corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    receipts, errors = service.store.list_receipts()
    assert receipts
    assert errors == [{"receipt_id": "receipt-corrupt", "error_class": "JSONDecodeError"}]


def test_idempotency_derivation_uses_hmac_for_sensitive_identifiers() -> None:
    keyed = derive_idempotency_key(
        "tenant-id",
        "channel-id",
        "message-id",
        secret_material="local-test-secret",
    )
    fallback = derive_idempotency_key(
        "local-cli",
        "synthetic",
        sensitive=False,
        allow_local_fallback=True,
    )

    assert keyed.startswith("hmac-sha256:")
    assert "tenant-id" not in keyed
    assert fallback.startswith("sha256-local:")
    with pytest.raises(WorkQueueError):
        derive_idempotency_key("tenant-id", sensitive=True)


def test_external_action_telemetry_and_journal_allowlist(tmp_path: Path) -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        service, _, journal, _ = _service(tmp_path)
        receipt = service.execute(_capture_request())
    finally:
        telemetry.set_test_sink(None)

    assert receipt.outcome == OUTCOME_CAPTURED
    rendered = json.dumps(sink.logs + [span.attributes for span in sink.spans])
    assert "activity/raw-123" not in rendered
    assert "sponsor-user-id" not in rendered
    assert "teams.example" not in rendered
    assert any(span.name == "external_action.capture_work" for span in sink.spans)
    metrics = [
        metric
        for metric in sink.metrics
        if metric["name"] == "agentic_mesh.events_total"
        and metric["attributes"]["event_type"].startswith("external_action")
    ]
    assert metrics
    assert all("action_type" in metric["attributes"] for metric in metrics)
    assert all("tenant_id" not in metric["attributes"] for metric in metrics)
    assert "external_action_receipt_recorded" in [
        event["event_type"] for event in journal.read_all()
    ]


def test_cli_capture_and_denied_promotion_return_receipts(tmp_path: Path) -> None:
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
            "CLI receipt",
            "--summary",
            "Capture through external actions.",
            "--owner-role",
            "product-manager",
            "--work-item-type",
            "slice",
            "--source-message-id",
            "raw/source/123",
            "--actor",
            "raw-user-id",
            "--external-url",
            "https://teams.example/raw",
            "--idempotency-key",
            "cli-receipt-key",
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
            "promote",
            "--queue-item-id",
            payload["queue_item_id"],
            "--reason",
            "synthetic denied promotion",
        ],
        cwd=Path.cwd(),
        check=False,
        text=True,
        capture_output=True,
    )

    assert payload["schema_version"] == "external-action-receipt-v0"
    assert payload["outcome"] == "captured"
    assert payload["queue_item_id"].startswith("queue-")
    assert "raw/source/123" not in capture.stdout
    assert "raw-user-id" not in capture.stdout
    assert "teams.example" not in capture.stdout
    assert denied.returncode == 1
    denied_payload = json.loads(denied.stderr)
    assert denied_payload["outcome"] == "action_denied"
    assert denied_payload["safe_reason"] == "promotion_denied_by_default"
