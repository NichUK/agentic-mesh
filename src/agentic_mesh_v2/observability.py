from __future__ import annotations

import os
from contextlib import contextmanager
from collections.abc import Iterator

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
except ImportError:  # pragma: no cover - exercised in minimal host environments.
    trace = None
    OTLPSpanExporter = None
    Resource = None
    TracerProvider = None
    BatchSpanProcessor = None


_CONFIGURED = False


def configure_observability(service_name: str) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    if trace is None:
        _CONFIGURED = True
        return
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        _CONFIGURED = True
        return
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "agentic-mesh",
            "deployment.environment": os.environ.get(
                "AGENTIC_MESH_ENVIRONMENT",
                "dogfood",
            ),
        }
    )
    provider = TracerProvider(resource=resource)
    insecure = os.environ.get("OTEL_EXPORTER_OTLP_INSECURE", "true").casefold() == "true"
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint,
                insecure=insecure,
            )
        )
    )
    trace.set_tracer_provider(provider)
    _CONFIGURED = True


@contextmanager
def span(name: str, **attributes: object) -> Iterator[None]:
    if trace is None:
        yield
        return
    tracer = trace.get_tracer("agentic_mesh_v2")
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield
