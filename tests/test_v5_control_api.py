from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
from agentic_mesh_v5.database import load_migrations
from agentic_mesh_v5.fleet import FleetAction
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.usage import CapacityDraft
from agentic_mesh_v5.usage import TurnUsageDraft
from agentic_mesh_v5.usage import UsageConflict
from agentic_mesh_v5.usage import UsageStore
from agentic_mesh_v5.worker_provider import ProviderCapacity
from agentic_mesh_v5.worker_provider import ProviderCredits
from agentic_mesh_v5.worker_provider import ProviderRateLimitWindow
from agentic_mesh_v5.worker_provider import ProviderSpendControl
from agentic_mesh_v5.worker_provider import ProviderUsage


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
                   ('alpha', 'project-manager', 'project-manager'),
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
    assert f"{API_PREFIX}/projects/{{project_id}}/routes" in paths
    assert f"{API_PREFIX}/projects/{{project_id}}/handoffs" in paths
    assert (
        f"{API_PREFIX}/projects/{{project_id}}/handoffs/"
        "{handoff_id}/accept"
    ) in paths
    assert f"{API_PREFIX}/projects/{{project_id}}/usage" in paths
    assert f"{API_PREFIX}/pm-monitor/sweep" in paths
    assert (
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "reliability/incidents"
    ) in paths
    assert (
        f"{API_PREFIX}/projects/{{project_id}}/work-items/{{work_item_id}}/"
        "reliability/incidents/{incident_id}/attempts"
    ) in paths
    assert f"{API_PREFIX}/fleet/reconcile" in paths
    assert (
        f"{API_PREFIX}/projects/{{project_id}}/fleet/policies/{{role_id}}"
        in paths
    )
    assert f"{API_PREFIX}/projects/{{project_id}}/recovery" in paths
    assert (
        f"{API_PREFIX}/projects/{{project_id}}/work-items/"
        "{work_item_id}/progress"
    ) in paths
    responses = paths[f"{API_PREFIX}/projects/{{project_id}}"]["get"]["responses"]
    assert all("application/problem+json" in responses[code]["content"] for code in ("401", "500"))

    response = client.get(f"{API_PREFIX}/projects/alpha")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert response.json()["type"].endswith(":authentication_required")
    assert TOKENS["alpha"] not in response.text


def test_global_pm_monitor_api_is_operator_only_and_hides_token_from_status(
    api_database: tuple[str, TestClient],
) -> None:
    _database_url, client = api_database
    claim = client.post(
        f"{API_PREFIX}/pm-monitor/claim",
        headers=_headers("operator"),
        json={"owner_id": "pm-process-1", "lease_seconds": 60},
    )
    token = claim.json()["lease_token"]
    status_response = client.get(
        f"{API_PREFIX}/pm-monitor", headers=_headers("operator")
    )
    heartbeat = client.post(
        f"{API_PREFIX}/pm-monitor/heartbeat",
        headers=_headers("operator"),
        json={"owner_id": "pm-process-1", "lease_token": token},
    )
    sweep = client.post(
        f"{API_PREFIX}/pm-monitor/sweep",
        headers=_headers("operator"),
        json={"owner_id": "pm-process-1", "lease_token": token},
    )
    forbidden = client.post(
        f"{API_PREFIX}/pm-monitor/claim",
        headers=_headers("alpha"),
        json={"owner_id": "foreign-process", "lease_seconds": 60},
    )

    assert claim.status_code == 201
    assert status_response.status_code == heartbeat.status_code == 200
    assert "lease_token" not in status_response.json()
    assert sweep.json()["observations"] == []
    assert sweep.json()["sweep_count"] == 1
    assert forbidden.status_code == 403


