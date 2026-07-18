from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

from fastapi.testclient import TestClient
import httpx
import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.api import create_app
from agentic_mesh_v5.api_auth import TokenAuthorizer
from agentic_mesh_v5.api_client import ApiCallError
from agentic_mesh_v5.api_client import ApiClientConfigurationError
from agentic_mesh_v5.api_client import ControlApiClient
from agentic_mesh_v5.cli import main
from agentic_mesh_v5.database import MigrationRunner


TOKEN = "bootstrap-cli-token-for-tests"


class _TestClientTransport(httpx.BaseTransport):
    def __init__(self, app) -> None:
        self.client = TestClient(app, raise_server_exceptions=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.client.request(
            request.method,
            request.url.path,
            headers=dict(request.headers),
            content=request.read(),
        )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    def close(self) -> None:
        pass


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


def _token_file(tmp_path: Path, token: str = TOKEN) -> Path:
    path = tmp_path / f"token-{uuid.uuid4().hex}"
    path.write_text(token, encoding="utf-8")
    return path


def _client(tmp_path: Path, handler) -> ControlApiClient:
    return ControlApiClient(
        base_url="https://mesh.example",
        token_file=_token_file(tmp_path),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://mesh.example",
        "https://user:password@mesh.example",
        "https://mesh.example/api",
        "file:///mesh",
    ],
)
def test_client_rejects_unsafe_api_urls(tmp_path: Path, url: str) -> None:
    with pytest.raises(ApiClientConfigurationError):
        ControlApiClient(base_url=url, token_file=_token_file(tmp_path))


@pytest.mark.parametrize(
    "token", [" token", "token ", "token with space", "token-é", "token\x7f"]
)
def test_client_rejects_header_unsafe_tokens(tmp_path: Path, token: str) -> None:
    with pytest.raises(ApiClientConfigurationError, match="opaque ASCII"):
        ControlApiClient(
            base_url="https://mesh.example", token_file=_token_file(tmp_path, token)
        )


@pytest.mark.parametrize(
    "path",
    [
        "https://mesh.example/api/v1/health",
        "/api/v1/../secrets",
        "/api/v1/%2e%2e/secrets",
        "/api/v1/health?token=value",
        "/not-versioned",
    ],
)
def test_client_rejects_paths_outside_the_versioned_boundary(
    tmp_path: Path, path: str
) -> None:
    client = _client(tmp_path, lambda request: httpx.Response(200, request=request))

    with pytest.raises(ApiClientConfigurationError):
        client.call(method="GET", path=path)


def test_client_redacts_secrets_and_maps_problem_exit_codes(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b""
        action_id = request.headers["x-request-id"]
        if request.url.path == "/api/v1/ok":
            return httpx.Response(
                200,
                headers={"x-request-id": action_id},
                json={
                    "lease_token": "lease-secret",
                    "nested": {"password": "hidden"},
                    "message": f"never echo {TOKEN}",
                },
            )
        status = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            status,
            headers={"x-request-id": action_id},
            json={
                "type": "urn:agentic-mesh:error:rejected",
                "detail": f"safe detail without {TOKEN}",
            },
        )

    client = _client(tmp_path, handler)
    result = client.call(method="GET", path="/api/v1/ok").to_dict()

    assert result["result"] == {
        "lease_token": "<redacted>",
        "nested": {"password": "<redacted>"},
        "message": "never echo <redacted>",
    }
    assert len(result["action_id"]) == 32
    for status, exit_code in ((401, 3), (409, 4), (503, 5)):
        with pytest.raises(ApiCallError) as failure:
            client.call(method="GET", path=f"/api/v1/{status}")
        assert failure.value.exit_code == exit_code
        assert TOKEN not in json.dumps(failure.value.to_dict())


def test_client_rejects_unconfirmed_or_non_json_api_responses(tmp_path: Path) -> None:
    no_confirmation = _client(
        tmp_path, lambda request: httpx.Response(200, json={}, request=request)
    )
    with pytest.raises(ApiCallError, match="confirm the action"):
        no_confirmation.call(method="GET", path="/api/v1/health")

    def non_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": request.headers["x-request-id"]},
            text="not json",
        )

    with pytest.raises(ApiCallError, match="non-JSON"):
        _client(tmp_path, non_json).call(method="GET", path="/api/v1/health")


