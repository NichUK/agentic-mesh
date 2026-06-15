from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.observability import TelemetrySettings
from agentic_mesh_v3.observability import configure_observability
from agentic_mesh_v3.observability import reset_observability_for_tests
from agentic_mesh_v3.tools import V3ToolService


def test_telemetry_settings_load_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_SERVICE_NAMESPACE", "mesh-tests")
    monkeypatch.setenv("AGENTIC_MESH_ENVIRONMENT", "ci")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_INSECURE", "false")

    settings = TelemetrySettings.from_env(service_name="agentic-mesh-v3.test")

    assert settings.service_name == "agentic-mesh-v3.test"
    assert settings.service_namespace == "mesh-tests"
    assert settings.environment == "ci"
    assert settings.otlp_endpoint == "http://collector:4317"
    assert settings.insecure is False


def test_configure_observability_without_endpoint_is_safe(monkeypatch) -> None:
    reset_observability_for_tests()
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    telemetry = configure_observability(TelemetrySettings.from_env(service_name="agentic-mesh-v3.test"))

    telemetry.increment("agentic_mesh_v3_test_counter", attributes={"status": "ok"})
    telemetry.log_event("v3.test.event", status="ok")


def test_tool_service_emits_success_telemetry(tmp_path: Path) -> None:
    fake = FakeTelemetry()
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        service = V3ToolService(db, telemetry=fake)

        result = service.call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="backlog.upsert",
            payload={
                "queue_item_id": "queue-1",
                "title": "Test queue item",
                "summary": "Queue item summary.",
                "owner_role": "product-manager",
            },
        )

        assert result.tool_name == "backlog.upsert"
        assert fake.spans == [("v3.tool_call", "backlog.upsert")]
        assert fake.counters == [("agentic_mesh_v3_tool_calls", "ok")]
        assert fake.events == [("v3.tool_call.recorded", "backlog.upsert")]
    finally:
        db.close()


def test_tool_service_emits_failure_telemetry(tmp_path: Path) -> None:
    fake = FakeTelemetry()
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        service = V3ToolService(db, telemetry=fake)

        try:
            service.call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="engineering-only.internal",
                payload={},
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("unknown/disallowed tool should fail")

        assert fake.spans == [("v3.tool_call", "engineering-only.internal")]
        assert fake.counters == [("agentic_mesh_v3_tool_calls", "error")]
        assert fake.events == [("v3.tool_call.failed", "engineering-only.internal")]
    finally:
        db.close()


class FakeTelemetry:
    def __init__(self) -> None:
        self.spans: list[tuple[str, str | None]] = []
        self.counters: list[tuple[str, str | None]] = []
        self.events: list[tuple[str, str | None]] = []

    @contextmanager
    def span(self, name: str, **attributes: object):
        self.spans.append((name, _optional_str(attributes.get("tool_name"))))
        yield

    def increment(self, name: str, *, value: int = 1, attributes: dict[str, object] | None = None) -> None:
        self.counters.append((name, _optional_str((attributes or {}).get("status"))))

    def log_event(self, event_name: str, **fields: object) -> None:
        self.events.append((event_name, _optional_str(fields.get("tool_name"))))


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)
