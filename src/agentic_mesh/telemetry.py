from __future__ import annotations

import hashlib
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from agentic_mesh.config import project_team_slug
from agentic_mesh.models import MeshConfig
from agentic_mesh.models import RoleInstanceConfig


HEX_TRACE_LENGTH = 32
HEX_SPAN_LENGTH = 16

_configured_service_name: str | None = None
_configured_resource_attributes: dict[str, str] = {}
_test_sink: "TelemetryTestSink | None" = None
_tracer: Any = None
_meter: Any = None
_logger: Any = None
_event_counter: Any = None
_metric_instruments: dict[str, Any] = {}
_gauge_values: dict[str, list[tuple[float, dict[str, Any]]]] = {}


@dataclass
class RecordedSpan:
    name: str
    attributes: dict[str, Any]
    trace_context: dict[str, str]
    duration_seconds: float


@dataclass
class TelemetryTestSink:
    spans: list[RecordedSpan] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)


def set_test_sink(sink: TelemetryTestSink | None) -> None:
    global _test_sink
    _test_sink = sink


def trace_id_from_correlation(correlation_id: str | None) -> str:
    value = str(correlation_id or "").removeprefix("corr-").lower()
    if len(value) == HEX_TRACE_LENGTH and all(ch in "0123456789abcdef" for ch in value):
        return value if value != "0" * HEX_TRACE_LENGTH else "1" * HEX_TRACE_LENGTH
    digest = hashlib.sha256(str(correlation_id or "agentic-mesh").encode("utf-8")).hexdigest()
    return digest[:HEX_TRACE_LENGTH]


def new_span_id() -> str:
    span_id = secrets.token_hex(8)
    return span_id if span_id != "0" * HEX_SPAN_LENGTH else "1" * HEX_SPAN_LENGTH


def base_trace_context(correlation_id: str | None) -> dict[str, str]:
    return {"trace_id": trace_id_from_correlation(correlation_id)}


def current_trace_context(correlation_id: str | None = None) -> dict[str, str]:
    context = base_trace_context(correlation_id)
    try:
        from opentelemetry import trace

        span_context = trace.get_current_span().get_span_context()
        if span_context and span_context.is_valid:
            context["trace_id"] = f"{span_context.trace_id:032x}"
            context["parent_span_id"] = f"{span_context.span_id:016x}"
    except Exception:
        pass
    return context


def configure_process_telemetry(
    mesh_config: MeshConfig,
    *,
    service_name: str,
    role_instance: RoleInstanceConfig | None = None,
    component: str | None = None,
) -> None:
    global _configured_service_name, _configured_resource_attributes
    global _tracer, _meter, _logger, _event_counter

    if _configured_service_name == service_name:
        return

    _configured_service_name = service_name
    naming = mesh_config.organization.naming_defaults
    team_slug = project_team_slug(mesh_config.project)
    attrs = {
        "service.name": service_name,
        "service.namespace": naming.resource_namespace,
        "agentic_mesh.brand_prefix": naming.brand_prefix,
        "agentic_mesh.team_slug": team_slug,
        "agentic_mesh.project_id": mesh_config.project.project_id,
    }
    if role_instance is not None:
        attrs.update(
            {
                "service.instance.id": role_instance.instance_id,
                "agentic_mesh.role_id": role_instance.role_id,
                "agentic_mesh.role_instance_id": role_instance.instance_id,
            }
        )
    if component is not None:
        attrs["agentic_mesh.component"] = component
    _configured_resource_attributes = attrs

    if os.getenv("AGENTIC_MESH_OTEL_ENABLED", "true").lower() in {"0", "false", "no"}:
        return

    try:
        from opentelemetry import trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        return

    resource = Resource.create(attrs)
    try:
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)
        _tracer = trace.get_tracer("agentic_mesh")
    except Exception:
        _tracer = trace.get_tracer("agentic_mesh")

    try:
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        _meter = meter_provider.get_meter("agentic_mesh")
        _event_counter = _meter.create_counter("agentic_mesh.events_total")
        _create_metric_instruments()
    except Exception:
        _meter = None
        _event_counter = None

    try:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(logger_provider)
        _logger = logger_provider.get_logger("agentic_mesh")
    except Exception:
        _logger = None


