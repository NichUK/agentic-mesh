import json
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import utc_now_iso
from agentic_mesh.problem_status import worker_problem_status
from agentic_mesh.provider_conditions import CodexProviderConditionProfile
from agentic_mesh.provider_conditions import ProviderProbeCadence
from agentic_mesh.recovery_alerts import FileRecoveryAlertStore
from agentic_mesh.recovery_alerts import RecoveryAlertState
from agentic_mesh.recovery_observability import build_recovery_observability_view
from agentic_mesh.recovery_observability import connector_display_facts
from agentic_mesh.recovery_poller import RecoveryPoller
from agentic_mesh.recovery_poller import RecoveryPollerPolicy
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.work_item_recovery import DuplicateActiveWorkGuard
from agentic_mesh.work_item_recovery import FileRecoveryStatusStore
from agentic_mesh.work_item_recovery import RecoveryActionService
from agentic_mesh.work_item_recovery import RecoveryClassifier


PROJECT_ID = "agentic-mesh-dev"
FORBIDDEN_MARKERS = [
    "tenant_id",
    "team_id",
    "channel_id",
    "conversation_id",
    "activity_id",
    "service_url",
    "secret_ref",
    "mount_ref",
    "stdout",
    "stderr",
    "/mesh/",
    "raw_payload",
    "prompt",
]


def _problem(*, failure_class: str, retry_after: str | None = None):
    problem = worker_problem_status(
        failure_class=failure_class,
        reason="Worker stopped with safe provider evidence",
        recovery_action="retry_same_state",
        retryable=True,
        role_id="engineering",
        role_instance_id=f"{PROJECT_ID}.engineering.1",
        message_payload={
            "work_item_id": "work-sro",
            "work_item_type": "spike",
            "queue_item_id": "queue-sro",
            "source_anchor": {
                "source_anchor_ref": "source:cb60017d90f656d7",
                "display_label": "Smart Recovery Observability Spike",
                "tenant_id": "forbidden-tenant",
            },
        },
        source_message_id="msg-source",
        correlation_id="corr-sro",
        lifecycle_state="implementation",
        worker_adapter="codex-cli",
        worker_model="codex",
    )
    payload = problem.to_dict()
    if retry_after:
        payload["retry_after"] = retry_after
    return payload


def _service(tmp_path: Path):
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, PROJECT_ID)
    message_store = FileMessageStore(state_root, PROJECT_ID, journal)
    store = FileRecoveryStatusStore(state_root, PROJECT_ID)
    service = RecoveryActionService(
        project_id=PROJECT_ID,
        store=store,
        message_store=message_store,
        journal=journal,
        duplicate_guard=DuplicateActiveWorkGuard(
            state_root=state_root,
            project_id=PROJECT_ID,
        ),
    )
    return state_root, journal, message_store, store, service


def test_codex_provider_condition_maps_safe_evidence_and_denies_unsafe_probe() -> None:
    result = CodexProviderConditionProfile().classify_safe_evidence(
        {
            "safe_non_prompt_evidence": True,
            "failure_class": "rate_limited",
            "confidence": "confirmed",
            "retry_after": "2026-06-06T12:00:00+00:00",
            "raw_provider_output": "must not appear",
        }
    )

    payload = result.to_dict()
    assert payload["condition_class"] == "rate_limited"
    assert payload["provider_display_label"] == "Codex"
    assert payload["redacted_diagnostic_class"] == "provider_rate_limit"
    assert "raw_provider_output" not in json.dumps(payload)

    unsafe = CodexProviderConditionProfile().classify_safe_evidence(
        {"safe_non_prompt_evidence": False, "failure_class": "rate_limited"}
    )
    assert unsafe.condition_class == "operator_review_required"
    assert unsafe.skipped is True
    assert unsafe.denial_reason == "unsafe_prompt_or_capacity_probe"


def test_probe_cadence_skips_before_next_allowed_window() -> None:
    cadence = ProviderProbeCadence(minimum_interval_seconds=300, jitter_seconds=0)
    next_allowed = cadence.next_allowed_at(
        project_id=PROJECT_ID,
        provider_adapter_id="codex-cli",
        role_id="engineering",
        last_probe_at="2026-06-06T12:00:00+00:00",
        now="2026-06-06T12:01:00+00:00",
    )

    assert next_allowed == "2026-06-06T12:05:00+00:00"
    assert cadence.should_probe(
        next_allowed_at=next_allowed,
        now="2026-06-06T12:04:59+00:00",
    ) is False
    assert cadence.should_probe(
        next_allowed_at=next_allowed,
        now="2026-06-06T12:05:00+00:00",
    ) is True


