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
import httpx
import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.api import API_PREFIX, create_app
from agentic_mesh_v5.api_auth import TokenAuthorizer
from agentic_mesh_v5.api_client import ControlApiClient
from agentic_mesh_v5.cli import main as cli_main
from agentic_mesh_v5.database import MigrationRunner, load_migrations
from agentic_mesh_v5.document_store import DocumentContent, DocumentMetadata, DocumentPage
from agentic_mesh_v5.flow_definition import validate_flow
from agentic_mesh_v5.flow_engine import FlowEngine, TransitionSource
from agentic_mesh_v5.governance import GovernanceStore
from agentic_mesh_v5.lifecycle import LifecycleAuthorizationError, LifecycleConflict
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.sponsor_approvals import SponsorApprovalCoordinator


class _Documents:
    def stat(self, path: str) -> DocumentMetadata:
        return DocumentMetadata(
            item_id=path,
            name=path.rsplit("/", 1)[-1],
            path=path,
            size=128,
            etag=f'"{path}:v1"',
            is_folder=False,
            mime_type="text/markdown",
            created_at=None,
            modified_at=None,
        )

    def list(self, path="", *, cursor=None, page_size=200):
        return DocumentPage((), None)

    def read(self, path):
        return DocumentContent(self.stat(path), b"")

    def create(self, path, content, *, content_type):
        return self.stat(path)

    def update(self, path, content, *, content_type, expected_etag):
        return self.stat(path)