def _create_metric_instruments() -> None:
    if _meter is None:
        return
    _metric_instruments["agent_run_duration"] = _meter.create_histogram(
        "agentic_mesh.agent.run.duration",
        unit="s",
    )
    _metric_instruments["queue_wait_duration"] = _meter.create_histogram(
        "agentic_mesh.queue.wait.duration",
        unit="s",
    )
    _metric_instruments["human_response_wait_duration"] = _meter.create_histogram(
        "agentic_mesh.human_response.wait.duration",
        unit="s",
    )
    _metric_instruments["teams_send_duration"] = _meter.create_histogram(
        "agentic_mesh.teams.send.duration",
        unit="s",
    )
    _metric_instruments["work_completed"] = _meter.create_counter(
        "agentic_mesh.work.completed_total"
    )
    _metric_instruments["handoffs"] = _meter.create_counter("agentic_mesh.handoffs_total")
    _metric_instruments["human_responses"] = _meter.create_counter(
        "agentic_mesh.human_responses_total"
    )
    _metric_instruments["connector_messages"] = _meter.create_counter(
        "agentic_mesh.connector.messages_total"
    )
    for name in [
        "agentic_mesh.role_queue.pending",
        "agentic_mesh.connector_outbox.pending",
    ]:
        _meter.create_observable_gauge(
            name,
            callbacks=[lambda options, metric_name=name: _observe_gauge(metric_name)],
        )


def _observe_gauge(metric_name: str):
    try:
        from opentelemetry.sdk.metrics import Observation
    except Exception:
        return []
    return [
        Observation(value, attributes)
        for value, attributes in _gauge_values.get(metric_name, [])
    ]


def service_name_for_instance(instance: RoleInstanceConfig) -> str:
    return instance.telemetry_service_name


def service_name_for_component(mesh_config: MeshConfig, component: str, ordinal: int = 1) -> str:
    naming = mesh_config.organization.naming_defaults
    return naming.service_name_template.format(
        brand_prefix=naming.brand_prefix,
        team_slug=project_team_slug(mesh_config.project),
        project_id=mesh_config.project.project_id,
        role_id=component,
        role_display_name=component.replace("-", " ").title(),
        ordinal=ordinal,
    )


def resource_attributes() -> dict[str, str]:
    return dict(_configured_resource_attributes)


def span_attributes(**fields: Any) -> dict[str, Any]:
    attrs = {key: value for key, value in fields.items() if value is not None}
    human_response_value = attrs.get("human_response_value", attrs.get("response_value"))
    if human_response_value is not None:
        attrs["human_response.value"] = human_response_value
    attrs.update({f"resource.{key}": value for key, value in _configured_resource_attributes.items()})
    return attrs


@contextmanager
def start_span(
    name: str,
    *,
    correlation_id: str | None = None,
    trace_context: dict[str, str] | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[dict[str, str]]:
    attrs = attributes or {}
    context = dict(trace_context or base_trace_context(correlation_id))
    if "trace_id" not in context:
        context["trace_id"] = trace_id_from_correlation(correlation_id)
    start = time.perf_counter()
    span_obj = None
    token_context = _otel_parent_context(context)
    try:
        if _tracer is not None:
            span_cm = _tracer.start_as_current_span(
                name,
                context=token_context,
                attributes=attrs,
            )
            span_obj = span_cm.__enter__()
            span_context = span_obj.get_span_context()
            if span_context and span_context.is_valid:
                context["trace_id"] = f"{span_context.trace_id:032x}"
                context["parent_span_id"] = f"{span_context.span_id:016x}"
        yield context
    except Exception as exc:
        if span_obj is not None:
            try:
                span_obj.record_exception(exc)
                span_obj.set_attribute("error", True)
            except Exception:
                pass
        raise
    finally:
        duration = time.perf_counter() - start
        if span_obj is not None:
            try:
                span_cm.__exit__(None, None, None)
            except Exception:
                pass
        if _test_sink is not None:
            _test_sink.spans.append(
                RecordedSpan(
                    name=name,
                    attributes=dict(attrs),
                    trace_context=dict(context),
                    duration_seconds=duration,
                )
            )


def _otel_parent_context(trace_context: dict[str, str]):
    try:
        from opentelemetry import trace
        from opentelemetry.trace import NonRecordingSpan
        from opentelemetry.trace import SpanContext
        from opentelemetry.trace import TraceFlags
        from opentelemetry.trace import TraceState

        trace_id = int(trace_context["trace_id"], 16)
        span_id = int(trace_context.get("parent_span_id") or "1" * HEX_SPAN_LENGTH, 16)
        parent = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
        return trace.set_span_in_context(NonRecordingSpan(parent))
    except Exception:
        return None


def emit_log(event: dict[str, Any]) -> None:
    if _test_sink is not None:
        _test_sink.logs.append(dict(event))
    if _logger is not None:
        try:
            from opentelemetry._logs import LogRecord
            from opentelemetry._logs import SeverityNumber
            from opentelemetry.trace import TraceFlags

            correlation_id = event.get("correlation_id")
            trace_id = int(trace_id_from_correlation(str(correlation_id)), 16) if correlation_id else None

            _logger.emit(
                LogRecord(
                    timestamp=_timestamp_ns(event.get("timestamp")),
                    body=str(event.get("event_type") or "agentic_mesh.event"),
                    trace_id=trace_id,
                    span_id=int("1" * HEX_SPAN_LENGTH, 16) if trace_id is not None else None,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED) if trace_id is not None else None,
                    severity_number=SeverityNumber.INFO,
                    severity_text="INFO",
                    attributes=_clean_attributes(event),
                )
            )
        except Exception:
            pass


