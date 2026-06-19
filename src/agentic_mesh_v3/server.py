from __future__ import annotations

import base64
from http.cookies import SimpleCookie
import hashlib
import html
import hmac
import json
import re
import os
import secrets
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
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen

import bleach
import markdown

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.document_links import render_document_markdown_links
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.reporting import artifact_viewer_path
from agentic_mesh_v3.reporting import render_agents_page
from agentic_mesh_v3.reporting import render_status_page
from agentic_mesh_v3.reporting import render_work_item_detail_page
from agentic_mesh_v3.teams_ingress import TeamsActivityRouter


@dataclass(frozen=True)
class DashboardAuthConfig:
    enabled: bool = False
    auth_mode: str = "proxy"
    bearer_token: str | None = None
    allowed_users: tuple[str, ...] = ()
    session_secret: str | None = None
    session_ttl_seconds: int = 43200
    entra_client_id: str | None = None
    entra_tenant_id: str | None = None
    entra_scopes: str = "openid profile email User.Read"
    entra_flow: str = "auth_code_pkce"
    entra_redirect_uri: str | None = None
    entra_client_secret: str | None = None
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
    dashboard_oauth_cookie_name: str = "agentic_mesh_dashboard_oauth"
    configured_role_instance_ids: tuple[str, ...] = ()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json({"status": "ok", "runtime": "agentic_mesh_v3"})
            return
        if path == "/login":
            self._send_dashboard_login_page()
            return
        if path == "/auth/entra/device":
            self._handle_entra_device_poll()
            return
        if path == "/auth/entra/callback":
            self._handle_entra_auth_code_callback()
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
        if config.auth_mode == "token" and config.bearer_token and auth_header.startswith("Bearer "):
            supplied = auth_header.removeprefix("Bearer ").strip()
            if hmac.compare_digest(supplied, config.bearer_token):
                return True
        secret = _dashboard_session_secret(config)
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
        if (config.auth_mode == "token" and config.bearer_token) or (
            config.auth_mode == "entra" and config.entra_client_id
        ):
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
        if config.auth_mode == "entra":
            if config.entra_flow == "device_code":
                self._start_entra_device_login()
            else:
                self._start_entra_auth_code_login()
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
        if not config.enabled or config.auth_mode != "token" or not config.bearer_token:
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

    def _start_entra_auth_code_login(self) -> None:
        config = self.dashboard_auth
        if not config.entra_client_id or not config.entra_tenant_id or not config.entra_redirect_uri:
            self._send_auth_required()
            return
        query = parse_qs(urlparse(self.path).query)
        next_path = _safe_dashboard_next_path((query.get("next") or ["/status"])[0])
        state = secrets.token_urlsafe(24)
        verifier = _pkce_code_verifier()
        login_state = {
            "provider": "entra-auth-code",
            "state": state,
            "code_verifier": verifier,
            "next": next_path,
        }
        cookie_value = _create_signed_dashboard_payload(
            login_state,
            _dashboard_session_secret(config),
            ttl_seconds=900,
        )
        authorize_url = _entra_authorize_url(config, state=state, code_verifier=verifier)
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", authorize_url)
        self.send_header(
            "Set-Cookie",
            f"{self.dashboard_oauth_cookie_name}={cookie_value}; Max-Age=900; Path=/; HttpOnly; SameSite=Lax",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_entra_auth_code_callback(self) -> None:
        config = self.dashboard_auth
        query = parse_qs(urlparse(self.path).query)
        if query.get("error"):
            description = (query.get("error_description") or query.get("error") or ["Entra login failed"])[0]
            self._send_html(
                "<!doctype html><html><head><title>Agentic Mesh login failed</title></head>"
                "<body><h1>Microsoft sign-in failed</h1>"
                f"<p>{html.escape(description)}</p>"
                '<p><a href="/login">Start again</a></p>'
                "</body></html>",
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        cookie_value = _cookie_value(self.headers.get("Cookie") or "", self.dashboard_oauth_cookie_name)
        state_payload = _verify_signed_dashboard_payload(cookie_value or "", _dashboard_session_secret(config))
        supplied_state = (query.get("state") or [""])[0]
        if (
            not state_payload
            or state_payload.get("provider") != "entra-auth-code"
            or not hmac.compare_digest(str(state_payload.get("state") or ""), supplied_state)
        ):
            self._send_html(
                "<!doctype html><html><head><title>Agentic Mesh login failed</title></head>"
                "<body><h1>Microsoft sign-in failed</h1><p>Login state was missing or invalid.</p>"
                '<p><a href="/login">Start again</a></p></body></html>',
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        code = (query.get("code") or [""])[0]
        if not code:
            self._send_html(
                "<!doctype html><html><head><title>Agentic Mesh login failed</title></head>"
                "<body><h1>Microsoft sign-in failed</h1><p>No authorization code was returned.</p>"
                '<p><a href="/login">Start again</a></p></body></html>',
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        try:
            token_response = _exchange_entra_auth_code_token(
                config,
                code=code,
                code_verifier=str(state_payload["code_verifier"]),
            )
            self._complete_entra_login(
                token_response,
                _safe_dashboard_next_path(str(state_payload.get("next") or "/status")),
            )
        except Exception as exc:
            self._send_html(
                "<!doctype html><html><head><title>Agentic Mesh login failed</title></head>"
                "<body><h1>Microsoft sign-in failed</h1>"
                f"<p>{html.escape(str(exc))}</p>"
                '<p><a href="/login">Start again</a></p></body></html>',
                status=HTTPStatus.BAD_GATEWAY,
            )

    def _start_entra_device_login(self, *, error: str | None = None) -> None:
        config = self.dashboard_auth
        if not config.entra_client_id or not config.entra_tenant_id:
            self._send_auth_required()
            return
        query = parse_qs(urlparse(self.path).query)
        next_path = _safe_dashboard_next_path((query.get("next") or ["/status"])[0])
        try:
            device = _request_entra_device_code(config)
        except Exception as exc:
            self._send_html(
                "<!doctype html><html><head><title>Agentic Mesh login</title></head>"
                "<body><h1>Agentic Mesh Entra Login</h1>"
                f"<p>Unable to start Entra device-code login: {html.escape(str(exc))}</p>"
                "</body></html>",
                status=HTTPStatus.BAD_GATEWAY,
            )
            return
        expires_at = int(time.time()) + int(device.get("expires_in") or 900)
        state = {
            "provider": "entra-device-code",
            "device_code": str(device["device_code"]),
            "next": next_path,
            "expires_at": expires_at,
        }
        cookie_value = _create_signed_dashboard_payload(
            state,
            _dashboard_session_secret(config),
            ttl_seconds=int(device.get("expires_in") or 900),
        )
        verification_uri = str(device.get("verification_uri") or device.get("verification_url") or "")
        user_code = str(device.get("user_code") or "")
        interval = max(5, int(device.get("interval") or 5))
        message = str(device.get("message") or "")
        error_html = f"<p><strong>{html.escape(error)}</strong></p>" if error else ""
        body = (
            "<!doctype html><html><head><title>Agentic Mesh Entra Login</title>"
            f'<meta http-equiv="refresh" content="{interval}; url=/auth/entra/device">'
            "</head><body><h1>Agentic Mesh Entra Login</h1>"
            "<p>Sign in with Microsoft Entra to access the Agentic Mesh dashboard and artifacts.</p>"
            f"{error_html}"
            f"<p>Open <a href=\"{html.escape(verification_uri, quote=True)}\" target=\"_blank\" rel=\"noopener noreferrer\">"
            f"{html.escape(verification_uri)}</a> and enter this code:</p>"
            f"<p><code>{html.escape(user_code)}</code></p>"
            f"<p>{html.escape(message)}</p>"
            "<p>This page will continue automatically after sign-in completes.</p>"
            "</body></html>"
        )
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header(
            "Set-Cookie",
            (
                f"{self.dashboard_oauth_cookie_name}={cookie_value}; "
                f"Max-Age={int(device.get('expires_in') or 900)}; Path=/; HttpOnly; SameSite=Lax"
            ),
        )
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _handle_entra_device_poll(self) -> None:
        config = self.dashboard_auth
        secret = _dashboard_session_secret(config)
        cookie_value = _cookie_value(self.headers.get("Cookie") or "", self.dashboard_oauth_cookie_name)
        state = _verify_signed_dashboard_payload(cookie_value or "", secret)
        if not state or state.get("provider") != "entra-device-code":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if int(state.get("expires_at") or 0) < int(time.time()):
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/login")
            self.send_header(
                "Set-Cookie",
                f"{self.dashboard_oauth_cookie_name}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            token_response = _poll_entra_device_token(config, str(state["device_code"]))
        except _EntraAuthorizationPending:
            next_path = _safe_dashboard_next_path(str(state.get("next") or "/status"))
            body = (
                "<!doctype html><html><head><title>Agentic Mesh login pending</title>"
                '<meta http-equiv="refresh" content="5; url=/auth/entra/device"></head>'
                "<body><h1>Waiting for Microsoft sign-in</h1>"
                "<p>Complete the device-code sign-in in the Microsoft page. This page will refresh automatically.</p>"
                f"<p>After sign-in you will continue to <code>{html.escape(next_path)}</code>.</p>"
                "</body></html>"
            )
            self._send_html(body)
            return
        except Exception as exc:
            self._send_html(
                "<!doctype html><html><head><title>Agentic Mesh login failed</title></head>"
                "<body><h1>Microsoft sign-in failed</h1>"
                f"<p>{html.escape(str(exc))}</p>"
                '<p><a href="/login">Start again</a></p>'
                "</body></html>",
                status=HTTPStatus.BAD_GATEWAY,
            )
            return
        self._complete_entra_login(token_response, _safe_dashboard_next_path(str(state.get("next") or "/status")))

    def _complete_entra_login(self, token_response: dict[str, Any], next_path: str) -> None:
        config = self.dashboard_auth
        claims = _decode_jwt_payload(str(token_response.get("id_token") or ""))
        _validate_entra_id_token_claims(claims, config)
        user = _dashboard_user_from_entra_claims(claims)
        if not _dashboard_user_is_allowed(user, config):
            self._send_html(
                "<!doctype html><html><head><title>Agentic Mesh access denied</title></head>"
                "<body><h1>Access denied</h1>"
                "<p>Your Microsoft Entra account is authenticated but is not authorized for this Agentic Mesh dashboard.</p>"
                "</body></html>",
                status=HTTPStatus.FORBIDDEN,
            )
            return
        cookie_value = _create_dashboard_session_cookie(
            _dashboard_session_secret(config),
            ttl_seconds=config.session_ttl_seconds,
            user=user,
        )
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", next_path)
        self.send_header(
            "Set-Cookie",
            (
                f"{self.dashboard_session_cookie_name}={cookie_value}; "
                f"Max-Age={config.session_ttl_seconds}; Path=/; HttpOnly; SameSite=Lax"
            ),
        )
        self.send_header(
            "Set-Cookie",
            f"{self.dashboard_oauth_cookie_name}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
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
            return db.status_snapshot(
                project_id=self.project_id,
                configured_role_instance_ids=self.configured_role_instance_ids,
            )
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
        relative_path: str | None = None
        if parts and parts[0] == "work-items":
            relative_path = _safe_artifact_relative_path("/".join(parts))
        elif len(parts) >= 2:
            work_item_id = parts[0]
            filename = "/".join(parts[1:])
            db = V3Database(self.db_path)
            try:
                db.migrate()
                artifact = db.artifact_for(work_item_id, Path(filename).name)
            finally:
                db.close()
            relative_path = artifact["relative_path"] if artifact else artifact_viewer_path(work_item_id, filename)
            relative_path = _safe_artifact_relative_path(relative_path)
        if relative_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "artifact not found")
            return
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
    configured_role_instance_ids: tuple[str, ...] = (),
) -> None:
    class Handler(V3StatusHandler):
        pass

    Handler.db_path = db_path
    Handler.project_id = project_id
    Handler.document_library = document_library
    Handler.teams_activity_router = teams_activity_router
    Handler.dashboard_auth = dashboard_auth or dashboard_auth_config_from_env()
    Handler.configured_role_instance_ids = configured_role_instance_ids
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


def dashboard_auth_config_from_env(env: dict[str, str] | None = None) -> DashboardAuthConfig:
    source = env if env is not None else os.environ
    enabled_raw = (source.get("AGENTIC_MESH_DASHBOARD_AUTH_ENABLED") or "").strip().casefold()
    auth_mode = (source.get("AGENTIC_MESH_DASHBOARD_AUTH_MODE") or "").strip().casefold()
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
    entra_client_id = (
        source.get("AGENTIC_MESH_DASHBOARD_ENTRA_CLIENT_ID")
        or source.get("AGENTIC_MESH_GRAPH_CLIENT_ID")
        or ""
    ).strip() or None
    entra_tenant_id = (
        source.get("AGENTIC_MESH_DASHBOARD_ENTRA_TENANT_ID")
        or source.get("AGENTIC_MESH_GRAPH_TENANT_ID")
        or source.get("AGENTIC_MESH_TENANT_ID")
        or ""
    ).strip() or None
    entra_scopes = (
        source.get("AGENTIC_MESH_DASHBOARD_ENTRA_SCOPES")
        or "openid profile email User.Read"
    ).strip()
    public_url_root = (source.get("AGENTIC_MESH_URL_ROOT") or "").strip().rstrip("/")
    entra_flow = (source.get("AGENTIC_MESH_DASHBOARD_ENTRA_FLOW") or "auth_code_pkce").strip().casefold()
    entra_redirect_uri = (source.get("AGENTIC_MESH_DASHBOARD_ENTRA_REDIRECT_URI") or "").strip()
    if not entra_redirect_uri and public_url_root:
        entra_redirect_uri = f"{public_url_root}/auth/entra/callback"
    entra_client_secret = (source.get("AGENTIC_MESH_DASHBOARD_ENTRA_CLIENT_SECRET") or "").strip() or None
    if not auth_mode:
        auth_mode = "entra" if entra_client_id and entra_tenant_id else "token" if bearer_token else "proxy"
    enabled = enabled_raw in {"1", "true", "yes", "on"} or bool(bearer_token or allowed_users)
    enabled = enabled or (auth_mode == "entra" and bool(entra_client_id and entra_tenant_id))
    return DashboardAuthConfig(
        enabled=enabled,
        auth_mode=auth_mode,
        bearer_token=bearer_token,
        allowed_users=allowed_users,
        session_secret=session_secret,
        session_ttl_seconds=session_ttl_seconds,
        entra_client_id=entra_client_id,
        entra_tenant_id=entra_tenant_id,
        entra_scopes=entra_scopes,
        entra_flow=entra_flow,
        entra_redirect_uri=entra_redirect_uri or None,
        entra_client_secret=entra_client_secret,
        trusted_user_headers=header_names or DashboardAuthConfig().trusted_user_headers,
    )


def _dashboard_session_secret(config: DashboardAuthConfig) -> str:
    return config.session_secret or config.bearer_token or config.entra_client_id or "agentic-mesh-dashboard"


def _create_dashboard_session_cookie(
    secret: str,
    *,
    ttl_seconds: int,
    now: float | None = None,
    user: dict[str, object] | None = None,
) -> str:
    expires_at = int((now if now is not None else time.time()) + ttl_seconds)
    payload: dict[str, object] = {"scope": "dashboard", "expires_at": expires_at}
    if user:
        payload["user"] = user
    return _create_signed_dashboard_payload(payload, secret, ttl_seconds=ttl_seconds, now=now)


def _verify_dashboard_session_cookie(value: str, secret: str, *, now: float | None = None) -> bool:
    payload = _verify_signed_dashboard_payload(value, secret, now=now)
    return bool(payload and payload.get("scope") == "dashboard")


def _create_signed_dashboard_payload(
    payload: dict[str, object],
    secret: str,
    *,
    ttl_seconds: int,
    now: float | None = None,
) -> str:
    if "expires_at" not in payload:
        payload = dict(payload)
        payload["expires_at"] = int((now if now is not None else time.time()) + ttl_seconds)
    body = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _verify_signed_dashboard_payload(value: str, secret: str, *, now: float | None = None) -> dict[str, object] | None:
    try:
        body, signature = value.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expires_at = int(payload.get("expires_at") or 0)
    if expires_at < int(now if now is not None else time.time()):
        return None
    return payload


class _EntraAuthorizationPending(Exception):
    pass


def _request_entra_device_code(config: DashboardAuthConfig) -> dict[str, Any]:
    return _post_entra_form(
        config,
        "devicecode",
        {
            "client_id": config.entra_client_id or "",
            "scope": config.entra_scopes,
        },
    )


def _entra_authorize_url(config: DashboardAuthConfig, *, state: str, code_verifier: str) -> str:
    tenant = quote(config.entra_tenant_id or "common", safe="")
    params = {
        "client_id": config.entra_client_id or "",
        "response_type": "code",
        "redirect_uri": config.entra_redirect_uri or "",
        "response_mode": "query",
        "scope": config.entra_scopes,
        "state": state,
        "code_challenge": _pkce_code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{urlencode(params)}"


def _exchange_entra_auth_code_token(
    config: DashboardAuthConfig,
    *,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    payload = {
        "client_id": config.entra_client_id or "",
        "scope": config.entra_scopes,
        "code": code,
        "redirect_uri": config.entra_redirect_uri or "",
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    if config.entra_client_secret:
        payload["client_secret"] = config.entra_client_secret
    try:
        return _post_entra_form(config, "token", payload)
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise
        raise RuntimeError(str(body.get("error_description") or body.get("error") or exc)) from exc


def _poll_entra_device_token(config: DashboardAuthConfig, device_code: str) -> dict[str, Any]:
    try:
        return _post_entra_form(
            config,
            "token",
            {
                "client_id": config.entra_client_id or "",
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
        )
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise
        if body.get("error") == "authorization_pending":
            raise _EntraAuthorizationPending(str(body.get("error_description") or "authorization pending")) from exc
        raise RuntimeError(str(body.get("error_description") or body.get("error") or exc)) from exc


def _post_entra_form(config: DashboardAuthConfig, endpoint: str, payload: dict[str, str]) -> dict[str, Any]:
    tenant = quote(config.entra_tenant_id or "common", safe="")
    body = urlencode(payload).encode("utf-8")
    request = Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{endpoint}",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("Entra token endpoint returned a non-object response")
    return parsed


def _pkce_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def _pkce_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        padded = payload + ("=" * (-len(payload) % 4))
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Entra response did not include a readable ID token") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Entra ID token payload was not an object")
    return parsed


def _validate_entra_id_token_claims(claims: dict[str, Any], config: DashboardAuthConfig) -> None:
    if claims.get("aud") != config.entra_client_id:
        raise RuntimeError("Entra ID token audience does not match the dashboard app registration")
    if config.entra_tenant_id and claims.get("tid") != config.entra_tenant_id:
        raise RuntimeError("Entra ID token tenant does not match the configured tenant")
    if int(claims.get("exp") or 0) < int(time.time()):
        raise RuntimeError("Entra ID token is expired")


def _dashboard_user_from_entra_claims(claims: dict[str, Any]) -> dict[str, object]:
    return {
        "provider": "entra",
        "tenant_id": claims.get("tid") or "",
        "object_id": claims.get("oid") or claims.get("sub") or "",
        "subject": claims.get("sub") or "",
        "name": claims.get("name") or "",
        "username": claims.get("preferred_username") or claims.get("email") or claims.get("upn") or "",
        "roles": claims.get("roles") if isinstance(claims.get("roles"), list) else [],
    }


def _dashboard_user_is_allowed(user: dict[str, object], config: DashboardAuthConfig) -> bool:
    if not config.allowed_users:
        return True
    allowed = {raw.casefold() for raw in config.allowed_users}
    candidates = {
        str(user.get("username") or "").casefold(),
        str(user.get("object_id") or "").casefold(),
        str(user.get("subject") or "").casefold(),
    }
    return bool(allowed.intersection(candidates))


def _cookie_value(raw_cookie: str, name: str) -> str | None:
    if not raw_cookie:
        return None
    cookie = SimpleCookie(raw_cookie)
    morsel = cookie.get(name)
    if morsel is None:
        return None
    return morsel.value


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


_UNSAFE_ARTIFACT_ROUTE_PARTS = {
    ".ssh",
    "credential",
    "credentials",
    "oauth",
    "private",
    "secret",
    "secrets",
    "state",
    "token",
    "tokens",
}


def _safe_artifact_relative_path(raw_path: str) -> str | None:
    clean = unquote(str(raw_path)).replace("\\", "/").lstrip("/")
    if "\x00" in clean:
        return None
    parts = [part for part in clean.split("/") if part]
    if len(parts) < 3 or parts[0] != "work-items":
        return None
    lowered = {part.casefold() for part in parts}
    if any(part in {".", ".."} for part in parts) or lowered & _UNSAFE_ARTIFACT_ROUTE_PARTS:
        return None
    return "/".join(parts)


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
    content = render_document_markdown_links(content, source_path=relative_path).content
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
