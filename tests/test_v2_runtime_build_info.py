import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.server import V2StatusHandler


BUILD_ENV = (
    "AGENTIC_MESH_IMAGE_TAG",
    "AGENTIC_MESH_V2_IMAGE_TAG",
    "AGENTIC_MESH_SOURCE_COMMIT",
    "AGENTIC_MESH_COMMIT_SHA",
    "AGENTIC_MESH_GIT_SHA",
    "AGENTIC_MESH_BUILD_REF",
    "AGENTIC_MESH_RELEASE",
    "AGENTIC_MESH_BUILD_TIME",
    "AGENTIC_MESH_ENVIRONMENT",
)


def _snapshot(tmp_path: Path) -> dict[str, object]:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        return db.status_snapshot()
    finally:
        db.close()


def _render(snapshot: dict[str, object]) -> str:
    handler = object.__new__(V2StatusHandler)
    handler._snapshot = lambda: snapshot
    handler.db_path = Path(str(snapshot["database"]))
    handler.project_file = None
    handler.path = "/status"
    return handler._render_status()


def test_runtime_build_info_defaults_when_metadata_is_missing(tmp_path: Path, monkeypatch) -> None:
    for name in BUILD_ENV:
        monkeypatch.delenv(name, raising=False)

    info = _snapshot(tmp_path)["runtime_build_info"]

    assert info["runtime_name"] == "agentic_mesh_v2"
    assert info["image_tag"] == "Not configured"
    assert info["source_commit"] == "Not configured"
    assert info["source_branch"] == "Not configured"
    assert info["environment"] == "Not configured"
    assert info["database_schema_version"] == "1"
    assert info["metadata_source"] == "not_configured"


def test_status_json_and_html_expose_runtime_build_info(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_MESH_IMAGE_TAG", "agentic-mesh-v2:2026.06.14")
    monkeypatch.setenv("AGENTIC_MESH_SOURCE_COMMIT", "abc1234")
    monkeypatch.setenv("AGENTIC_MESH_SOURCE_BRANCH", "codex/v2-runtime-reset")
    monkeypatch.setenv("AGENTIC_MESH_BUILD_REF", "release-2026.06.14")
    monkeypatch.setenv("AGENTIC_MESH_BUILD_TIME", "2026-06-14T13:00:00Z")
    monkeypatch.setenv("AGENTIC_MESH_ENVIRONMENT", "linuxch")

    snapshot = _snapshot(tmp_path)
    info = snapshot["runtime_build_info"]
    html = _render(snapshot)

    assert info["image_tag"] == "agentic-mesh-v2:2026.06.14"
    assert info["source_commit"] == "abc1234"
    assert info["source_branch"] == "codex/v2-runtime-reset"
    assert info["build_ref"] == "release-2026.06.14"
    assert info["build_time"] == "2026-06-14T13:00:00Z"
    assert info["environment"] == "linuxch"
    assert "Runtime Build Info:" in html
    assert "image=agentic-mesh-v2:2026.06.14" in html
    assert "commit=abc1234" in html
    assert "branch=codex/v2-runtime-reset" in html
    assert "env=linuxch" in html


def test_runtime_build_info_redacts_and_escapes_unsafe_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_MESH_IMAGE_TAG", "/tmp/private/image:latest")
    monkeypatch.setenv("AGENTIC_MESH_SOURCE_COMMIT", "19:activity@thread.tacv2")
    monkeypatch.setenv("AGENTIC_MESH_BUILD_REF", "secret-token-value")
    monkeypatch.setenv("AGENTIC_MESH_BUILD_TIME", "https://build.example/private")

    rendered = json.dumps(_snapshot(tmp_path)["runtime_build_info"])

    assert rendered.count("Redacted") == 4
    assert "/tmp/private" not in rendered
    assert "activity@thread" not in rendered
    assert "secret-token-value" not in rendered
    assert "build.example" not in rendered

    snapshot = _snapshot(tmp_path)
    snapshot["runtime_build_info"]["build_ref"] = "<script>alert(1)</script>"
    html = _render(snapshot)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_v2_status_json_and_healthz_routes_remain_stable(tmp_path: Path) -> None:
    db_path = tmp_path / "v2.sqlite3"
    db = V2Database(db_path)
    try:
        db.migrate()
    finally:
        db.close()

    V2StatusHandler.db_path = db_path
    V2StatusHandler.project_file = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), V2StatusHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/status.json")
        response = connection.getresponse()
        status_payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert "runtime_build_info" in status_payload

        connection.request("GET", "/healthz")
        response = connection.getresponse()
        health_payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert health_payload == {"status": "ok", "runtime": "agentic_mesh_v2"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
