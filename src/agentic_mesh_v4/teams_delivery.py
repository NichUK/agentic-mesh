from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol


class TeamsDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class TeamsRoleIdentity:
    role_id: str
    app_id: str
    app_secret: str
    display_name: str


class BotFrameworkTransport(Protocol):
    def post_form(self, url: str, payload: dict[str, str]) -> dict[str, object]:
        ...

    def post_json(self, url: str, payload: dict[str, object], *, authorization: str) -> dict[str, object]:
        ...


class UrlLibBotFrameworkTransport:
    def post_form(self, url: str, payload: dict[str, str]) -> dict[str, object]:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return _read_json_response(request, error_label="Bot Framework token request")

    def post_json(self, url: str, payload: dict[str, object], *, authorization: str) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        return _read_json_response(request, error_label="Bot Framework message send")


class TeamsReplySender:
    def __init__(
        self,
        *,
        transport: BotFrameworkTransport | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.transport = transport or UrlLibBotFrameworkTransport()
        self.tenant_id = tenant_id
        self._token_cache: dict[str, tuple[str, float]] = {}

    @classmethod
    def from_env(cls) -> TeamsReplySender:
        return cls(tenant_id=os.environ.get("AGENTIC_MESH_GRAPH_TENANT_ID"))

    def send_reply(
        self,
        *,
        role_id: str,
        activity: dict[str, object],
        text_markdown: str,
    ) -> str:
        identity = _identity_from_env(role_id)
        service_url = _required_string(activity.get("serviceUrl"), "Teams activity serviceUrl").rstrip("/")
        conversation = activity.get("conversation")
        if not isinstance(conversation, dict):
            raise TeamsDeliveryError("Teams activity conversation is missing")
        conversation_id = _required_string(conversation.get("id"), "Teams conversation id")
        reply_to_id = activity.get("replyToId") or activity.get("id")
        reply_to_id = str(reply_to_id) if isinstance(reply_to_id, str) and reply_to_id else None
        token = self._token(identity)
        target_conversation_id = _teams_thread_conversation_id(conversation_id, reply_to_id)
        target_reply_to_id = None if target_conversation_id != conversation_id else reply_to_id
        endpoint = service_url + f"/v3/conversations/{urllib.parse.quote(target_conversation_id, safe='')}/activities"
        if target_reply_to_id:
            endpoint += f"/{urllib.parse.quote(target_reply_to_id, safe='')}"
        payload: dict[str, object] = {
            "type": "message",
            "text": text_markdown,
            "textFormat": "markdown",
        }
        if reply_to_id:
            payload["replyToId"] = reply_to_id
        response = self.transport.post_json(endpoint, payload, authorization=f"Bearer {token}")
        return _delivery_id(response, endpoint + text_markdown)

    def _token(self, identity: TeamsRoleIdentity) -> str:
        cached = self._token_cache.get(identity.role_id)
        now = time.time()
        if cached is not None and cached[1] > now + 60:
            return cached[0]
        authority = self.tenant_id or "botframework.com"
        response = self.transport.post_form(
            f"https://login.microsoftonline.com/{urllib.parse.quote(authority, safe='')}/oauth2/v2.0/token",
            {
                "grant_type": "client_credentials",
                "client_id": identity.app_id,
                "client_secret": identity.app_secret,
                "scope": "https://api.botframework.com/.default",
            },
        )
        token = response.get("access_token") if isinstance(response, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise TeamsDeliveryError("Bot Framework token response did not include an access token")
        try:
            ttl = float(response.get("expires_in"))
        except (TypeError, ValueError):
            ttl = 1800
        self._token_cache[identity.role_id] = (token.strip(), now + ttl)
        return token.strip()


def _identity_from_env(role_id: str) -> TeamsRoleIdentity:
    env_role = role_id.upper().replace("-", "_")
    app_id = os.environ.get(f"TEAMS_BOT_{env_role}_APP_ID")
    secret = os.environ.get(f"TEAMS_BOT_{env_role}_SECRET")
    if not app_id or not secret:
        raise TeamsDeliveryError(f"Teams bot identity is not configured for role `{role_id}`")
    return TeamsRoleIdentity(
        role_id=role_id,
        app_id=app_id,
        app_secret=secret,
        display_name=f"AM-{role_id.replace('-', ' ').title()}",
    )


def _read_json_response(request: urllib.request.Request, *, error_label: str) -> dict[str, object]:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise TeamsDeliveryError(f"{error_label} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TeamsDeliveryError(f"{error_label} failed: {exc.reason}") from exc
    if not raw.strip():
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def _teams_thread_conversation_id(conversation_id: str, reply_to_id: str | None) -> str:
    if not reply_to_id:
        return conversation_id
    if ";messageid=" in conversation_id.casefold():
        return conversation_id
    if "@thread.tacv2" not in conversation_id:
        return conversation_id
    return f"{conversation_id};messageid={reply_to_id}"


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamsDeliveryError(f"{label} is missing")
    return value.strip()


def _delivery_id(response: dict[str, object], fallback_seed: str) -> str:
    for key in ("id", "activityId"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"bot-framework-message-{hashlib.sha256(fallback_seed.encode('utf-8')).hexdigest()[:16]}"
