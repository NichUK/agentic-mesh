from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest

from agentic_mesh_v5.api import API_PREFIX, create_app
from agentic_mesh_v5.api_auth import EntraAuthorizer, Principal, TokenAuthorizer
from agentic_mesh_v5.api_auth import AuthenticationConfigurationError
from agentic_mesh_v5.api_auth import authorizer_from_environment
from agentic_mesh_v5.d8a_proxy import D8AProxy, D8AProxyError, D8AProxyResult


TENANT_ID = "11111111-1111-4111-8111-111111111111"
OBJECT_ID = "22222222-2222-4222-8222-222222222222"
AUDIENCE = "api://agentic-mesh-v5"


class _SigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _Jwks:
    def __init__(self, key) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, _token: str) -> _SigningKey:
        return _SigningKey(self._key)


def _entra_authorizer(role: str = "AgenticMesh.Sponsor") -> tuple[EntraAuthorizer, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "tid": TENANT_ID,
            "oid": OBJECT_ID,
            "roles": [role],
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    authorizer = EntraAuthorizer(
        tenant_id=TENANT_ID,
        audience=AUDIENCE,
        bindings=[
            {"object_id": OBJECT_ID, "subject": "sponsor-1", "projects": ["alpha"]}
        ],
    )
    authorizer._jwks = _Jwks(private_key.public_key())
    return authorizer, token


def test_entra_authorizer_validates_token_and_maps_app_role_to_project_scope() -> None:
    authorizer, token = _entra_authorizer()

    principal = authorizer.resolve(token)

    assert principal == Principal(
        subject="sponsor-1",
        projects=frozenset({"alpha"}),
        scopes=frozenset({"read", "write"}),
        roles=frozenset({"AgenticMesh.Sponsor"}),
    )
    assert not principal.permits(project_id="bravo", scope="read")
    assert authorizer.resolve(token + "tampered") is None


def test_entra_authorizer_rejects_foreign_tenant_unbound_identity_and_unknown_role() -> None:
    claims = {
        "tid": "33333333-3333-4333-8333-333333333333",
        "oid": OBJECT_ID,
        "roles": ["AgenticMesh.Sponsor"],
    }
    authorizer = EntraAuthorizer(
        tenant_id=TENANT_ID,
        audience=AUDIENCE,
        bindings=[
            {"object_id": OBJECT_ID, "subject": "sponsor-1", "projects": ["alpha"]}
        ],
        decoder=lambda _token: claims,
    )
    assert authorizer.resolve("token") is None
    claims["tid"] = TENANT_ID
    claims["oid"] = "44444444-4444-4444-8444-444444444444"
    assert authorizer.resolve("token") is None
    claims["oid"] = OBJECT_ID
    claims["roles"] = ["AgenticMesh.Untrusted"]
    assert authorizer.resolve("token") is None


@pytest.mark.parametrize(
    ("role", "scopes"),
    [
        ("AgenticMesh.Viewer", {"read"}),
        ("AgenticMesh.Sponsor", {"read", "write"}),
        (
            "AgenticMesh.Operator",
            {"project:create", "read", "write", "recovery:execute"},
        ),
    ],
)
def test_entra_app_roles_have_exact_mesh_permissions(role: str, scopes: set[str]) -> None:
    authorizer = EntraAuthorizer(
        tenant_id=TENANT_ID,
        audience=AUDIENCE,
        bindings=[
            {"object_id": OBJECT_ID, "subject": "subject", "projects": ["alpha"]}
        ],
        decoder=lambda _token: {
            "tid": TENANT_ID,
            "oid": OBJECT_ID,
            "roles": [role],
        },
    )

    principal = authorizer.resolve("token")

    assert principal is not None
    assert principal.scopes == scopes


def test_partial_entra_environment_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_MESH_V5_ENTRA_TENANT_ID", TENANT_ID)
    monkeypatch.delenv("AGENTIC_MESH_V5_ENTRA_AUDIENCE", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_V5_ENTRA_BINDINGS_FILE", raising=False)

    with pytest.raises(AuthenticationConfigurationError, match="required together"):
        authorizer_from_environment()


class _ManifestStore:
    def __init__(self, roots: tuple[str, ...]) -> None:
        self._roots = roots

    def get_active_manifest(self, _project_id: str):
        return SimpleNamespace(
            snapshot={
                "documents": {
                    root: {"d8a_root_id": root} for root in self._roots
                }
            }
        )


def _principal() -> Principal:
    return Principal(
        subject="sponsor-1",
        projects=frozenset({"alpha"}),
        scopes=frozenset({"read", "write"}),
        roles=frozenset({"AgenticMesh.Sponsor"}),
    )


async def _d8a_proxy_filters_roots_and_forwards_only_trusted_identity() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"roots": [{"rootId": "alpha-root"}, {"rootId": "bravo-root"}]},
        )

    proxy = D8AProxy(
        manifest_store=_ManifestStore(("alpha-root",)),
        base_url="https://documents.example.test",
        tenant_slug="seerstone",
        tenant_id=TENANT_ID,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await proxy.forward(
        project_id="alpha",
        method="GET",
        path="roots",
        query={},
        content=b"",
        content_type=None,
        principal=_principal(),
        request_id="request-1",
    )

    assert json.loads(result.content) == {"roots": [{"rootId": "alpha-root"}]}
    forwarded = seen[0]
    assert forwarded.url.path == "/api/portal/roots"
    assert "authorization" not in forwarded.headers
    assert "x-ms-token-aad-access-token" not in forwarded.headers
    assert "x-quantauma-portal-principal" not in forwarded.headers
    encoded = forwarded.headers["x-ms-client-principal"]
    identity = json.loads(base64.b64decode(encoded))
    assert identity == {
        "userId": "sponsor-1",
        "userDetails": "sponsor-1",
        "userRoles": ["authenticated", "admin"],
    }


async def _d8a_proxy_rejects_foreign_root_before_calling_upstream() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    proxy = D8AProxy(
        manifest_store=_ManifestStore(("alpha-root",)),
        base_url="https://documents.example.test",
        tenant_slug="seerstone",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await proxy.forward(
            project_id="alpha",
            method="GET",
            path="nodes/node-1/content",
            query={"rootId": "bravo-root"},
            content=b"",
            content_type=None,
            principal=_principal(),
            request_id="request-1",
        )
    except D8AProxyError as exc:
        assert exc.status_code == 403
        assert exc.code == "d8a_root_denied"
    else:
        raise AssertionError("foreign root was accepted")
    assert calls == 0


async def _d8a_proxy_streams_document_content() -> None:
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"document-"
            yield b"bytes"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "*/*"
        return httpx.Response(
            200, stream=Stream(), headers={"content-type": "application/pdf"}
        )

    proxy = D8AProxy(
        manifest_store=_ManifestStore(("alpha-root",)),
        base_url="https://documents.example.test",
        tenant_slug="seerstone",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await proxy.forward(
        project_id="alpha",
        method="GET",
        path="nodes/node-1/content-stream",
        query={"rootId": "alpha-root"},
        content=b"",
        content_type=None,
        principal=_principal(),
        request_id="request-1",
    )
    assert result.content == b""
    assert result.stream is not None
    assert b"".join([part async for part in result.stream]) == b"document-bytes"
    assert result.close is not None
    await result.close()


def test_d8a_proxy_filters_roots_and_forwards_only_trusted_identity() -> None:
    asyncio.run(_d8a_proxy_filters_roots_and_forwards_only_trusted_identity())


def test_d8a_proxy_rejects_foreign_root_before_calling_upstream() -> None:
    asyncio.run(_d8a_proxy_rejects_foreign_root_before_calling_upstream())


def test_d8a_proxy_streams_document_content() -> None:
    asyncio.run(_d8a_proxy_streams_document_content())


class _ApiProxy:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def forward(self, **kwargs) -> D8AProxyResult:
        self.calls.append(kwargs)
        return D8AProxyResult(200, {"content-type": "application/json"}, b"{}")


def _token_authorizer(token: str, scopes: list[str]) -> TokenAuthorizer:
    import hashlib

    return TokenAuthorizer(
        [
            {
                "subject": "viewer",
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "projects": ["alpha"],
                "scopes": scopes,
            }
        ]
    )


def test_d8a_api_enforces_project_roles_without_forwarding_access_token() -> None:
    proxy = _ApiProxy()
    app = create_app(
        "postgresql://unused/mesh",
        authorizer=_token_authorizer("viewer-token", ["read"]),
        d8a_proxy=proxy,
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer viewer-token"}

    response = client.get(
        f"{API_PREFIX}/projects/alpha/documents/d8aroom/roots", headers=headers
    )
    denied = client.post(
        f"{API_PREFIX}/projects/alpha/documents/d8aroom/nodes/node-1/comments"
        "?rootId=alpha-root",
        headers=headers,
        json={"text": "hello"},
    )

    assert response.status_code == 200
    assert proxy.calls[0]["principal"].subject == "viewer"
    assert denied.status_code == 403
    assert len(proxy.calls) == 1


def test_d8a_environment_requires_explicit_tenant_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTIC_MESH_V5_D8A_BASE_URL", "https://documents.example.test")
    monkeypatch.delenv("AGENTIC_MESH_V5_D8A_TENANT_SLUG", raising=False)

    with pytest.raises(ValueError, match="D8A_TENANT_SLUG is required"):
        create_app(
            "postgresql://unused/mesh",
            authorizer=_token_authorizer("viewer-token", ["read"]),
        )