class _TestClientTransport(httpx.BaseTransport):
    def __init__(self, app) -> None:
        self._client = TestClient(app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._client.request(
            request.method,
            request.url.path,
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for Postgres tests")
    database_name = f"mesh_v5_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
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


def _flow_value(gate_type: str = "sponsor_approval"):
    return {
        "schema_version": 1,
        "flow_id": "sponsor-flow",
        "leader_role": "project-manager",
        "entry_state": "product-definition",
        "terminal_states": ["release"],
        "states": {
            "product-definition": {
                "owner_role": "product-manager",
                "purpose": "Obtain product approval.",
                "artifact": "work-items/{work_item_id}/product.md",
                "consults": [],
                "gates": [
                    {
                        "id": "product-signoff",
                        "type": gate_type,
                        "requested_from": "sponsor",
                    }
                ],
                "informs": [],
                "routes": [
                    {
                        "id": "release-route",
                        "outcome": "completed",
                        "target_state": "release",
                        "target_role": "release-manager",
                    }
                ],
                "terminal": False,
            },
            "release": {
                "owner_role": "release-manager",
                "purpose": "Release the approved result.",
                "artifact": "work-items/{work_item_id}/release.md",
                "consults": [],
                "gates": [],
                "informs": [],
                "routes": [],
                "terminal": True,
            },
        },
    }


def test_migration_backfills_resolved_legacy_gate(postgres_database: str) -> None:
    migrations = load_migrations()
    MigrationRunner(postgres_database, migrations=migrations[:23]).migrate()
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('legacy', 'Legacy')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items(project_id, work_item_id, title)
            VALUES ('legacy', 'legacy-work', 'Legacy work')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.gates
                (project_id, gate_id, work_item_id, gate_type, status,
                 requested_by, resolved_at, correlation_id)
            VALUES ('legacy', 'legacy-gate', 'legacy-work', 'sponsor', 'approved',
                    'legacy-pm', clock_timestamp(), 'legacy-correlation')
            """
        )
    assert MigrationRunner(postgres_database).migrate().current_version == 26
    with psycopg.connect(postgres_database) as connection:
        row = connection.execute(
            """
            SELECT resolved_operation_id, length(resolved_request_digest)
            FROM agentic_mesh_v5.gates
            WHERE project_id = 'legacy' AND gate_id = 'legacy-gate'
            """
        ).fetchone()
    assert row == ("legacy-resolution:legacy-gate", 64)


def _bootstrap(
    database_url: str,
    *,
    open_gate: bool = True,
    gate_type: str = "sponsor_approval",
):
    assert MigrationRunner(database_url).migrate().current_version == 26
    lifecycle = LifecycleStore(database_url)
    lifecycle.create_project(
        project_id="alpha",
        display_name="Alpha",
        sponsor_ids=("sponsor-1", "sponsor-2"),
    )
    lifecycle.create_project(
        project_id="bravo",
        display_name="Bravo",
        sponsor_ids=("bravo-sponsor",),
    )
    roles = ("product-manager", "release-manager", "project-manager")
    with psycopg.connect(database_url) as connection:
        for role in roles:
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
                VALUES ('alpha', %s, %s)
                """,
                (role, role),
            )
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.role_instances
                    (project_id, instance_id, role_id, status)
                VALUES ('alpha', %s, %s, 'running')
                """,
                (f"{role}-1", role),
            )
    queues = RoleQueueStore(database_url)
    for role in roles:
        queues.create_queue(project_id="alpha", queue_id=role, role_id=role)
    lifecycle.create_work_item(
        project_id="alpha",
        work_item_id="work-1",
        title="Work 1",
        owner_role_id="product-manager",
        actor_id="project-manager",
        correlation_id="create-work",
    )
    lifecycle.transition_work_item(
        project_id="alpha",
        work_item_id="work-1",
        target_status="active",
        actor_id="project-manager",
        correlation_id="activate-work",
        expected_version=1,
    )
    queues.enqueue(
        project_id="alpha",
        queue_id="product-manager",
        queue_item_id="source-item",
        work_item_id="work-1",
        idempotency_key="source-item",
        payload={},
    )
    source = queues.claim(
        project_id="alpha",
        queue_id="product-manager",
        owner_instance_id="product-manager-1",
        lease_seconds=3600,
    )
    engine = FlowEngine(database_url)
    engine.start(
        project_id="alpha",
        work_item_id="work-1",
        flow=validate_flow(_flow_value(gate_type), digest="a" * 64),
        fields={},
        actor_id="project-manager",
        operation_id="start-flow",
    )
    path = "work-items/work-1/product.md"
    GovernanceStore(database_url, _Documents()).verify_artifact(
        project_id="alpha",
        work_item_id="work-1",
        path=path,
        actor_role_id="product-manager",
        record_id="product-artifact",
    )
    coordinator = SponsorApprovalCoordinator(database_url)
    gate = None
    if open_gate:
        gate = coordinator.open(
            project_id="alpha",
            work_item_id="work-1",
            gate_id="product-signoff",
            obligation_id="product-signoff",
            requested_by="product-manager",
            sponsor_ids=("sponsor-1", "sponsor-2"),
            correlation_id="open-product-signoff",
            expected_version=2,
            evidence={"summary": "Approve product definition."},
        )
    return lifecycle, queues, engine, source, coordinator, gate


def test_approval_satisfies_gate_and_routes_owner_once(postgres_database: str) -> None:
    lifecycle, _queues, engine, source, coordinator, gate = _bootstrap(
        postgres_database
    )
    assert gate.status == "pending"
    assert lifecycle.get_work_item("alpha", "work-1").status == "gated"
    approval = coordinator.decide(
        project_id="alpha",
        gate_id="product-signoff",
        sponsor_id="sponsor-1",
        decision="approved",
        rationale="Product scope is accepted.",
        evidence={"channel": "api"},
        operation_id="approve-product",
    )
    replay = coordinator.decide(
        project_id="alpha",
        gate_id="product-signoff",
        sponsor_id="sponsor-1",
        decision="approved",
        rationale="Product scope is accepted.",
        evidence={"channel": "api"},
        operation_id="approve-product-retry",
    )
    assert replay == approval
    assert lifecycle.get_work_item("alpha", "work-1").status == "active"
    obligations = engine.obligations("alpha", "work-1")
    assert next(item for item in obligations if item.kind == "gate").status == "satisfied"
    with psycopg.connect(postgres_database) as connection:
        continuation = connection.execute(
            """
            SELECT queue_id, count(*) FROM agentic_mesh_v5.queue_items
            WHERE project_id = 'alpha'
              AND idempotency_key = 'sponsor-gate:product-signoff:approved'
            GROUP BY queue_id
            """
        ).fetchone()
        evidence = connection.execute(
            """
            SELECT decision, actor_role_id, document_path, document_etag
            FROM agentic_mesh_v5.governance_records
            WHERE project_id = 'alpha' AND record_id = 'sponsor:product-signoff'
            """
        ).fetchone()
        events = connection.execute(
            """
            SELECT event_type, count(*) FROM agentic_mesh_v5.events
            WHERE project_id = 'alpha' AND aggregate_type = 'sponsor-gate'
            GROUP BY event_type ORDER BY event_type
            """
        ).fetchall()
    assert continuation == ("product-manager", 1)
    assert evidence == (
        "approved",
        "sponsor-1",
        "work-items/work-1/product.md",
        '"work-items/work-1/product.md:v1"',
    )
    assert events == [("sponsor_gate.approved", 1), ("sponsor_gate.opened", 1)]
    pending = engine.prepare_transition(
        project_id="alpha",
        work_item_id="work-1",
        outcome="completed",
        fields={},
        source=TransitionSource(source.lease_id, source.lease_token),
        expected_version=1,
        actor_id="product-manager",
        operation_id="prepare-release",
    )
    assert pending.pending_target_state == "release"


def test_rejection_routes_pm_and_allows_a_new_approval_round(
    postgres_database: str,
) -> None:
    lifecycle, _queues, engine, _source, coordinator, _gate = _bootstrap(
        postgres_database
    )
    coordinator.decide(
        project_id="alpha",
        gate_id="product-signoff",
        sponsor_id="sponsor-2",
        decision="rejected",
        rationale="Clarify the non-goals.",
        evidence={},
        operation_id="reject-product",
    )
    assert lifecycle.get_work_item("alpha", "work-1").status == "active"
    assert next(
        item for item in engine.obligations("alpha", "work-1") if item.kind == "gate"
    ).status == "pending"
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            """
            SELECT queue_id, count(*) FROM agentic_mesh_v5.queue_items
            WHERE idempotency_key = 'sponsor-gate:product-signoff:rejected'
            GROUP BY queue_id
            """
        ).fetchone() == ("project-manager", 1)
    revised = coordinator.open(
        project_id="alpha",
        work_item_id="work-1",
        gate_id="product-signoff-round-2",
        obligation_id="product-signoff",
        requested_by="project-manager",
        sponsor_ids=("sponsor-1",),
        correlation_id="open-product-signoff-round-2",
        expected_version=4,
        evidence={"changes": "Non-goals clarified."},
    )
    assert revised.status == "pending"
    assert revised.flow_obligation_id == "product-signoff"


def test_timeout_routes_pm_once_and_unauthorized_sponsor_is_rejected(
    postgres_database: str,
) -> None:
    lifecycle, _queues, engine, _source, coordinator, gate = _bootstrap(
        postgres_database
    )
    with pytest.raises(LifecycleAuthorizationError, match="not authorized"):
        coordinator.decide(
            project_id="alpha",
            gate_id="product-signoff",
            sponsor_id="bravo-sponsor",
            decision="approved",
            rationale="Cross-project attempt.",
            evidence={},
            operation_id="foreign-decision",
        )
    observed = datetime.now(timezone.utc) + timedelta(days=3)
    timed_out = coordinator.timeout(
        project_id="alpha",
        gate_id="product-signoff",
        actor_id="project-manager",
        operation_id="timeout-product",
        observed_at=observed,
    )
    replay = coordinator.timeout(
        project_id="alpha",
        gate_id="product-signoff",
        actor_id="project-manager",
        operation_id="timeout-product-retry",
        observed_at=observed + timedelta(minutes=1),
    )
    assert replay == timed_out
    assert timed_out.status == "timed_out"
    assert lifecycle.get_work_item("alpha", "work-1").status == "active"
    assert next(
        item for item in engine.obligations("alpha", "work-1") if item.kind == "gate"
    ).status == "pending"
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            """
            SELECT queue_id, count(*) FROM agentic_mesh_v5.queue_items
            WHERE idempotency_key = 'sponsor-gate:product-signoff:timed_out'
            GROUP BY queue_id
            """
        ).fetchone() == ("project-manager", 1)


def test_concurrent_sponsor_decisions_commit_one_outcome(postgres_database: str) -> None:
    _lifecycle, _queues, _engine, _source, coordinator, _gate = _bootstrap(
        postgres_database
    )

    def decide(sponsor_id: str, decision: str):
        try:
            return coordinator.decide(
                project_id="alpha",
                gate_id="product-signoff",
                sponsor_id=sponsor_id,
                decision=decision,
                rationale=f"{decision} rationale",
                evidence={},
                operation_id=f"{decision}-{sponsor_id}",
            ).decision
        except LifecycleConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda values: decide(*values),
                (("sponsor-1", "approved"), ("sponsor-2", "rejected")),
            )
        )
    assert outcomes.count("conflict") == 1
    assert len(set(outcomes) - {"conflict"}) == 1
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE idempotency_key LIKE 'sponsor-gate:product-signoff:%'
            """
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.events
            WHERE project_id = 'alpha' AND event_type = 'sponsor_gate.opened'
            """
        ).fetchone()[0] == 1


def test_concurrent_exact_gate_open_replays_one_request(postgres_database: str) -> None:
    _lifecycle, _queues, _engine, _source, coordinator, _gate = _bootstrap(
        postgres_database, open_gate=False
    )

    def open_gate():
        return coordinator.open(
            project_id="alpha",
            work_item_id="work-1",
            gate_id="product-signoff",
            obligation_id="product-signoff",
            requested_by="product-manager",
            sponsor_ids=("sponsor-1",),
            correlation_id="concurrent-open",
            expected_version=2,
            evidence={"summary": "Approve product definition."},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _index: open_gate(), range(2)))
    assert records[0] == records[1]
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.gates
            WHERE project_id = 'alpha' AND gate_id = 'product-signoff'
            """
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.events
            WHERE project_id = 'alpha' AND event_type = 'sponsor_gate.opened'
            """
        ).fetchone()[0] == 1


def test_non_finite_evidence_fails_before_open(postgres_database: str) -> None:
    lifecycle, _queues, _engine, _source, coordinator, _gate = _bootstrap(
        postgres_database, open_gate=False
    )
    with pytest.raises(ValueError, match="JSON values"):
        coordinator.open(
            project_id="alpha",
            work_item_id="work-1",
            gate_id="product-signoff",
            obligation_id="product-signoff",
            requested_by="product-manager",
            sponsor_ids=("sponsor-1",),
            correlation_id="invalid-evidence",
            expected_version=2,
            evidence={"score": float("nan")},
        )
    assert lifecycle.get_work_item("alpha", "work-1").status == "active"


@pytest.mark.parametrize("gate_type", ["sponsor_approval", "human_response"])
def test_authenticated_api_and_cli_complete_sponsor_gate(
    postgres_database: str, tmp_path: Path, monkeypatch, capsys, gate_type: str
) -> None:
    _lifecycle, _queues, _engine, _source, _coordinator, _gate = _bootstrap(
        postgres_database, open_gate=False, gate_type=gate_type
    )
    tokens = {
        "operator": "operator-token",
        "sponsor": "sponsor-token",
        "outsider": "outsider-token",
    }
    authorizer = TokenAuthorizer(
        [
            {
                "subject": subject,
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "projects": ["alpha"],
                "scopes": ["read", "write"],
            }
            for subject, token in (
                ("product-manager", tokens["operator"]),
                ("sponsor-1", tokens["sponsor"]),
                ("outsider", tokens["outsider"]),
            )
        ]
    )
    app = create_app(postgres_database, authorizer=authorizer)
    api = TestClient(app)
    unauthorized_open = api.post(
        f"{API_PREFIX}/projects/alpha/work-items/work-1/gates",
        headers={"Authorization": f"Bearer {tokens['outsider']}"},
        json={
            "gate_id": "product-signoff",
            "gate_type": gate_type,
            "flow_obligation_id": "product-signoff",
            "sponsor_ids": ["sponsor-1"],
            "correlation_id": "unauthorized-open",
            "expected_version": 2,
        },
    )
    assert unauthorized_open.status_code == 403
    opened = api.post(
        f"{API_PREFIX}/projects/alpha/work-items/work-1/gates",
        headers={"Authorization": f"Bearer {tokens['operator']}"},
        json={
            "gate_id": "product-signoff",
            "gate_type": gate_type,
            "flow_obligation_id": "product-signoff",
            "sponsor_ids": ["sponsor-1"],
            "correlation_id": "api-open-product",
            "expected_version": 2,
        },
    )
    assert opened.status_code == 201
    unauthorized = api.post(
        f"{API_PREFIX}/projects/alpha/gates/product-signoff/decision",
        headers={"Authorization": f"Bearer {tokens['outsider']}"},
        json={"decision": "approved", "rationale": "Unauthorized."},
    )
    assert unauthorized.status_code == 403

    token_file = tmp_path / "sponsor.token"
    token_file.write_text(tokens["sponsor"], encoding="ascii")
    client = ControlApiClient(
        base_url="https://mesh.example",
        token_file=token_file,
        transport=_TestClientTransport(app),
    )
    monkeypatch.setattr(
        ControlApiClient, "from_environment", classmethod(lambda cls: client)
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps({"source": "bootstrap-cli"}), encoding="utf-8")
    arguments = [
        "--json",
        "sponsor-decision",
        "--project",
        "alpha",
        "--gate",
        "product-signoff",
        "--decision",
        "approve",
        "--rationale",
        "Approved without a dashboard.",
        "--evidence-file",
        str(evidence_path),
    ]
    assert cli_main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli_main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["result"]["decision"] == "approved"
    assert second["result"] == first["result"]
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE idempotency_key = 'sponsor-gate:product-signoff:approved'
            """
        ).fetchone()[0] == 1