def record_journal_event(event: dict[str, Any]) -> None:
    emit_log(event)
    event_type = str(event.get("event_type") or "unknown")
    attrs = _metric_attrs(**event)
    attrs["event_type"] = event_type
    if _event_counter is not None:
        try:
            _event_counter.add(1, attrs)
        except Exception:
            pass
    if _test_sink is not None:
        _test_sink.metrics.append({"name": "agentic_mesh.events_total", "value": 1, "attributes": attrs})
    _record_derived_metric(event)


def _record_derived_metric(event: dict[str, Any]) -> None:
    event_type = event.get("event_type")
    attrs = _metric_attrs(**event)
    if event_type == "work_completed":
        _add_counter("work_completed", attrs)
    elif event_type == "handoff_emitted":
        _add_counter("handoffs", attrs)
    elif event_type in {"human_response_recorded", "human_response_received_from_teams"}:
        _add_counter("human_responses", attrs)
    elif event_type in {
        "connector_message_completed",
        "teams_bot_message_sent",
        "teams_graph_message_sent",
        "teams_connector_message_prepared",
    }:
        _add_counter("connector_messages", attrs)


def _add_counter(key: str, attrs: dict[str, Any]) -> None:
    instrument = _metric_instruments.get(key)
    if instrument is not None:
        try:
            instrument.add(1, attrs)
        except Exception:
            pass


def record_duration(name: str, seconds: float, attributes: dict[str, Any]) -> None:
    key = {
        "agentic_mesh.agent.run.duration": "agent_run_duration",
        "agentic_mesh.queue.wait.duration": "queue_wait_duration",
        "agentic_mesh.human_response.wait.duration": "human_response_wait_duration",
        "agentic_mesh.teams.send.duration": "teams_send_duration",
    }.get(name)
    if key and _metric_instruments.get(key) is not None:
        try:
            _metric_instruments[key].record(seconds, attributes)
        except Exception:
            pass
    if _test_sink is not None:
        _test_sink.metrics.append({"name": name, "value": seconds, "attributes": dict(attributes)})


def set_gauge(name: str, value: float, attributes: dict[str, Any]) -> None:
    existing = [
        item
        for item in _gauge_values.get(name, [])
        if item[1] != attributes
    ]
    existing.append((value, dict(attributes)))
    _gauge_values[name] = existing
    if _test_sink is not None:
        _test_sink.metrics.append({"name": name, "value": value, "attributes": dict(attributes)})


def elapsed_seconds(iso_timestamp: str | None) -> float | None:
    if not iso_timestamp:
        return None
    try:
        start = datetime.fromisoformat(iso_timestamp)
        return max(0.0, (datetime.now(start.tzinfo) - start).total_seconds())
    except Exception:
        return None


def _clean_attributes(fields: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, str | bool | int | float):
            attrs[key] = value
        elif value is not None:
            attrs[key] = str(value)
    human_response_value = attrs.get("human_response_value", attrs.get("response_value"))
    if human_response_value is not None:
        attrs["human_response.value"] = human_response_value
    return attrs


def _metric_attrs(**fields: Any) -> dict[str, Any]:
    allowed = {
        "event_type",
        "project_id",
        "role_id",
        "role_instance_id",
        "lifecycle_state",
        "status",
        "channel",
        "connector_id",
        "gate_id",
        "response_value",
        "work_item_type",
    }
    return {
        key: value
        for key, value in _clean_attributes(fields).items()
        if key in allowed
    }


def _timestamp_ns(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1_000_000_000)
    except Exception:
        return None
