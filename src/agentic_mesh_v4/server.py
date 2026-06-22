from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlparse

from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import WebSocketTransport
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.reporting import render_agent_thread
from agentic_mesh_v4.reporting import render_agents
from agentic_mesh_v4.reporting import render_artifact
from agentic_mesh_v4.reporting import render_status
from agentic_mesh_v4.reporting import render_work_item
from agentic_mesh_v4.runtime import V4Runtime


class V4Handler(BaseHTTPRequestHandler):
    db_path: Path
    project_config: V4ProjectConfig
    document_root: Path

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json({"status": "ok", "runtime": "agentic_mesh_v4"})
            return
        db = V4Database(self.db_path)
        try:
            db.migrate()
            if path in {"/", "/status"}:
                self._html(render_status(db.snapshot()))
                return
            if path == "/status.json":
                self._json(db.snapshot())
                return
            if path == "/agents":
                self._html(render_agents(db.snapshot()))
                return
            if path.startswith("/agent/") and path.endswith("/thread"):
                role_id = unquote(path.removeprefix("/agent/").removesuffix("/thread")).strip("/")
                events = [
                    dict(row)
                    for row in db.connection.execute(
                        """
                        SELECT * FROM agent_events
                        WHERE role_instance_id LIKE ?
                        ORDER BY created_at DESC LIMIT 200
                        """,
                        (f"%.{role_id}.%",),
                    )
                ]
                self._html(render_agent_thread(role_id, events))
                return
            if path.startswith("/work-item/"):
                work_item_id = unquote(path.removeprefix("/work-item/")).strip("/")
                rows = [
                    dict(row)
                    for row in db.connection.execute(
                        "SELECT * FROM work_items WHERE work_item_id=?",
                        (work_item_id,),
                    )
                ]
                self._html(render_work_item(work_item_id, rows))
                return
            if path.startswith("/artifact-viewer/"):
                artifact_path = unquote(path.removeprefix("/artifact-viewer/")).strip("/")
                self._html(render_artifact(_safe_artifact_path(self.document_root, artifact_path)))
                return
        finally:
            db.close()
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/teams/activity":
            self._handle_teams_activity()
            return
        if path == "/api/messages":
            self._handle_api_message()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle_teams_activity(self) -> None:
        payload = self._read_json()
        text = str(payload.get("text") or payload.get("message") or "")
        target_role = _target_role(payload)
        conversation_ref = str(payload.get("conversation_ref") or payload.get("conversation", {}).get("id") or "")
        thread_ref = str(payload.get("reply_thread_ref") or payload.get("thread_ref") or "")
        db = V4Database(self.db_path)
        try:
            db.migrate()
            runtime = V4Runtime(
                db=db,
                project_config=self.project_config,
                client_factory=_client_factory(),
            )
            runtime.register_roles()
            message_id = runtime.enqueue_or_steer_conversation(
                target_role=target_role,
                text=text,
                source="teams",
                conversation_ref=conversation_ref or None,
                thread_ref=thread_ref or None,
                payload=payload,
            )
        finally:
            db.close()
        self._json({"message_id": message_id, "target_role": target_role, "status": "queued"}, status=HTTPStatus.ACCEPTED)

    def _handle_api_message(self) -> None:
        payload = self._read_json()
        target_role = str(payload.get("target_role") or "project-manager")
        text = str(payload.get("text") or "")
        steering = bool(payload.get("steering"))
        db = V4Database(self.db_path)
        try:
            db.migrate()
            runtime = V4Runtime(
                db=db,
                project_config=self.project_config,
                client_factory=_client_factory(),
            )
            runtime.register_roles()
            message_id = runtime.enqueue_or_steer_conversation(
                target_role=target_role,
                text=text,
                source="api",
                steering=steering,
                payload=payload,
            )
        finally:
            db.close()
        self._json({"message_id": message_id, "target_role": target_role, "status": "queued"}, status=HTTPStatus.ACCEPTED)

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b"{}"
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request JSON must be an object")
        return value

    def _html(self, body: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(
    *,
    db_path: Path,
    project_config: V4ProjectConfig,
    document_root: Path,
    host: str,
    port: int,
) -> None:
    V4Handler.db_path = db_path
    V4Handler.project_config = project_config
    V4Handler.document_root = document_root
    server = ThreadingHTTPServer((host, port), V4Handler)
    server.serve_forever()


def _target_role(payload: dict[str, object]) -> str:
    direct = payload.get("target_role")
    if isinstance(direct, str) and direct:
        return direct
    text = str(payload.get("text") or "").casefold()
    for role_id in (
        "project-manager",
        "delivery-manager",
        "product-manager",
        "engineering",
        "qa-engineer",
        "release-manager",
    ):
        if role_id in text or role_id.replace("-", " ") in text:
            return role_id
    return "project-manager"


def _safe_artifact_path(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    candidate = (root / relative_path).resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError("artifact path escapes document root")
    return candidate


def _client_factory():
    def factory(role_id: str) -> CodexAppServerClient:
        role_config = V4Handler.project_config.role(role_id)
        token_file = Path("/mesh/project/state/v4/agent-configs") / role_id / "1" / "ws-token"
        token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else None
        return CodexAppServerClient(
            WebSocketTransport(
                f"ws://{role_config.service_name}:{role_config.codex_port}",
                bearer_token=token,
            )
        )

    return factory
