from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
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
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.document_store import (
    DocumentConflict,
    DocumentContent,
    DocumentMetadata,
    DocumentNotFound,
    DocumentPage,
)
from agentic_mesh_v5.flow_definition import validate_flow


TOKENS = {
    "operator": "qualification-operator-token",
    "project-manager": "qualification-project-manager-token",
    "engineering": "qualification-engineering-token",
    "qa": "qualification-qa-token",
    "release-manager": "qualification-release-manager-token",
    "sponsor-1": "qualification-sponsor-token",
}


class _Documents:
    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str, str]] = {}

    def list(self, path="", *, cursor=None, page_size=200):
        del path, cursor, page_size
        return DocumentPage(tuple(self.stat(path) for path in self._items), None)

    def stat(self, path: str) -> DocumentMetadata:
        try:
            content, content_type, etag = self._items[path]
        except KeyError:
            raise DocumentNotFound("document was not found") from None
        return DocumentMetadata(
            item_id=hashlib.sha256(path.encode()).hexdigest(),
            name=path.rsplit("/", 1)[-1],
            path=path,
            size=len(content),
            etag=etag,
            is_folder=False,
            mime_type=content_type,
            created_at=None,
            modified_at=None,
        )

    def read(self, path: str) -> DocumentContent:
        return DocumentContent(self.stat(path), self._items[path][0])

    def create(self, path: str, content: bytes, *, content_type: str):
        if path in self._items:
            raise DocumentConflict("document already exists")
        etag = f'"{hashlib.sha256(content).hexdigest()}"'
        self._items[path] = (bytes(content), content_type, etag)
        return self.stat(path)

    def update(self, path, content, *, content_type, expected_etag):
        if self.stat(path).etag != expected_etag:
            raise DocumentConflict("document eTag is stale")
        etag = f'"{hashlib.sha256(content).hexdigest()}"'
        self._items[path] = (bytes(content), content_type, etag)
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


def _record(subject: str, token: str, projects: list[str]):
    return {
        "subject": subject,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "projects": projects,
        "scopes": ["project:create", "read", "write"],
    }


def _authorizer() -> TokenAuthorizer:
    return TokenAuthorizer(
        [
            _record("operator", TOKENS["operator"], ["*"]),
            *[
                _record(role, TOKENS[role], ["qualification"])
                for role in (
                    "project-manager",
                    "engineering",
                    "qa",
                    "release-manager",
                    "sponsor-1",
                )
            ],
        ]
    )


def _flow():
    value = {
        "schema_version": 1,
        "flow_id": "kernel-qualification",
        "leader_role": "project-manager",
        "entry_state": "product-definition",
        "terminal_states": ["release"],
        "states": {
            "product-definition": {
                "owner_role": "project-manager",
                "purpose": "Define and approve the small product change.",
                "artifact": "projects/{project_id}/work/{work_item_id}/product.md",
                "consults": [{"id": "feasibility", "role": "engineering"}],
                "gates": [
                    {
                        "id": "product-signoff",
                        "type": "sponsor_approval",
                        "requested_from": "sponsor",
                    }
                ],
                "informs": [],
                "routes": [
                    {
                        "id": "build",
                        "outcome": "completed",
                        "target_state": "development",
                        "target_role": "engineering",
                    }
                ],
                "terminal": False,
            },
            "development": {
                "owner_role": "engineering",
                "purpose": "Implement the approved small change.",
                "artifact": "projects/{project_id}/work/{work_item_id}/development.md",
                "consults": [{"id": "test-review", "role": "qa"}],
                "gates": [],
                "informs": [],
                "routes": [
                    {
                        "id": "verify",
                        "outcome": "completed",
                        "target_state": "verification",
                        "target_role": "qa",
                    }
                ],
                "terminal": False,
            },
            "verification": {
                "owner_role": "qa",
                "purpose": "Verify the implementation and release evidence.",
                "artifact": "projects/{project_id}/work/{work_item_id}/qa.md",
                "consults": [],
                "gates": [
                    {
                        "id": "release-readiness",
                        "reviewer_role": "release-manager",
                    }
                ],
                "informs": [],
                "routes": [
                    {
                        "id": "release",
                        "outcome": "completed",
                        "target_state": "release",
                        "target_role": "release-manager",
                    }
                ],
                "terminal": False,
            },
            "release": {
                "owner_role": "release-manager",
                "purpose": "Record the exact released revision.",
                "artifact": "projects/{project_id}/work/{work_item_id}/release.md",
                "consults": [],
                "gates": [],
                "informs": [],
                "routes": [],
                "terminal": True,
            },
        },
    }
    digest = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return validate_flow(value, digest=digest)