def test_recovery_observability_view_uses_allowlisted_fields(tmp_path: Path) -> None:
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(
            failure_class="rate_limited",
            retry_after="2026-06-06T12:00:00+00:00",
        ),
    )
    provider = CodexProviderConditionProfile().classify_safe_evidence(
        {
            "failure_class": "rate_limited",
            "confidence": "confirmed",
            "retry_after": "2026-06-06T12:00:00+00:00",
        }
    )
    alert_store = FileRecoveryAlertStore(tmp_path / "state", PROJECT_ID)
    alert = alert_store.write_current(
        RecoveryAlertState(
            alert_id="alert-sro",
            recovery_id=status.recovery_id,
            project_id=PROJECT_ID,
            work_item_id=status.work_item_id,
            queue_item_id=status.queue_item_id,
            severity="action_required",
            alert_state="ops_alert_open",
            route_label="platform",
            source_anchor_ref=status.source_anchor_ref,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
    )

    view = build_recovery_observability_view(
        status,
        provider_condition=provider,
        alert_state=alert,
        notification_state={"status": "dead_lettered", "dead_lettered": True},
    ).to_dict()
    display = connector_display_facts(view)

    assert view["schema_version"] == "recovery-observability-view-v1"
    assert view["blocker_display_label"] == "Provider rate limit"
    assert view["retry_policy_state"] == "waiting_retry_after"
    assert view["provider_condition_class"] == "rate_limited"
    assert view["alert_state"] == "ops_alert_open"
    assert view["notification_dead_lettered"] is True
    assert display["primary_label"] == "Provider rate limit"
    serialized = json.dumps({"view": view, "display": display})
    for marker in FORBIDDEN_MARKERS:
        assert marker not in serialized


def test_alert_acknowledgement_and_silence_do_not_resolve_recovery(tmp_path: Path) -> None:
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(failure_class="auth_failed"),
    )
    store = FileRecoveryAlertStore(tmp_path / "state", PROJECT_ID)
    store.write_current(
        RecoveryAlertState(
            alert_id="alert-sro",
            recovery_id=status.recovery_id,
            project_id=PROJECT_ID,
            work_item_id=status.work_item_id,
            queue_item_id=status.queue_item_id,
            severity="action_required",
            alert_state="ops_alert_open",
            route_label="platform",
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
    )

    ack = store.acknowledge(
        work_item_id=status.work_item_id,
        actor="operator",
        reason="seen",
        scope="work_item",
        expected_revision=1,
        correlation_id="corr-alert",
    )
    assert ack.outcome == "accepted"
    assert ack.recovery_remains_active is True
    assert store.get_current(status.work_item_id).alert_state == "acknowledged"

    denial = store.silence(
        work_item_id=status.work_item_id,
        actor="operator",
        reason="do not hide decision",
        scope="work_item",
        silence_expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        expected_revision=2,
        correlation_id="corr-alert-silence-deny",
        human_decision_prompt=True,
    )
    assert denial.outcome == "denied"
    assert denial.decision_reason == "human_decision_silence_denied"

    accepted = store.silence(
        work_item_id=status.work_item_id,
        actor="operator",
        reason="suppress duplicate connector repeats",
        scope="work_item",
        silence_expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        expected_revision=2,
        correlation_id="corr-alert-silence",
    )
    assert accepted.outcome == "accepted"
    assert store.get_current(status.work_item_id).silence_active() is True


def test_recovery_poller_only_uses_official_action_for_safe_due_rate_limit(tmp_path: Path) -> None:
    state_root, journal, message_store, store, service = _service(tmp_path)
    due = "2026-06-06T12:00:00+00:00"
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(failure_class="rate_limited", retry_after=due),
    )
    status = replace(status, next_retry_at=due, retry_policy_state="eligible_for_retry")
    store.write_current(status)

    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        result = RecoveryPoller(
            project_id=PROJECT_ID,
            state_root=state_root,
            store=store,
            action_service=service,
            policy=RecoveryPollerPolicy(enabled=True),
        ).run_once(now="2026-06-06T12:00:01+00:00")
    finally:
        telemetry.set_test_sink(None)

    assert result["outcomes"][0]["outcome"] == "queued"
    assert message_store.pending_count("engineering") == 1
    assert store.get_current("work-sro").recovery_state == "recovery_queued"
    events = [event["event_type"] for event in journal.read_all()]
    assert "recovery_retry_queued" in events
    assert "recovery_poll_skipped" not in events
    assert any(span.name == "recovery.policy.evaluate" for span in sink.spans)


def test_recovery_poller_skips_auth_and_human_decision_states(tmp_path: Path) -> None:
    state_root, journal, message_store, store, service = _service(tmp_path)
    auth = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(failure_class="auth_failed"),
    )
    store.write_current(auth)

    result = RecoveryPoller(
        project_id=PROJECT_ID,
        state_root=state_root,
        store=store,
        action_service=service,
    ).run_once(now="2026-06-06T12:00:01+00:00")

    assert result["outcomes"][0]["outcome"] == "skipped"
    assert result["outcomes"][0]["decision_reason"] == "not_safe_rate_limit_retry"
    assert message_store.pending_count("engineering") == 0
    events = [event["event_type"] for event in journal.read_all()]
    assert "recovery_poll_skipped" in events
