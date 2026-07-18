from __future__ import annotations

import hashlib
import json
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.api import API_PREFIX, create_app
from agentic_mesh_v5.api_auth import TokenAuthorizer
from agentic_mesh_v5.database import MigrationRunner, load_migrations
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.telemetry import Telemetry, telemetry_from_environment


TOKEN = "telemetry-test-token"
PARENT_TRACE_ID = "11111111111111111111111111111111"
PARENT_SPAN_ID = "2222222222222222"


def _authorizer() -> TokenAuthorizer:
    return TokenAuthorizer(
        [
            {
                "subject": "operator",
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "projects": ["alpha"],
                "scopes": ["read", "write"],
            }
        ]
    )


def _telemetry():
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    return (
        Telemetry(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
        ),
        span_exporter,
        metric_reader,
    )


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for Postgres tests")
    database_name = f"mesh_v5_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    parsed = urlsplit(base_url)
    database_url = urlunsplit(parsed._replace(path=f"/{database_name}"))
    try:
        yield database_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


def test_request_trace_continuity_safe_attributes_and_metrics() -> None:
    telemetry, exporter, metric_reader = _telemetry()
    client = TestClient(
        create_app(
            "postgresql://db-user:db-password@unused/mesh",
            authorizer=_authorizer(),
            telemetry=telemetry,
        )
    )

    response = client.get(
        f"{API_PREFIX}/projects/alpha",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "traceparent": f"00-{PARENT_TRACE_ID}-{PARENT_SPAN_ID}-01",
            "x-request-id": "request-safe-1",
        },
    )

    assert response.status_code == 503
    assert response.headers["x-request-id"] == "request-safe-1"
    assert response.headers["traceparent"].startswith(f"00-{PARENT_TRACE_ID}-")
    spans = exporter.get_finished_spans()
    request_span = spans[-1]
    assert request_span.context.trace_id == int(PARENT_TRACE_ID, 16)
    assert request_span.parent.span_id == int(PARENT_SPAN_ID, 16)
    assert request_span.name == "GET /api/v1/projects/{project_id}"
    assert request_span.attributes["http.route"] == "/api/v1/projects/{project_id}"
    assert request_span.attributes["mesh.project.id"] == "alpha"
    assert request_span.attributes["mesh.request.id"] == "request-safe-1"
    serialized = json.dumps(dict(request_span.attributes), sort_keys=True)
    assert TOKEN not in serialized
    assert "db-password" not in serialized
    assert "postgresql://" not in serialized

    metrics = metric_reader.get_metrics_data()
    points = [
        point
        for resource_metric in metrics.resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
        for point in metric.data.data_points
        if metric.name == "agentic_mesh.control.requests"
    ]
    assert len(points) == 1
    assert points[0].attributes == {
        "http.request.method": "GET",
        "http.route": "/api/v1/projects/{project_id}",
        "http.response.status_code": 503,
    }


def test_unmatched_paths_and_operation_spans_do_not_capture_content() -> None:
    telemetry, exporter, _metric_reader = _telemetry()
    client = TestClient(
        create_app(
            "postgresql://unused/mesh",
            authorizer=_authorizer(),
            telemetry=telemetry,
        )
    )

    response = client.get("/secret-project/token-value")
    with telemetry.operation(
        "handoff.accept",
        project_id="alpha",
        work_item_id="work-1",
        correlation_id="corr-1",
    ):
        pass

    assert response.status_code == 404
    spans = exporter.get_finished_spans()
    request_span = next(item for item in spans if item.name == "GET unmatched")
    operation_span = next(item for item in spans if item.name == "mesh.handoff.accept")
    assert request_span.attributes["http.route"] == "unmatched"
    assert "secret-project" not in json.dumps(dict(request_span.attributes))
    assert operation_span.attributes == {
        "mesh.project.id": "alpha",
        "mesh.work_item.id": "work-1",
        "mesh.correlation.id": "corr-1",
    }


def test_liveness_is_database_independent_and_readiness_is_redacted() -> None:
    telemetry, _exporter, metric_reader = _telemetry()
    client = TestClient(
        create_app(
            "postgresql://db-user:db-password@127.0.0.1:1/not-there?connect_timeout=1",
            authorizer=_authorizer(),
            telemetry=telemetry,
        )
    )

    live = client.get(f"{API_PREFIX}/health/live")
    ready = client.get(f"{API_PREFIX}/health/ready")
    specification = client.get("/openapi.json").json()

    assert live.status_code == 200
    assert live.json()["kind"] == "liveness"
    assert live.json()["dependencies"] == []
    assert ready.status_code == 503
    assert ready.json()["status"] == "degraded"
    assert ready.json()["dependencies"] == [
        {
            "name": "postgres",
            "status": "unavailable",
            "reason": "connection_failed",
        }
    ]
    assert "db-password" not in ready.text
    assert "postgresql://" not in ready.text
    assert f"{API_PREFIX}/health/live" in specification["paths"]
    assert f"{API_PREFIX}/health/ready" in specification["paths"]
    health_points = [
        point
        for resource_metric in metric_reader.get_metrics_data().resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
        for point in metric.data.data_points
        if metric.name == "agentic_mesh.health.checks"
    ]
    assert len(health_points) == 1
    assert health_points[0].attributes == {
        "dependency": "postgres",
        "status": "unavailable",
    }


def test_pending_and_current_migrations_control_readiness(
    postgres_database: str,
) -> None:
    MigrationRunner(
        postgres_database, migrations=load_migrations()[:1]
    ).migrate()
    telemetry, exporter, _metric_reader = _telemetry()
    client = TestClient(
        create_app(
            postgres_database,
            authorizer=_authorizer(),
            telemetry=telemetry,
        )
    )

    pending = client.get(f"{API_PREFIX}/health")
    MigrationRunner(postgres_database).migrate()
    current = client.get(f"{API_PREFIX}/health/ready")
    LifecycleStore(postgres_database).create_project(
        project_id="alpha", display_name="Alpha", sponsor_ids=("operator",)
    )
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering')
            """
        )
    work = client.post(
        f"{API_PREFIX}/projects/alpha/work-items",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "work_item_id": "work-traced",
            "title": "Traced work",
            "owner_role_id": "engineering",
            "correlation_id": "corr-traced",
        },
    )

    assert pending.status_code == 503
    assert pending.json()["dependencies"][0]["reason"] == "pending_migrations"
    assert pending.json()["schema_version"] == 1
    assert current.status_code == 200
    assert current.json()["status"] == "ok"
    assert current.json()["dependencies"][0]["reason"] == "schema_current"
    assert work.status_code == 201
    work_span = next(
        item
        for item in exporter.get_finished_spans()
        if item.name == "POST /api/v1/projects/{project_id}/work-items"
    )
    assert work_span.attributes["mesh.project.id"] == "alpha"
    assert work_span.attributes["mesh.work_item.id"] == "work-traced"
    assert work_span.attributes["mesh.correlation.id"] == "corr-traced"


def test_environment_telemetry_does_not_require_a_collector(monkeypatch) -> None:
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)

    telemetry = telemetry_from_environment()
    with telemetry.operation("runtime.start"):
        pass
    telemetry.shutdown()

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://unreachable.invalid:4317")
    disabled = telemetry_from_environment()
    with disabled.operation("runtime.disabled"):
        pass
    disabled.shutdown()
