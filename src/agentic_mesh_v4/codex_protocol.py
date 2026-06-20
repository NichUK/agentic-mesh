from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen


CODEX_APP_SERVER_METHODS: frozenset[str] = frozenset(
    {
        "initialize",
        "initialized",
        "thread/start",
        "thread/resume",
        "turn/start",
        "turn/steer",
        "turn/interrupt",
        "thread/read",
        "thread/turns/list",
        "thread/turns/items/list",
    }
)


@dataclass(frozen=True)
class JsonRpcMessage:
    method: str | None = None
    params: dict[str, Any] | None = None
    id: int | str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.method is not None:
            data["method"] = self.method
        if self.params is not None:
            data["params"] = self.params
        if self.id is not None:
            data["id"] = self.id
        if self.result is not None:
            data["result"] = self.result
        if self.error is not None:
            data["error"] = self.error
        return data


class CodexProtocolError(RuntimeError):
    pass


class AppServerTransport:
    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    def receive(self) -> dict[str, Any] | None:
        raise NotImplementedError


class InMemoryTransport(AppServerTransport):
    """Deterministic fake transport for protocol and runtime tests."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any] | None] = []
        self.notifications: list[dict[str, Any]] = []

    def queue_response(self, response: dict[str, Any] | None) -> None:
        self.responses.append(response)

    def queue_notification(self, notification: dict[str, Any]) -> None:
        self.notifications.append(notification)

    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        self.sent.append(message)
        if self.responses:
            return self.responses.pop(0)
        return {"id": message.get("id"), "result": {}}

    def receive(self) -> dict[str, Any] | None:
        if self.notifications:
            return self.notifications.pop(0)
        return None


class WebSocketTransport(AppServerTransport):
    """Thin runtime transport for Codex app-server WebSocket JSON-RPC."""

    def __init__(self, endpoint: str, *, bearer_token: str | None = None, timeout_seconds: int = 30) -> None:
        try:
            import websocket  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional runtime package.
            raise RuntimeError("websocket-client is required for V4 Codex app-server WebSocket transport") from exc
        headers = []
        if bearer_token:
            headers.append(f"Authorization: Bearer {bearer_token}")
        self._socket = websocket.create_connection(endpoint, header=headers, timeout=timeout_seconds)

    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        self._socket.send(json.dumps(message, sort_keys=True))
        if "id" not in message:
            return None
        return self.receive()

    def receive(self) -> dict[str, Any] | None:
        raw = self._socket.recv()
        if raw is None:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise CodexProtocolError("Codex app-server sent a non-object JSON-RPC frame")
        return value


class CodexAppServerClient:
    def __init__(self, transport: AppServerTransport, *, client_name: str = "agentic_mesh_v4") -> None:
        self.transport = transport
        self.client_name = client_name
        self._next_id = 1
        self.initialized = False

    def initialize(self) -> dict[str, Any]:
        response = self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": self.client_name,
                    "title": "Agentic Mesh V4",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            require_initialized=False,
        )
        self._notify("initialized", {})
        self.initialized = True
        return response

    def start_thread(self, *, model: str, cwd: str | None = None, sandbox_mode: str | None = None) -> str:
        params: dict[str, Any] = {"model": model}
        if cwd:
            params["cwd"] = cwd
        if sandbox_mode:
            params["sandbox"] = {"mode": sandbox_mode}
        response = self._request("thread/start", params)
        thread = response.get("thread")
        if not isinstance(thread, dict) or not thread.get("id"):
            raise CodexProtocolError("thread/start response did not include thread.id")
        return str(thread["id"])

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        return self._request("thread/resume", {"threadId": thread_id})

    def start_turn(self, *, thread_id: str, text: str, model: str | None = None) -> str | None:
        params: dict[str, Any] = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
        if model:
            params["model"] = model
        response = self._request("turn/start", params)
        turn = response.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
        return None

    def steer_turn(self, *, thread_id: str, text: str) -> dict[str, Any]:
        return self._request(
            "turn/steer",
            {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
        )

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> dict[str, Any]:
        return self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        return self._request("thread/read", {"threadId": thread_id})

    def list_turns(self, thread_id: str, *, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if cursor:
            params["cursor"] = cursor
        return self._request("thread/turns/list", params)

    def list_turn_items(self, thread_id: str, turn_id: str, *, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id, "turnId": turn_id}
        if cursor:
            params["cursor"] = cursor
        return self._request("thread/turns/items/list", params)

    def receive_event(self) -> dict[str, Any] | None:
        return self.transport.receive()

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        require_initialized: bool = True,
    ) -> dict[str, Any]:
        _validate_method(method)
        if require_initialized and not self.initialized:
            raise CodexProtocolError("Codex app-server client is not initialized")
        request_id = self._next_id
        self._next_id += 1
        response = self.transport.send({"method": method, "id": request_id, "params": params})
        if response is None:
            return {}
        if response.get("error"):
            raise CodexProtocolError(str(response["error"]))
        if response.get("id") != request_id:
            raise CodexProtocolError(f"response id mismatch for {method}: {response.get('id')} != {request_id}")
        result = response.get("result") or {}
        if not isinstance(result, dict):
            raise CodexProtocolError(f"response result for {method} must be an object")
        return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        _validate_method(method)
        self.transport.send({"method": method, "params": params})


def load_generated_protocol_methods(schema_dir: str | Path) -> set[str]:
    """Return request methods from a generated Codex app-server schema bundle."""

    schema_path = Path(schema_dir) / "ClientRequest.json"
    raw = json.loads(schema_path.read_text(encoding="utf-8"))
    methods: set[str] = set()
    _collect_const_strings(raw, methods)
    return {method for method in methods if "/" in method or method in {"initialize", "initialized"}}


def app_server_healthz(endpoint: str, *, timeout_seconds: int = 3) -> bool:
    http_url = endpoint.replace("ws://", "http://", 1).replace("wss://", "https://", 1).rstrip("/") + "/healthz"
    try:
        with urlopen(http_url, timeout=timeout_seconds) as response:  # noqa: S310 - endpoint is configured internal infra.
            return 200 <= int(response.status) < 300
    except Exception:
        return False


def _validate_method(method: str) -> None:
    if method not in CODEX_APP_SERVER_METHODS:
        raise CodexProtocolError(f"unsupported Codex app-server method: {method}")


def _collect_const_strings(value: Any, output: set[str]) -> None:
    if isinstance(value, dict):
        const = value.get("const")
        if isinstance(const, str):
            output.add(const)
        for child in value.values():
            _collect_const_strings(child, output)
    elif isinstance(value, list):
        for child in value:
            _collect_const_strings(child, output)
