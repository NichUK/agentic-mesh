import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_mesh.agent_run_state import AgentRunState
from agentic_mesh.agent_run_state import FileAgentRunStateStore
from agentic_mesh.control_plane_actions import CONTROL_PLANE_ACTION_REGISTRY
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import Message
from agentic_mesh.problem_status import worker_problem_status
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.work_item_recovery import DuplicateActiveWorkGuard
from agentic_mesh.work_item_recovery import FileRecoveryStatusStore
from agentic_mesh.work_item_recovery import RECOVERY_MUTATION_ACTIONS
from agentic_mesh.work_item_recovery import RECOVERY_READ_ACTION
from agentic_mesh.work_item_recovery import RecoveryActionRequest
from agentic_mesh.work_item_recovery import RecoveryActionService
from agentic_mesh.work_item_recovery import RecoveryClassifier
from agentic_mesh.work_item_recovery import RecoveryStatus
from agentic_mesh.work_item_recovery import StaleRecoveryWrite


PROJECT_ID = "agentic-mesh-dev"


def _problem(
    *,
    failure_class: str = "timeout",
    work_item_id: str = "work-recover",
    role_id: str = "engineering",
    artifact_paths: list[str] | None = None,
):
    return worker_problem_status(
        failure_class=failure_class,
        reason="Worker stopped without a valid role result",
        recovery_action="retry_same_state",
        retryable=True,
        role_id=role_id,
        role_instance_id=f"{PROJECT_ID}.{role_id}.1",
        message_payload={
            "work_item_id": work_item_id,
            "work_item_type": "slice",
            "queue_item_id": "queue-recover",
            "source_anchor": {
                "source_anchor_ref": "source:abc123",
                "display_label": "Codex request",
            },
        },
        source_message_id="msg-source",
        correlation_id="corr-recover",
        lifecycle_state="implementation",
        worker_adapter="codex-cli",
        worker_model="codex",
        artifact_paths=artifact_paths,
    )


def _service(tmp_path: Path):
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, PROJECT_ID)
    message_store = FileMessageStore(state_root, PROJECT_ID, journal)
    recovery_store = FileRecoveryStatusStore(state_root, PROJECT_ID)
    service = RecoveryActionService(
        project_id=PROJECT_ID,
        store=recovery_store,
        message_store=message_store,
        journal=journal,
        duplicate_guard=DuplicateActiveWorkGuard(
            state_root=state_root,
            project_id=PROJECT_ID,
        ),
    )
    return state_root, journal, message_store, recovery_store, service


def test_recovery_contract_serializes_safe_fields_without_raw_detail() -> None:
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(failure_class="auth_failed"),
    )

    payload = status.to_dict()

    assert payload["schema_version"] == "work-item-recovery-v0"
    assert payload["recovery_reason_class"] == "provider_auth"
    assert payload["recoverability_class"] == "fix_runtime_first"
    assert payload["recovery_state"] == "runtime_fix_required"
    assert payload["problem_status"]["status"] == "needs_runtime_recovery"
    serialized = json.dumps(payload)
    for forbidden in [
        "tenant_id",
        "channel_id",
        "service_url",
        "secret_ref",
        "mount_ref",
        "stdout",
        "stderr",
        "/mesh/",
    ]:
        assert forbidden not in serialized


def test_classifier_maps_timeout_retry_and_split_required() -> None:
    classifier = RecoveryClassifier(retry_limit=2)
    first = classifier.classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(failure_class="timeout"),
    )
    assert first.recovery_reason_class == "worker_timeout"
    assert first.recoverability_class == "operator_retryable"
    assert first.recovery_state == "recovery_needed"

    repeated = classifier.classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(failure_class="timeout"),
        current=RecoveryStatus.from_dict({**first.to_dict(), "retry_count": 2}),
    )
    assert repeated.recoverability_class == "split_required"
    assert repeated.recovery_state == "split_required"
    assert "Product or Delivery" in repeated.next_action


