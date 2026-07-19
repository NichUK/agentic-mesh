from __future__ import annotations

import json
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from agentic_mesh_v5.ado_adapter import AdoAuthenticationError
from agentic_mesh_v5.ado_adapter import AdoConflict
from agentic_mesh_v5.ado_adapter import AdoConfigurationError
from agentic_mesh_v5.ado_adapter import AdoForeignProject
from agentic_mesh_v5.ado_adapter import AdoInvalidResponse
from agentic_mesh_v5.ado_adapter import AdoNotFound
from agentic_mesh_v5.ado_adapter import AdoPermissionDenied
from agentic_mesh_v5.ado_adapter import AdoUnavailable
from agentic_mesh_v5.ado_adapter import ProjectAdoAdapter
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.document_store import HttpResponse


ORGANIZATION = "https://dev.azure.com/seerstone"
ADO_PROJECT = "alpha-ado"
TOKEN = "test-token-never-persist"


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


@pytest.fixture
def ado_project(postgres_database: str) -> str:
    assert MigrationRunner(postgres_database).migrate().current_version == 27
    _activate_manifest(postgres_database, digest="d" * 64)
    return postgres_database


def _snapshot(*, ado_project: str = ADO_PROJECT) -> dict[str, object]:
    return {
        "ado": {
            "organization": ORGANIZATION,
            "project": ado_project,
            "credential": "ado",
            "owner_project_id": "alpha",
        },
        "credentials": {
            "ado": {
                "scope": "project",
                "provider": "ado",
                "reference": "secret://projects/alpha/ado",
            }
        },
    }


