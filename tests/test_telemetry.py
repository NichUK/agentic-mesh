import json
from pathlib import Path

from agentic_mesh import telemetry
from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.config import load_mesh_config
from agentic_mesh.config import render_bot_display_name
from agentic_mesh.config import render_service_name
from agentic_mesh.config import slug_value
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import Message
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.runtime import AgentRuntime
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.threaded_context import FileThreadedContextStore
from agentic_mesh.threaded_context import ThreadRouteRecord
from agentic_mesh.workers import StubCodexWorkerAdapter


def test_service_name_and_bot_display_name_rendering() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    naming = mesh_config.organization.naming_defaults

    assert slug_value("Dev Team") == "dev-team"
    assert render_service_name(
        naming,
        team_slug="dev-team",
        project_id="agentic-mesh-dev",
        role_id="engineering",
        ordinal=1,
    ) == "AM.dev-team.engineering.1"
    assert render_service_name(
        naming,
        team_slug="accounting",
        project_id="accounting-project",
        role_id="engineering",
        ordinal=1,
    ) == "AM.accounting.engineering.1"
    assert render_bot_display_name(naming, "engineering") == "AM-Engineering"
    assert render_bot_display_name(naming, "qa-engineer") == "AM-QA Engineer"


def test_trace_id_derives_from_correlation_id() -> None:
    trace_id = telemetry.trace_id_from_correlation(
        "corr-1234567890abcdef1234567890abcdef"
    )

    assert trace_id == "1234567890abcdef1234567890abcdef"
    assert len(trace_id) == 32
    assert len(telemetry.trace_id_from_correlation("corr-not-hex")) == 32


def test_journal_append_emits_structured_otel_log(tmp_path: Path) -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        journal = EventJournal(tmp_path, "agentic-mesh-dev")
        event = journal.append(
            "test_event",
            project_id="agentic-mesh-dev",
            work_item_id="slice-telemetry",
            correlation_id="corr-1234567890abcdef1234567890abcdef",
        )
    finally:
        telemetry.set_test_sink(None)

    assert sink.logs == [event]
    assert sink.metrics[0]["name"] == "agentic_mesh.events_total"
    assert sink.metrics[0]["attributes"]["event_type"] == "test_event"


def test_emit_log_uses_public_otel_log_record_api() -> None:
    class FakeLogger:
        def __init__(self) -> None:
            self.records = []

        def emit(self, record) -> None:
            self.records.append(record)

    fake_logger = FakeLogger()
    original_logger = telemetry._logger
    telemetry._logger = fake_logger
    try:
        telemetry.emit_log(
            {
                "event_type": "test_event",
                "project_id": "agentic-mesh-dev",
                "correlation_id": "corr-1234567890abcdef1234567890abcdef",
                "response_value": "approved",
                "timestamp": "2026-06-03T09:00:00+00:00",
            }
        )
    finally:
        telemetry._logger = original_logger

    assert len(fake_logger.records) == 1
    record = fake_logger.records[0]
    assert record.body == "test_event"
    assert record.trace_id == int("1234567890abcdef1234567890abcdef", 16)
    assert record.attributes["project_id"] == "agentic-mesh-dev"
    assert record.attributes["human_response.value"] == "approved"


def test_span_attributes_adds_human_response_value_alias() -> None:
    attrs = telemetry.span_attributes(
        gate_id="release_decision_response",
        response_value="approved",
    )

    assert attrs["response_value"] == "approved"
    assert attrs["human_response.value"] == "approved"