def test_classifier_requires_partial_artifact_reconciliation() -> None:
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(
            failure_class="timeout",
            artifact_paths=["work-items/work-recover/partial.md"],
        ),
    )

    assert status.partial_artifacts_present is True
    assert status.partial_artifact_action == "reconcile_before_retry"
    assert status.recovery_state == "runtime_fix_required"


def test_file_recovery_store_writes_current_history_receipts_and_denies_stale_revision(
    tmp_path: Path,
) -> None:
    _, _, _, store, service = _service(tmp_path)
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(),
    )
    store.write_current(status)

    current = store.get_current("work-recover")
    assert current is not None
    assert current.revision == 1
    assert store.history_path("work-recover").exists()

    updated = store.apply_transition(
        status,
        expected_revision=1,
        recovery_state="recovery_failed",
    )
    assert updated.revision == 2
    with pytest.raises(StaleRecoveryWrite):
        store.apply_transition(updated, expected_revision=1, recovery_state="recovery_queued")

    request = RecoveryActionRequest(
        action_type=RECOVERY_READ_ACTION,
        project_id=PROJECT_ID,
        work_item_id="work-recover",
        actor=None,
        reason=None,
        idempotency_key=None,
        expected_revision=None,
        correlation_id="corr-read",
    )
    receipt = service.execute(request)
    assert receipt.outcome == "current"


def test_duplicate_guard_detects_pending_claimed_and_active_run_state(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, PROJECT_ID)
    messages = FileMessageStore(state_root, PROJECT_ID, journal)
    pending = Message.create(
        role_id="engineering",
        message_type="sdlc.recovery_retry",
        source="test",
        correlation_id="corr-msg",
        payload={"work_item_id": "work-recover", "lifecycle_state": "implementation"},
    )
    messages.enqueue(pending)
    claimed = messages.claim_next("engineering", "agentic-mesh-dev.engineering.1")
    assert claimed is not None
    run_store = FileAgentRunStateStore(state_root, PROJECT_ID)
    run_store.write_current(
        AgentRunState(
            project_id=PROJECT_ID,
            role_id="engineering",
            role_instance_id="agentic-mesh-dev.engineering.1",
            message_id=claimed.message_id,
            work_item_id="work-recover",
            work_item_type="slice",
            queue_item_id="queue-recover",
            lifecycle_state="implementation",
            correlation_id="corr-msg",
            worker_adapter="codex-cli",
            worker_model="codex",
            run_state="running",
            started_at="2026-06-05T10:00:00+00:00",
            last_heartbeat_at="2026-06-05T10:01:00+00:00",
        )
    )

    result = DuplicateActiveWorkGuard(
        state_root=state_root,
        project_id=PROJECT_ID,
    ).check(
        work_item_id="work-recover",
        lifecycle_state="implementation",
        affected_role="engineering",
    )

    assert result.result == "duplicate_active_work"
    sources = {ref["source"] for ref in result.active_references}
    assert "claimed_message" in sources
    assert "active_run_state" in sources


def test_duplicate_guard_ignores_stale_active_run_state_without_claim(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    run_store = FileAgentRunStateStore(state_root, PROJECT_ID)
    run_store.write_current(
        AgentRunState(
            project_id=PROJECT_ID,
            role_id="engineering",
            role_instance_id="agentic-mesh-dev.engineering.1",
            message_id="msg-stale",
            work_item_id="work-recover",
            work_item_type="slice",
            queue_item_id="queue-recover",
            lifecycle_state="implementation",
            correlation_id="corr-msg",
            worker_adapter="codex-cli",
            worker_model="codex",
            run_state="running",
            started_at="2026-06-05T10:00:00+00:00",
            last_heartbeat_at="2026-06-05T10:01:00+00:00",
        )
    )

    result = DuplicateActiveWorkGuard(
        state_root=state_root,
        project_id=PROJECT_ID,
    ).check(
        work_item_id="work-recover",
        lifecycle_state="implementation",
        affected_role="engineering",
    )

    assert result.result == "clear"


def test_recovery_action_requires_controls_before_mutation(tmp_path: Path) -> None:
    _, _, _, store, service = _service(tmp_path)
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(),
    )
    store.write_current(status)

    receipt = service.execute(
        RecoveryActionRequest(
            action_type="retry_work_item_recovery",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            actor="operator",
            reason="retry after timeout",
            idempotency_key="idem-1",
            expected_revision=None,
            correlation_id="corr-deny",
        )
    )

    assert receipt.outcome == "denied"
    assert receipt.decision_reason == "expected_revision_required"
    assert store.get_current("work-recover").revision == status.revision


