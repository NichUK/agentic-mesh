from pathlib import Path
from dataclasses import replace
import json

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.worker_runs import FileWorkerRunStore
from agentic_mesh.worker_runs import WorkerRun
from agentic_mesh.worker_runs import WorkerRunObserver


def test_complete_run_preserves_history_and_clears_current(tmp_path: Path) -> None:
    store = FileWorkerRunStore(tmp_path, "agentic-mesh-dev")
    run = WorkerRun(
        run_id="run-terminal",
        project_id="agentic-mesh-dev",
        role_id="delivery-manager",
        role_instance_id="agentic-mesh-dev.delivery-manager.1",
        message_id="msg-terminal",
        work_item_id="work-terminal",
        work_item_type="slice",
        queue_item_id="queue-terminal",
        source_message_id="msg-source",
        source_anchor_ref=None,
        correlation_id="corr-terminal",
        lifecycle_state="delivery_readiness",
        worker_adapter="codex-cli",
        worker_model="codex",
        run_status="running",
        run_condition="active_output",
        started_at="2026-06-06T14:00:00+00:00",
    )

    store.start_run(run)
    terminal = replace(
        run,
        run_status="failed",
        run_condition="runtime_publication_failed",
        completed_at="2026-06-06T14:01:00+00:00",
        elapsed_seconds=60,
    )
    store.complete_run(terminal)

    assert store.read_current("agentic-mesh-dev.delivery-manager.1") is None
    recent = store.list_recent(role_instance_id="agentic-mesh-dev.delivery-manager.1")
    assert len(recent) == 1
    assert isinstance(recent[0], WorkerRun)
    assert recent[0].run_status == "failed"


def test_capped_output_does_not_refresh_last_output_or_inflate_count(tmp_path: Path) -> None:
    store = FileWorkerRunStore(tmp_path, "agentic-mesh-dev")
    run = WorkerRun(
        run_id="run-capped",
        project_id="agentic-mesh-dev",
        role_id="engineering",
        role_instance_id="agentic-mesh-dev.engineering.1",
        message_id="msg-capped",
        work_item_id="work-capped",
        work_item_type="slice",
        queue_item_id="queue-capped",
        source_message_id=None,
        source_anchor_ref=None,
        correlation_id="corr-capped",
        lifecycle_state="implementation",
        worker_adapter="codex-cli",
        worker_model="codex",
        run_status="running",
        run_condition="active_output",
        started_at="2026-06-06T14:00:00+00:00",
        last_output_at="2026-06-06T14:00:05+00:00",
        output_record_count=2,
        output_record_cap=2,
    )
    store.start_run(run)
    observer = WorkerRunObserver(store=store, run=run)

    capped = observer.observe_output("stderr", "new output after cap")
    capped_again = observer.observe_output("stderr", "more output after cap")

    assert capped.run_condition == "output_capped"
    assert capped.last_output_at == "2026-06-06T14:00:05+00:00"
    assert capped.output_record_count == 3
    assert capped_again.run_condition == "output_capped"
    assert capped_again.last_output_at == "2026-06-06T14:00:05+00:00"
    assert capped_again.output_record_count == 3


def test_worker_run_journal_and_otel_events_are_allowlisted(tmp_path: Path) -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
        store = FileWorkerRunStore(
            tmp_path / "state",
            "agentic-mesh-dev",
            journal=journal,
        )
        observer = WorkerRunObserver(
            store=store,
            run=_worker_run("run-events", output_record_cap=2),
        )

        store.start_run(observer.run)
        observer.observe_output(
            "stdout",
            "prompt: https://graph.microsoft.com/raw credential_ref=codex-home "
            "connector_id=teams-hidden provider error body /tmp/private/path",
        )
        observer.observe_progress("result_file")
        observer.observe_output("stderr", "extra output after cap")
        observer.run = replace(
            observer.run,
            last_progress_at="2026-06-01T00:00:00+00:00",
            last_heartbeat_at="2026-06-01T00:00:00+00:00",
            stale_output_seconds=1,
        )
        store.update_run(observer.run)
        observer.mark_stale_if_needed()
        observer.mark_terminal(
            run_status="failed",
            run_condition="provider_limited",
            failure_class="usage_limit",
            provider_recovery_class="usage_limit",
            retryable=True,
            recovery_action="retry_after_wait",
        )

        completed = WorkerRunObserver(store=store, run=_worker_run("run-completed"))
        store.start_run(completed.run)
        completed.mark_terminal(
            run_status="completed",
            run_condition="completed_valid_result",
        )

        timed_out = WorkerRunObserver(store=store, run=_worker_run("run-timeout"))
        store.start_run(timed_out.run)
        timed_out.mark_terminal(
            run_status="timed_out",
            run_condition="timed_out",
            failure_class="timeout",
            provider_recovery_class="timeout",
            retryable=True,
            recovery_action="retry_same_state",
        )

        store.prune("agentic-mesh-dev.engineering.1", keep_terminal=0)
    finally:
        telemetry.set_test_sink(None)

    required_events = {
        "worker_run_started",
        "worker_run_progress_observed",
        "worker_run_output_recorded",
        "worker_run_output_truncated",
        "worker_run_stale_output_detected",
        "worker_run_completed",
        "worker_run_failed",
        "worker_run_timed_out",
        "worker_run_provider_recovery_classified",
        "worker_run_pruned",
    }
    journal_event_types = {event["event_type"] for event in journal.read_all()}
    assert required_events <= journal_event_types
    assert required_events <= {event["event_type"] for event in sink.logs}

    metric_event_types = {
        metric["attributes"]["event_type"]
        for metric in sink.metrics
        if metric["name"] == "agentic_mesh.events_total"
    }
    assert required_events <= metric_event_types
    assert any(
        metric["attributes"].get("run_status") == "failed"
        and metric["attributes"].get("run_condition") == "provider_limited"
        for metric in sink.metrics
        if metric["name"] == "agentic_mesh.events_total"
    )

    serialized = "\n".join(
        [
            json.dumps(journal.read_all(), sort_keys=True),
            json.dumps(sink.logs, sort_keys=True),
            json.dumps(sink.metrics, sort_keys=True),
        ]
    )
    for forbidden in [
        "graph.microsoft.com",
        "credential_ref",
        "codex-home",
        "connector_id",
        "teams-hidden",
        "provider error body",
        "/tmp/private/path",
        "prompt:",
        "raw output",
        "model_response",
        "provider_error_body",
    ]:
        assert forbidden not in serialized


def _worker_run(run_id: str, *, output_record_cap: int = 200) -> WorkerRun:
    return WorkerRun(
        run_id=run_id,
        project_id="agentic-mesh-dev",
        role_id="engineering",
        role_instance_id="agentic-mesh-dev.engineering.1",
        message_id=f"msg-{run_id}",
        work_item_id="work-worker-run",
        work_item_type="slice",
        queue_item_id="queue-worker-run",
        source_message_id="msg-source",
        source_anchor_ref="source:worker-run",
        correlation_id=f"corr-{run_id}",
        lifecycle_state="implementation",
        worker_adapter="codex-cli",
        worker_model="codex",
        run_status="running",
        run_condition="quiet_running",
        started_at="2026-06-06T14:00:00+00:00",
        last_heartbeat_at="2026-06-06T14:00:00+00:00",
        output_record_cap=output_record_cap,
    )
