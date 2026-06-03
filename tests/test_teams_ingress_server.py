from __future__ import annotations

import json
from http.client import HTTPConnection
from threading import Thread

from agentic_mesh.teams_ingress import TeamsBotIngressHandler
from agentic_mesh.teams_ingress import TeamsBotIngressServer


class FakeReloadableIngress:
    def __init__(self) -> None:
        self.reload_count = 0

    def status(self) -> dict:
        return {
            "status": "ok",
            "ingress": {
                "project_id": "agentic-mesh-dev",
                "connector": "teams",
            },
        }

    def reload(self, *, reason: str) -> dict:
        self.reload_count += 1
        return {
            "status": "reloaded",
            "reason": reason,
            "project_id": "agentic-mesh-dev",
        }

    def receive_activity(self, activity: dict) -> dict:
        return {"status": "accepted", "activity_id": activity["id"]}


def test_teams_ingress_admin_reload_endpoint() -> None:
    ingress = FakeReloadableIngress()
    server = TeamsBotIngressServer(("127.0.0.1", 0), TeamsBotIngressHandler, ingress)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/admin/config")
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert body["ingress"]["project_id"] == "agentic-mesh-dev"

        connection.request("POST", "/admin/reload-config", body=b"")
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert body == {
            "status": "reloaded",
            "reason": "admin_http",
            "project_id": "agentic-mesh-dev",
        }
        assert ingress.reload_count == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_teams_ingress_message_endpoint_uses_current_ingress() -> None:
    ingress = FakeReloadableIngress()
    server = TeamsBotIngressServer(("127.0.0.1", 0), TeamsBotIngressHandler, ingress)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        payload = json.dumps({"type": "message", "id": "activity-1"})

        connection.request(
            "POST",
            "/api/messages",
            body=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert body == {"status": "accepted", "activity_id": "activity-1"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
