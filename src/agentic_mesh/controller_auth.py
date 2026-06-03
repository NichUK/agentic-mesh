from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from threading import Thread
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse
from uuid import uuid4

from agentic_mesh.config import load_mesh_config
from agentic_mesh.models import AuthCredential
from agentic_mesh.models import AuthMethod
from agentic_mesh.models import MeshConfig


@dataclass
class OAuthLoginSession:
    session_id: str
    credential_id: str
    codex_home: Path
    started_at: float
    output: list[str] = field(default_factory=list)
    returncode: int | None = None
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        if self.returncode is None:
            return "running"
        if self.returncode == 0:
            return "completed"
        return "failed"


class ControllerAuthService:
    def __init__(
        self,
        *,
        config_root: Path,
        project_file: str,
        state_root: Path,
    ) -> None:
        self.config_root = config_root
        self.project_file = project_file
        self.state_root = state_root
        self.secret_root = state_root / "secrets"
        self.mount_root = state_root / "worker_mounts"
        self._sessions: dict[str, OAuthLoginSession] = {}
        self._lock = Lock()

    def load_config(self) -> MeshConfig:
        return load_mesh_config(self.config_root, project_file=self.project_file)

    def credential_statuses(self) -> list[dict[str, Any]]:
        mesh_config = self.load_config()
        roles_by_credential: dict[str, list[str]] = {}
        for role_id, role in mesh_config.project.roles.items():
            auth = role.worker.auth
            if auth and auth.credential_ref:
                roles_by_credential.setdefault(auth.credential_ref, []).append(role_id)

        statuses = []
        for credential_id, credential in sorted(
            mesh_config.project.auth_credentials.items()
        ):
            method = mesh_config.auth_methods[credential.method]
            statuses.append(
                self.credential_status(
                    credential,
                    method,
                    roles=sorted(roles_by_credential.get(credential_id, [])),
                )
            )
        return statuses

    def credential_status(
        self,
        credential: AuthCredential,
        method: AuthMethod,
        *,
        roles: list[str] | None = None,
    ) -> dict[str, Any]:
        status = "configured"
        detail = "Credential reference is configured."
        secret_path = (
            self.secret_root / credential.secret_ref
            if credential.secret_ref is not None
            else None
        )
        mount_path = (
            self.mount_root / credential.mount_ref
            if credential.mount_ref is not None
            else None
        )

        if method.requires_secret_ref:
            if secret_path is None or not secret_path.exists():
                status = "missing"
                detail = "Secret file is missing."
            elif not secret_path.read_text(encoding="utf-8").strip():
                status = "missing"
                detail = "Secret file is empty."
            else:
                status = "configured"
                detail = "Secret file exists and is non-empty."
        elif method.method_id == "codex_oauth_cache":
            status, detail = self._codex_oauth_status(credential, mount_path)
        elif method.requires_mount_ref:
            if mount_path is None or not mount_path.exists():
                status = "missing"
                detail = "Credential mount is missing."
            else:
                status = "configured"
                detail = "Credential mount exists."

        return {
            "credential": credential.credential_id,
            "method": credential.method,
            "category": method.category,
            "status": status,
            "detail": detail,
            "secret_ref": credential.secret_ref,
            "mount_ref": credential.mount_ref,
            "roles": roles or [],
            "redacted": True,
        }

    def _codex_oauth_status(
        self,
        credential: AuthCredential,
        mount_path: Path | None,
    ) -> tuple[str, str]:
        if mount_path is None:
            return "missing", "OAuth credential has no mount_ref."
        if not mount_path.exists():
            return "missing", "Codex OAuth CODEX_HOME mount is missing."
        codex_bin = shutil.which("codex")
        if not codex_bin:
            auth_json = mount_path / "auth.json"
            if auth_json.exists():
                return "unknown", "Codex CLI is missing; auth.json exists."
            return "missing", "Codex CLI is missing and no auth.json exists."
        env = os.environ.copy()
        env.update(credential.env)
        env["CODEX_HOME"] = str(mount_path)
        try:
            completed = subprocess.run(
                [codex_bin, "login", "status"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "unknown", "Codex login status timed out."
        output = "\n".join(
            part.strip()
            for part in [completed.stdout, completed.stderr]
            if part and part.strip()
        )
        if completed.returncode == 0:
            return "configured", output or "Codex reports this login is configured."
        return "missing", output or "Codex reports this login is not configured."

    def store_secret(
        self,
        credential_id: str,
        secret_value: str,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        mesh_config = self.load_config()
        credential = self._credential(mesh_config, credential_id)
        method = mesh_config.auth_methods[credential.method]
        if not method.requires_secret_ref or not credential.secret_ref:
            raise ValueError(f"Credential `{credential_id}` does not use secret_ref.")
        secret_value = secret_value.strip()
        if not secret_value:
            raise ValueError("Secret value must not be empty.")
        secret_path = self.secret_root / credential.secret_ref
        if secret_path.exists() and not overwrite:
            raise FileExistsError(
                f"Secret `{credential.secret_ref}` already exists."
            )
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(secret_value, encoding="utf-8")
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
        return {
            "credential": credential.credential_id,
            "method": credential.method,
            "secret_ref": credential.secret_ref,
            "status": "stored",
            "redacted": True,
        }

    def start_codex_oauth_login(self, credential_id: str) -> OAuthLoginSession:
        mesh_config = self.load_config()
        credential = self._credential(mesh_config, credential_id)
        method = mesh_config.auth_methods[credential.method]
        if method.method_id != "codex_oauth_cache" or not credential.mount_ref:
            raise ValueError(
                f"Credential `{credential_id}` is not a codex_oauth_cache credential."
            )
        codex_bin = shutil.which("codex")
        if not codex_bin:
            raise FileNotFoundError("Codex CLI is not installed or not on PATH.")

        codex_home = self.mount_root / credential.mount_ref
        codex_home.mkdir(parents=True, exist_ok=True)
        session = OAuthLoginSession(
            session_id=f"oauth-{uuid4().hex}",
            credential_id=credential.credential_id,
            codex_home=codex_home,
            started_at=time.time(),
        )
        with self._lock:
            self._sessions[session.session_id] = session
        thread = Thread(
            target=self._run_codex_oauth_login,
            args=(session.session_id, credential, codex_bin, codex_home),
            daemon=True,
        )
        thread.start()
        return session

    def _run_codex_oauth_login(
        self,
        session_id: str,
        credential: AuthCredential,
        codex_bin: str,
        codex_home: Path,
    ) -> None:
        env = os.environ.copy()
        env.update(credential.env)
        env["CODEX_HOME"] = str(codex_home)
        try:
            process = subprocess.Popen(
                [codex_bin, "login", "--device-auth"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._append_session_output(session_id, line.rstrip())
            returncode = process.wait()
            with self._lock:
                self._sessions[session_id].returncode = returncode
        except Exception as exc:
            with self._lock:
                session = self._sessions[session_id]
                session.error = str(exc)
                session.returncode = 1

    def _append_session_output(self, session_id: str, line: str) -> None:
        if not line:
            return
        with self._lock:
            session = self._sessions[session_id]
            session.output.append(line)
            if len(session.output) > 200:
                session.output = session.output[-200:]

    def session(self, session_id: str) -> OAuthLoginSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def _credential(
        self,
        mesh_config: MeshConfig,
        credential_id: str,
    ) -> AuthCredential:
        credential = mesh_config.project.auth_credentials.get(credential_id)
        if credential is None:
            raise KeyError(f"Unknown credential `{credential_id}`.")
        return credential


class ControllerAuthServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        handler_class,
        service: ControllerAuthService,
    ):
        super().__init__(server_address, handler_class)
        self.service = service


class ControllerAuthHandler(BaseHTTPRequestHandler):
    server: ControllerAuthServer

    def do_GET(self) -> None:
        path = urlparse(self.path)
        if path.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path.path == "/auth/status.json":
            self._send_json(
                HTTPStatus.OK,
                {"credentials": self.server.service.credential_statuses()},
            )
            return
        if path.path in {"/", "/auth", "/auth/credentials"}:
            self._send_html(HTTPStatus.OK, self._credentials_page())
            return
        if path.path == "/auth/codex/session":
            session_id = parse_qs(path.query).get("id", [""])[0]
            session = self.server.service.session(session_id)
            if session is None:
                self._send_html(
                    HTTPStatus.NOT_FOUND,
                    self._layout("Session Not Found", "<p>Unknown session.</p>"),
                )
                return
            self._send_html(HTTPStatus.OK, self._session_page(session))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path)
        try:
            form = self._read_form()
            if path.path == "/auth/secret":
                result = self.server.service.store_secret(
                    form.get("credential", ""),
                    form.get("secret_value", ""),
                    overwrite=form.get("overwrite") == "yes",
                )
                self._redirect(
                    "/auth/credentials?"
                    + urlencode(
                        {
                            "notice": (
                                f"Stored secret for {result['credential']}."
                            )
                        }
                    )
                )
                return
            if path.path == "/auth/codex/start":
                session = self.server.service.start_codex_oauth_login(
                    form.get("credential", "")
                )
                self._redirect(
                    "/auth/codex/session?" + urlencode({"id": session.session_id})
                )
                return
        except Exception as exc:
            self._send_html(
                HTTPStatus.BAD_REQUEST,
                self._layout(
                    "Auth Action Failed",
                    f"<p>{html.escape(str(exc))}</p><p><a href=\"/auth/credentials\">Back</a></p>",
                ),
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _credentials_page(self) -> str:
        path = urlparse(self.path)
        notice = parse_qs(path.query).get("notice", [""])[0]
        rows = []
        for credential in self.server.service.credential_statuses():
            roles = ", ".join(credential["roles"]) or "No roles"
            action = self._credential_action(credential)
            rows.append(
                "<tr>"
                f"<td><code>{html.escape(credential['credential'])}</code></td>"
                f"<td>{html.escape(credential['method'])}</td>"
                f"<td>{html.escape(credential['status'])}</td>"
                f"<td>{html.escape(credential['detail'])}</td>"
                f"<td>{html.escape(roles)}</td>"
                f"<td>{action}</td>"
                "</tr>"
            )
        notice_html = (
            f"<p class=\"notice\">{html.escape(notice)}</p>" if notice else ""
        )
        body = f"""
{notice_html}
<p>Credential values are never shown. Teams and Slack buttons should link here
for setup and status instead of carrying secrets in chat.</p>
<table>
  <thead>
    <tr>
      <th>Credential</th>
      <th>Method</th>
      <th>Status</th>
      <th>Detail</th>
      <th>Roles</th>
      <th>Action</th>
    </tr>
  </thead>
  <tbody>
    {''.join(rows)}
  </tbody>
</table>
"""
        return self._layout("Agentic Mesh Auth", body)

    def _credential_action(self, credential: dict[str, Any]) -> str:
        credential_id = html.escape(credential["credential"])
        method = credential["method"]
        if method == "codex_oauth_cache":
            return f"""
<form method="post" action="/auth/codex/start">
  <input type="hidden" name="credential" value="{credential_id}">
  <button type="submit">Start Codex OAuth</button>
</form>
"""
        if credential["secret_ref"]:
            return f"""
<form method="post" action="/auth/secret">
  <input type="hidden" name="credential" value="{credential_id}">
  <input type="password" name="secret_value" autocomplete="off" placeholder="Secret value">
  <label><input type="checkbox" name="overwrite" value="yes"> Replace</label>
  <button type="submit">Store Secret</button>
</form>
"""
        return "No setup action"

    def _session_page(self, session: OAuthLoginSession) -> str:
        refresh = (
            '<meta http-equiv="refresh" content="3">'
            if session.status == "running"
            else ""
        )
        output = "\n".join(session.output) or "Waiting for Codex output..."
        body = f"""
{refresh}
<p>Status: <strong>{html.escape(session.status)}</strong></p>
<p>Credential: <code>{html.escape(session.credential_id)}</code></p>
<p>CODEX_HOME: <code>{html.escape(str(session.codex_home))}</code></p>
<pre>{html.escape(output)}</pre>
<p><a href="/auth/credentials">Back to credentials</a></p>
"""
        return self._layout("Codex OAuth Login", body)

    def _layout(self, title: str, body: str) -> str:
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.4; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d4d4d4; padding: 0.5rem; vertical-align: top; }}
    th {{ background: #f4f4f4; text-align: left; }}
    input[type=password] {{ min-width: 18rem; }}
    pre {{ background: #111; color: #eee; padding: 1rem; white-space: pre-wrap; }}
    .notice {{ background: #e9f7ef; border: 1px solid #9bd7ad; padding: 0.75rem; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  {body}
</body>
</html>"""

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, payload: str) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_controller_auth(
    *,
    host: str,
    port: int,
    service: ControllerAuthService,
) -> None:
    server = ControllerAuthServer((host, port), ControllerAuthHandler, service)
    server.serve_forever()
