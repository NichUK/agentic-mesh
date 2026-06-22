from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_mesh_v4.onedrive_sync import sync_local_documents_to_onedrive


class FakeResponse:
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_v4_syncs_local_documents_to_configured_onedrive_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_config = tmp_path / "project-v4.yaml"
    project_config.write_text(
        "\n".join(
            [
                "document_library:",
                "  adapter: onedrive",
                "  drive_id: drive-1",
                "  root_path: /documents",
            ]
        ),
        encoding="utf-8",
    )
    local_root = tmp_path / "documents"
    artifact = local_root / "work-items" / "work-1" / "index.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Work 1", encoding="utf-8")

    calls: list[urllib.request.Request] = []
    existing: set[str] = set()

    def fake_urlopen(request: urllib.request.Request, timeout: int = 0) -> FakeResponse:
        calls.append(request)
        method = request.get_method()
        url = request.full_url
        if method == "GET" and url not in existing:
            raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b"{}"))
        if method == "POST":
            payload = json.loads((request.data or b"{}").decode("utf-8"))
            parent = url.split("/root:", 1)[1].split(":/children", 1)[0]
            existing.add(f"https://graph.microsoft.com/v1.0/drives/drive-1/root:{parent}/{payload['name']}")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = sync_local_documents_to_onedrive(
        project_config=project_config,
        local_root=local_root,
        access_token="token",
    )

    assert result.uploaded == 1
    assert result.root_path == "/documents"
    methods = [call.get_method() for call in calls]
    assert "PUT" in methods
    assert any(
        call.full_url.endswith("/root:/documents/work-items/work-1/index.md:/content")
        for call in calls
        if call.get_method() == "PUT"
    )
