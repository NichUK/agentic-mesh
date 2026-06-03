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
    assert "connector.outbox.enqueue" in span_names
    assert message.trace_context["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert handoff is not None
    assert handoff.trace_context["trace_id"] == "1234567890abcdef1234567890abcdef"