def test_fleet_policy_and_reconciliation_api_are_strictly_scoped(
    api_database: tuple[str, TestClient],
) -> None:
    database_url, _client = api_database

    class Supervisor:
        def __init__(self) -> None:
            self.actions: list[FleetAction] = []

        def apply(self, action: FleetAction) -> None:
            self.actions.append(action)

    supervisor = Supervisor()
    client = TestClient(
        create_app(
            database_url,
            authorizer=_authorizer(),
            fleet_supervisor=supervisor,
            fleet_reconcile_interval_seconds=300,
        )
    )
    configured = client.put(
        f"{API_PREFIX}/projects/alpha/fleet/policies/engineering",
        headers=_headers("alpha"),
        json={
            "min_warm_instances": 0,
            "max_instances": 2,
            "scale_after_seconds": 60,
            "idle_grace_seconds": 300,
            "hibernation_enabled": True,
        },
    )
    policies = client.get(
        f"{API_PREFIX}/projects/alpha/fleet/policies",
        headers=_headers("viewer"),
    )
    reconciled = client.post(
        f"{API_PREFIX}/fleet/reconcile",
        headers=_headers("operator"),
        json={"project_id": "alpha"},
    )
    forbidden_reconcile = client.post(
        f"{API_PREFIX}/fleet/reconcile",
        headers=_headers("alpha"),
        json={"project_id": "alpha"},
    )
    forbidden_project = client.get(
        f"{API_PREFIX}/projects/alpha/fleet/policies",
        headers=_headers("bravo"),
    )

    assert configured.status_code == 200
    assert configured.json()["max_instances"] == 2
    assert policies.status_code == 200
    assert [item["role_id"] for item in policies.json()] == ["engineering"]
    assert reconciled.status_code == 200
    assert reconciled.json()["project_id"] == "alpha"
    assert reconciled.json()["actions"] == []
    assert forbidden_reconcile.status_code == 403
    assert forbidden_project.status_code == 403
    unavailable = _client.post(
        f"{API_PREFIX}/fleet/reconcile",
        headers=_headers("operator"),
        json={"project_id": "alpha"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["type"].endswith(":fleet_supervisor_unavailable")


def test_progress_write_is_structured_project_scoped_and_stale_safe(
    api_database: tuple[str, TestClient],
) -> None:
    _database_url, client = api_database
    created_work = client.post(
        f"{API_PREFIX}/projects/alpha/work-items",
        headers=_headers("alpha"),
        json={
            "work_item_id": "progress-work",
            "title": "Progress work",
            "owner_role_id": "engineering",
            "correlation_id": "corr-progress",
        },
    )
    assert created_work.status_code == 201
    payload = {
        "checkpoint_id": "checkpoint-api-1",
        "role_instance_id": "eng-1",
        "expected_previous_sequence": 0,
        "status": "working",
        "goal": "Deliver structured progress",
        "step": "Record the first checkpoint",
        "completed_action": None,
        "activity": "Writing the API test",
        "blocker": None,
        "next_action": "Read the live progress view",
        "safe_summary": "The first checkpoint is recorded.",
    }

    recorded = client.post(
        f"{API_PREFIX}/projects/alpha/work-items/progress-work/progress",
        headers=_headers("alpha"),
        json=payload,
    )

    assert recorded.status_code == 201
    assert recorded.json()["sequence"] == 1
    assert recorded.json()["safe_summary"] == payload["safe_summary"]
    progress = client.get(
        f"{API_PREFIX}/projects/alpha/progress", headers=_headers("alpha")
    )
    assert progress.status_code == 200
    assert progress.json()["records"][0]["checkpoint_id"] == "checkpoint-api-1"

    stale = client.post(
        f"{API_PREFIX}/projects/alpha/work-items/progress-work/progress",
        headers=_headers("alpha"),
        json={**payload, "checkpoint_id": "checkpoint-api-2"},
    )
    assert stale.status_code == 409
    restricted = "sk-" + "a" * 32
    rejected = client.post(
        f"{API_PREFIX}/projects/alpha/work-items/progress-work/progress",
        headers=_headers("alpha"),
        json={
            **payload,
            "checkpoint_id": "checkpoint-api-3",
            "expected_previous_sequence": 1,
            "safe_summary": restricted,
        },
    )
    assert rejected.status_code == 422
    assert restricted not in rejected.text
    forbidden = client.post(
        f"{API_PREFIX}/projects/alpha/work-items/progress-work/progress",
        headers=_headers("viewer"),
        json={**payload, "checkpoint_id": "checkpoint-api-4"},
    )
    assert forbidden.status_code == 403


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
    assert health.json()["schema_version"] == load_migrations()[-1].version
    assert created.status_code == 201
    assert created.json()["project_id"] == "charlie"
    assert [item["project_id"] for item in visible.json()] == ["alpha"]
    assert forbidden.status_code == 403
    assert forbidden.json()["type"].endswith(":project_access_denied")
    assert [item["instance_id"] for item in agents.json()["instances"]] == [
        "eng-1",
        "eng-2",
    ]
    assert usage.json()["turn_count"] == 0
    assert usage.json()["total_tokens"] == 0
    assert usage.json()["average_tokens_per_turn"] is None
    assert usage.json()["capacity"]["status"] == "unknown"
    assert usage.json()["capacity"]["observed_at"] is None
    assert forbidden_create.status_code == 403


def test_usage_is_idempotent_concurrent_and_exposes_typed_capacity(
    api_database,
) -> None:
    database_url, client = api_database
    created = client.post(
        f"{API_PREFIX}/projects/alpha/work-items",
        headers=_headers("alpha"),
        json={
            "work_item_id": "usage-work",
            "title": "Usage work",
            "owner_role_id": "engineering",
            "correlation_id": "usage-correlation",
        },
    )
    assert created.status_code == 201
    store = UsageStore(database_url)
    draft = TurnUsageDraft(
        project_id="alpha",
        work_item_id="usage-work",
        role_instance_id="eng-1",
        provider_id="codex-local",
        account_scope="default",
        turn_id="provider-turn-1",
        usage=ProviderUsage(100, 20, 40, 10, 150),
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda _index: store.record_turn(draft), range(16)))

    assert {record.turn_id for record in records} == {"provider-turn-1"}
    updated = store.record_turn(
        TurnUsageDraft(
            project_id="alpha",
            work_item_id="usage-work",
            role_instance_id="eng-1",
            provider_id="codex-local",
            account_scope="default",
            turn_id="provider-turn-1",
            usage=ProviderUsage(120, 25, 50, 12, 182),
        )
    )
    assert updated.usage.total_tokens == 182
    with pytest.raises(UsageConflict, match="cannot decrease"):
        store.record_turn(draft)

    observed_at = datetime.now(timezone.utc)
    store.record_capacity(
        CapacityDraft(
            project_id="alpha",
            provider_id="codex-local",
            account_scope="default",
            observation_id="capacity-1",
            observed_at=observed_at,
            capacity=ProviderCapacity(
                available=True,
                limit_id="codex",
                limit_name="Codex weekly",
                plan_type="plus",
                primary=ProviderRateLimitWindow(
                    25, observed_at + timedelta(hours=4), 300
                ),
                secondary=ProviderRateLimitWindow(
                    80, observed_at + timedelta(days=4), 10_080
                ),
                credits=ProviderCredits("12.50", True, False),
                individual_limit=ProviderSpendControl(
                    "50", "7.5", 85, observed_at + timedelta(days=4)
                ),
                reset_credits_available=2,
                reset_credits_earliest_expiry=observed_at + timedelta(days=1),
            ),
        )
    )

    response = client.get(
        f"{API_PREFIX}/projects/alpha/usage", headers=_headers("viewer")
    )
    store.record_capacity(
        CapacityDraft(
            project_id="bravo",
            provider_id="codex-local",
            account_scope="default",
            observation_id="capacity-missing",
            observed_at=observed_at,
            capacity=ProviderCapacity.unknown(),
        )
    )
    foreign = client.get(
        f"{API_PREFIX}/projects/bravo/usage", headers=_headers("bravo")
    )
    forbidden = client.get(
        f"{API_PREFIX}/projects/alpha/usage", headers=_headers("bravo")
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["turn_count"] == 1
    assert payload["total_tokens"] == 182
    assert payload["average_tokens_per_turn"] == 182.0
    assert payload["account_scope_shared"] is True
    assert payload["capacity"]["primary"]["remaining_percent"] == 75
    assert payload["capacity"]["secondary"]["remaining_percent"] == 20
    assert payload["capacity"]["credits"]["balance"] == "12.50"
    assert payload["capacity"]["individual_limit"]["remaining_percent"] == 85
    assert payload["capacity"]["reset_credits_available"] == 2
    assert "prompt" not in response.text.lower()
    assert foreign.json()["capacity"]["status"] == "unknown"
    assert foreign.json()["capacity"]["observed_at"] is not None
    assert foreign.json()["turn_count"] == 0
    assert forbidden.status_code == 403


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


def test_reliability_api_is_project_scoped_and_guards_early_terminal_error(
    api_database,
) -> None:
    _database_url, client = api_database
    headers = _headers("alpha")
    client.post(
        f"{API_PREFIX}/projects/alpha/work-items",
        headers=headers,
        json={
            "work_item_id": "reliability-work",
            "title": "Reliability work",
            "owner_role_id": "engineering",
            "correlation_id": "reliability-work",
        },
    )
    client.post(
        f"{API_PREFIX}/projects/alpha/work-items/reliability-work/transitions",
        headers=headers,
        json={
            "target_status": "active",
            "expected_version": 1,
            "correlation_id": "reliability-work",
        },
    )
    for role_id in ("engineering", "project-manager"):
        assert client.post(
            f"{API_PREFIX}/projects/alpha/queues",
            headers=headers,
            json={"queue_id": role_id, "role_id": role_id},
        ).status_code == 201
    path = (
        f"{API_PREFIX}/projects/alpha/work-items/reliability-work/"
        "reliability/incidents"
    )
    payload = {
        "idempotency_key": "reliability-failure",
        "failure_category": "execution",
        "safe_summary": "Acceptance remains unhealthy",
        "source_ref": "evidence://reliability",
    }
    assert client.post(path, headers=_headers("viewer"), json=payload).status_code == 403
    assert client.post(path, headers=_headers("bravo"), json=payload).status_code == 403
    started = client.post(path, headers=headers, json=payload)
    assert started.status_code == 201
    assert started.json()["incident"]["next_stage"] == "technical"
    incident_id = started.json()["incident"]["incident_id"]
    viewed = client.get(
        f"{API_PREFIX}/projects/alpha/work-items/reliability-work/reliability",
        headers=_headers("viewer"),
    )
    assert viewed.status_code == 200
    assert viewed.json()["incident"]["incident_id"] == incident_id
    invalid_recovery = client.post(
        f"{path}/{incident_id}/attempts",
        headers=headers,
        json={
            "attempt_id": "bad-recovery",
            "stage": "recovery",
            "attempt_number": 2,
            "outcome": "failed",
        },
    )
    assert invalid_recovery.status_code == 422
    assert invalid_recovery.json()["errors"][0]["location"] == ["body"]
    invalid_instruction = client.post(
        f"{path}/{incident_id}/attempts",
        headers=headers,
        json={
            "attempt_id": "bad-instruction",
            "stage": "technical",
            "attempt_number": 1,
            "outcome": "failed",
            "correction_instruction": "not valid for technical retry",
        },
    )
    assert invalid_instruction.status_code == 422
    assert "only valid for PM correction" in invalid_instruction.json()["errors"][0]["message"]
    early = client.post(
        f"{API_PREFIX}/projects/alpha/work-items/reliability-work/transitions",
        headers=headers,
        json={
            "target_status": "error",
            "expected_version": 2,
            "correlation_id": "early-terminal",
            "reason": "too early",
        },
    )
    assert early.status_code == 409
    assert "exhausted retry" in early.json()["detail"]


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


def test_authenticated_route_is_idempotent_capability_bound_and_isolated(
    api_database,
) -> None:
    _database_url, client = api_database
    headers = _headers("alpha")
    client.post(
        f"{API_PREFIX}/projects/alpha/work-items",
        headers=headers,
        json={
            "work_item_id": "route-work",
            "title": "Route work",
            "owner_role_id": "engineering",
            "correlation_id": "route-correlation",
        },
    )
    queue = client.post(
        f"{API_PREFIX}/projects/alpha/queues",
        headers=headers,
        json={
            "queue_id": "engineering-browser",
            "role_id": "engineering",
            "capability": "browser",
        },
    )
    route = {
        "work_item_id": "route-work",
        "target_role_id": "engineering",
        "capability": "browser",
        "idempotency_key": "route-once",
        "priority": 25,
        "payload": {"action": "test"},
    }
    first = client.post(
        f"{API_PREFIX}/projects/alpha/routes", headers=headers, json=route
    )
    duplicate = client.post(
        f"{API_PREFIX}/projects/alpha/routes", headers=headers, json=route
    )
    missing = client.post(
        f"{API_PREFIX}/projects/alpha/routes",
        headers=headers,
        json={**route, "idempotency_key": "route-missing", "capability": "gpu"},
    )
    forbidden = client.post(
        f"{API_PREFIX}/projects/alpha/routes",
        headers=_headers("viewer"),
        json={**route, "idempotency_key": "route-forbidden"},
    )
    invalid = client.post(
        f"{API_PREFIX}/projects/alpha/routes",
        headers=headers,
        json={**route, "idempotency_key": "invalid route"},
    )

    assert queue.status_code == 201
    assert first.status_code == duplicate.status_code == 201
    assert first.json() == duplicate.json()
    assert first.json()["queue_id"] == "engineering-browser"
    assert first.json()["priority"] == 25
    assert missing.status_code == 404
    assert forbidden.status_code == 403
    assert invalid.status_code == 422
    assert invalid.json()["errors"][0]["location"][-1] == "idempotency_key"


def test_authenticated_handoff_requires_target_claim_and_acceptance(
    api_database,
) -> None:
    database_url, client = api_database
    headers = _headers("alpha")
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'qa', 'qa')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('alpha', 'qa-1', 'qa', 'running')
            """
        )
    assert client.post(
        f"{API_PREFIX}/projects/alpha/work-items",
        headers=headers,
        json={
            "work_item_id": "handoff-work",
            "title": "Handoff work",
            "owner_role_id": "engineering",
            "correlation_id": "handoff-correlation",
        },
    ).status_code == 201
    for queue_id, role_id in (("engineering-handoff", "engineering"), ("qa", "qa")):
        assert client.post(
            f"{API_PREFIX}/projects/alpha/queues",
            headers=headers,
            json={"queue_id": queue_id, "role_id": role_id},
        ).status_code == 201
    assert client.post(
        f"{API_PREFIX}/projects/alpha/queues/engineering-handoff/items",
        headers=headers,
        json={
            "queue_item_id": "handoff-source",
            "work_item_id": "handoff-work",
            "idempotency_key": "handoff-source",
        },
    ).status_code == 201
    source = client.post(
        f"{API_PREFIX}/projects/alpha/queues/engineering-handoff/claim",
        headers=headers,
        json={"owner_instance_id": "eng-1", "lease_seconds": 3600},
    ).json()
    offer_payload = {
        "source_lease_id": source["lease_id"],
        "source_lease_token": source["lease_token"],
        "target_role_id": "qa",
        "idempotency_key": "api-handoff",
        "summary": "Verify this work.",
        "payload": {"evidence": ["test://api"]},
    }
    offered = client.post(
        f"{API_PREFIX}/projects/alpha/handoffs",
        headers=headers,
        json=offer_payload,
    )
    repeated = client.post(
        f"{API_PREFIX}/projects/alpha/handoffs",
        headers=headers,
        json=offer_payload,
    )
    body = offered.json()
    assert offered.status_code == repeated.status_code == 201
    assert body == repeated.json()
    assert "lease_token" not in body
    assert client.get(
        f"{API_PREFIX}/projects/alpha/handoffs/{body['handoff_id']}",
        headers=_headers("viewer"),
    ).status_code == 200
    assert client.post(
        f"{API_PREFIX}/projects/alpha/leases/{source['lease_id']}/complete",
        headers=headers,
        json={"lease_token": source["lease_token"]},
    ).status_code == 409

    target = client.post(
        f"{API_PREFIX}/projects/alpha/queues/qa/claim",
        headers=headers,
        json={"owner_instance_id": "qa-1", "lease_seconds": 3600},
    ).json()
    action = {"lease_id": target["lease_id"], "lease_token": target["lease_token"]}
    claimed = client.post(
        f"{API_PREFIX}/projects/alpha/handoffs/{body['handoff_id']}/claim",
        headers=headers,
        json=action,
    )
    accepted = client.post(
        f"{API_PREFIX}/projects/alpha/handoffs/{body['handoff_id']}/accept",
        headers=headers,
        json=action,
    )
    foreign = client.get(
        f"{API_PREFIX}/projects/bravo/handoffs/{body['handoff_id']}",
        headers=_headers("bravo"),
    )
    forbidden = client.post(
        f"{API_PREFIX}/projects/alpha/handoffs",
        headers=_headers("viewer"),
        json={**offer_payload, "idempotency_key": "forbidden-handoff"},
    )

    assert claimed.json()["status"] == "claimed"
    assert accepted.json()["status"] == "accepted"
    assert foreign.status_code == 404
    assert forbidden.status_code == 403
    assert client.post(
        f"{API_PREFIX}/projects/alpha/leases/{source['lease_id']}/complete",
        headers=headers,
        json={"lease_token": source["lease_token"]},
    ).json()["status"] == "completed"


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
