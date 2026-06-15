from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - exercised when optional OTEL runtime is installed.
    from opentelemetry import metrics
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs import LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry._logs import set_logger_provider
except ImportError:  # pragma: no cover - minimal host environments.
    metrics = None
    trace = None
    OTLPLogExporter = None
    OTLPMetricExporter = None
    OTLPSpanExporter = None
    LoggerProvider = None
    LoggingHandler = None
    BatchLogRecordProcessor = None
    MeterProvider = None
    PeriodicExportingMetricReader = None
    Resource = None
    TracerProvider = None
    BatchSpanProcessor = None
    set_logger_provider = None


@dataclass(frozen=True)
class TelemetrySettings:
    service_name: str
    service_namespace: str = "agentic-mesh"
    environment: str = "local"
    otlp_endpoint: str | None = None
    insecure: bool = True

    @classmethod
    def from_env(cls, *, service_name: str) -> "TelemetrySettings":
        return cls(
            service_name=service_name,
            service_namespace=os.environ.get("OTEL_SERVICE_NAMESPACE", "agentic-mesh"),
            environment=os.environ.get("AGENTIC_MESH_ENVIRONMENT", "local"),
            otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            insecure=os.environ.get("OTEL_EXPORTER_OTLP_INSECURE", "true").casefold() == "true",
        )


class V3Telemetry:
    def __init__(self, *, tracer: Any = None, meter: Any = None, logger: logging.Logger | None = None) -> None:
        self._tracer = tracer
        self._meter = meter
        self._logger = logger or logging.getLogger("agentic_mesh_v3")
        self._counters: dict[str, Any] = {}

    @contextmanager
    def span(self, name: str, **attributes: object) -> Iterator[None]:
        if self._tracer is None:
            yield
            return
        with self._tracer.start_as_current_span(name) as current:
            for key, value in attributes.items():
                if value is not None:
                    current.set_attribute(key, value)
            yield

    def increment(self, name: str, *, value: int = 1, attributes: dict[str, object] | None = None) -> None:
        if self._meter is None:
            return
        counter = self._counters.get(name)
        if counter is None:
            counter = self._meter.create_counter(name)
            self._counters[name] = counter
        counter.add(value, attributes or {})

    def log_event(self, event_name: str, *, level: int = logging.INFO, **fields: object) -> None:
        payload = {"event": event_name, **{key: value for key, value in fields.items() if value is not None}}
        self._logger.log(level, json.dumps(payload, sort_keys=True), extra={"agentic_mesh_event": payload})


_CONFIGURED = False
_TELEMETRY = V3Telemetry()


def configure_observability(settings: TelemetrySettings | None = None) -> V3Telemetry:
    global _CONFIGURED
    global _TELEMETRY
    if _CONFIGURED:
        return _TELEMETRY

    settings = settings or TelemetrySettings.from_env(service_name="agentic-mesh-v3")
    logger = logging.getLogger("agentic_mesh_v3")
    logger.addHandler(logging.NullHandler())

    if not settings.otlp_endpoint or trace is None or metrics is None:
        _TELEMETRY = V3Telemetry(logger=logger)
        _CONFIGURED = True
        return _TELEMETRY

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.namespace": settings.service_namespace,
            "deployment.environment": settings.environment,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=settings.insecure))
    )
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=settings.otlp_endpoint, insecure=settings.insecure)
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)

    if LoggerProvider is not None and set_logger_provider is not None:
        log_provider = LoggerProvider(resource=resource)
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=settings.otlp_endpoint, insecure=settings.insecure))
        )
        set_logger_provider(log_provider)
        logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=log_provider))

    _TELEMETRY = V3Telemetry(
        tracer=trace.get_tracer("agentic_mesh_v3"),
        meter=metrics.get_meter("agentic_mesh_v3"),
        logger=logger,
    )
    _CONFIGURED = True
    return _TELEMETRY


def get_telemetry() -> V3Telemetry:
    return _TELEMETRY


def reset_observability_for_tests() -> None:
    global _CONFIGURED
    global _TELEMETRY
    _CONFIGURED = False
    _TELEMETRY = V3Telemetry()