def _headers(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[role]}"}


def _post(client: TestClient, role: str, path: str, body: dict, status=200):
    response = client.post(path, headers=_headers(role), json=body)
    assert response.status_code == status, response.text
    return response.json()


def _claim(client: TestClient, role: str, instance: str, seconds=3600):
    return _post(
        client,
        role,
        f"{API_PREFIX}/projects/qualification/queues/{role}/claim",
        {"owner_instance_id": instance, "lease_seconds": seconds},
    )


def _complete_lease(client: TestClient, role: str, lease: dict):
    return _post(
        client,
        role,
        f"{API_PREFIX}/projects/qualification/leases/{lease['lease_id']}/complete",
        {"lease_token": lease["lease_token"]},
    )


def _verify_document(
    client: TestClient,
    documents: _Documents,
    role: str,
    state: str,
    content: str,
):
    path = f"projects/qualification/work/kernel-proof/{state}.md"
    documents.create(path, content.encode(), content_type="text/markdown")
    return _post(
        client,
        role,
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow/artifact",
        {"path": path, "record_id": f"{state}-artifact"},
    )


def _progress(client: TestClient, role: str, instance: str, sequence: int, state: str):
    return _post(
        client,
        role,
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/progress",
        {
            "checkpoint_id": f"{state}-complete",
            "role_instance_id": instance,
            "expected_previous_sequence": sequence,
            "status": "progressing" if state != "release" else "completed",
            "goal": "Qualify the autonomous kernel.",
            "step": state,
            "completed_action": f"Verified {state} evidence.",
            "next_action": "Continue the pinned flow.",
            "safe_summary": f"{state} evidence is complete.",
        },
        status=201,
    )


def _prepare_handoff(
    client: TestClient, source_role: str, source: dict, flow_version: int
):
    run = _post(
        client,
        source_role,
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/"
        "flow/transitions/prepare",
        {
            "outcome": "completed",
            "source_lease_id": source["lease_id"],
            "source_lease_token": source["lease_token"],
            "expected_version": flow_version,
            "operation_id": f"prepare-{source_role}-{flow_version}",
        },
    )
    assert run["status"] == "handoff_pending"
    return run


def _accept_and_pickup(
    client: TestClient,
    source_role: str,
    source: dict,
    target_role: str,
    target_instance: str,
    run: dict,
    *,
    lease_seconds=3600,
):
    target = _claim(client, target_role, target_instance, lease_seconds)
    action = {"lease_id": target["lease_id"], "lease_token": target["lease_token"]}
    handoff_path = (
        f"{API_PREFIX}/projects/qualification/handoffs/{run['pending_handoff_id']}"
    )
    claimed = _post(client, target_role, f"{handoff_path}/claim", action)
    assert claimed["status"] == "claimed"
    accepted = _post(client, target_role, f"{handoff_path}/accept", action)
    assert accepted["status"] == "accepted"
    _complete_lease(client, source_role, source)
    picked_up = _post(
        client,
        target_role,
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/"
        "flow/transitions/pickup",
        {
            "expected_version": run["version"],
            "operation_id": f"pickup-{target_role}-{run['version']}",
        },
    )
    assert picked_up["owner_role_id"] == target_role
    return target, picked_up


