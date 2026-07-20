from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import uuid
from urllib.parse import parse_qs, urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest
import yaml

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.document_store import DocumentConflict
from agentic_mesh_v5.document_store import DocumentInvalidResponse
from agentic_mesh_v5.document_store import DocumentNotFound
from agentic_mesh_v5.document_store import DocumentPermissionDenied
from agentic_mesh_v5.document_store import DocumentStore
from agentic_mesh_v5.document_store import DocumentTooLarge
from agentic_mesh_v5.document_store import DocumentUnavailable
from agentic_mesh_v5.document_store import HttpResponse
from agentic_mesh_v5.document_store import MountedAccessTokenProvider
from agentic_mesh_v5.document_store import OneDriveDocumentStore
from agentic_mesh_v5.document_store import OneDriveDocumentStoreFactory
from agentic_mesh_v5.project_manifest import ProjectManifestStore
from agentic_mesh_v5.project_manifest import load_project_manifest


CHUNK = 10 * 1024 * 1024


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


class Tokens:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = (
            {"secret://projects/alpha/graph": "alpha-token"}
            if values is None
            else values
        )
        self.calls: list[tuple[str, str]] = []

    def access_token(self, *, provider: str, reference: str) -> str:
        self.calls.append((provider, reference))
        return self.values[reference]


class FailingTokens:
    def access_token(self, *, provider: str, reference: str) -> str:
        raise RuntimeError("provider accidentally included bearer-value")


def _mounted_token(
    root: Path, reference: str, value: bytes, *, provider: str = "graph"
) -> Path:
    scheme, relative = reference.split("://", 1)
    path = root / provider / scheme / Path(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def test_mounted_tokens_are_project_scoped_and_refresh_without_restart(
    tmp_path: Path,
) -> None:
    alpha_reference = "oauth-cache://projects/alpha/graph"
    beta_reference = "oauth-cache://projects/beta/graph"
    alpha = _mounted_token(tmp_path, alpha_reference, b"alpha-token-1")
    _mounted_token(tmp_path, beta_reference, b"beta-token")
    provider = MountedAccessTokenProvider(tmp_path)

    assert provider.access_token(
        provider="graph", reference=alpha_reference
    ) == "alpha-token-1"
    assert provider.access_token(
        provider="graph", reference=beta_reference
    ) == "beta-token"

    replacement = alpha.with_name("graph.next")
    replacement.write_bytes(b"alpha-token-2")
    replacement.replace(alpha)
    assert provider.access_token(
        provider="graph", reference=alpha_reference
    ) == "alpha-token-2"


@pytest.mark.parametrize(
    ("reference", "value"),
    (
        ("oauth-cache://projects/alpha/graph", b"token with whitespace"),
        ("oauth-cache://projects/alpha/graph", b"x" * (64 * 1024 + 1)),
        ("oauth-cache://projects/../outside", b"token"),
        ("oauth-cache://projects//alpha/graph", b"token"),
        ("oauth-cache://projects/missing/graph", None),
    ),
    ids=("whitespace", "oversized", "traversal", "empty-segment", "missing"),
)
def test_mounted_tokens_fail_closed_without_disclosing_credentials(
    tmp_path: Path, reference: str, value: bytes | None
) -> None:
    if value is not None and ".." not in reference:
        _mounted_token(tmp_path, reference, value)
    provider = MountedAccessTokenProvider(tmp_path)

    with pytest.raises(DocumentUnavailable) as error:
        provider.access_token(provider="graph", reference=reference)

    assert str(error.value) == "document credential resolution failed"
    assert "token" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_mounted_tokens_reject_a_link_that_escapes_the_credential_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mounted"
    root.mkdir()
    outside = tmp_path / "outside-token"
    outside.write_bytes(b"outside-token")
    link = root / "graph" / "oauth-cache" / "projects" / "alpha" / "graph"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("the test platform does not permit symbolic links")

    provider = MountedAccessTokenProvider(root)
    with pytest.raises(DocumentUnavailable):
        provider.access_token(
            provider="graph", reference="oauth-cache://projects/alpha/graph"
        )


class ScriptedTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        follow_redirects: bool = False,
    ) -> HttpResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "content": content,
                "follow_redirects": follow_redirects,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def _response(status: int, value: object, **headers: str) -> HttpResponse:
    return HttpResponse(
        status,
        {key.casefold(): item for key, item in headers.items()},
        json.dumps(value).encode("utf-8"),
    )