def test_cli_returns_service_exit_without_printing_transport_details(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed with {TOKEN}", request=request)

    client = _client(tmp_path, unavailable)
    monkeypatch.setattr(
        ControlApiClient, "from_environment", classmethod(lambda cls: client)
    )

    exit_code = main(["--json", "control-status"])
    output = capsys.readouterr().out

    assert exit_code == 5
    assert json.loads(output)["error"]["code"] == "service_unavailable"
    assert TOKEN not in output


def test_cli_operates_lifecycle_and_sponsor_gate_through_real_api(
    postgres_database: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    MigrationRunner(postgres_database).migrate()
    authorizer = TokenAuthorizer(
        [
            {
                "subject": "sponsor-1",
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "projects": ["*"],
                "scopes": ["project:create", "read", "write"],
            }
        ]
    )
    app = create_app(postgres_database, authorizer=authorizer)
    client = ControlApiClient(
        base_url="https://mesh.example",
        token_file=_token_file(tmp_path),
        transport=_TestClientTransport(app),
    )
    monkeypatch.setattr(
        ControlApiClient, "from_environment", classmethod(lambda cls: client)
    )

    def call(method: str, path: str, body: dict | None = None):
        arguments = ["--json", "control-call", method, path]
        if body is not None:
            body_path = tmp_path / f"body-{uuid.uuid4().hex}.json"
            body_path.write_text(json.dumps(body), encoding="utf-8")
            arguments.extend(("--body-file", str(body_path)))
        assert main(arguments) == 0
        return json.loads(capsys.readouterr().out)

    outputs = [
        call(
            "POST",
            "/api/v1/projects",
            {
                "project_id": "alpha",
                "display_name": "Alpha",
                "sponsor_ids": ["sponsor-1"],
            },
        )
    ]
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('alpha', 'eng-1', 'engineering', 'hibernated')
            """
        )
    outputs.extend(
        [
            call(
                "POST",
                "/api/v1/projects/alpha/work-items",
                {
                    "work_item_id": "work-1",
                    "title": "Work 1",
                    "owner_role_id": "engineering",
                    "correlation_id": "corr-1",
                },
            ),
            call(
                "POST",
                "/api/v1/projects/alpha/work-items/work-1/transitions",
                {
                    "target_status": "active",
                    "expected_version": 1,
                    "correlation_id": "corr-1",
                },
            ),
            call(
                "POST",
                "/api/v1/projects/alpha/work-items/work-1/gates",
                {
                    "gate_id": "gate-1",
                    "gate_type": "sponsor",
                    "sponsor_ids": ["sponsor-1"],
                    "correlation_id": "corr-1",
                    "expected_version": 2,
                },
            ),
            call(
                "POST",
                "/api/v1/projects/alpha/gates/gate-1/decision",
                {"decision": "approved", "rationale": "Proceed"},
            ),
        ]
    )
    recovery = call("GET", "/api/v1/projects/alpha/recovery")
    fleet_policy = call(
        "PUT",
        "/api/v1/projects/alpha/fleet/policies/engineering",
        {
            "min_warm_instances": 0,
            "max_instances": 1,
            "scale_after_seconds": 60,
            "idle_grace_seconds": 300,
            "hibernation_enabled": True,
        },
    )
    configuration = call("GET", "/api/v1/projects/alpha/configuration")
    assert main(["control-status"]) == 0
    human_status = capsys.readouterr().out

    assert all(len(output["action_id"]) == 32 for output in outputs)
    assert len({output["action_id"] for output in outputs}) == len(outputs)
    assert outputs[-1]["result"]["approver_id"] == "sponsor-1"
    assert recovery["status"] == "ok"
    assert recovery["result"] == {"project_id": "alpha", "items": []}
    assert fleet_policy["result"]["max_instances"] == 1
    assert configuration["result"] == {"records": []}
    assert "status: ok" in human_status and "http_status: 200" in human_status
    assert TOKEN not in json.dumps(outputs)


def test_cli_authentication_failure_is_redacted(
    postgres_database: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    MigrationRunner(postgres_database).migrate()
    valid = "different-valid-token"
    app = create_app(
        postgres_database,
        authorizer=TokenAuthorizer(
            [
                {
                    "subject": "operator",
                    "token_sha256": hashlib.sha256(valid.encode()).hexdigest(),
                    "projects": ["*"],
                    "scopes": ["read"],
                }
            ]
        ),
    )
    client = ControlApiClient(
        base_url="https://mesh.example",
        token_file=_token_file(tmp_path),
        transport=_TestClientTransport(app),
    )
    monkeypatch.setattr(
        ControlApiClient, "from_environment", classmethod(lambda cls: client)
    )

    exit_code = main(["--json", "control-call", "GET", "/api/v1/projects"])
    output = capsys.readouterr().out

    assert exit_code == 3
    assert json.loads(output)["http_status"] == 401
    assert TOKEN not in output
