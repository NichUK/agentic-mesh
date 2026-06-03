from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any

from agentic_mesh.config import load_mesh_config
from agentic_mesh.connectors import TeamsBotIngress
from agentic_mesh.connectors import FileSecretResolver
from agentic_mesh.journal import EventJournal
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore


class ReloadableTeamsBotIngress:
    def __init__(
        self,
        *,
        config_root: Path,
        project_file: str,
        state_root: Path,
        connector: str,
        connector_id: str,
        secret_root: Path,
    ) -> None:
        self.config_root = config_root
        self.project_file = project_file
        self.state_root = state_root
        self.connector = connector
        self.connector_id = connector_id
        self.secret_root = secret_root
        self._lock = Lock()
        self._ingress: TeamsBotIngress | None = None
        self._metadata: dict[str, Any] = {}
        self.reload(reason="startup")

    def receive_activity(self, activity: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            ingress = self._ingress
        if ingress is None:
            raise RuntimeError("Teams bot ingress is not loaded")
        return ingress.receive_activity(activity)

    def reload(self, *, reason: str) -> dict[str, Any]:
        mesh_config = load_mesh_config(self.config_root, project_file=self.project_file)
        connector_config = mesh_config.project.connectors[self.connector]
        journal = EventJournal(self.state_root, mesh_config.project.project_id)
        message_store = FileMessageStore(
            self.state_root,
            mesh_config.project.project_id,
            journal,
        )
        connector_outbox = FileConnectorOutbox(
            self.state_root,
            mesh_config.project.project_id,
            journal,
        )
        ingress = TeamsBotIngress(
            connector_id=self.connector_id,
            project_id=mesh_config.project.project_id,
            state_root=self.state_root,
            message_store=message_store,
            journal=journal,
            connector_config=connector_config,
            project_config=mesh_config.project,
            secrets=FileSecretResolver(self.secret_root),
            connector_outbox=connector_outbox,
        )
        metadata = {
            "status": "reloaded",
            "reason": reason,
            "project_id": mesh_config.project.project_id,
            "connector": self.connector,
            "connector_id": self.connector_id,
            "project_file": self.project_file,
            "config_root": str(self.config_root),
            "state_root": str(self.state_root),
            "channels": sorted(connector_config.channels),
            "roles": sorted(mesh_config.project.roles),
        }
        journal.append(
            "teams_bot_ingress_config_reloaded",
            project_id=mesh_config.project.project_id,
            connector_id=self.connector_id,
            reason=reason,
            project_file=self.project_file,
            config_root=str(self.config_root),
            state_root=str(self.state_root),
        )
        with self._lock:
            self._ingress = ingress
            self._metadata = metadata
        return metadata

    def status(self) -> dict[str, Any]:
        with self._lock:
            metadata = dict(self._metadata)
        return {"status": "ok", "ingress": metadata}


class TeamsBotIngressServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        handler_class,
        ingress: ReloadableTeamsBotIngress,
    ):
        super().__init__(server_address, handler_class)
        self.ingress = ingress


class TeamsBotIngressHandler(BaseHTTPRequestHandler):
    server: TeamsBotIngressServer

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path == "/admin/config":
            self._send_json(HTTPStatus.OK, self.server.ingress.status())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/admin/reload-config":
            try:
                result = self.server.ingress.reload(reason="admin_http")
            except Exception as exc:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"status": "error", "error": str(exc)},
                )
                return
            self._send_json(HTTPStatus.OK, result)
            return

        if self.path != "/api/messages":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        try:
            payload = self._read_json()
            result = self.server.ingress.receive_activity(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return
        except KeyError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "missing_field", "field": str(exc)},
            )
            return

        self._send_json(HTTPStatus.OK, result)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length).decode("utf-8")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise json.JSONDecodeError("expected JSON object", body, 0)
        return data

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_teams_bot_ingress(
    *,
    host: str,
    port: int,
    ingress: ReloadableTeamsBotIngress,
) -> None:
    server = TeamsBotIngressServer((host, port), TeamsBotIngressHandler, ingress)
    server.serve_forever()
