from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
import os
from time import monotonic
from typing import Any

from opentelemetry import metrics, propagate, trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from agentic_mesh_v5 import __version__


INSTRUMENTATION_NAME = "agentic_mesh_v5"
SAFE_IDENTIFIER_LIMIT = 256


@dataclass(frozen=True, slots=True)
class RequestObservation:
    span: Span
    started_at: float


class Telemetry:
    """Small provider-neutral telemetry boundary for the V5 runtime."""

    def __init__(
        self,
        *,
        tracer_provider: Any | None = None,
        meter_provider: Any | None = None,
        owns_providers: bool = False,
    ) -> None:
        selected_tracer_provider = tracer_provider or trace.get_tracer_provider()
        selected_meter_provider = meter_provider or metrics.get_meter_provider()
        self.tracer: Tracer = selected_tracer_provider.get_tracer(
            INSTRUMENTATION_NAME, __version__
        )
        self.meter: Meter = selected_meter_provider.get_meter(
            INSTRUMENTATION_NAME, __version__
        )
        self._tracer_provider = selected_tracer_provider
        self._meter_provider = selected_meter_provider
        self._owns_providers = owns_providers
        self._request_count = self.meter.create_counter(
            "agentic_mesh.control.requests",
            unit="{request}",
            description="V5 control API requests that reached a response",
        )
        self._request_duration = self.meter.create_histogram(
            "agentic_mesh.control.response.start.duration",
            unit="s",
            description="Time until the V5 control API starts a response",
        )
        self._health_count = self.meter.create_counter(
            "agentic_mesh.health.checks",
            unit="{check}",
            description="Completed V5 health dependency checks",
        )
        self._health_duration = self.meter.create_histogram(
            "agentic_mesh.health.check.duration",
            unit="s",
            description="V5 health dependency check duration",
        )

    @contextmanager
    def request(
        self,
        *,
        method: str,
        request_id: str,
        carrier: Mapping[str, str],
    ) -> Iterator[RequestObservation]:
        parent = propagate.extract(carrier=carrier)
        attributes = {
            "http.request.method": method,
            "mesh.request.id": _safe_identifier(request_id),
        }
        with self.tracer.start_as_current_span(
            f"HTTP {method}",
            context=parent,
            kind=SpanKind.SERVER,
            attributes=attributes,
        ) as span:
            yield RequestObservation(span=span, started_at=monotonic())

    def finish_request(
        self,
        observation: RequestObservation,
        *,
        method: str,
        route: str,
        status_code: int,
        identifiers: Mapping[str, str],
    ) -> None:
        safe_route = route if route.startswith("/api/v1/") else "unmatched"
        span = observation.span
        span.update_name(f"{method} {safe_route}")
        span.set_attribute("http.route", safe_route)
        span.set_attribute("http.response.status_code", status_code)
        for name, value in identifiers.items():
            if name in {
                "project_id", "work_item_id", "queue_id", "gate_id", "handoff_id"
            }:
                span.set_attribute(
                    f"mesh.{name.removesuffix('_id')}.id", _safe_identifier(value)
                )
        if status_code >= 500:
            span.set_status(Status(StatusCode.ERROR))
        metric_attributes = {
            "http.request.method": method,
            "http.route": safe_route,
            "http.response.status_code": status_code,
        }
        self._request_count.add(1, metric_attributes)
        self._request_duration.record(
            max(0.0, monotonic() - observation.started_at), metric_attributes
        )

    def annotate(
        self,
        *,
        project_id: str | None = None,
        work_item_id: str | None = None,
        queue_id: str | None = None,
        handoff_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        span = trace.get_current_span()
        for name, value in {
            "mesh.project.id": project_id,
            "mesh.work_item.id": work_item_id,
            "mesh.queue.id": queue_id,
            "mesh.handoff.id": handoff_id,
            "mesh.correlation.id": correlation_id,
        }.items():
            if value:
                span.set_attribute(name, _safe_identifier(value))

    @contextmanager
    def operation(
        self,
        name: str,
        *,
        project_id: str | None = None,
        work_item_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Iterator[Span]:
        """Trace a domain operation without recording its content or credentials."""
        with self.tracer.start_as_current_span(
            f"mesh.{_operation_name(name)}", kind=SpanKind.INTERNAL
        ) as span:
            for attribute, value in {
                "mesh.project.id": project_id,
                "mesh.work_item.id": work_item_id,
                "mesh.correlation.id": correlation_id,
            }.items():
                if value:
                    span.set_attribute(attribute, _safe_identifier(value))
            yield span

    def inject_response_context(self, carrier: MutableMapping[str, str]) -> None:
        propagate.inject(carrier=carrier)

    def record_health(self, *, dependency: str, status: str, duration: float) -> None:
        attributes = {"dependency": dependency, "status": status}
        self._health_count.add(1, attributes)
        self._health_duration.record(max(0.0, duration), attributes)

    @staticmethod
    def record_safe_failure(span: Span, *, code: str) -> None:
        safe_code = _operation_name(code)
        span.record_exception(
            RuntimeError("operation failed"),
            attributes={"mesh.error.code": safe_code},
            escaped=False,
        )
        span.set_status(Status(StatusCode.ERROR))

    def shutdown(self) -> None:
        if not self._owns_providers:
            return
        for provider in (self._tracer_provider, self._meter_provider):
            shutdown = getattr(provider, "shutdown", None)
            if shutdown is not None:
                shutdown()
        self._owns_providers = False


def telemetry_from_environment() -> Telemetry:
    """Build local providers and enable OTLP only when an endpoint is configured."""
    if _sdk_disabled():
        return Telemetry()
    resource = Resource.create(
        {
            "service.name": "agentic-mesh-v5",
            "service.version": __version__,
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    metric_readers = []
    if _otlp_configured("traces"):
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    if _otlp_configured("metrics"):
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )

        metric_readers.append(
            PeriodicExportingMetricReader(OTLPMetricExporter())
        )
    meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
    return Telemetry(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        owns_providers=True,
    )


def _otlp_configured(signal: str) -> bool:
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        or os.environ.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT", "").strip()
    )


def _sdk_disabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() in {"true", "1"}


def _safe_identifier(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        return "unknown"
    return normalized[:SAFE_IDENTIFIER_LIMIT]


def _operation_name(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
        for character in normalized
    ):
        raise ValueError("telemetry operation name is invalid")
    return normalized