def test_runtime_flow_creates_trace_spans_and_propagates_context(tmp_path: Path) -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        mesh_config = load_mesh_config(Path.cwd())
        journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
        message_store = FileMessageStore(
            tmp_path / "state",
            mesh_config.project.project_id,
            journal,
        )
        connector_outbox = FileConnectorOutbox(
            tmp_path / "state",
            mesh_config.project.project_id,
            journal,
        )
        artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
        runtime = AgentRuntime(
            message_store,
            artifacts,
            journal,
            mesh_config.project,
            StubCodexWorkerAdapter(),
            connector_outbox=connector_outbox,
            response_types=mesh_config.response_types,
        )
        message = message_store.enqueue(
            Message.create(
                role_id="business-analyst",
                message_type="sdlc.intake",
                payload={
                    "title": "Telemetry slice",
                    "summary": "Trace the first role.",
                    "work_item_id": "slice-telemetry",
                    "work_item_type": "slice",
                    "lifecycle_state": "business_analysis",
                    "auto_handoff": True,
                },
                source="test",
                correlation_id="corr-1234567890abcdef1234567890abcdef",
            )
        )

        runtime.run_once(
            "agentic-mesh-dev.business-analyst.1",
            mesh_config.instances["agentic-mesh-dev.business-analyst.1"],
        )
        handoff = message_store.claim_next(
            "product-manager",
            "agentic-mesh-dev.product-manager.1",
        )
    finally:
        telemetry.set_test_sink(None)

    span_names = [span.name for span in sink.spans]
    assert "work.enqueue" in span_names
    assert "queue.wait" in span_names
    assert "work.claim" in span_names
    assert "agent.run" in span_names
    assert "worker.run" in span_names
    assert "artifact.write" in span_names
    assert "handoff.emit" in span_names
    assert "connector.outbox.enqueue" not in span_names
    assert "notification_policy_evaluated" in [
        event["event_type"] for event in journal.read_all()
    ]
    assert message.trace_context["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert handoff is not None
    assert handoff.trace_context["trace_id"] == "1234567890abcdef1234567890abcdef"


def test_generated_artifact_write_telemetry_and_journal_are_redacted(
    tmp_path: Path,
) -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
        store = ArtifactStore(
            tmp_path / "workspace",
            "agentic-mesh-dev",
            journal,
            document_library_root=tmp_path / "docs",
        )
        store.write_generated_artifact(
            relative_path="work-items/work-life/lifecycle-flow.md",
            content="# Lifecycle Flow",
            work_item_id="work-life",
            queue_item_id="queue-life",
            lifecycle_state="implementation",
            component_id="lifecycle-export",
            correlation_id="corr-1234567890abcdef1234567890abcdef",
        )
    finally:
        telemetry.set_test_sink(None)

    generated_spans = [
        span for span in sink.spans if span.name == "artifact.generated.write"
    ]
    assert len(generated_spans) == 1
    attrs = generated_spans[0].attributes
    assert attrs["project_id"] == "agentic-mesh-dev"
    assert attrs["path"] == "work-items/work-life/lifecycle-flow.md"
    assert attrs["work_item_id"] == "work-life"
    assert attrs["queue_item_id"] == "queue-life"
    assert attrs["generated_artifact"] is True
    assert attrs["result"] == "success"

    event = journal.read_all()[0]
    forbidden = {
        "tenant_id",
        "team_id",
        "channel_id",
        "service_url",
        "secret_ref",
        "mount_ref",
        "raw_payload",
        "state_root",
        "role_memory",
    }
    assert forbidden.isdisjoint(event)
    assert forbidden.isdisjoint(attrs)


def test_threaded_context_journal_and_metrics_are_redacted(tmp_path: Path) -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
        store = FileThreadedContextStore(tmp_path / "state", "agentic-mesh-dev", journal)
        route = ThreadRouteRecord.create(
            connector_type="teams",
            connector_id="teams-shared",
            source_scope="engineering",
            root_message_ref="activity-parent-secret",
            parent_work_item_id="work-parent-1",
            parent_work_item_type="slice",
            lifecycle_state="implementation",
            owner_role="engineering",
            source_anchor_ref="source:parent",
        )
        store.upsert_route(route)
        store.capture(
            route=route,
            source_message_ref="reply-secret",
            actor_label="Nich",
            text="Please include this context from https://graph.microsoft.com/raw.",
            mentioned_roles=[],
            source_anchor_ref="source:reply",
            correlation_id="corr-threaded",
        )
    finally:
        telemetry.set_test_sink(None)

    serialized_logs = "\n".join(json.dumps(event, sort_keys=True) for event in sink.logs)
    serialized_metrics = "\n".join(
        json.dumps(metric, sort_keys=True) for metric in sink.metrics
    )
    for forbidden in [
        "activity-parent-secret",
        "reply-secret",
        "graph.microsoft.com",
        "service_url",
        "raw_payload",
        "secret_ref",
    ]:
        assert forbidden not in serialized_logs
        assert forbidden not in serialized_metrics
    assert "threaded_context.captured" in serialized_logs


def test_activation_telemetry_event_uses_allowlisted_fields_only() -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        telemetry.record_activation_event(
            {
                "event_type": "activation_blocked",
                "project_id": "agentic-mesh-dev",
                "work_item_id": "work-activation",
                "work_item_type": "slice",
                "queue_item_id": "queue-activation",
                "lifecycle_state": "implementation",
                "role_id": "engineering",
                "role_instance_id": "agentic-mesh-dev.engineering.1",
                "correlation_id": "corr-activation",
                "source_anchor_ref": "source:activation",
                "schema_version": "activation-evidence-v0",
                "impact_categories": ["runtime_code", "route_or_ingress"],
                "activation_paths": ["rebuild_image"],
                "target_labels": ["dogfood_compose"],
                "source_status": "source_ready",
                "activation_status": "blocked",
                "smoke_status": "failed",
                "failure_class": "source_changed_running_service_not_updated",
                "action_owner": "runtime/operator",
                "retryable": True,
                "status_url_present": True,
                "service_url": "https://graph.microsoft.com/hidden",
                "secret_ref": "hidden-secret",
            }
        )
    finally:
        telemetry.set_test_sink(None)

    serialized_logs = "\n".join(json.dumps(event, sort_keys=True) for event in sink.logs)
    serialized_metrics = "\n".join(
        json.dumps(metric, sort_keys=True) for metric in sink.metrics
    )
    assert "activation_blocked" in serialized_logs
    assert "agentic_mesh.events_total" in serialized_metrics
    for forbidden in ["service_url", "graph.microsoft.com", "secret_ref", "hidden-secret"]:
        assert forbidden not in serialized_logs
        assert forbidden not in serialized_metrics
