from __future__ import annotations

import json
import os
import re
import time
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
from agentic_mesh_v4.decision_records import DecisionRecordError
from agentic_mesh_v4.decision_records import resolve_decision_and_update_card
from agentic_mesh_v4.reporting import render_agent_thread
from agentic_mesh_v4.reporting import render_agents
from agentic_mesh_v4.reporting import render_artifact
from agentic_mesh_v4.reporting import render_status
from agentic_mesh_v4.reporting import render_work_item
from agentic_mesh_v4.runtime import V4Runtime
from agentic_mesh_v4.teams_delivery import TeamsReplySender


class V4Handler(BaseHTTPRequestHandler):
    db_path: str | None
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
            if path.startswith("/agent/") and path.endswith("/thread/events"):
                role_id = unquote(path.removeprefix("/agent/").removesuffix("/thread/events")).strip("/")
                self._handle_agent_thread_events(db, role_id)
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
                events.reverse()
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
        if path == "/teams/decision-callback":
            self._handle_decision_callback()
            return
        if path == "/api/messages":
            self._handle_api_message()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle_agent_thread_events(self, db: V4Database, role_id: str) -> None:
        role_pattern = f"%.{role_id}.%"
        latest = db.connection.execute(
            """
            SELECT created_at, event_id FROM agent_events
            WHERE role_instance_id LIKE ?
            ORDER BY created_at DESC, event_id DESC LIMIT 1
            """,
            (role_pattern,),
        ).fetchone()
        last_created_at = str(latest["created_at"]) if latest else ""
        last_event_id = str(latest["event_id"]) if latest else ""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for _ in range(300):
            rows = [
                dict(row)
                for row in db.connection.execute(
                    """
                    SELECT event_id, created_at, event_type, turn_id, message_id, content
                    FROM agent_events
                    WHERE role_instance_id LIKE ?
                      AND (created_at > ? OR (created_at = ? AND event_id > ?))
                    ORDER BY created_at ASC, event_id ASC
                    LIMIT 100
                    """,
                    (role_pattern, last_created_at, last_created_at, last_event_id),
                )
            ]
            try:
                if rows:
                    for row in rows:
                        last_created_at = str(row.get("created_at") or last_created_at)
                        last_event_id = str(row.get("event_id") or last_event_id)
                        self.wfile.write(f"data: {json.dumps(row)}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(1)

    def _handle_teams_activity(self) -> None:
        payload = _normalise_teams_activity_payload(self._read_json())
        if _is_decision_callback_activity(payload):
            self._process_decision_callback(payload)
            return
        text = str(payload.get("text") or payload.get("message") or "")
        target_role = _target_role(payload, self.project_config)
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

    def _handle_decision_callback(self) -> None:
        payload = self._read_json()
        self._process_decision_callback(payload)

    def _process_decision_callback(self, payload: dict[str, object]) -> None:
        value = payload.get("value") if isinstance(payload.get("value"), dict) else payload
        if not isinstance(value, dict):
            value = payload
        decision_id = _string_value(value.get("decision_id"))
        selected_option = _string_value(value.get("selected_option") or value.get("option"))
        if decision_id is None or selected_option is None:
            self._json({"status": "failed", "error": "decision_id and selected_option are required"}, status=HTTPStatus.BAD_REQUEST)
            return
        responder_ref = (
            _string_value(value.get("responder_ref"))
            or _nested_string(payload.get("from"), "aadObjectId")
            or _nested_string(payload.get("from"), "id")
            or _string_value(payload.get("from"))
            or "unknown"
        )
        idempotency_key = (
            _string_value(value.get("idempotency_key"))
            or _string_value(payload.get("replyToId"))
            or _string_value(payload.get("id"))
            or f"{decision_id}:{responder_ref}:{selected_option}"
        )
        db = V4Database(self.db_path)
        try:
            db.migrate()
            try:
                result = resolve_decision_and_update_card(
                    db=db,
                    decision_id=decision_id,
                    responder_ref=responder_ref,
                    selected_option=selected_option,
                    sender=TeamsReplySender.from_env(),
                    rationale=str(value.get("rationale") or ""),
                    idempotency_key=idempotency_key,
                    delivery_id=_string_value(value.get("delivery_id")),
                    raw_payload=payload,
                )
            except DecisionRecordError as exc:
                self._json({"status": "failed", "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
        finally:
            db.close()
        status = HTTPStatus.OK if result.get("state") in {"accepted", "callback_failed"} else HTTPStatus.ACCEPTED
        self._json(result, status=status)

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
    db_path: str | None,
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


def _target_role(payload: dict[str, object], project_config: V4ProjectConfig) -> str:
    if str(payload.get("channelId") or "").casefold() == "msteams":
        recipient_role = _target_role_from_recipient(payload, project_config)
        if recipient_role:
            return recipient_role
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


def _is_decision_callback_activity(payload: dict[str, object]) -> bool:
    value = payload.get("value") if isinstance(payload.get("value"), dict) else payload
    if not isinstance(value, dict):
        return False
    if value.get("action") == "decision_callback":
        return True
    return "decision_id" in value and ("selected_option" in value or "option" in value)


def _normalise_teams_activity_payload(payload: dict[str, object]) -> dict[str, object]:
    normalised = dict(payload)
    conversation = normalised.get("conversation")
    if not isinstance(conversation, dict):
        return normalised
    if str(conversation.get("conversationType") or "").casefold() != "personal":
        return normalised
    graph_chat_id = (
        _string_value(normalised.get("graph_chat_id"))
        or _string_value(normalised.get("chat_id"))
        or _nested_string(normalised.get("channelData"), "graph", "chatId")
        or _nested_string(normalised.get("channelData"), "graph", "chat_id")
        or _nested_string(normalised.get("channelData"), "graphChatId")
    )
    graph_message_id = (
        _string_value(normalised.get("graph_chat_message_id"))
        or _string_value(normalised.get("graph_message_id"))
        or _string_value(normalised.get("chatMessageId"))
        or _nested_string(normalised.get("channelData"), "graph", "chatMessageId")
        or _nested_string(normalised.get("channelData"), "graph", "messageId")
        or _nested_string(normalised.get("channelData"), "graphChatMessageId")
    )
    if graph_chat_id and graph_message_id:
        original_activity_id = _string_value(normalised.get("id"))
        if original_activity_id and original_activity_id != graph_message_id:
            normalised.setdefault("bot_framework_activity_id", original_activity_id)
        normalised["graph_chat_id"] = graph_chat_id
        normalised["id"] = graph_message_id
    return normalised


def _target_role_from_recipient(payload: dict[str, object], project_config: V4ProjectConfig) -> str | None:
    recipient = payload.get("recipient")
    if not isinstance(recipient, dict):
        return None
    recipient_id = str(recipient.get("id") or "")
    recipient_name = _normalise_role_label(str(recipient.get("name") or ""))
    for role in project_config.roles:
        if recipient_name in {
            _normalise_role_label(role.display_name),
            _normalise_role_label(f"AM-{role.display_name}"),
            _normalise_role_label(role.role_id),
            _normalise_role_label(role.role_id.replace("-", " ")),
        }:
            return role.role_id
        env_role = role.role_id.upper().replace("-", "_")
        app_id = os.environ.get(f"TEAMS_BOT_{env_role}_APP_ID")
        if app_id and recipient_id in {app_id, f"28:{app_id}"}:
            return role.role_id
    return None


def _normalise_role_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _normalise_teams_activity_payload(payload: dict[str, object]) -> dict[str, object]:
    normalised = dict(payload)
    conversation = normalised.get("conversation")
    if not isinstance(conversation, dict):
        return normalised
    if str(conversation.get("conversationType") or "").casefold() != "personal":
        return normalised
    graph_chat_id = (
        _string_value(normalised.get("graph_chat_id"))
        or _string_value(normalised.get("chat_id"))
        or _nested_string(normalised.get("channelData"), "graph", "chatId")
        or _nested_string(normalised.get("channelData"), "graph", "chat_id")
        or _nested_string(normalised.get("channelData"), "graphChatId")
    )
    graph_message_id = (
        _string_value(normalised.get("graph_chat_message_id"))
        or _string_value(normalised.get("graph_message_id"))
        or _string_value(normalised.get("chatMessageId"))
        or _nested_string(normalised.get("channelData"), "graph", "chatMessageId")
        or _nested_string(normalised.get("channelData"), "graph", "messageId")
        or _nested_string(normalised.get("channelData"), "graphChatMessageId")
    )
    if graph_chat_id and graph_message_id:
        original_activity_id = _string_value(normalised.get("id"))
        if original_activity_id and original_activity_id != graph_message_id:
            normalised.setdefault("bot_framework_activity_id", original_activity_id)
        normalised["graph_chat_id"] = graph_chat_id
        normalised["id"] = graph_message_id
    return normalised


def _nested_string(data: object, *path: str) -> str | None:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _string_value(current)


def _string_value(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


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
