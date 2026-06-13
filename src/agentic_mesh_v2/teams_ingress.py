from __future__ import annotations

import html
import json
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.observability import configure_observability
from agentic_mesh_v2.observability import span
from agentic_mesh_v2.project_config import load_teams_connector_config


class TeamsIngressHandler(BaseHTTPRequestHandler):
    db_path: Path
    project_file: Path
    ingress_path: str = "/api/messages"
    external_base_url: str | None = None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json({"status": "ok", "runtime": "agentic_mesh_v2_teams_ingress"})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        with span("v2.teams_ingress_request", http_method="POST", http_route=path):
            if path != self.ingress_path:
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
                return
            try:
                raw = self._read_json()
                db = V2Database(self.db_path)
                try:
                    db.migrate()
                    config = load_teams_connector_config(
                        self.project_file,
                        external_base_url=self.external_base_url or os.environ.get("AGENTIC_MESH_URL_ROOT"),
                    )
                    adapter = LocalTeamsTestAdapter(db, config)
                    adapter.install()
                    event = normalize_bot_activity(raw, role_display_names=_role_display_names(config))
                    replayed = adapter.replay_event(event)
                finally:
                    db.close()
            except Exception as exc:
                self._send_json({"status": "failed", "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(
                {
                    "status": "accepted",
                    "receipt_id": replayed.receipt_id,
                    "conversation_id": replayed.conversation_id,
                    "duplicate": replayed.duplicate,
                    "route_type": replayed.route_type,
                    "mentioned_roles": list(replayed.mentioned_roles),
                }
            )

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Teams ingress payload must be a JSON object")
        return value

    def _send_json(self, payload: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_teams_ingress(
    *,
    host: str,
    port: int,
    db_path: Path,
    project_file: Path,
    ingress_path: str = "/api/messages",
    external_base_url: str | None = None,
) -> None:
    configure_observability("agentic-mesh-v2-teams-ingress")
    TeamsIngressHandler.db_path = db_path
    TeamsIngressHandler.project_file = project_file
    TeamsIngressHandler.ingress_path = ingress_path
    TeamsIngressHandler.external_base_url = external_base_url
    server = ThreadingHTTPServer((host, port), TeamsIngressHandler)
    server.serve_forever()


def normalize_bot_activity(activity: dict[str, Any], *, role_display_names: dict[str, str]) -> dict[str, Any]:
    activity_type = str(activity.get("type") or "message")
    if activity_type != "message":
        raise ValueError(f"unsupported Teams activity type `{activity_type}`")
    message_id = _first_non_empty(activity.get("id"), activity.get("replyToId"))
    if message_id is None:
        raise ValueError("Teams activity missing message id")
    channel_data = activity.get("channelData") if isinstance(activity.get("channelData"), dict) else {}
    conversation = activity.get("conversation") if isinstance(activity.get("conversation"), dict) else {}
    source_type = _source_type(conversation, channel_data)
    conversation_ref = _conversation_ref(source_type=source_type, conversation=conversation, channel_data=channel_data)
    sender = activity.get("from") if isinstance(activity.get("from"), dict) else {}
    recipient = activity.get("recipient") if isinstance(activity.get("recipient"), dict) else {}
    body = _clean_activity_text(str(activity.get("text") or ""))
    mentioned_roles = _mentioned_roles_from_activity(activity, role_display_names=role_display_names)
    target_role_id = _role_from_display(str(recipient.get("name") or ""), role_display_names=role_display_names)
    event: dict[str, Any] = {
        "event_type": "message.created",
        "message_id": str(message_id),
        "conversation_ref": conversation_ref,
        "sender_ref": _first_non_empty(sender.get("aadObjectId"), sender.get("id"), sender.get("name")) or "unknown-human",
        "source_type": source_type,
        "body": body,
        "thread_ref": _first_non_empty(activity.get("replyToId"), message_id),
        "reply_to_id": str(message_id),
        "service_url": _first_non_empty(activity.get("serviceUrl")),
        "target_ref": _first_non_empty(recipient.get("id"), recipient.get("name")),
    }
    if mentioned_roles:
        event["mentioned_roles"] = mentioned_roles
    if target_role_id is not None:
        event["target_role_id"] = target_role_id
    return event


def _source_type(conversation: dict[str, Any], channel_data: dict[str, Any]) -> str:
    conversation_type = str(conversation.get("conversationType") or "").casefold()
    if conversation_type in {"personal", "dm"}:
        return "dm"
    if channel_data.get("channel"):
        return "channel"
    return "dm" if conversation_type == "personal" else "channel"


def _conversation_ref(*, source_type: str, conversation: dict[str, Any], channel_data: dict[str, Any]) -> str:
    if source_type == "channel":
        channel = channel_data.get("channel") if isinstance(channel_data.get("channel"), dict) else {}
        value = _first_non_empty(channel.get("id"), conversation.get("id"))
    else:
        value = _first_non_empty(conversation.get("id"), channel_data.get("conversationId"))
    if value is None:
        raise ValueError("Teams activity missing conversation reference")
    return str(value)


def _mentioned_roles_from_activity(activity: dict[str, Any], *, role_display_names: dict[str, str]) -> list[str]:
    roles: list[str] = []
    entities = activity.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, dict) or entity.get("type") != "mention":
                continue
            role_id = _role_from_display(str(entity.get("text") or ""), role_display_names=role_display_names)
            mentioned = entity.get("mentioned") if isinstance(entity.get("mentioned"), dict) else {}
            if role_id is None:
                role_id = _role_from_display(str(mentioned.get("name") or ""), role_display_names=role_display_names)
            if role_id is not None:
                roles.append(role_id)
    text = str(activity.get("text") or "")
    for role_id, display_name in role_display_names.items():
        if f"<at>{html.escape(display_name)}</at>" in text or f"@{display_name}" in text:
            roles.append(role_id)
    return list(dict.fromkeys(roles))


def _role_display_names(config: Any) -> dict[str, str]:
    return {role_id: identity.display_name for role_id, identity in config.role_identities.items()}


def _role_from_display(value: str, *, role_display_names: dict[str, str]) -> str | None:
    normalized = _normalize_mention_text(value)
    for role_id, display_name in role_display_names.items():
        if normalized in {_normalize_mention_text(display_name), _normalize_mention_text(f"@{display_name}")}:
            return role_id
    return None


def _clean_activity_text(value: str) -> str:
    value = re.sub(r"</?at>", "", value)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


def _normalize_mention_text(value: str) -> str:
    value = _clean_activity_text(value)
    return value.removeprefix("@").strip().casefold()


def _first_non_empty(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
