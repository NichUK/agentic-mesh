from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
from typing import Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
import psycopg

from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA


_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_CHUNK_SIZE = 10 * 1024 * 1024
_MAX_JSON_BYTES = 1024 * 1024


class DocumentStoreError(DatabaseError):
    pass


class DocumentNotFound(DocumentStoreError):
    pass


class DocumentConflict(DocumentStoreError):
    pass


class DocumentPermissionDenied(DocumentStoreError):
    pass


class DocumentTooLarge(DocumentStoreError):
    pass


class DocumentUnavailable(DocumentStoreError):
    pass


class DocumentInvalidResponse(DocumentStoreError):
    pass


class DocumentTransportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    item_id: str
    name: str
    path: str
    size: int
    etag: str
    is_folder: bool
    mime_type: str | None
    created_at: str | None
    modified_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "etag": self.etag,
            "is_folder": self.is_folder,
            "mime_type": self.mime_type,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
        }


@dataclass(frozen=True, slots=True)
class DocumentPage:
    items: tuple[DocumentMetadata, ...]
    next_cursor: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "next_cursor": self.next_cursor,
        }


@dataclass(frozen=True, slots=True)
class DocumentContent:
    metadata: DocumentMetadata
    content: bytes


@runtime_checkable
class DocumentStore(Protocol):
    def list(
        self, path: str = "", *, cursor: str | None = None, page_size: int = 200
    ) -> DocumentPage: ...

    def stat(self, path: str) -> DocumentMetadata: ...

    def read(self, path: str) -> DocumentContent: ...

    def create(
        self, path: str, content: bytes, *, content_type: str
    ) -> DocumentMetadata: ...

    def update(
        self,
        path: str,
        content: bytes,
        *,
        content_type: str,
        expected_etag: str,
    ) -> DocumentMetadata: ...


class AccessTokenProvider(Protocol):
    def access_token(self, *, provider: str, reference: str) -> str: ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None = None,
        follow_redirects: bool = False,
    ) -> HttpResponse: ...


class HttpxTransport:
    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None = None,
        follow_redirects: bool = False,
    ) -> HttpResponse:
        try:
            response = self._client.request(
                method,
                url,
                headers=dict(headers),
                content=content,
                follow_redirects=follow_redirects,
            )
        except httpx.HTTPError:
            raise DocumentTransportError("HTTP transport failed") from None
        return HttpResponse(
            response.status_code,
            {key.casefold(): value for key, value in response.headers.items()},
            response.content,
        )

    def close(self) -> None:
        self._client.close()


