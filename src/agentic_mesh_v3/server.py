from __future__ import annotations

import html
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlparse

import bleach
import markdown

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.reporting import artifact_viewer_path
from agentic_mesh_v3.reporting import render_agents_page
from agentic_mesh_v3.reporting import render_status_page
from agentic_mesh_v3.reporting import render_work_item_detail_page
from agentic_mesh_v3.teams_ingress import TeamsActivityRouter


class V3StatusHandler(BaseHTTPRequestHandler):
    db_path: Path
    project_id: str
    document_library: DocumentLibraryAdapter | None = None
    teams_activity_router: TeamsActivityRouter | None = None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/status"}:
            self._send_html(self._render_status())
            return
        if path == "/agents":
            self._send_html(self._render_agents())
            return
        if path == "/status.json":
            self._send_json_snapshot()
            return
        if path == "/healthz":
            self._send_json({"status": "ok", "runtime": "agentic_mesh_v3"})
            return
        if path.startswith("/work-item/"):
            work_item_id = unquote(path.removeprefix("/work-item/")).strip("/")
            self._send_html(self._render_work_item(work_item_id))
            return
        if path.startswith("/artifact-viewer/"):
            self._render_artifact_route(path.removeprefix("/artifact-viewer/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/teams/activity":
            self._handle_teams_activity()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _snapshot(self):
        db = V3Database(self.db_path)
        try:
            db.migrate()
            return db.status_snapshot(project_id=self.project_id)
        finally:
            db.close()

    def _render_status(self) -> str:
        return render_status_page(self._snapshot())

    def _render_agents(self) -> str:
        return render_agents_page(self._snapshot())

    def _render_work_item(self, work_item_id: str) -> str:
        db = V3Database(self.db_path)
        try:
            db.migrate()
            detail = db.work_item_detail(work_item_id)
        finally:
            db.close()
        return render_work_item_detail_page(detail, work_item_id)

    def _render_artifact_route(self, route_path: str) -> None:
        parts = [part for part in unquote(route_path).split("/") if part]
        if len(parts) < 2:
            self.send_error(HTTPStatus.NOT_FOUND, "artifact route requires work item id and filename")
            return
        work_item_id = parts[0]
        filename = "/".join(parts[1:])
        db = V3Database(self.db_path)
        try:
            db.migrate()
            artifact = db.artifact_for(work_item_id, Path(filename).name)
        finally:
            db.close()
        relative_path = artifact["relative_path"] if artifact else artifact_viewer_path(work_item_id, filename)
        adapter = self.document_library
        if adapter is None:
            self.send_error(HTTPStatus.NOT_FOUND, "document library root is not configured")
            return
        if not adapter.exists(relative_path):
            self.send_error(HTTPStatus.NOT_FOUND, f"artifact not found: {relative_path}")
            return
        content = adapter.read_text(relative_path)
        self._send_html(_artifact_page(relative_path, content))

    def _send_json_snapshot(self) -> None:
        snapshot = self._snapshot()
        self._send_json(
            {
                "project_id": snapshot.project_id,
                "backlog": [item.__dict__ for item in snapshot.backlog],
                "work_items": [item.__dict__ for item in snapshot.work_items],
                "agents": [item.__dict__ for item in snapshot.agents],
                "recent_completions": [item.__dict__ for item in snapshot.recent_completions],
            }
        )

    def _handle_teams_activity(self) -> None:
        router = self.teams_activity_router
        if router is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Teams activity router is not configured")
            return
        try:
            payload = self._read_json_body()
            self._send_json(_teams_activity_response(payload, router))
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def _read_json_body(self) -> dict[str, Any]:
        import json

        length = int(self.headers.get("Content-Length") or 0)
        if length < 1:
            raise ValueError("JSON body is required")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _send_json(self, payload: dict[str, object]) -> None:
        import json

        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(
    *,
    db_path: Path,
    project_id: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    document_library: DocumentLibraryAdapter | None = None,
    teams_activity_router: TeamsActivityRouter | None = None,
) -> None:
    class Handler(V3StatusHandler):
        pass

    Handler.db_path = db_path
    Handler.project_id = project_id
    Handler.document_library = document_library
    Handler.teams_activity_router = teams_activity_router
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


def _teams_activity_response(activity: dict[str, Any], router: TeamsActivityRouter) -> dict[str, object]:
    subjects = router.route_activity(activity)
    return {"status": "routed", "subjects": subjects}


def _artifact_page(relative_path: str, content: str) -> str:
    content = _strip_raw_script_blocks(content)
    rendered = markdown.markdown(content, extensions=["tables", "fenced_code"])
    rendered, has_mermaid = _promote_mermaid_blocks(rendered)
    safe = bleach.clean(
        rendered,
        tags={
            "a",
            "blockquote",
            "br",
            "code",
            "em",
            "h1",
            "h2",
            "h3",
            "h4",
            "hr",
            "li",
            "ol",
            "p",
            "pre",
            "strong",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "ul",
        },
        attributes={"a": ["href", "title"], "pre": ["class"], "code": ["class"]},
    )
    mermaid_loader = (
        "<script type=\"module\">"
        "import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';"
        "mermaid.initialize({startOnLoad:true,securityLevel:'strict'});"
        "</script>"
        if has_mermaid
        else ""
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(relative_path)}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.45;max-width:72rem}"
        "pre{background:#f3f4f6;padding:1rem;overflow:auto}"
        "pre.mermaid{background:#fff;border:1px solid #d1d5db}"
        "code{background:#f3f4f6;padding:.05rem .2rem}"
        "table{border-collapse:collapse}th,td{border:1px solid #d1d5db;padding:.35rem}</style>"
        "</head><body>"
        f"<p><a href=\"/status\">Status</a></p><h1>{html.escape(relative_path)}</h1>{safe}{mermaid_loader}</body></html>"
    )


def _promote_mermaid_blocks(rendered_markdown: str) -> tuple[str, bool]:
    pattern = re.compile(
        r"<pre><code class=\"language-mermaid\">(?P<body>.*?)</code></pre>",
        re.DOTALL,
    )

    def replace(match: re.Match[str]) -> str:
        body = html.unescape(match.group("body")).strip()
        return f"<pre class=\"mermaid\">{html.escape(body)}</pre>"

    promoted, count = pattern.subn(replace, rendered_markdown)
    return promoted, count > 0


def _strip_raw_script_blocks(content: str) -> str:
    return re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", "", content)
