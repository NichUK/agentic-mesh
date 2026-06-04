from __future__ import annotations

import html
import json
import os
import re
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
from urllib.parse import quote
from urllib.parse import unquote
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
    login_url: str | None = None
    user_code: str | None = None

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
            return "missing", "OpenAI sign-in has not been completed."
        codex_bin = shutil.which("codex")
        if not codex_bin:
            auth_json = mount_path / "auth.json"
            if auth_json.exists():
                return "unknown", "OpenAI sign-in appears to be configured."
            return "missing", "OpenAI sign-in has not been completed."
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
            return "configured", output or "OpenAI sign-in is configured."
        return "missing", output or "OpenAI sign-in has not been completed."

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
            url = _first_url(line)
            if url:
                session.login_url = url
            code = _first_device_code(line)
            if code:
                session.user_code = code
            if len(session.output) > 200:
                session.output = session.output[-200:]

    def session(self, session_id: str) -> OAuthLoginSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def session_payload(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            return {
                "session_id": session.session_id,
                "credential_id": session.credential_id,
                "status": session.status,
                "login_url": session.login_url,
                "user_code": session.user_code,
                "output": "\n".join(session.output),
                "error": session.error,
                "redacted": True,
            }

    def work_item_status(self, work_item_id: str) -> dict[str, Any]:
        mesh_config = self.load_config()
        project_id = mesh_config.project.project_id
        events = [
            event
            for event in self._journal_events(project_id)
            if event.get("work_item_id") == work_item_id
        ]
        queue_entries = self._queue_entries(project_id, work_item_id)
        latest_completed_by_message = {
            event.get("message_id"): event
            for event in events
            if event.get("event_type") == "work_completed" and event.get("message_id")
        }
        active_claims = [
            entry
            for entry in queue_entries
            if entry["queue_state"] == "claimed"
            and entry["message_id"] not in latest_completed_by_message
        ]
        pending = [
            entry for entry in queue_entries if entry["queue_state"] == "pending"
        ]

        current = self._current_work_item_state(events, active_claims, pending)
        artifacts = sorted(
            {
                str(event.get("path"))
                for event in events
                if event.get("event_type") == "documentation_updated"
                and event.get("path")
            }
        )
        for event in events:
            for path in event.get("artifact_paths") or []:
                if path:
                    artifacts.append(str(path))
        teams_messages = [
            {
                "timestamp": event.get("timestamp"),
                "channel": event.get("channel"),
                "message_type": event.get("message_type"),
                "teams_activity_id": event.get("teams_activity_id"),
                "teams_conversation_id": event.get("teams_conversation_id"),
            }
            for event in events
            if event.get("teams_activity_id")
        ]
        return {
            "project_id": project_id,
            "work_item_id": work_item_id,
            "status": current["status"],
            "current": current,
            "counts": {
                "events": len(events),
                "pending": len(pending),
                "claimed": len(active_claims),
                "completed": len(
                    [
                        event
                        for event in events
                        if event.get("event_type") == "work_completed"
                    ]
                ),
            },
            "queue_entries": queue_entries,
            "artifacts": sorted(set(artifacts)),
            "teams_messages": teams_messages,
            "timeline": events,
        }

    def _journal_events(self, project_id: str) -> list[dict[str, Any]]:
        path = self.state_root / "projects" / project_id / "journal" / "events.jsonl"
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _queue_entries(self, project_id: str, work_item_id: str) -> list[dict[str, Any]]:
        queue_root = self.state_root / "projects" / project_id / "queues"
        if not queue_root.exists():
            return []
        entries: list[dict[str, Any]] = []
        for role_dir in sorted(path for path in queue_root.iterdir() if path.is_dir()):
            role_id = role_dir.name
            for queue_state in ["pending", "completed"]:
                for path in sorted((role_dir / queue_state).glob("*.json")):
                    entry = self._queue_entry(path, role_id, queue_state, work_item_id)
                    if entry:
                        entries.append(entry)
            for path in sorted((role_dir / "claimed").glob("*/*.json")):
                entry = self._queue_entry(path, role_id, "claimed", work_item_id)
                if entry:
                    entries.append(entry)
        return entries

    def _queue_entry(
        self,
        path: Path,
        role_id: str,
        queue_state: str,
        work_item_id: str,
    ) -> dict[str, Any] | None:
        with path.open("r", encoding="utf-8") as handle:
            message = json.load(handle)
        payload = message.get("payload") or {}
        if payload.get("work_item_id") != work_item_id:
            return None
        return {
            "role_id": role_id,
            "queue_state": queue_state,
            "message_id": message.get("message_id"),
            "message_type": message.get("type"),
            "lifecycle_state": payload.get("lifecycle_state"),
            "created_at": message.get("created_at"),
            "claimed_at": message.get("claimed_at"),
            "claimed_by": message.get("claimed_by"),
            "path": str(path),
        }

    @staticmethod
    def _current_work_item_state(
        events: list[dict[str, Any]],
        active_claims: list[dict[str, Any]],
        pending: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if active_claims:
            claim = sorted(
                active_claims,
                key=lambda entry: str(entry.get("claimed_at") or ""),
            )[-1]
            return {
                "status": "running",
                "role_id": claim.get("role_id"),
                "role_instance_id": claim.get("claimed_by"),
                "lifecycle_state": claim.get("lifecycle_state"),
                "message_id": claim.get("message_id"),
                "since": claim.get("claimed_at"),
            }
        if pending:
            next_item = sorted(
                pending,
                key=lambda entry: str(entry.get("created_at") or ""),
            )[0]
            return {
                "status": "pending",
                "role_id": next_item.get("role_id"),
                "lifecycle_state": next_item.get("lifecycle_state"),
                "message_id": next_item.get("message_id"),
                "since": next_item.get("created_at"),
            }
        completed = [
            event for event in events if event.get("event_type") == "work_completed"
        ]
        if completed:
            latest = completed[-1]
            return {
                "status": str(latest.get("status") or "completed"),
                "role_id": latest.get("role_id"),
                "role_instance_id": latest.get("role_instance_id"),
                "lifecycle_state": latest.get("lifecycle_state"),
                "message_id": latest.get("message_id"),
                "since": latest.get("timestamp"),
            }
        if events:
            latest = events[-1]
            return {
                "status": "observed",
                "role_id": latest.get("role_id") or latest.get("target_role"),
                "lifecycle_state": latest.get("lifecycle_state")
                or latest.get("target_lifecycle_state"),
                "message_id": latest.get("message_id"),
                "since": latest.get("timestamp"),
            }
        return {"status": "not_found"}

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
        if path.path.startswith("/work-items/") and path.path.endswith(".json"):
            work_item_id = unquote(path.path.removeprefix("/work-items/")[:-5])
            payload = self.server.service.work_item_status(work_item_id)
            if payload["status"] == "not_found":
                self._send_json(HTTPStatus.NOT_FOUND, payload)
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        if path.path.startswith("/work-items/"):
            work_item_id = unquote(path.path.removeprefix("/work-items/"))
            payload = self.server.service.work_item_status(work_item_id)
            if payload["status"] == "not_found":
                self._send_html(
                    HTTPStatus.NOT_FOUND,
                    self._layout("Work Item Not Found", "<p>Unknown work item.</p>"),
                )
                return
            self._send_html(HTTPStatus.OK, self._work_item_page(payload))
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
        if path.path == "/auth/codex/session.json":
            session_id = parse_qs(path.query).get("id", [""])[0]
            payload = self.server.service.session_payload(session_id)
            if payload is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, payload)
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
        query = parse_qs(path.query)
        notice = query.get("notice", [""])[0]
        selected = query.get("credential", [""])[0]
        rows = []
        for credential in self.server.service.credential_statuses():
            roles = ", ".join(credential["roles"]) or "No roles"
            action = self._credential_action(credential)
            row_class = (
                ' class="selected"'
                if selected and selected == credential["credential"]
                else ""
            )
            rows.append(
                f"<tr id=\"credential-{html.escape(credential['credential'])}\"{row_class}>"
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
            if credential["status"] == "configured":
                return """
<button class="status-button" type="button" disabled title="OpenAI sign-in is configured">
  <svg aria-hidden="true" viewBox="0 0 24 24">
    <path d="M20 6 9 17l-5-5"></path>
  </svg>
  Signed in
</button>
"""
            return f"""
<form method="post" action="/auth/codex/start">
  <input type="hidden" name="credential" value="{credential_id}">
  <button type="submit">Sign in with OpenAI</button>
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
        output = "\n".join(session.output) or "Waiting for Codex output..."
        openai_button = (
            f"""
<p id="openai-link-row">
  <a class="primary" id="openai-link" href="{html.escape(session.login_url)}" target="_blank" rel="noopener">
    Open OpenAI Sign-In
  </a>
</p>
"""
            if session.login_url
            else '<p id="openai-link-row">Preparing OpenAI sign-in...</p>'
        )
        code = (
            f"""
<div class="code-panel" id="code-panel">
  <p>OpenAI will ask for this one-time code:</p>
  <div class="code-row">
    <code class="login-code" id="login-code">{html.escape(session.user_code)}</code>
    <button class="icon-button" id="copy-code" type="button" title="Copy code" aria-label="Copy code">
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <rect x="9" y="9" width="10" height="10" rx="2"></rect>
        <path d="M5 15V7a2 2 0 0 1 2-2h8"></path>
      </svg>
    </button>
    <span class="copy-status" id="copy-status" aria-live="polite"></span>
  </div>
</div>
<p id="waiting-code" hidden>Waiting for Codex to issue the one-time code...</p>
"""
            if session.user_code
            else """
<div class="code-panel" id="code-panel" hidden>
  <p>OpenAI will ask for this one-time code:</p>
  <div class="code-row">
    <code class="login-code" id="login-code"></code>
    <button class="icon-button" id="copy-code" type="button" title="Copy code" aria-label="Copy code">
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <rect x="9" y="9" width="10" height="10" rx="2"></rect>
        <path d="M5 15V7a2 2 0 0 1 2-2h8"></path>
      </svg>
    </button>
    <span class="copy-status" id="copy-status" aria-live="polite"></span>
  </div>
</div>
<p id="waiting-code">Waiting for Codex to issue the one-time code...</p>
"""
        )
        instructions = (
            "OpenAI uses device-code sign-in here: open the sign-in page, "
            "enter the one-time code below, then return to this tab. Agentic Mesh "
            "will detect completion and store the credential cache automatically."
            if session.status == "running"
            else "This sign-in session has finished. Return to credentials to check status."
        )
        script = f"""
<script>
const sessionId = {json.dumps(session.session_id)};
const statusEl = document.getElementById("session-status");
const instructionsEl = document.getElementById("session-instructions");
const linkRow = document.getElementById("openai-link-row");
const codePanel = document.getElementById("code-panel");
const codeEl = document.getElementById("login-code");
const waitingCode = document.getElementById("waiting-code");
const outputEl = document.getElementById("technical-output");
const copyButton = document.getElementById("copy-code");
const copyStatus = document.getElementById("copy-status");

function runningInstructions() {{
  return "OpenAI uses device-code sign-in here: open the sign-in page, enter the one-time code below, then return to this tab. Agentic Mesh will detect completion and store the credential cache automatically.";
}}

function updateSession(data) {{
  statusEl.textContent = data.status;
  instructionsEl.textContent = data.status === "running"
    ? runningInstructions()
    : "This sign-in session has finished. Return to credentials to check status.";
  if (data.login_url) {{
    linkRow.innerHTML = '<a class="primary" id="openai-link" target="_blank" rel="noopener">Open OpenAI Sign-In</a>';
    linkRow.querySelector("a").href = data.login_url;
  }}
  if (data.user_code) {{
    codeEl.textContent = data.user_code;
    codePanel.hidden = false;
    waitingCode.hidden = true;
  }}
  outputEl.textContent = data.output || "Waiting for Codex output...";
  if (data.status === "running") {{
    window.setTimeout(pollSession, 3000);
  }}
}}

async function pollSession() {{
  try {{
    const response = await fetch("/auth/codex/session.json?id=" + encodeURIComponent(sessionId), {{
      cache: "no-store"
    }});
    if (response.ok) {{
      updateSession(await response.json());
    }} else {{
      window.setTimeout(pollSession, 5000);
    }}
  }} catch (_error) {{
    window.setTimeout(pollSession, 5000);
  }}
}}

copyButton.addEventListener("click", async () => {{
  const code = codeEl.textContent.trim();
  if (!code) {{
    return;
  }}
  try {{
    await navigator.clipboard.writeText(code);
    copyStatus.textContent = "Copied";
  }} catch (_error) {{
    const range = document.createRange();
    range.selectNodeContents(codeEl);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    copyStatus.textContent = "Selected";
  }}
}});

if (statusEl.textContent === "running") {{
  window.setTimeout(pollSession, 3000);
}}
</script>
"""
        body = f"""
<p>Status: <strong id="session-status">{html.escape(session.status)}</strong></p>
<p>Credential: <code>{html.escape(session.credential_id)}</code></p>
<p id="session-instructions">{html.escape(instructions)}</p>
{openai_button}
{code}
<details>
  <summary>Technical output</summary>
  <pre id="technical-output">{html.escape(output)}</pre>
</details>
<p><a href="/auth/credentials">Back to credentials</a></p>
{script}
"""
        return self._layout("Codex OAuth Login", body)

    def _work_item_page(self, payload: dict[str, Any]) -> str:
        current = payload["current"]
        queue_rows = []
        for entry in payload["queue_entries"]:
            queue_rows.append(
                "<tr>"
                f"<td>{html.escape(str(entry.get('queue_state') or ''))}</td>"
                f"<td>{html.escape(str(entry.get('role_id') or ''))}</td>"
                f"<td>{html.escape(str(entry.get('lifecycle_state') or ''))}</td>"
                f"<td><code>{html.escape(str(entry.get('message_id') or ''))}</code></td>"
                f"<td>{html.escape(str(entry.get('claimed_by') or ''))}</td>"
                f"<td>{html.escape(str(entry.get('claimed_at') or entry.get('created_at') or ''))}</td>"
                "</tr>"
            )
        timeline_rows = []
        for event in payload["timeline"][-80:]:
            role = event.get("role_id") or event.get("source_role") or ""
            target = event.get("target_role") or ""
            lifecycle = (
                event.get("lifecycle_state")
                or event.get("target_lifecycle_state")
                or event.get("source_lifecycle_state")
                or ""
            )
            detail = event.get("status") or event.get("message_type") or ""
            timeline_rows.append(
                "<tr>"
                f"<td>{html.escape(str(event.get('timestamp') or ''))}</td>"
                f"<td>{html.escape(str(event.get('event_type') or ''))}</td>"
                f"<td>{html.escape(str(role))}</td>"
                f"<td>{html.escape(str(target))}</td>"
                f"<td>{html.escape(str(lifecycle))}</td>"
                f"<td>{html.escape(str(detail))}</td>"
                "</tr>"
            )
        artifact_items = "".join(
            f"<li><code>{html.escape(path)}</code></li>"
            for path in payload["artifacts"]
        ) or "<li>None recorded</li>"
        teams_items = "".join(
            "<li>"
            f"{html.escape(str(item.get('timestamp') or ''))} "
            f"{html.escape(str(item.get('channel') or 'unknown'))}: "
            f"<code>{html.escape(str(item.get('teams_activity_id') or ''))}</code>"
            "</li>"
            for item in payload["teams_messages"]
        ) or "<li>None recorded</li>"
        json_path = (
            "/work-items/"
            + quote(str(payload["work_item_id"]), safe="")
            + ".json"
        )
        body = f"""
<p class="summary">
  <strong>Status:</strong> {html.escape(str(payload["status"]))}
  <br><strong>Current role:</strong> {html.escape(str(current.get("role_id") or "none"))}
  <br><strong>Lifecycle state:</strong> {html.escape(str(current.get("lifecycle_state") or "none"))}
  <br><strong>Message:</strong> <code>{html.escape(str(current.get("message_id") or "none"))}</code>
  <br><strong>Since:</strong> {html.escape(str(current.get("since") or "unknown"))}
</p>
<p><a href="{html.escape(json_path)}">JSON status</a></p>
<h2>Queue</h2>
<table>
  <thead>
    <tr>
      <th>State</th>
      <th>Role</th>
      <th>Lifecycle</th>
      <th>Message</th>
      <th>Claimed By</th>
      <th>Timestamp</th>
    </tr>
  </thead>
  <tbody>{''.join(queue_rows)}</tbody>
</table>
<h2>Artifacts</h2>
<ul>{artifact_items}</ul>
<h2>Teams Messages</h2>
<ul>{teams_items}</ul>
<h2>Timeline</h2>
<table>
  <thead>
    <tr>
      <th>Time</th>
      <th>Event</th>
      <th>Role</th>
      <th>Target</th>
      <th>Lifecycle</th>
      <th>Detail</th>
    </tr>
  </thead>
  <tbody>{''.join(timeline_rows)}</tbody>
</table>
"""
        title = f"Work Item {payload['work_item_id']}"
        return self._layout(title, body)

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
    tr.selected {{ outline: 3px solid #6aa1ff; }}
    input[type=password] {{ min-width: 18rem; }}
    pre {{ background: #111; color: #eee; padding: 1rem; white-space: pre-wrap; }}
    h2 {{ margin-top: 2rem; }}
    .notice {{ background: #e9f7ef; border: 1px solid #9bd7ad; padding: 0.75rem; }}
    .summary {{ background: #f8fafc; border: 1px solid #cbd5e1; padding: 1rem; }}
    .primary {{ display: inline-block; background: #111827; color: white; padding: 0.75rem 1rem; text-decoration: none; }}
    .code-panel {{ border: 2px solid #111827; display: inline-block; padding: 1rem 1.25rem; margin: 1rem 0; }}
    .code-row {{ align-items: center; display: flex; gap: 0.75rem; }}
    .login-code {{ display: block; font-size: 2rem; letter-spacing: 0.08em; }}
    .icon-button {{ align-items: center; background: #111827; border: 0; color: white; cursor: pointer; display: inline-flex; height: 2.75rem; justify-content: center; width: 2.75rem; }}
    .icon-button svg {{ fill: none; height: 1.25rem; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 2; width: 1.25rem; }}
    .status-button {{ align-items: center; background: #e9f7ef; border: 1px solid #166534; color: #166534; display: inline-flex; gap: 0.35rem; padding: 0.45rem 0.7rem; }}
    .status-button svg {{ fill: none; height: 1rem; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 2.5; width: 1rem; }}
    .copy-status {{ color: #166534; min-width: 4rem; }}
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


def _first_url(text: str) -> str | None:
    text = _strip_ansi(text)
    match = re.search(r"https?://[^\s)>\"]+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,")


def _first_device_code(text: str) -> str | None:
    text = _strip_ansi(text)
    match = re.search(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b", text)
    if not match:
        return None
    return match.group(0)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