def test_retry_queues_once_and_idempotency_replay_returns_original_receipt(
    tmp_path: Path,
) -> None:
    _, journal, message_store, store, service = _service(tmp_path)
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(),
    )
    store.write_current(status)
    request = RecoveryActionRequest(
        action_type="retry_work_item_recovery",
        project_id=PROJECT_ID,
        work_item_id="work-recover",
        lifecycle_state="implementation",
        affected_role="engineering",
        actor="operator",
        reason="Retry after transient worker timeout",
        idempotency_key="idem-retry",
        expected_revision=1,
        correlation_id="corr-retry",
    )

    receipt = service.execute(request)
    replay = service.execute(request)

    assert receipt.outcome == "queued"
    assert replay.outcome == "duplicate"
    assert message_store.pending_count("engineering") == 1
    events = [event["event_type"] for event in journal.read_all()]
    assert "recovery_retry_queued" in events
    assert events.count("message_accepted") == 1


def test_retry_clears_current_problem_status_read_model(tmp_path: Path) -> None:
    _, journal, _, store, service = _service(tmp_path)
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(),
    )
    store.write_current(status)
    problem_path = store.problem_status_path("work-recover")
    problem_path.write_text(json.dumps(status.problem_status), encoding="utf-8")

    receipt = service.execute(
        RecoveryActionRequest(
            action_type="retry_work_item_recovery",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            lifecycle_state="implementation",
            affected_role="engineering",
            actor="operator",
            reason="Retry after transient worker timeout",
            idempotency_key="idem-clear-problem",
            expected_revision=1,
            correlation_id="corr-clear-problem",
        )
    )

    assert receipt.outcome == "queued"
    assert not problem_path.exists()
    events = [event["event_type"] for event in journal.read_all()]
    assert "problem_status_cleared_for_recovery" in events


def test_recovery_repair_confirmed_allows_fix_runtime_first_retry(tmp_path: Path) -> None:
    _, _, message_store, store, service = _service(tmp_path)
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(failure_class="auth_failed"),
    )
    store.write_current(status)

    denied = service.execute(
        RecoveryActionRequest(
            action_type="record_work_item_recovery",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            lifecycle_state="implementation",
            affected_role="engineering",
            actor="operator",
            reason="auth fixed",
            idempotency_key="idem-auth-deny",
            expected_revision=1,
            correlation_id="corr-auth-deny",
        )
    )
    assert denied.outcome == "denied"
    assert denied.decision_reason == "repair_confirmation_required"

    accepted = service.execute(
        RecoveryActionRequest(
            action_type="record_work_item_recovery",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            lifecycle_state="implementation",
            affected_role="engineering",
            actor="operator",
            reason="auth fixed",
            idempotency_key="idem-auth-accept",
            expected_revision=1,
            correlation_id="corr-auth-accept",
            repair_confirmed=True,
        )
    )
    assert accepted.outcome == "queued"
    assert message_store.pending_count("engineering") == 1