class OneDriveDocumentStore:
    def __init__(
        self,
        *,
        drive_id: str,
        root: str,
        credential_provider: str,
        credential_reference: str,
        token_provider: AccessTokenProvider,
        transport: HttpTransport,
        max_download_bytes: int = 100 * 1024 * 1024,
        max_upload_bytes: int = 250 * 1024 * 1024,
    ) -> None:
        self._drive_id = _external_id(drive_id, "drive_id")
        self._root = _root_path(root)
        self._credential_provider = _identifier(
            credential_provider, "credential provider"
        )
        self._credential_reference = _credential_reference(credential_reference)
        if max_download_bytes <= 0 or max_upload_bytes <= 0:
            raise ValueError("document size limits must be positive")
        self._max_download_bytes = max_download_bytes
        self._max_upload_bytes = max_upload_bytes
        self._token_provider = token_provider
        self._transport = transport
        drive = quote(self._drive_id, safe="")
        self._base_url = f"https://graph.microsoft.com/v1.0/drives/{drive}"

    def list(
        self, path: str = "", *, cursor: str | None = None, page_size: int = 200
    ) -> DocumentPage:
        relative = _relative_path(path, allow_empty=True)
        if type(page_size) is not int or page_size < 1 or page_size > 200:
            raise ValueError("page_size must be between 1 and 200")
        skip_token = None if cursor is None else _decode_cursor(cursor, relative)
        query = {
            "$select": "id,name,size,eTag,createdDateTime,lastModifiedDateTime,file,folder",
            "$top": str(page_size),
        }
        if skip_token is not None:
            query["$skiptoken"] = skip_token
        url = f"{self._children_url(relative)}?{urlencode(query)}"
        payload = self._graph_json("GET", url, expected={200})
        values = payload.get("value")
        if not isinstance(values, list) or len(values) > page_size:
            raise DocumentInvalidResponse("Graph children response is invalid")
        items = tuple(
            self._metadata(item, _join_relative(relative, _item_name(item)))
            for item in values
        )
        next_cursor = self._next_cursor(payload.get("@odata.nextLink"), relative)
        return DocumentPage(items, next_cursor)

    def stat(self, path: str) -> DocumentMetadata:
        relative = _relative_path(path, allow_empty=True)
        payload = self._graph_json(
            "GET",
            self._metadata_url(relative),
            expected={200},
        )
        return self._metadata(payload, relative)

    def read(self, path: str) -> DocumentContent:
        relative = _relative_path(path, allow_empty=False)
        metadata = self.stat(relative)
        if metadata.is_folder:
            raise DocumentConflict("folders do not have document content")
        if metadata.size > self._max_download_bytes:
            raise DocumentTooLarge("document exceeds the configured download limit")
        response = self._graph_request(
            "GET",
            self._content_url(relative),
            headers={"If-Match": metadata.etag},
            content=None,
        )
        redirects = 0
        while response.status_code == 302:
            redirects += 1
            if redirects > 3:
                raise DocumentInvalidResponse("Graph download redirected too many times")
            location = response.headers.get("location")
            if not isinstance(location, str):
                raise DocumentInvalidResponse("Graph download redirect is missing")
            download_url = _preauthenticated_url(location)
            response = self._send(
                "GET",
                download_url,
                headers={},
                content=None,
                follow_redirects=False,
            )
        self._raise_for_status(response, expected={200})
        if len(response.content) > self._max_download_bytes:
            raise DocumentTooLarge("document exceeds the configured download limit")
        if len(response.content) != metadata.size:
            raise DocumentInvalidResponse("downloaded document size does not match metadata")
        return DocumentContent(metadata, response.content)

    def create(
        self, path: str, content: bytes, *, content_type: str
    ) -> DocumentMetadata:
        relative = _relative_path(path, allow_empty=False)
        body = _content_bytes(content, self._max_upload_bytes)
        mime_type = _content_type(content_type)
        if not body:
            return self._empty_write(relative, body, mime_type, create=True)
        name = PurePosixPath(relative).name
        session = self._graph_json(
            "POST",
            self._create_session_url(relative),
            expected={200},
            headers={"Content-Type": "application/json"},
            content=json.dumps(
                {
                    "item": {
                        "@microsoft.graph.conflictBehavior": "fail",
                        "name": name,
                    }
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        return self._upload(relative, body, mime_type, session)

    def update(
        self,
        path: str,
        content: bytes,
        *,
        content_type: str,
        expected_etag: str,
    ) -> DocumentMetadata:
        relative = _relative_path(path, allow_empty=False)
        body = _content_bytes(content, self._max_upload_bytes)
        mime_type = _content_type(content_type)
        etag = _etag(expected_etag)
        current = self.stat(relative)
        if current.is_folder:
            raise DocumentConflict("folders cannot be updated as documents")
        if current.etag != etag:
            raise DocumentConflict("document eTag is stale")
        if not body:
            return self._empty_write(
                relative, body, mime_type, create=False, item_id=current.item_id, etag=etag
            )
        session = self._graph_json(
            "POST",
            f"{self._base_url}/items/{quote(current.item_id, safe='')}/createUploadSession",
            expected={200},
            headers={"Content-Type": "application/json", "If-Match": etag},
            content=b'{"item":{"@microsoft.graph.conflictBehavior":"replace"}}',
        )
        return self._upload(relative, body, mime_type, session)

    def _empty_write(
        self,
        relative: str,
        body: bytes,
        mime_type: str,
        *,
        create: bool,
        item_id: str | None = None,
        etag: str | None = None,
    ) -> DocumentMetadata:
        if create:
            url = self._content_url(relative)
            headers = {"Content-Type": mime_type, "If-None-Match": "*"}
        else:
            if item_id is None or etag is None:
                raise DocumentStoreError("conditional update evidence is missing")
            url = f"{self._base_url}/items/{quote(item_id, safe='')}/content"
            headers = {"Content-Type": mime_type, "If-Match": etag}
        payload = self._graph_json(
            "PUT", url, expected={200, 201}, headers=headers, content=body
        )
        return self._metadata(payload, relative)

    def _upload(
        self,
        relative: str,
        content: bytes,
        content_type: str,
        session: Mapping[str, object],
    ) -> DocumentMetadata:
        upload_url = session.get("uploadUrl")
        if not isinstance(upload_url, str):
            raise DocumentInvalidResponse("Graph upload session URL is missing")
        upload_url = _preauthenticated_url(upload_url)
        total = len(content)
        final_payload: Mapping[str, object] | None = None
        for start in range(0, total, _CHUNK_SIZE):
            chunk = content[start : start + _CHUNK_SIZE]
            end = start + len(chunk) - 1
            response = self._send(
                "PUT",
                upload_url,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                },
                content=chunk,
                follow_redirects=False,
            )
            final = end + 1 == total
            self._raise_for_status(response, expected={200, 201} if final else {202})
            if final:
                final_payload = _json_response(response)
        if final_payload is None:
            raise DocumentInvalidResponse("Graph upload did not return final metadata")
        return self._metadata(final_payload, relative)

    def _metadata(self, value: object, relative: str) -> DocumentMetadata:
        if not isinstance(value, Mapping):
            raise DocumentInvalidResponse("Graph driveItem is invalid")
        item_id = _response_text(value.get("id"), "driveItem id")
        name = _response_text(value.get("name"), "driveItem name")
        size = value.get("size")
        etag = value.get("eTag")
        folder = isinstance(value.get("folder"), Mapping)
        file_value = value.get("file")
        if type(size) is not int or size < 0 or not isinstance(etag, str) or not etag:
            raise DocumentInvalidResponse("Graph driveItem metadata is incomplete")
        if not folder and not isinstance(file_value, Mapping):
            raise DocumentInvalidResponse("Graph driveItem has no file or folder facet")
        mime_type = None
        if isinstance(file_value, Mapping):
            candidate = file_value.get("mimeType")
            mime_type = candidate if isinstance(candidate, str) and candidate else None
        return DocumentMetadata(
            item_id,
            name,
            relative,
            size,
            etag,
            folder,
            mime_type,
            _optional_response_text(value.get("createdDateTime")),
            _optional_response_text(value.get("lastModifiedDateTime")),
        )

    def _next_cursor(self, value: object, relative: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 8192:
            raise DocumentInvalidResponse("Graph paging link is invalid")
        parsed = urlsplit(value)
        expected_prefix = f"/v1.0/drives/{quote(self._drive_id, safe='')}/"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "graph.microsoft.com"
            or not parsed.path.startswith(expected_prefix)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise DocumentInvalidResponse("Graph paging link escaped the configured drive")
        tokens = parse_qs(parsed.query, keep_blank_values=False).get("$skiptoken", [])
        if len(tokens) != 1:
            raise DocumentInvalidResponse("Graph paging link has no single skip token")
        token = tokens[0]
        if not token or len(token) > 4096 or any(ord(item) < 32 for item in token):
            raise DocumentInvalidResponse("Graph paging token is invalid")
        return _encode_cursor(relative, token)

    def _graph_json(
        self,
        method: str,
        url: str,
        *,
        expected: set[int],
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> Mapping[str, object]:
        response = self._graph_request(
            method, url, headers=headers or {}, content=content
        )
        self._raise_for_status(response, expected=expected)
        return _json_response(response)

    def _graph_request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None,
    ) -> HttpResponse:
        try:
            token = self._token_provider.access_token(
                provider=self._credential_provider,
                reference=self._credential_reference,
            )
        except Exception:
            raise DocumentUnavailable("document credential resolution failed") from None
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise DocumentUnavailable("document credential resolution failed")
        request_headers = {"Authorization": f"Bearer {token}", **dict(headers)}
        return self._send(
            method,
            url,
            headers=request_headers,
            content=content,
            follow_redirects=False,
        )

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None,
        follow_redirects: bool,
    ) -> HttpResponse:
        try:
            return self._transport.request(
                method,
                url,
                headers=headers,
                content=content,
                follow_redirects=follow_redirects,
            )
        except DocumentTransportError:
            raise DocumentUnavailable("document service transport failed") from None
        except DocumentStoreError:
            raise
        except Exception:
            raise DocumentUnavailable("document service transport failed") from None

    @staticmethod
    def _raise_for_status(response: HttpResponse, *, expected: set[int]) -> None:
        if response.status_code in expected:
            return
        status = response.status_code
        code = _graph_error_code(response)
        detail = f"status {status}" + (f", code {code}" if code else "")
        if 200 <= status <= 299:
            raise DocumentInvalidResponse(
                f"document provider returned an unexpected success ({detail})"
            )
        if status in {401, 403}:
            raise DocumentPermissionDenied(f"document access denied ({detail})")
        if status == 404:
            raise DocumentNotFound(f"document was not found ({detail})")
        if status in {409, 412}:
            raise DocumentConflict(f"document conflict ({detail})")
        if status in {413, 507}:
            raise DocumentTooLarge(f"document storage limit reached ({detail})")
        if status == 429 or 500 <= status <= 599:
            raise DocumentUnavailable(f"document service is unavailable ({detail})")
        raise DocumentStoreError(f"document request failed ({detail})")

    def _metadata_url(self, relative: str) -> str:
        fields = "id,name,size,eTag,createdDateTime,lastModifiedDateTime,file,folder"
        return f"{self._item_url(relative)}?$select={fields}"

    def _children_url(self, relative: str) -> str:
        item = self._item_url(relative)
        return f"{item}/children" if item.endswith("/root") else f"{item}:/children"

    def _content_url(self, relative: str) -> str:
        item = self._item_url(relative)
        if item.endswith("/root"):
            raise DocumentConflict("the configured root is a folder")
        return f"{item}:/content"

    def _create_session_url(self, relative: str) -> str:
        item = self._item_url(relative)
        return f"{item}:/createUploadSession"

    def _item_url(self, relative: str) -> str:
        full = _join_root(self._root, relative)
        if full == "/":
            return f"{self._base_url}/root"
        return f"{self._base_url}/root:/{quote(full.strip('/'), safe='/')}"


class OneDriveDocumentStoreFactory:
    def __init__(
        self,
        database_url: str,
        *,
        token_provider: AccessTokenProvider,
        transport: HttpTransport,
        max_download_bytes: int = 100 * 1024 * 1024,
        max_upload_bytes: int = 250 * 1024 * 1024,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._token_provider = token_provider
        self._transport = transport
        self._max_download_bytes = max_download_bytes
        self._max_upload_bytes = max_upload_bytes

    def create(self, *, project_id: str, root_id: str) -> OneDriveDocumentStore:
        project_id = _identifier(project_id, "project_id")
        root_id = _identifier(root_id, "root_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                row = connection.execute(
                    f"""
                    SELECT snapshot.snapshot
                    FROM {SCHEMA}.project_manifest_active AS active
                    JOIN {SCHEMA}.project_manifest_snapshots AS snapshot
                      ON snapshot.project_id = active.project_id
                     AND snapshot.manifest_digest = active.manifest_digest
                    WHERE active.project_id = %s
                    """,
                    (project_id,),
                ).fetchone()
        except Exception:
            raise DocumentUnavailable("document configuration read failed") from None
        if row is None:
            raise DocumentNotFound("active project document configuration was not found")
        snapshot = row[0]
        documents = snapshot.get("documents") if isinstance(snapshot, Mapping) else None
        credentials = snapshot.get("credentials") if isinstance(snapshot, Mapping) else None
        document = documents.get(root_id) if isinstance(documents, Mapping) else None
        if not isinstance(document, Mapping):
            raise DocumentNotFound("project document root was not found")
        if document.get("adapter") != "onedrive":
            raise DocumentStoreError("project document root is not a OneDrive adapter")
        credential_id = document.get("credential")
        credential = (
            credentials.get(credential_id)
            if isinstance(credentials, Mapping) and isinstance(credential_id, str)
            else None
        )
        if not isinstance(credential, Mapping):
            raise DocumentStoreError("project document credential is invalid")
        return OneDriveDocumentStore(
            drive_id=document.get("drive_id"),
            root=document.get("root"),
            credential_provider=credential.get("provider"),
            credential_reference=credential.get("reference"),
            token_provider=self._token_provider,
            transport=self._transport,
            max_download_bytes=self._max_download_bytes,
            max_upload_bytes=self._max_upload_bytes,
        )


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _external_id(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(item) < 33 for item in value)
        or any(item in value for item in ("/", "\\", "?", "#", "@"))
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _root_path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("document root must be absolute")
    normalized = _relative_path(value.removeprefix("/"), allow_empty=True)
    return "/" + normalized if normalized else "/"


def _relative_path(value: object, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("document path is invalid")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError("document path must be relative and contained")
    if any(ord(item) < 32 for item in value):
        raise ValueError("document path contains control characters")
    if not value:
        if allow_empty:
            return ""
        raise ValueError("document path is required")
    parts = value.split("/")
    if any(not item or item in {".", ".."} for item in parts):
        raise ValueError("document path must be canonical and contained")
    return "/".join(parts)


def _credential_reference(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(("secret://", "mount://", "oauth-cache://"))
        or len(value) > 512
        or any(ord(item) < 33 for item in value)
    ):
        raise ValueError("credential reference is invalid")
    return value


def _content_bytes(value: object, maximum: int) -> bytes:
    if not isinstance(value, bytes):
        raise ValueError("document content must be bytes")
    if len(value) > maximum:
        raise DocumentTooLarge("document exceeds the configured upload limit")
    return value


def _content_type(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or any(item in value for item in ("\r", "\n"))
    ):
        raise ValueError("content_type is invalid")
    return value


def _etag(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(item in value for item in ("\r", "\n"))
    ):
        raise ValueError("expected_etag is invalid")
    return value


def _join_root(root: str, relative: str) -> str:
    return root if not relative else root.rstrip("/") + "/" + relative


def _join_relative(parent: str, name: str) -> str:
    safe_name = _relative_path(name, allow_empty=False)
    if "/" in safe_name:
        raise DocumentInvalidResponse("Graph child name contains a path separator")
    return safe_name if not parent else f"{parent}/{safe_name}"


def _item_name(value: object) -> str:
    if not isinstance(value, Mapping):
        raise DocumentInvalidResponse("Graph child item is invalid")
    return _response_text(value.get("name"), "driveItem name")


def _response_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or "\x00" in value
    ):
        raise DocumentInvalidResponse(f"Graph {field} is invalid")
    return value


def _optional_response_text(value: object) -> str | None:
    if value is None:
        return None
    return _response_text(value, "timestamp")


def _json_response(response: HttpResponse) -> Mapping[str, object]:
    if len(response.content) > _MAX_JSON_BYTES:
        raise DocumentInvalidResponse("Graph JSON response is too large")
    try:
        value = json.loads(response.content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DocumentInvalidResponse("Graph JSON response is invalid") from None
    if not isinstance(value, Mapping):
        raise DocumentInvalidResponse("Graph JSON response must be an object")
    return value


def _graph_error_code(response: HttpResponse) -> str | None:
    if not response.content or len(response.content) > _MAX_JSON_BYTES:
        return None
    try:
        value = json.loads(response.content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    error = value.get("error") if isinstance(value, Mapping) else None
    code = error.get("code") if isinstance(error, Mapping) else None
    if not isinstance(code, str) or not code or len(code) > 100:
        return None
    return code if re.fullmatch(r"[A-Za-z0-9._-]+", code) else None


def _preauthenticated_url(value: str) -> str:
    if len(value) > 8192:
        raise DocumentInvalidResponse("pre-authenticated URL is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise DocumentInvalidResponse("pre-authenticated URL is invalid")
    return value


def _encode_cursor(path: str, token: str) -> str:
    value = json.dumps(
        {"path": path, "skip_token": token}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_cursor(value: object, expected_path: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError("document cursor is invalid")
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.b64decode(value + padding, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("document cursor is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"path", "skip_token"}:
        raise ValueError("document cursor is invalid")
    if payload.get("path") != expected_path:
        raise ValueError("document cursor does not match the requested path")
    token = payload.get("skip_token")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 4096
        or any(ord(item) < 32 for item in token)
    ):
        raise ValueError("document cursor is invalid")
    return token
