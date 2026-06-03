from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any

from agentic_mesh.connectors import TeamsBotIngress


class TeamsBotIngressServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, ingress: TeamsBotIngress):
        super().__init__(server_address, handler_class)
        self.ingress = ingress


class TeamsBotIngressHandler(BaseHTTPRequestHandler):
    server: TeamsBotIngressServer

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
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
    ingress: TeamsBotIngress,
) -> None:
    server = TeamsBotIngressServer((host, port), TeamsBotIngressHandler, ingress)
    server.serve_forever()
