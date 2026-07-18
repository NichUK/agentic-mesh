from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.api import API_PREFIX
from agentic_mesh_v5.api import create_app
from agentic_mesh_v5.api_auth import AuthenticationConfigurationError
from agentic_mesh_v5.api_auth import TokenAuthorizer
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.lifecycle import LifecycleStore


TOKENS = {
    "operator": "operator-token-for-tests",
    "alpha": "alpha-token-for-tests",
    "bravo": "bravo-token-for-tests",
    "viewer": "viewer-token-for-tests",
}


def _record(subject: str, token: str, projects: list[str], scopes: list[str]):
    return {
        "subject": subject,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "projects": projects,
        "scopes": scopes,
    }


def _authorizer() -> TokenAuthorizer:
    return TokenAuthorizer(
        [
            _record(
                "operator", TOKENS["operator"], ["*"],
                ["project:create", "read", "write"],
            ),
            _record("sponsor-1", TOKENS["alpha"], ["alpha"], ["project:create", "read", "write"]),
            _record(
                "bravo-sponsor", TOKENS["bravo"], ["bravo"], ["read", "write"]
            ),
            _record("viewer", TOKENS["viewer"], ["alpha"], ["read"]),
        ]
    )


def _headers(token_name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[token_name]}"}


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


@pytest.fixture
def api_database(postgres_database: str) -> tuple[str, TestClient]:
    MigrationRunner(postgres_database).migrate()
    lifecycle = LifecycleStore(postgres_database)
    lifecycle.create_project(
        project_id="alpha", display_name="Alpha", sponsor_ids=("sponsor-1",)
    )
    lifecycle.create_project(
        project_id="bravo", display_name="Bravo", sponsor_ids=("bravo-sponsor",)
    )
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering'),
                   ('bravo', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('alpha', 'eng-1', 'engineering', 'running'),
                   ('alpha', 'eng-2', 'engineering', 'running'),
                   ('bravo', 'eng-1', 'engineering', 'running')
            """
        )
    return postgres_database, TestClient(
        create_app(postgres_database, authorizer=_authorizer())
    )


def test_external_token_records_are_hashed_strict_and_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "principals.json"
    path.write_text(
        json.dumps(
            {
                "principals": [
                    _record("sponsor-1", TOKENS["alpha"], ["alpha"], ["read"])
                ]
            }
        ),
        encoding="utf-8",
    )

    authorizer = TokenAuthorizer.from_file(path)

    assert authorizer.resolve(TOKENS["alpha"]).subject == "sponsor-1"
    assert authorizer.resolve("wrong-token") is None
    bad = json.loads(path.read_text(encoding="utf-8"))
    bad["principals"][0]["token"] = TOKENS["alpha"]
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(AuthenticationConfigurationError, match="requires"):
        TokenAuthorizer.from_file(path)


def test_openapi_and_problem_contract_do_not_require_a_database() -> None:
    client = TestClient(
        create_app("postgresql://unused/mesh", authorizer=_authorizer()),
        raise_server_exceptions=False,
    )

    specification = client.get("/openapi.json").json()
    paths = specification["paths"]

    assert specification["info"]["title"] == "Agentic Mesh V5 Control API"
    assert specification["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert f"{API_PREFIX}/projects/{{project_id}}/work-items" in paths
    assert f"{API_PREFIX}/projects/{{project_id}}/queues/{{queue_id}}/claim" in paths
    assert f"{API_PREFIX}/projects/{{project_id}}/usage" in paths
    assert f"{API_PREFIX}/projects/{{project_id}}/recovery" in paths
    responses = paths[f"{API_PREFIX}/projects/{{project_id}}"]["get"]["responses"]
    assert all("application/problem+json" in responses[code]["content"] for code in ("401", "500"))

    response = client.get(f"{API_PREFIX}/projects/alpha")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert response.json()["type"].endswith(":authentication_required")
    assert TOKENS["alpha"] not in response.text


def test_project_authorization_and_read_views_are_isolated(api_database) -> None:
    _database_url, client = api_database

    health = client.get(f"{API_PREFIX}/health")
    created = client.post(
        f"{API_PREFIX}/projects",
        headers=_headers("operator"),
        json={
            "project_id": "charlie",
            "display_name": "Charlie",
            "sponsor_ids": ["charlie-sponsor"],
        },
    )
    visible = client.get(f"{API_PREFIX}/projects", headers=_headers("alpha"))
    forbidden = client.get(
        f"{API_PREFIX}/projects/alpha", headers=_headers("bravo")
    )
    agents = client.get(
        f"{API_PREFIX}/projects/alpha/agents", headers=_headers("viewer")
    )
    usage = client.get(
        f"{API_PREFIX}/projects/alpha/usage", headers=_headers("viewer")
    )
    forbidden_create = client.post(
        f"{API_PREFIX}/projects", headers=_headers("alpha"),
        json={"project_id": "foreign", "display_name": "Foreign", "sponsor_ids": ["sponsor"]},
    )

    assert health.json()["status"] == "ok"
    assert health.json()["schema_version"] == 6
    assert created.status_code == 201
    assert created.json()["project_id"] == "charlie"
    assert [item["project_id"] for item in visible.json()] == ["alpha"]
    assert forbidden.status_code == 403
    assert forbidden.json()["type"].endswith(":project_access_denied")
    assert [item["instance_id"] for item in agents.json()["instances"]] == [
        "eng-1",
        "eng-2",
    ]
    assert usage.json() == {
        "domain": "usage",
        "status": "planned",
        "planned_story": "AMV5-028",
        "available_operations": [],
    }
    assert forbidden_create.status_code == 403


def test_lifecycle_operations_use_authenticated_actor_and_structured_conflicts(
    api_database,
) -> None:
    database_url, client = api_database
    headers = _headers("alpha")

    created = client.post(
        f"{API_PREFIX}/projects/alpha/work-items",
        headers=headers,
        json={
            "work_item_id": "work-1",
            "title": "Work 1",
            "owner_role_id": "engineering",
            "correlation_id": "corr-1",
        },
    )
    active = client.post(
        f"{API_PREFIX}/projects/alpha/work-items/work-1/transitions",
        headers=headers,
        json={
            "target_status": "active",
            "expected_version": 1,
            "correlation_id": "corr-1",
        },
    )
    stale = client.post(
        f"{API_PREFIX}/projects/alpha/work-items/work-1/transitions",
        headers=headers,
        json={
            "target_status": "completed",
            "expected_version": 1,
            "correlation_id": "corr-1",
            "reason": "stale",
        },
    )
    gate = client.post(
        f"{API_PREFIX}/projects/alpha/work-items/work-1/gates",
        headers=headers,
        json={
            "gate_id": "gate-1",
            "gate_type": "sponsor",
            "sponsor_ids": ["sponsor-1"],
            "correlation_id": "corr-1",
            "expected_version": 2,
        },
    )
    decision = client.post(
        f"{API_PREFIX}/projects/alpha/gates/gate-1/decision",
        headers=headers,
        json={"decision": "approved", "rationale": "Proceed"},
    )
    work_items = client.get(
        f"{API_PREFIX}/projects/alpha/work-items", headers=headers
    )

    assert created.status_code == 201
    assert active.json()["status"] == "active"
    assert stale.status_code == 409
    assert stale.json()["type"].endswith(":conflict")
    assert gate.status_code == 201
    assert decision.json()["approver_id"] == "sponsor-1"
    assert [item["work_item_id"] for item in work_items.json()] == ["work-1"]
    with psycopg.connect(database_url) as connection:
        actor = connection.execute(
            """
            SELECT actor_id FROM agentic_mesh_v5.events
            WHERE project_id = 'alpha' AND event_type = 'work.created'
            """
        ).fetchone()[0]
    assert actor == "sponsor-1"


def test_queue_claim_is_concurrent_and_lease_token_controls_completion(
    api_database,
) -> None:
    _database_url, client = api_database
    headers = _headers("alpha")
    client.post(
        f"{API_PREFIX}/projects/alpha/work-items",
        headers=headers,
        json={
            "work_item_id": "work-queue",
            "title": "Queue work",
            "owner_role_id": "engineering",
            "correlation_id": "corr-queue",
        },
    )
    assert client.post(
        f"{API_PREFIX}/projects/alpha/queues",
        headers=headers,
        json={"queue_id": "engineering", "role_id": "engineering"},
    ).status_code == 201
    assert client.post(
        f"{API_PREFIX}/projects/alpha/queues/engineering/items",
        headers=headers,
        json={
            "queue_item_id": "item-1",
            "work_item_id": "work-queue",
            "idempotency_key": "idem-item-1",
            "priority": 10,
        },
    ).status_code == 201

    def claim(instance_id: str):
        with TestClient(client.app) as isolated:
            return isolated.post(
                f"{API_PREFIX}/projects/alpha/queues/engineering/claim",
                headers=headers,
                json={"owner_instance_id": instance_id, "lease_seconds": 60},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(claim, ["eng-1", "eng-2"]))
    payloads = [response.json() for response in responses]
    claims = [payload for payload in payloads if payload is not None]

    assert all(response.status_code == 200 for response in responses)
    assert len(claims) == 1
    claimed = claims[0]
    rejected = client.post(
        f"{API_PREFIX}/projects/alpha/leases/{claimed['lease_id']}/complete",
        headers=headers,
        json={"lease_token": "wrong-token"},
    )
    completed = client.post(
        f"{API_PREFIX}/projects/alpha/leases/{claimed['lease_id']}/complete",
        headers=headers,
        json={"lease_token": claimed["lease_token"]},
    )
    metrics = client.get(
        f"{API_PREFIX}/projects/alpha/queues/engineering/metrics",
        headers=_headers("viewer"),
    )

    assert rejected.status_code == 403
    assert completed.json()["status"] == "completed"
    assert metrics.json()["depth"] == 0
    assert metrics.json()["total_attempts"] == 1


def test_validation_and_store_failures_are_actionable_and_redacted() -> None:
    client = TestClient(
        create_app(
            "postgresql://127.0.0.1:1/not-there?connect_timeout=1",
            authorizer=_authorizer(),
        ),
        raise_server_exceptions=False,
    )

    invalid = client.post(
        f"{API_PREFIX}/projects",
        headers=_headers("operator"),
        json={"project_id": "alpha", "display_name": "Alpha", "sponsor_ids": []},
    )
    unavailable = client.get(f"{API_PREFIX}/projects/alpha/work-items/missing", headers=_headers("alpha"))

    assert invalid.status_code == 422
    assert invalid.json()["errors"][0]["location"][-1] == "sponsor_ids"
    assert unavailable.status_code == 503
    assert unavailable.json()["type"].endswith(":durable_store_unavailable")
    assert "127.0.0.1" not in unavailable.text
