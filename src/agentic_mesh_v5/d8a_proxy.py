from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
import re
from urllib.parse import quote, urlencode, urlsplit

import httpx

from agentic_mesh_v5.api_auth import Principal
from agentic_mesh_v5.project_manifest import ProjectManifestStore


_NODE_ID = re.compile(r"^[^/\\]{1,4096}$")
_ROOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_NODE_ACTIONS = frozenset(
    {"children", "path", "open", "edit-open", "content", "content-stream", "comments"}
)
_MUTATIONS = frozenset({("PUT", "content"), ("POST", "comments")})
_MAX_JSON_BYTES = 2 * 1024 * 1024


class D8AProxyError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class D8AProxyResult:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    stream: AsyncIterator[bytes] | None = None
    close: Callable[[], Awaitable[None]] | None = None


class D8AProxy:
    """Narrow, project-scoped proxy for the D8Aroom documents component."""

    def __init__(
        self,
        *,
        manifest_store: ProjectManifestStore,
        base_url: str,
        tenant_slug: str,
        tenant_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("D8Aroom base_url must be a fixed HTTPS origin or path")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", tenant_slug):
            raise ValueError("D8Aroom tenant_slug is invalid")
        self._manifest_store = manifest_store
        self._base_url = base_url.rstrip("/")
        self._tenant_slug = tenant_slug
        self._tenant_id = tenant_id
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=False
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def forward(
        self,
        *,
        project_id: str,
        method: str,
        path: str,
        query: Mapping[str, str],
        content: bytes,
        content_type: str | None,
        principal: Principal,
        request_id: str,
    ) -> D8AProxyResult:
        method = method.upper()
        upstream_path, response_mode = self._target(path, method)
        roots = self._project_roots(project_id)
        if upstream_path != "/api/portal/roots":
            root_id = query.get("rootId", "").strip()
            if _ROOT_ID.fullmatch(root_id) is None:
                raise D8AProxyError(400, "d8a_root_required", "a valid rootId is required")
            if root_id not in roots:
                raise D8AProxyError(
                    403, "d8a_root_denied", "document root is outside the project boundary"
                )
        if len(content) > _MAX_JSON_BYTES:
            raise D8AProxyError(413, "d8a_request_too_large", "D8Aroom request is too large")

        headers = {
            "accept": "*/*" if response_mode == "stream" else "application/json",
            "x-ms-client-principal": _principal_header(principal),
            "x-agentic-mesh-project-id": project_id,
            "x-correlation-id": request_id,
            "x-d8-tenant-slug": self._tenant_slug,
        }
        if self._tenant_id:
            headers["x-d8-tenant-id"] = self._tenant_id
        if content_type and content:
            headers["content-type"] = content_type
        url = f"{self._base_url}{upstream_path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        try:
            request = self._client.build_request(
                method, url, headers=headers, content=content or None
            )
            response = await self._client.send(
                request, stream=response_mode == "stream", follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise D8AProxyError(
                502, "d8a_unavailable", "D8Aroom document service is unavailable"
            ) from exc
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise D8AProxyError(502, "d8a_redirect_rejected", "D8Aroom redirect was rejected")
        if response_mode == "stream" and response.is_success:
            safe_headers = _safe_response_headers(response, include_length=True)
            return D8AProxyResult(
                response.status_code,
                safe_headers,
                b"",
                stream=response.aiter_raw(),
                close=response.aclose,
            )
        payload = (
            await response.aread() if response_mode == "stream" else response.content
        )
        await response.aclose()
        if len(payload) > _MAX_JSON_BYTES and response_mode == "stream":
            raise D8AProxyError(
                502,
                "d8a_response_too_large",
                "D8Aroom error response is too large",
            )
        if response_mode == "json":
            if len(payload) > _MAX_JSON_BYTES:
                raise D8AProxyError(
                    502, "d8a_response_too_large", "D8Aroom response is too large"
                )
            if upstream_path == "/api/portal/roots" and response.is_success:
                payload = _filter_roots(payload, roots)
        safe_headers = _safe_response_headers(response)
        return D8AProxyResult(response.status_code, safe_headers, payload)

    def _project_roots(self, project_id: str) -> frozenset[str]:
        manifest = self._manifest_store.get_active_manifest(project_id)
        if manifest is None:
            raise D8AProxyError(
                503, "project_manifest_unavailable", "active project manifest is unavailable"
            )
        documents = manifest.snapshot.get("documents")
        if not isinstance(documents, Mapping):
            return frozenset()
        return frozenset(
            root_id
            for item in documents.values()
            if isinstance(item, Mapping)
            and isinstance((root_id := item.get("d8a_root_id")), str)
        )

    @staticmethod
    def _target(path: str, method: str) -> tuple[str, str]:
        parts = tuple(item for item in path.split("/") if item)
        if parts == ("roots",) and method == "GET":
            return "/api/portal/roots", "json"
        if (
            len(parts) == 3
            and parts[0] == "nodes"
            and _NODE_ID.fullmatch(parts[1])
            and ".." not in parts[1]
        ):
            action = parts[2]
            if action in _NODE_ACTIONS:
                allowed = method == "GET" or (method, action) in _MUTATIONS
                if allowed:
                    mode = "stream" if action == "content-stream" else "json"
                    return f"/api/portal/nodes/{quote(parts[1], safe='')}/{action}", mode
        raise D8AProxyError(404, "d8a_route_not_found", "D8Aroom route is not available")


def _principal_header(principal: Principal) -> str:
    roles = ["authenticated"]
    if "write" in principal.scopes:
        roles.append("admin")
    payload = {
        "userId": principal.subject,
        "userDetails": principal.subject,
        "userRoles": roles,
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(encoded).decode("ascii")


def _filter_roots(payload: bytes, allowed: frozenset[str]) -> bytes:
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise D8AProxyError(502, "d8a_invalid_response", "D8Aroom returned invalid JSON") from exc
    if isinstance(value, list):
        filtered: object = [item for item in value if _root_id(item) in allowed]
    elif isinstance(value, dict) and isinstance(value.get("roots"), list):
        filtered = {
            **value,
            "roots": [item for item in value["roots"] if _root_id(item) in allowed],
        }
    else:
        raise D8AProxyError(
            502, "d8a_invalid_response", "D8Aroom roots response is invalid"
        )
    return json.dumps(filtered, separators=(",", ":")).encode("utf-8")


def _root_id(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    root_id = value.get("rootId")
    return root_id if isinstance(root_id, str) else None


def _safe_response_headers(
    response: httpx.Response, *, include_length: bool = False
) -> dict[str, str]:
    names = ["content-type", "content-disposition", "etag", "last-modified"]
    if include_length:
        names.append("content-length")
    headers = {
        name: value
        for name in names
        if (value := response.headers.get(name))
    }
    headers["cache-control"] = "no-store"
    return headers