def _git_evidence(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Kernel QA"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "kernel@example.invalid"],
        check=True,
    )
    (root / "feature.txt").write_text("small verified feature\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "feature.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "Build small feature"],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_small_sdlc_project_completes_through_api_and_cli_with_restart_recovery(
    postgres_database: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    assert MigrationRunner(postgres_database).migrate().current_version == 24
    flow = _flow()
    documents = _Documents()

    def app():
        return create_app(
            postgres_database,
            authorizer=_authorizer(),
            flow_resolver=lambda project_id: flow
            if project_id == "qualification"
            else None,
            document_store_resolver=lambda project_id: documents
            if project_id == "qualification"
            else None,
        )

    client = TestClient(app())
    _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects",
        {
            "project_id": "qualification",
            "display_name": "Kernel Qualification",
            "sponsor_ids": ["sponsor-1"],
        },
        status=201,
    )
    roles = ("project-manager", "engineering", "qa", "release-manager")
    with psycopg.connect(postgres_database) as connection:
        connection.cursor().executemany(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('qualification', %s, %s)
            """,
            [(role, role) for role in roles],
        )
        connection.cursor().executemany(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('qualification', %s, %s, 'running')
            """,
            [
                (f"qualification.{role}.{number}", role)
                for role in roles
                for number in (1, 2)
            ],
        )
    for role in roles:
        _post(
            client,
            "project-manager",
            f"{API_PREFIX}/projects/qualification/queues",
            {"queue_id": role, "role_id": role},
            status=201,
        )
    _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects/qualification/work-items",
        {
            "work_item_id": "kernel-proof",
            "title": "Build one small verified feature",
            "owner_role_id": "project-manager",
            "correlation_id": "create-kernel-proof",
        },
        status=201,
    )
    _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/transitions",
        {
            "target_status": "active",
            "expected_version": 1,
            "correlation_id": "activate-kernel-proof",
        },
    )
    _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects/qualification/routes",
        {
            "work_item_id": "kernel-proof",
            "target_role_id": "project-manager",
            "idempotency_key": "kernel-proof-start",
        },
        status=201,
    )
    source = _claim(client, "project-manager", "qualification.project-manager.1", 1)
    _post(
        client,
        "operator",
        f"{API_PREFIX}/pm-monitor/claim",
        {"owner_id": "qualification-monitor-1", "lease_seconds": 1},
        status=201,
    )
    spoofed_role = client.post(
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow",
        headers=_headers("project-manager"),
        json={
            "operation_id": "spoofed-start",
            "actor_role_id": "engineering",
        },
    )
    assert spoofed_role.status_code == 422
    started = _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow",
        {"operation_id": "start-kernel-proof"},
        status=201,
    )
    assert started["flow_digest"] == flow.digest

    time.sleep(1.1)
    client = TestClient(app())
    source = _claim(client, "project-manager", "qualification.project-manager.2")
    monitor = _post(
        client,
        "operator",
        f"{API_PREFIX}/pm-monitor/claim",
        {"owner_id": "qualification-monitor-2", "lease_seconds": 60},
        status=201,
    )
    sweep = _post(
        client,
        "operator",
        f"{API_PREFIX}/pm-monitor/sweep",
        {"owner_id": monitor["owner_id"], "lease_token": monitor["lease_token"]},
    )
    assert sweep["observations"][0]["disposition"] == "progressing"

    _verify_document(
        client,
        documents,
        "project-manager",
        "product",
        "# Product\n\nA deliberately small feature with explicit non-goals.",
    )
    _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow/dispatch",
        {"kind": "consult", "obligation_id": "feasibility"},
    )
    consultation = _claim(client, "engineering", "qualification.engineering.1")
    _post(
        client,
        "engineering",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/"
        "flow/consultations",
        {
            "obligation_id": "feasibility",
            "decision": "responded",
            "record_id": "product-feasibility",
            "evidence": {"assessment": "small and feasible"},
        },
    )
    _complete_lease(client, "engineering", consultation)
    opened = _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/gates",
        {
            "gate_id": "product-signoff-round-1",
            "gate_type": "sponsor_approval",
            "flow_obligation_id": "product-signoff",
            "sponsor_ids": ["sponsor-1"],
            "correlation_id": "open-product-signoff",
            "expected_version": 2,
            "evidence": {"summary": "Approve the small product definition."},
        },
        status=201,
    )
    (tmp_path / "sponsor.token").write_text(TOKENS["sponsor-1"], encoding="ascii")
    cli_client = ControlApiClient(
        base_url="https://mesh.example",
        token_file=tmp_path / "sponsor.token",
        transport=_TestClientTransport(app()),
    )
    monkeypatch.setattr(
        ControlApiClient, "from_environment", classmethod(lambda cls: cli_client)
    )
    assert (
        cli_main(
            [
                "--json",
                "sponsor-decision",
                "--project",
                "qualification",
                "--gate",
                opened["gate_id"],
                "--decision",
                "approve",
                "--rationale",
                "Small scope approved.",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"]["decision"] == "approved"
    sponsor_continuation = _claim(
        client, "project-manager", "qualification.project-manager.1"
    )
    _complete_lease(client, "project-manager", sponsor_continuation)
    _progress(
        client,
        "project-manager",
        "qualification.project-manager.2",
        0,
        "product",
    )

    pending = _prepare_handoff(client, "project-manager", source, started["version"])
    engineering, run = _accept_and_pickup(
        client,
        "project-manager",
        source,
        "engineering",
        "qualification.engineering.1",
        pending,
    )
    commit = _git_evidence(tmp_path / "project-repo")
    _verify_document(
        client,
        documents,
        "engineering",
        "development",
        f"# Development\n\nImplemented and committed as `{commit}`.",
    )
    _post(
        client,
        "engineering",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow/dispatch",
        {"kind": "consult", "obligation_id": "test-review"},
    )
    test_review = _claim(client, "qa", "qualification.qa.1")
    _post(
        client,
        "qa",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/"
        "flow/consultations",
        {
            "obligation_id": "test-review",
            "decision": "responded",
            "record_id": "development-test-review",
            "evidence": {"plan": "automated acceptance and recovery checks"},
        },
    )
    _complete_lease(client, "qa", test_review)
    _progress(
        client,
        "engineering",
        "qualification.engineering.1",
        1,
        "development",
    )

    pending = _prepare_handoff(client, "engineering", engineering, run["version"])
    qa_expired = _claim(client, "qa", "qualification.qa.1", 1)
    action = {
        "lease_id": qa_expired["lease_id"],
        "lease_token": qa_expired["lease_token"],
    }
    handoff_path = (
        f"{API_PREFIX}/projects/qualification/handoffs/{pending['pending_handoff_id']}"
    )
    _post(client, "qa", f"{handoff_path}/claim", action)
    time.sleep(1.1)
    client = TestClient(app())
    qa = _claim(client, "qa", "qualification.qa.2")
    replacement = {"lease_id": qa["lease_id"], "lease_token": qa["lease_token"]}
    recovered = _post(client, "qa", f"{handoff_path}/claim", replacement)
    assert recovered["target_instance_id"] == "qualification.qa.2"
    assert _post(client, "qa", f"{handoff_path}/accept", replacement)["status"] == "accepted"
    _complete_lease(client, "engineering", engineering)
    run = _post(
        client,
        "qa",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/"
        "flow/transitions/pickup",
        {
            "expected_version": pending["version"],
            "operation_id": "pickup-qa-recovered",
        },
    )

    _verify_document(
        client,
        documents,
        "qa",
        "qa",
        "# QA\n\nAutomated flow, restart, handoff, and evidence checks passed.",
    )
    _post(
        client,
        "release-manager",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow/gates",
        {
            "obligation_id": "release-readiness",
            "decision": "approved",
            "record_id": "qa-release-readiness",
            "evidence": {"tests": "passed"},
        },
    )
    _progress(client, "qa", "qualification.qa.2", 2, "qa")

    pending = _prepare_handoff(client, "qa", qa, run["version"])
    release, run = _accept_and_pickup(
        client,
        "qa",
        qa,
        "release-manager",
        "qualification.release-manager.1",
        pending,
    )
    _verify_document(
        client,
        documents,
        "release-manager",
        "release",
        f"# Release\n\nQualified immutable commit `{commit}`.",
    )
    _progress(
        client,
        "release-manager",
        "qualification.release-manager.1",
        3,
        "release",
    )
    completion_evidence = {
        "git_commit": commit,
        "tests": "passed",
        "release": "qualified",
        "documents": ["product", "development", "qa", "release"],
    }
    completed = _post(
        client,
        "release-manager",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow/complete",
        {
            "expected_version": run["version"],
            "operation_id": "complete-kernel-proof",
            "evidence": completion_evidence,
        },
    )
    assert completed["status"] == "completed"
    _complete_lease(client, "release-manager", release)

    work = client.get(
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof",
        headers=_headers("project-manager"),
    ).json()
    obligations = client.get(
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/"
        "flow/obligations?current_only=false",
        headers=_headers("project-manager"),
    ).json()
    handoffs = client.get(
        f"{API_PREFIX}/projects/qualification/handoffs",
        headers=_headers("project-manager"),
    ).json()["records"]
    governance = client.get(
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow/governance",
        headers=_headers("project-manager"),
    ).json()
    progress = client.get(
        f"{API_PREFIX}/projects/qualification/progress",
        headers=_headers("project-manager"),
    ).json()["records"]
    assert work["status"] == "completed"
    assert work["terminal_evidence"] == completion_evidence
    assert len(obligations) == 8
    assert all(
        item["status"] == ("dispatched" if item["kind"] == "consult" else "satisfied")
        for item in obligations
    )
    assert len(handoffs) == 3 and {item["status"] for item in handoffs} == {
        "accepted"
    }
    assert len(governance) == 8
    assert len(progress) == 4
    assert len(documents._items) == 4
    subprocess.run(
        ["git", "-C", str(tmp_path / "project-repo"), "cat-file", "-e", commit],
        check=True,
    )
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE project_id = 'qualification' AND work_item_id = 'kernel-proof'
              AND status IN ('ready', 'leased')
            """
        ).fetchone()[0] == 0


def test_flow_and_document_adapters_fail_closed(postgres_database: str) -> None:
    MigrationRunner(postgres_database).migrate()
    client = TestClient(create_app(postgres_database, authorizer=_authorizer()))
    _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects",
        {
            "project_id": "qualification",
            "display_name": "Kernel Qualification",
            "sponsor_ids": ["sponsor-1"],
        },
        status=201,
    )
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('qualification', 'project-manager', 'project-manager')
            """
        )
    _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects/qualification/work-items",
        {
            "work_item_id": "kernel-proof",
            "title": "Kernel proof",
            "owner_role_id": "project-manager",
            "correlation_id": "create-kernel-proof",
        },
        status=201,
    )
    _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/transitions",
        {
            "target_status": "active",
            "expected_version": 1,
            "correlation_id": "activate-kernel-proof",
        },
    )
    unavailable = client.post(
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow",
        headers=_headers("project-manager"),
        json={"operation_id": "start-kernel-proof"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["type"].endswith("flow_configuration_unavailable")
    client = TestClient(
        create_app(
            postgres_database,
            authorizer=_authorizer(),
            flow_resolver=lambda _project_id: _flow(),
        )
    )
    _post(
        client,
        "project-manager",
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow",
        {"operation_id": "start-kernel-proof"},
        status=201,
    )
    missing_documents = client.post(
        f"{API_PREFIX}/projects/qualification/work-items/kernel-proof/flow/artifact",
        headers=_headers("project-manager"),
        json={
            "path": "projects/qualification/work/kernel-proof/product.md",
            "record_id": "product-artifact",
        },
    )
    assert missing_documents.status_code == 503
    assert missing_documents.json()["type"].endswith("document_store_unavailable")
