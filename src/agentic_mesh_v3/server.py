from __future__ import annotations

import base64
from http.cookies import SimpleCookie
import hashlib
import html
import hmac
import re
import os
import time
from dataclasses import dataclass
from dataclasses import asdict
from dataclasses import is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.parse import parse_qs
from urllib.parse import quote
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


@dataclass(frozen=True)
class DashboardAuthConfig:
    enabled: bool = False
    bearer_token: str | None = None
    allowed_users: tuple[str, ...] = ()
    session_secret: str | None = None
    session_ttl_seconds: int = 43200
    trusted_user_headers: tuple[str, ...] = (
        "Cf-Access-Authenticated-User-Email",
        "X-MS-CLIENT-PRINCIPAL-NAME",
        "X-Forwarded-User",
    )


class V3StatusHandler(BaseHTTPRequestHandler):
    db_path: Path
    project_id: str
    document_library: DocumentLibraryAdapter | None = None
    teams_activity_router: TeamsActivityRouter | None = None
    dashboard_auth: DashboardAuthConfig = DashboardAuthConfig()
    dashboard_session_cookie_name: str = "agentic_mesh_dashboard"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json({"status": "ok", "runtime": "agentic_mesh_v3"})
            return
        if path == "/login":
            self._send_dashboard_login_page()
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if not self._authorize_dashboard_request():
            return
        if path in {"/", "/status"}:
            self._send_html(self._render_status())
            return
        if path == "/agents":
            self._send_html(self._render_agents())
            return
        if path == "/agents.json":
            self._send_json_agents()
            return
        if path == "/work-items.json":
            self._send_json_work_items()
            return
        if path == "/status.json":
            self._send_json_snapshot()
            return
        if path.startswith("/work-item/") and path.endswith(".json"):
            work_item_id = unquote(path.removeprefix("/work-item/")[: -len(".json")]).strip("/")
            self._send_json_work_item(work_item_id)
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
        if path == "/login":
            self._handle_dashboard_login()
            return
        if path == "/logout":
            self._handle_dashboard_logout()
            return
        if path == "/teams/activity":
            self._handle_teams_activity()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authorize_dashboard_request(self) -> bool:
        config = self.dashboard_auth
        if not config.enabled:
            return True
        auth_header = self.headers.get("Authorization") or ""
        if config.bearer_token and auth_header.startswith("Bearer "):
            supplied = auth_header.removeprefix("Bearer ").strip()
            if hmac.compare_digest(supplied, config.bearer_token):
                return True
        secret = config.session_secret or config.bearer_token
        cookie_value = self._dashboard_session_cookie_value()
        if secret and cookie_value and _verify_dashboard_session_cookie(cookie_value, secret):
            return True
        allowed_users = {user.casefold() for user in config.allowed_users}
        for header in config.trusted_user_headers:
            user = (self.headers.get(header) or "").strip()
            if not user:
                continue
            if not allowed_users or user.casefold() in allowed_users:
                return True
        self._send_auth_required()
        return False

    def _dashboard_session_cookie_value(self) -> str | None:
        raw_cookie = self.headers.get("Cookie") or ""
        if not raw_cookie:
            return None
        cookie = SimpleCookie(raw_cookie)
        morsel = cookie.get(self.dashboard_session_cookie_name)
        if morsel is None:
            return None
        return morsel.value

    def _send_auth_required(self) -> None:
        config = self.dashboard_auth
        if config.bearer_token:
            next_path = _safe_dashboard_next_path(self.path)
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"/login?next={quote(next_path, safe='/?:=&%')}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = (
            "<!doctype html><html><head><title>Authentication required</title></head>"
            "<body><h1>Authentication required</h1>"
            "<p>The Agentic Mesh dashboard and artifact viewer require an authenticated, authorized user.</p>"
            "<p>No local dashboard login token is configured. Use a trusted identity proxy or set "
            "<code>AGENTIC_MESH_DASHBOARD_AUTH_TOKEN</code>.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("WWW-Authenticate", 'Bearer realm="agentic-mesh-dashboard"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_dashboard_login_page(self, *, error: str | None = None, status: HTTPStatus = HTTPStatus.OK) -> None:
        config = self.dashboard_auth
        if not config.enabled:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/status")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not config.bearer_token:
            self._send_auth_required()
            return
        query = parse_qs(urlparse(self.path).query)
        next_path = _safe_dashboard_next_path((query.get("next") or ["/status"])[0])
        error_html = f"<p><strong>{html.escape(error)}</strong></p>" if error else ""
        body = (
            "<!doctype html><html><head><title>Agentic Mesh login</title></head>"
            "<body><h1>Agentic Mesh Dashboard Login</h1>"
            "<p>Enter the configured dashboard access token to view status pages and artifacts.</p>"
            f"{error_html}"
            '<form method="post" action="/login">'
            f'<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">'
            '<p><label>Access token <input type="password" name="token" autocomplete="current-password" autofocus></label></p>'
            '<p><button type="submit">Sign in</button></p>'
            "</form></body></html>"
        )
        self._send_html(body, status=status)

    def _handle_dashboard_login(self) -> None:
        config = self.dashboard_auth
        if not config.enabled or not config.bearer_token:
            self._send_auth_required()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        fields = parse_qs(body)
        supplied = (fields.get("token") or [""])[0]
        next_path = _safe_dashboard_next_path((fields.get("next") or ["/status"])[0])
        if not hmac.compare_digest(supplied, config.bearer_token):
            self._send_dashboard_login_page(error="Invalid dashboard access token.", status=HTTPStatus.UNAUTHORIZED)
            return
        secret = config.session_secret or config.bearer_token
        cookie_value = _create_dashboard_session_cookie(secret, ttl_seconds=config.session_ttl_seconds)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", next_path)
        self.send_header(
            "Set-Cookie",
            (
                f"{self.dashboard_session_cookie_name}={cookie_value}; "
                f"Max-Age={config.session_ttl_seconds}; Path=/; HttpOnly; SameSite=Lax"
            ),
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_dashboard_logout(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/login")
        self.send_header(
            "Set-Cookie",
            f"{self.dashboard_session_cookie_name}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

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

    def _send_json_agents(self) -> None:
        snapshot = self._snapshot()
        self._send_json(
            {
                "status": "ok",
                "project_id": snapshot.project_id,
                "agents": _jsonable(snapshot.agents),
            }
        )

    def _send_json_work_items(self) -> None:
        snapshot = self._snapshot()
        self._send_json(
            {
                "status": "ok",
                "project_id": snapshot.project_id,
                "backlog": _jsonable(snapshot.backlog),
                "work_items": _jsonable(snapshot.work_items),
                "recent_completions": _jsonable(snapshot.recent_completions),
            }
        )

    def _render_work_item(self, work_item_id: str) -> str:
        db = V3Database(self.db_path)
        try:
            db.migrate()
            detail = db.work_item_detail(work_item_id)
        finally:
            db.close()
        return render_work_item_detail_page(
            detail,
            work_item_id,
            document_framework_id=getattr(self.document_library, "framework_id", "togaf-sdlc-v1"),
        )

    def _send_json_work_item(self, work_item_id: str) -> None:
        db = V3Database(self.db_path)
        try:
            db.migrate()
            detail = db.work_item_detail(work_item_id)
        finally:
            db.close()
        if detail is None:
            self._send_json(
                {
                    "status": "not_found",
                    "work_item_id": work_item_id,
                },
                status=HTTPStatus.NOT_FOUND,
            )
            return
        self._send_json(
            {
                "status": "ok",
                "work_item": _jsonable(detail),
                "document_framework_id": getattr(self.document_library, "framework_id", "togaf-sdlc-v1"),
            }
        )

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
        try:
            exists = adapter.exists(relative_path)
        except Exception as exc:
            self.send_error(HTTPStatus.BAD_GATEWAY, f"document library lookup failed: {exc}")
            return
        if not exists:
            self.send_error(HTTPStatus.NOT_FOUND, f"artifact not found: {relative_path}")
            return
        try:
            content = adapter.read_text(relative_path)
        except Exception as exc:
            self.send_error(HTTPStatus.BAD_GATEWAY, f"document library read failed: {exc}")
            return
        self._send_html(_artifact_page(relative_path, content))

    def _send_json_snapshot(self) -> None:
        snapshot = self._snapshot()
        self._send_json(
            {
                "project_id": snapshot.project_id,
                "backlog": _jsonable(snapshot.backlog),
                "work_items": _jsonable(snapshot.work_items),
                "agents": _jsonable(snapshot.agents),
                "recent_completions": _jsonable(snapshot.recent_completions),
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

    def _send_json(self, payload: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        import json

        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
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
    dashboard_auth: DashboardAuthConfig | None = None,
) -> None:
    class Handler(V3StatusHandler):
        pass

    Handler.db_path = db_path
    Handler.project_id = project_id
    Handler.document_library = document_library
    Handler.teams_activity_router = teams_activity_router
    Handler.dashboard_auth = dashboard_auth or dashboard_auth_config_from_env()
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


def dashboard_auth_config_from_env(env: dict[str, str] | None = None) -> DashboardAuthConfig:
    source = env if env is not None else os.environ
    enabled_raw = (source.get("AGENTIC_MESH_DASHBOARD_AUTH_ENABLED") or "").strip().casefold()
    bearer_token = (source.get("AGENTIC_MESH_DASHBOARD_AUTH_TOKEN") or "").strip() or None
    allowed_users = tuple(
        user.strip()
        for user in (source.get("AGENTIC_MESH_DASHBOARD_ALLOWED_USERS") or "").split(",")
        if user.strip()
    )
    header_names = tuple(
        header.strip()
        for header in (source.get("AGENTIC_MESH_DASHBOARD_USER_HEADERS") or "").split(",")
        if header.strip()
    )
    session_secret = (source.get("AGENTIC_MESH_DASHBOARD_SESSION_SECRET") or "").strip() or None
    try:
        session_ttl_seconds = int((source.get("AGENTIC_MESH_DASHBOARD_SESSION_TTL_SECONDS") or "").strip() or "43200")
    except ValueError:
        session_ttl_seconds = 43200
    session_ttl_seconds = max(60, session_ttl_seconds)
    enabled = enabled_raw in {"1", "true", "yes", "on"} or bool(bearer_token or allowed_users)
    return DashboardAuthConfig(
        enabled=enabled,
        bearer_token=bearer_token,
        allowed_users=allowed_users,
        session_secret=session_secret,
        session_ttl_seconds=session_ttl_seconds,
        trusted_user_headers=header_names or DashboardAuthConfig().trusted_user_headers,
    )


def _create_dashboard_session_cookie(secret: str, *, ttl_seconds: int, now: float | None = None) -> str:
    expires_at = int((now if now is not None else time.time()) + ttl_seconds)
    payload = f"dashboard:{expires_at}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii")


def _verify_dashboard_session_cookie(value: str, secret: str, *, now: float | None = None) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        scope, expires_raw, signature = decoded.split(":", 2)
        expires_at = int(expires_raw)
    except (ValueError, UnicodeDecodeError):
        return False
    if scope != "dashboard":
        return False
    if expires_at < int(now if now is not None else time.time()):
        return False
    payload = f"{scope}:{expires_at}"
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _safe_dashboard_next_path(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return "/status"
    path = parsed.path or "/status"
    if not path.startswith("/") or path.startswith("//"):
        return "/status"
    if path in {"/login", "/logout"}:
        return "/status"
    return path + (f"?{parsed.query}" if parsed.query else "")


def _teams_activity_response(activity: dict[str, Any], router: TeamsActivityRouter) -> dict[str, object]:
    subjects = router.route_activity(activity)
    return {"status": "routed", "subjects": subjects}


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(raw) for key, raw in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(raw) for raw in value]
    return value


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