def _file(
    name: str = "plan.md",
    *,
    item_id: str = "item-1",
    size: int = 12,
    etag: str = '"etag-1"',
) -> dict[str, object]:
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "eTag": etag,
        "file": {"mimeType": "text/markdown"},
        "createdDateTime": "2026-07-18T10:00:00Z",
        "lastModifiedDateTime": "2026-07-18T11:00:00Z",
    }


def _store(
    transport: ScriptedTransport,
    *,
    tokens: Tokens | None = None,
    max_download: int = 100 * 1024 * 1024,
    max_upload: int = 250 * 1024 * 1024,
) -> OneDriveDocumentStore:
    return OneDriveDocumentStore(
        drive_id="drive-alpha",
        root="/projects/alpha",
        credential_provider="graph",
        credential_reference="secret://projects/alpha/graph",
        token_provider=tokens or Tokens(),
        transport=transport,
        max_download_bytes=max_download,
        max_upload_bytes=max_upload,
    )


def test_browse_metadata_and_opaque_paging_stay_inside_root() -> None:
    next_link = (
        "https://graph.microsoft.com/v1.0/drives/drive-alpha/root:/projects/alpha/"
        "plans:/children?$skiptoken=opaque%2Btoken"
    )
    transport = ScriptedTransport(
        _response(200, {"value": [_file()], "@odata.nextLink": next_link}),
        _response(
            200,
            {
                "value": [
                    {
                        "id": "folder-1",
                        "name": "archive",
                        "size": 0,
                        "eTag": '"folder-etag"',
                        "folder": {"childCount": 2},
                    }
                ]
            },
        ),
    )
    tokens = Tokens()
    store = _store(transport, tokens=tokens)
    assert isinstance(store, DocumentStore)
    first = store.list("plans", page_size=1)
    assert first.items[0].path == "plans/plan.md"
    assert first.items[0].mime_type == "text/markdown"
    assert first.next_cursor is not None
    second = store.list("plans", cursor=first.next_cursor, page_size=1)
    assert second.items[0].is_folder is True
    assert second.items[0].path == "plans/archive"
    assert second.next_cursor is None
    assert len(tokens.calls) == 2
    assert all(
        request["headers"]["Authorization"] == "Bearer alpha-token"
        for request in transport.requests
    )
    assert "/root:/projects/alpha/plans:/children" in transport.requests[0]["url"]
    query = parse_qs(urlsplit(transport.requests[1]["url"]).query)
    assert query["$skiptoken"] == ["opaque+token"]

    forged = base64.urlsafe_b64encode(
        b'{"path":"other","skip_token":"token"}'
    ).decode("ascii")
    with pytest.raises(ValueError, match="does not match"):
        store.list("plans", cursor=forged)
    for path in ("/absolute", "../outside", "plans//draft", "plans\\draft"):
        with pytest.raises(ValueError):
            store.list(path)
    assert len(transport.requests) == 2


def test_read_redirect_strips_token_and_enforces_size() -> None:
    transport = ScriptedTransport(
        _response(200, _file(size=7)),
        HttpResponse(
            302,
            {"Location": "https://content.example.invalid/download?id=1"},
            b"",
        ),
        HttpResponse(200, {"content-type": "text/markdown"}, b"content"),
    )
    store = _store(transport)
    result = store.read("plans/plan.md")
    assert result.content == b"content"
    assert result.metadata.etag == '"etag-1"'
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer alpha-token"
    assert transport.requests[1]["headers"]["Authorization"] == "Bearer alpha-token"
    assert transport.requests[1]["headers"]["If-Match"] == '"etag-1"'
    assert transport.requests[2]["headers"] == {}
    assert transport.requests[2]["follow_redirects"] is False

    too_large = ScriptedTransport(_response(200, _file(size=8)))
    with pytest.raises(DocumentTooLarge, match="download limit"):
        _store(too_large, max_download=7).read("plans/plan.md")
    assert len(too_large.requests) == 1

    folder = ScriptedTransport(
        _response(
            200,
            {
                "id": "folder-1",
                "name": "plans",
                "size": 0,
                "eTag": '"folder"',
                "folder": {"childCount": 1},
            },
        )
    )
    with pytest.raises(DocumentConflict, match="folders"):
        _store(folder).read("plans")

    ambiguous = ScriptedTransport(
        _response(200, _file(size=7)),
        HttpResponse(
            302,
            {
                "Location": "https://content.example.invalid/one",
                "location": "https://content.example.invalid/two",
            },
            b"",
        ),
    )
    with pytest.raises(DocumentInvalidResponse, match="ambiguous"):
        _store(ambiguous).read("plans/plan.md")