def test_recovery_repair_confirmed_allows_partial_artifact_retry(
    tmp_path: Path,
) -> None:
    _, _, message_store, store, service = _service(tmp_path)
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(
            failure_class="timeout",
            artifact_paths=["work-items/work-recover/partial.md"],
        ),
    )
    store.write_current(status)

    denied = service.execute(
        RecoveryActionRequest(
            action_type="retry_work_item_recovery",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            lifecycle_state="implementation",
            affected_role="engineering",
            actor="operator",
            reason="retry without reconciling partial evidence",
            idempotency_key="idem-partial-deny",
            expected_revision=1,
            correlation_id="corr-partial-deny",
        )
    )
    assert denied.outcome == "denied"
    assert denied.decision_reason == "partial_artifact_reconciliation_required"

    accepted = service.execute(
        RecoveryActionRequest(
            action_type="record_work_item_recovery",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            lifecycle_state="implementation",
            affected_role="engineering",
            actor="operator",
            reason="partial evidence reviewed and safe to retry",
            idempotency_key="idem-partial-accept",
            expected_revision=1,
            correlation_id="corr-partial-accept",
            repair_confirmed=True,
        )
    )
    assert accepted.outcome == "queued"
    assert message_store.pending_count("engineering") == 1


def test_recovery_repair_confirmed_allows_retry_limit_override(tmp_path: Path) -> None:
    _, _, message_store, store, service = _service(tmp_path)
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(failure_class="process_failed"),
    )
    status = replace(status, retry_count=status.retry_limit)
    store.write_current(status)

    denied = service.execute(
        RecoveryActionRequest(
            action_type="retry_work_item_recovery",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            lifecycle_state="implementation",
            affected_role="engineering",
            actor="operator",
            reason="ordinary retry after limit",
            idempotency_key="idem-limit-deny",
            expected_revision=1,
            correlation_id="corr-limit-deny",
        )
    )
    assert denied.outcome == "denied"
    assert denied.decision_reason == "retry_limit_exceeded"

    accepted = service.execute(
        RecoveryActionRequest(
            action_type="record_work_item_recovery",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            lifecycle_state="implementation",
            affected_role="engineering",
            actor="operator",
            reason="runtime repair confirmed after failed retry loop",
            idempotency_key="idem-limit-accept",
            expected_revision=1,
            correlation_id="corr-limit-accept",
            repair_confirmed=True,
        )
    )
    assert accepted.outcome == "queued"
    assert message_store.pending_count("engineering") == 1


def test_supersede_and_mark_needs_decision_do_not_enqueue_retry(tmp_path: Path) -> None:
    _, _, message_store, store, service = _service(tmp_path)
    status = RecoveryClassifier().classify_problem(
        project_id=PROJECT_ID,
        problem_status=_problem(),
    )
    store.write_current(status)

    decision = service.execute(
        RecoveryActionRequest(
            action_type="mark_work_item_recovery_needs_decision",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            actor="operator",
            reason="needs sponsor decision",
            idempotency_key="idem-decision",
            expected_revision=1,
            correlation_id="corr-decision",
            decision_owner="release-sponsor",
        )
    )
    assert decision.outcome == "accepted"
    assert store.get_current("work-recover").recovery_state == "sponsor_decision_required"
    assert message_store.pending_count("engineering") == 0
    problem_path = store.problem_status_path("work-recover")
    problem_path.write_text(json.dumps(status.problem_status), encoding="utf-8")

    supersede = service.execute(
        RecoveryActionRequest(
            action_type="supersede_work_item_recovery",
            project_id=PROJECT_ID,
            work_item_id="work-recover",
            actor="operator",
            reason="replacement created",
            idempotency_key="idem-supersede",
            expected_revision=2,
            correlation_id="corr-supersede",
            replacement_work_item_id="work-replacement",
        )
    )
    assert supersede.outcome == "superseded"
    assert store.get_current("work-recover").recovery_state == "superseded"
    assert not problem_path.exists()
    assert message_store.pending_count("engineering") == 0


def test_control_plane_recovery_mutations_disabled_by_default() -> None:
    read = CONTROL_PLANE_ACTION_REGISTRY[RECOVERY_READ_ACTION]
    assert read.mutation is False

    for action_type in RECOVERY_MUTATION_ACTIONS:
        entry = CONTROL_PLANE_ACTION_REGISTRY[action_type]
        assert entry.sensitive is True
        assert entry.mutation is True
        assert entry.reason_required is True
        assert entry.idempotency_required is True
        assert entry.enabled_by_default is False
        assert entry.mcp_ready is False
