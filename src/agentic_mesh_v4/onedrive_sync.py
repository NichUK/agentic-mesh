from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class OneDriveSyncResult:
    uploaded: int
    folders_created: int
    root_path: str


class OneDriveSyncError(RuntimeError):
    pass


def sync_local_documents_to_onedrive(
    *,
    project_config: Path,
    local_root: Path,
    access_token: str | None = None,
    drive_id: str | None = None,
) -> OneDriveSyncResult:
    library = _document_library(project_config)
    adapter = str(library.get("adapter") or "").casefold().replace("_", "-")
    if adapter not in {"onedrive", "sharepoint"}:
        raise OneDriveSyncError(f"document_library.adapter must be onedrive/sharepoint, got {adapter!r}")
    token = access_token or _access_token_from_refresh() or os.environ.get("AGENTIC_MESH_ONEDRIVE_TOKEN")
    if not token:
        raise OneDriveSyncError("AGENTIC_MESH_ONEDRIVE_TOKEN is required")
    configured_drive_id = _expand(str(library.get("drive_id") or ""))
    effective_drive_id = drive_id or configured_drive_id or os.environ.get("AGENTIC_MESH_ONEDRIVE_DRIVE_ID")
    if not effective_drive_id:
        raise OneDriveSyncError("OneDrive drive id is required")
    root_path = _normalise_root_path(_expand(str(library.get("root_path") or "/documents")))
    if not local_root.exists():
        raise OneDriveSyncError(f"local document root does not exist: {local_root}")

    client = _GraphClient(token=token, drive_id=effective_drive_id)
    folder_cache: set[str] = set()
    folders_created = 0
    uploaded = 0
    for path in sorted(local_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(local_root).as_posix()
        remote_path = posixpath.join(root_path, relative)
        parent = posixpath.dirname(remote_path)
        created = client.ensure_folder(parent, folder_cache=folder_cache)
        folders_created += created
        client.upload(remote_path, path)
        uploaded += 1
    return OneDriveSyncResult(uploaded=uploaded, folders_created=folders_created, root_path=root_path)


class _GraphClient:
    def __init__(self, *, token: str, drive_id: str) -> None:
        self.token = token
        self.drive_id = drive_id

    def ensure_folder(self, folder_path: str, *, folder_cache: set[str]) -> int:
        folder_path = _normalise_root_path(folder_path)
        created = 0
        current = ""
        for part in [item for item in folder_path.split("/") if item]:
            parent = current or "/"
            current = _normalise_root_path(posixpath.join(current, part))
            if current in folder_cache:
                continue
            if self._exists(current):
                folder_cache.add(current)
                continue
            self._create_folder(parent, part)
            folder_cache.add(current)
            created += 1
        return created

    def upload(self, remote_path: str, local_path: Path) -> None:
        mime_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        self._request(
            "PUT",
            self._drive_item_content_url(remote_path),
            body=local_path.read_bytes(),
            content_type=mime_type,
        )

    def _exists(self, remote_path: str) -> bool:
        request = urllib.request.Request(
            self._drive_item_url(remote_path),
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=60):  # noqa: S310 - Graph URL is fixed.
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def _create_folder(self, parent_path: str, name: str) -> None:
        body = {
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "replace",
        }
        self._request(
            "POST",
            self._drive_children_url(parent_path),
            body=json.dumps(body).encode("utf-8"),
            content_type="application/json",
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - Graph URL is fixed.
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise OneDriveSyncError(f"Graph {method} failed for {url}: {exc.code} {detail}") from exc

    def _drive_item_url(self, path: str) -> str:
        encoded = urllib.parse.quote(_normalise_root_path(path), safe="/")
        return f"https://graph.microsoft.com/v1.0/drives/{self.drive_id}/root:{encoded}"

    def _drive_item_content_url(self, path: str) -> str:
        return f"{self._drive_item_url(path)}:/content"

    def _drive_children_url(self, path: str) -> str:
        encoded = urllib.parse.quote(_normalise_root_path(path), safe="/")
        return f"https://graph.microsoft.com/v1.0/drives/{self.drive_id}/root:{encoded}:/children"


def _document_library(project_config: Path) -> dict[str, Any]:
    raw = yaml.safe_load(project_config.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise OneDriveSyncError(f"project config must be a mapping: {project_config}")
    library = raw.get("document_library")
    if not isinstance(library, dict):
        raise OneDriveSyncError(f"project config has no document_library mapping: {project_config}")
    return library


def _expand(value: str) -> str:
    return os.path.expandvars(value)


def _normalise_root_path(path: str) -> str:
    path = "/" + path.strip("/")
    return posixpath.normpath(path)


def _access_token_from_refresh() -> str | None:
    client_id = os.environ.get("AGENTIC_MESH_GRAPH_CLIENT_ID")
    refresh_token = os.environ.get("AGENTIC_MESH_GRAPH_REFRESH_TOKEN")
    tenant_id = os.environ.get("AGENTIC_MESH_GRAPH_TENANT_ID") or os.environ.get("AGENTIC_MESH_TENANT_ID")
    scopes = os.environ.get("AGENTIC_MESH_GRAPH_SCOPES")
    if not client_id or not refresh_token or not tenant_id or not scopes:
        return None
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": scopes,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - Microsoft login URL.
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise OneDriveSyncError(f"Graph refresh-token exchange failed: {exc.code} {detail}") from exc
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OneDriveSyncError("Graph refresh-token exchange did not return an access token")
    return access_token