def test_create_large_document_uses_aligned_unauthenticated_ranges() -> None:
    content = b"a" * (CHUNK + 123)
    transport = ScriptedTransport(
        _response(200, {"uploadUrl": "https://upload.example.invalid/session-1"}),
        _response(202, {"nextExpectedRanges": [f"{CHUNK}-"]}),
        _response(201, _file("large.bin", size=len(content), item_id="large-1")),
    )
    result = _store(transport).create(
        "artifacts/large.bin", content, content_type="application/octet-stream"
    )
    assert result.item_id == "large-1"
    assert result.path == "artifacts/large.bin"
    session, first, final = transport.requests
    assert session["method"] == "POST"
    assert session["headers"]["Authorization"] == "Bearer alpha-token"
    session_body = json.loads(session["content"])
    assert session_body["item"]["@microsoft.graph.conflictBehavior"] == "fail"
    assert first["headers"]["Content-Range"] == f"bytes 0-{CHUNK - 1}/{len(content)}"
    assert len(first["content"]) == CHUNK
    assert first["headers"].get("Authorization") is None
    assert final["headers"]["Content-Range"] == (
        f"bytes {CHUNK}-{len(content) - 1}/{len(content)}"
    )
    assert len(final["content"]) == 123
    assert final["headers"].get("Authorization") is None


def test_conditional_update_and_empty_create_preserve_conflicts() -> None:
    stale = ScriptedTransport(_response(200, _file(etag='"current"')))
    with pytest.raises(DocumentConflict, match="stale"):
        _store(stale).update(
            "plan.md",
            b"changed",
            content_type="text/markdown",
            expected_etag='"old"',
        )
    assert len(stale.requests) == 1

    transport = ScriptedTransport(
        _response(200, _file(etag='"current"')),
        _response(200, {"uploadUrl": "https://upload.example.invalid/update"}),
        _response(200, _file(etag='"next"')),
    )
    updated = _store(transport).update(
        "plan.md",
        b"changed",
        content_type="text/markdown",
        expected_etag='"current"',
    )
    assert updated.etag == '"next"'
    assert transport.requests[1]["headers"]["If-Match"] == '"current"'
    assert transport.requests[2]["headers"].get("Authorization") is None

    empty = ScriptedTransport(_response(201, _file("empty.txt", size=0)))
    created = _store(empty).create("empty.txt", b"", content_type="text/plain")
    assert created.size == 0
    assert empty.requests[0]["headers"]["If-None-Match"] == "*"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, DocumentPermissionDenied),
        (403, DocumentPermissionDenied),
        (404, DocumentNotFound),
        (409, DocumentConflict),
        (412, DocumentConflict),
        (413, DocumentTooLarge),
        (507, DocumentTooLarge),
        (429, DocumentUnavailable),
        (503, DocumentUnavailable),
    ],
)
def test_graph_failures_are_explicit_and_redacted(
    status: int, error_type: type[Exception]
) -> None:
    secret = "response-body-secret"
    transport = ScriptedTransport(
        _response(status, {"error": {"code": "accessDenied", "message": secret}})
    )
    with pytest.raises(error_type) as caught:
        _store(transport).stat("plan.md")
    assert "accessDenied" in str(caught.value)
    assert secret not in str(caught.value)
    assert "alpha-token" not in str(caught.value)


def test_invalid_responses_and_upload_limits_fail_closed() -> None:
    bad_page = ScriptedTransport(_response(200, {"value": "not-a-list"}))
    with pytest.raises(DocumentInvalidResponse):
        _store(bad_page).list()
    escaped_page = ScriptedTransport(
        _response(
            200,
            {
                "value": [],
                "@odata.nextLink": "https://evil.example.invalid/?$skiptoken=secret",
            },
        )
    )
    with pytest.raises(DocumentInvalidResponse, match="escaped"):
        _store(escaped_page).list()
    with pytest.raises(DocumentTooLarge, match="upload limit"):
        _store(ScriptedTransport(), max_upload=2).create(
            "large.txt", b"abc", content_type="text/plain"
        )
    assert not escaped_page.responses

    bad_size = ScriptedTransport(
        _response(200, _file(size=5)),
        HttpResponse(200, {}, b"four"),
    )
    with pytest.raises(DocumentInvalidResponse, match="size"):
        _store(bad_size).read("plan.md")

    no_transport = ScriptedTransport()
    with pytest.raises(DocumentUnavailable, match="credential resolution") as caught:
        _store(no_transport, tokens=FailingTokens()).stat("plan.md")
    assert "secret://projects/alpha/graph" not in str(caught.value)
    assert "bearer-value" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert no_transport.requests == []

    override = ScriptedTransport(HttpResponse(200, {}, b"{}"))
    override_store = _store(override)
    override_store._graph_request(
        "GET",
        "https://graph.microsoft.com/v1.0/drives/drive-alpha/root",
        headers={"authorization": "Bearer attacker", "X-Test": "kept"},
        content=None,
    )
    assert override.requests[0]["headers"] == {
        "Authorization": "Bearer alpha-token",
        "X-Test": "kept",
    }