def _activate_manifest(
    database_url: str,
    *,
    digest: str,
    ado_project: str = ADO_PROJECT,
    create_project: bool = True,
) -> None:
    with psycopg.connect(database_url) as connection:
        if create_project:
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
                VALUES ('alpha', 'Alpha')
                """
            )
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.roles
                    (project_id, role_id, template_id)
                VALUES ('alpha', 'project-manager', 'project-manager')
                """
            )
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.work_items
                    (project_id, work_item_id, assigned_role_id, title, status)
                VALUES ('alpha', 'mesh-1', 'project-manager', 'Mesh work', 'active')
                """
            )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_manifest_snapshots
                (project_id, manifest_digest, source_revision, source_path,
                 snapshot, registered_by)
            VALUES ('alpha', %s, %s, 'agentic-mesh/project.yaml', %s, 'pm')
            """,
            (digest, "a" * 40, Jsonb(_snapshot(ado_project=ado_project))),
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_manifest_active
                (project_id, manifest_digest, activated_by)
            VALUES ('alpha', %s, 'pm')
            ON CONFLICT (project_id) DO UPDATE
            SET manifest_digest = EXCLUDED.manifest_digest,
                activated_by = EXCLUDED.activated_by,
                activated_at = clock_timestamp()
            """,
            (digest,),
        )


class TokenProvider:
    def __init__(self, *, error: bool = False) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def access_token(self, *, provider: str, reference: str) -> str:
        self.calls.append((provider, reference))
        if self.error:
            raise RuntimeError("token unavailable")
        return TOKEN


class SimulatedCrash(BaseException):
    pass


class FakeTransport:
    def __init__(self, outcomes: list[HttpResponse | BaseException]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers,
        content: bytes | None = None,
        follow_redirects: bool = False,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "content": content,
                "follow_redirects": follow_redirects,
            }
        )
        if not self.outcomes:
            raise AssertionError("unexpected ADO request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(
    *,
    external_id: int = 101,
    revision: int = 1,
    project: str = ADO_PROJECT,
    status: str = "New",
    title: str = "ADO work",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    payload = {
        "id": external_id,
        "rev": revision,
        "fields": {
            "System.TeamProject": project,
            "System.Title": title,
            "System.State": status,
        },
    }
    return HttpResponse(
        status_code,
        headers or {},
        json.dumps(payload, separators=(",", ":")).encode(),
    )


def _adapter(
    database_url: str,
    outcomes: list[HttpResponse | BaseException],
    *,
    token_provider: TokenProvider | None = None,
) -> tuple[ProjectAdoAdapter, FakeTransport, TokenProvider]:
    transport = FakeTransport(outcomes)
    tokens = token_provider or TokenProvider()
    adapter = ProjectAdoAdapter(
        database_url,
        token_provider=tokens,
        transport=transport,
        sleeper=lambda _: None,
    )
    return adapter, transport, tokens


def _link(adapter: ProjectAdoAdapter) -> None:
    adapter.link(
        project_id="alpha",
        work_item_id="mesh-1",
        external_work_item_id=101,
        actor_id="project-manager",
    )


def test_link_and_read_use_only_the_active_project_binding_and_are_idempotent(
    ado_project: str,
) -> None:
    adapter, transport, tokens = _adapter(
        ado_project, [_response(), _response(), _response(revision=2)]
    )

    first = adapter.link(
        project_id="alpha",
        work_item_id="mesh-1",
        external_work_item_id=101,
        actor_id="project-manager",
    )
    second = adapter.link(
        project_id="alpha",
        work_item_id="mesh-1",
        external_work_item_id=101,
        actor_id="replacement-worker",
    )
    remote = adapter.read(project_id="alpha", work_item_id="mesh-1")

    assert first == second
    assert first.external_url == (
        "https://dev.azure.com/seerstone/alpha-ado/_workitems/edit/101"
    )
    assert remote.revision == 2
    assert all("/alpha-ado/_apis/wit/workitems/101" in call["url"] for call in transport.calls)
    assert all(call["headers"]["Authorization"] == f"Bearer {TOKEN}" for call in transport.calls)
    assert tokens.calls == [
        ("ado", "secret://projects/alpha/ado"),
        ("ado", "secret://projects/alpha/ado"),
        ("ado", "secret://projects/alpha/ado"),
    ]
    with psycopg.connect(ado_project) as connection:
        runtime = connection.execute(
            """
            SELECT status, version FROM agentic_mesh_v5.work_items
            WHERE project_id='alpha' AND work_item_id='mesh-1'
            """
        ).fetchone()
        encoded = json.dumps(
            connection.execute(
                """
                SELECT row_to_json(link) FROM agentic_mesh_v5.work_item_ado_links link
                WHERE project_id='alpha' AND work_item_id='mesh-1'
                """
            ).fetchone()[0]
        )
    assert runtime == ("active", 1)
    assert TOKEN not in encoded


def test_foreign_project_response_is_rejected_before_link_persistence(
    ado_project: str,
) -> None:
    adapter, _, _ = _adapter(ado_project, [_response(project="foreign")])

    with pytest.raises(AdoForeignProject, match="another project"):
        _link(adapter)
    with pytest.raises(AdoNotFound, match="no ADO link"):
        adapter.get_link(project_id="alpha", work_item_id="mesh-1")


def test_update_is_durable_idempotent_and_never_changes_runtime_state(
    ado_project: str,
) -> None:
    adapter, transport, _ = _adapter(
        ado_project,
        [
            _response(),
            _response(revision=4, status="New"),
            _response(revision=5, status="Active"),
        ],
    )
    _link(adapter)

    result = adapter.update(
        project_id="alpha",
        work_item_id="mesh-1",
        operation_id="sync-1",
        fields={"System.State": "Active"},
        actor_id="project-manager",
    )
    replay = adapter.update(
        project_id="alpha",
        work_item_id="mesh-1",
        operation_id="sync-1",
        fields={"System.State": "Active"},
        actor_id="replacement-worker",
    )

    assert result == replay
    assert result.status == "succeeded"
    assert result.external_revision == 5
    assert result.attempt_count == 2
    patch_calls = [item for item in transport.calls if item["method"] == "PATCH"]
    assert len(patch_calls) == 1
    patch = json.loads(patch_calls[0]["content"])
    assert patch == [
        {"op": "test", "path": "/rev", "value": 4},
        {"op": "add", "path": "/fields/System.State", "value": "Active"},
    ]
    with psycopg.connect(ado_project) as connection:
        assert connection.execute(
            """
            SELECT status, version FROM agentic_mesh_v5.work_items
            WHERE project_id='alpha' AND work_item_id='mesh-1'
            """
        ).fetchone() == ("active", 1)


def test_interrupted_patch_is_reconciled_without_a_duplicate_external_update(
    ado_project: str,
) -> None:
    adapter, transport, _ = _adapter(
        ado_project,
        [
            _response(),
            _response(revision=4, status="New"),
            SimulatedCrash(),
            _response(revision=5, status="Active"),
        ],
    )
    _link(adapter)

    with pytest.raises(SimulatedCrash):
        adapter.update(
            project_id="alpha",
            work_item_id="mesh-1",
            operation_id="sync-crash",
            fields={"System.State": "Active"},
            actor_id="project-manager",
        )
    assert adapter.get_operation(
        project_id="alpha", operation_id="sync-crash"
    ).status == "pending"

    resumed = adapter.update(
        project_id="alpha",
        work_item_id="mesh-1",
        operation_id="sync-crash",
        fields={"System.State": "Active"},
        actor_id="project-manager",
    )

    assert resumed.status == "succeeded"
    assert len([item for item in transport.calls if item["method"] == "PATCH"]) == 1


def test_outage_is_bounded_and_leaves_a_resumable_pending_operation(
    ado_project: str,
) -> None:
    adapter, transport, _ = _adapter(
        ado_project,
        [
            _response(),
            RuntimeError("offline"),
            HttpResponse(503, {}, b""),
            HttpResponse(429, {"retry-after": "0"}, b""),
            _response(revision=6, status="Active"),
        ],
    )
    _link(adapter)

    with pytest.raises(AdoUnavailable) as unavailable:
        adapter.update(
            project_id="alpha",
            work_item_id="mesh-1",
            operation_id="sync-outage",
            fields={"System.State": "Active"},
            actor_id="project-manager",
        )
    assert unavailable.value.attempts == 3
    pending = adapter.get_operation(
        project_id="alpha", operation_id="sync-outage"
    )
    assert pending.status == "pending"
    assert pending.attempt_count == 3
    assert pending.last_error == "ADO is unavailable"

    resumed = adapter.update(
        project_id="alpha",
        work_item_id="mesh-1",
        operation_id="sync-outage",
        fields={"System.State": "Active"},
        actor_id="project-manager",
    )
    assert resumed.status == "succeeded"
    assert resumed.attempt_count == 4
    assert len([item for item in transport.calls if item["method"] == "PATCH"]) == 0


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (HttpResponse(401, {}, b""), AdoAuthenticationError),
        (HttpResponse(403, {}, b""), AdoPermissionDenied),
        (HttpResponse(404, {}, b""), AdoNotFound),
        (HttpResponse(200, {}, b"not-json"), AdoInvalidResponse),
    ],
)
def test_link_classifies_remote_failures(
    ado_project: str, response: HttpResponse, error: type[Exception]
) -> None:
    adapter, _, _ = _adapter(ado_project, [response])
    with pytest.raises(error):
        _link(adapter)


def test_missing_credential_and_operation_conflict_fail_without_external_patch(
    ado_project: str,
) -> None:
    denied, transport, _ = _adapter(
        ado_project, [], token_provider=TokenProvider(error=True)
    )
    with pytest.raises(AdoAuthenticationError, match="credential"):
        _link(denied)
    assert len(transport.calls) == 0

    adapter, transport, _ = _adapter(
        ado_project,
        [_response(), RuntimeError(), RuntimeError(), RuntimeError()],
    )
    _link(adapter)
    with pytest.raises(AdoUnavailable):
        adapter.update(
            project_id="alpha",
            work_item_id="mesh-1",
            operation_id="sync-conflict",
            fields={"System.State": "Active"},
            actor_id="project-manager",
        )
    before = len(transport.calls)
    with pytest.raises(AdoConflict, match="another ADO update"):
        adapter.update(
            project_id="alpha",
            work_item_id="mesh-1",
            operation_id="sync-conflict",
            fields={"System.State": "Closed"},
            actor_id="project-manager",
        )
    assert len(transport.calls) == before


def test_update_rejects_foreign_project_and_revision_conflict(
    ado_project: str,
) -> None:
    foreign, foreign_transport, _ = _adapter(
        ado_project, [_response(), _response(project="foreign")]
    )
    _link(foreign)
    with pytest.raises(AdoForeignProject):
        foreign.update(
            project_id="alpha",
            work_item_id="mesh-1",
            operation_id="sync-foreign",
            fields={"System.State": "Active"},
            actor_id="project-manager",
        )
    assert len(
        [item for item in foreign_transport.calls if item["method"] == "PATCH"]
    ) == 0

    conflict, conflict_transport, _ = _adapter(
        ado_project,
        [_response(revision=7, status="New"), HttpResponse(412, {}, b"")],
    )
    with pytest.raises(AdoConflict, match="revision changed"):
        conflict.update(
            project_id="alpha",
            work_item_id="mesh-1",
            operation_id="sync-revision",
            fields={"System.State": "Active"},
            actor_id="project-manager",
        )
    assert len(
        [item for item in conflict_transport.calls if item["method"] == "PATCH"]
    ) == 1
    with psycopg.connect(ado_project) as connection:
        assert connection.execute(
            """
            SELECT status, version FROM agentic_mesh_v5.work_items
            WHERE project_id='alpha' AND work_item_id='mesh-1'
            """
        ).fetchone() == ("active", 1)


def test_manifest_rebinding_invalidates_an_existing_link(ado_project: str) -> None:
    adapter, transport, _ = _adapter(ado_project, [_response()])
    _link(adapter)
    _activate_manifest(
        ado_project,
        digest="e" * 64,
        ado_project="replacement",
        create_project=False,
    )

    with pytest.raises(
        AdoConfigurationError, match="does not match the active project manifest"
    ):
        adapter.read(project_id="alpha", work_item_id="mesh-1")
    assert len(transport.calls) == 1