def _manifest(project_id: str, drive_id: str, root: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "display_name": project_id.title(),
        "packages": {
            "organization_release_digest": "a" * 64,
            "overrides": [f"project-override/{project_id}@1.0.0"],
        },
        "repositories": {
            "primary": {
                "url": f"https://github.com/example/{project_id}.git",
                "default_branch": "main",
                "credential": "git",
            }
        },
        "documents": {
            "library": {
                "adapter": "onedrive",
                "drive_id": drive_id,
                "root": root,
                "credential": "graph",
            }
        },
        "teams": {
            "tenant_id": "tenant-one",
            "team_id": f"team-{project_id}",
            "credential": "graph",
            "channels": {"project": f"channel-{project_id}"},
            "role_identities": {
                "engineering": {
                    "application_id": f"bot-{project_id}-engineering",
                    "display_name": f"AM {project_id.title()} Engineering",
                    "credential": "teams-engineering",
                }
            },
        },
        "ado": {
            "organization": "https://dev.azure.com/seerstone",
            "project": project_id,
            "credential": "ado",
        },
        "credentials": {
            name: {
                "scope": "project",
                "provider": name,
                "reference": f"secret://projects/{project_id}/{name}",
            }
            for name in ("git", "graph", "ado", "teams-engineering")
        },
        "roles": {
            "engineering": {
                "package": "role/engineering@1.0.0",
                "tool_profile": "tool-profile/development@1.0.0",
                "instances": {"minimum": 1, "maximum": 1},
            }
        },
        "limits": {"max_total_instances": 1},
    }


def test_factory_binds_each_project_to_its_manifest_root_and_credential(
    tmp_path: Path, postgres_database: str
):
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('alpha', 'Alpha'), ('beta', 'Beta')
            """
        )
    for index, (project, drive, root) in enumerate(
        (
            ("alpha", "drive-alpha", "/projects/alpha"),
            ("beta", "drive-beta", "/projects/beta"),
        )
    ):
        path = tmp_path / f"{project}.yaml"
        path.write_text(
            yaml.safe_dump(_manifest(project, drive, root), sort_keys=False),
            encoding="utf-8",
        )
        ProjectManifestStore(postgres_database).activate(
            load_project_manifest(path),
            source_revision=str(index + 1) * 40,
            actor_id="project-admin",
        )
    transport = ScriptedTransport(
        _response(200, _file()),
        _response(200, _file()),
    )
    tokens = Tokens(
        {
            "secret://projects/alpha/graph": "alpha-token",
            "secret://projects/beta/graph": "beta-token",
        }
    )
    factory = OneDriveDocumentStoreFactory(
        postgres_database, token_provider=tokens, transport=transport
    )
    factory.create(project_id="alpha", root_id="library").stat("plan.md")
    factory.create(project_id="beta", root_id="library").stat("plan.md")
    assert "/drives/drive-alpha/root:/projects/alpha/plan.md" in transport.requests[0]["url"]
    assert "/drives/drive-beta/root:/projects/beta/plan.md" in transport.requests[1]["url"]
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer alpha-token"
    assert transport.requests[1]["headers"]["Authorization"] == "Bearer beta-token"
    with pytest.raises(DocumentNotFound):
        factory.create(project_id="alpha", root_id="beta-library")
    with psycopg.connect(postgres_database) as connection:
        snapshots = json.dumps(
            connection.execute(
                "SELECT snapshot FROM agentic_mesh_v5.project_manifest_snapshots"
            ).fetchall()
        )
    assert "alpha-token" not in snapshots and "beta-token" not in snapshots
