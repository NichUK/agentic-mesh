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


PROCESSING_REACTION_NAME = "eyes"
PROCESSING_REACTION_GLYPH = "\U0001F440"
PROCESSING_REACTION_UNSUPPORTED_REASON = (
    "Microsoft Graph reaction route or delegated token is unavailable"
)
MISSING_GRAPH_ROUTE_REASON = "missing_graph_chat_route"
MISSING_DELEGATED_GRAPH_TOKEN_REASON = "missing_delegated_graph_token"


@dataclass(frozen=True)
class TeamsRoleIdentity:
    role_id: str
    app_id: str
    app_secret: str
    display_name: str


@dataclass(frozen=True)
class ProcessingReactionRoute:
    url: str
    route_type: str
    message_id: str


@dataclass(frozen=True)
class ProcessingReactionDiagnostics:
    route_type: str
    message_id: str | None
    unsupported_reason: str | None


class BotFrameworkTransport(Protocol):
    def post_form(self, url: str, payload: dict[str, str]) -> dict[str, object]:
        ...

    def post_json(self, url: str, payload: dict[str, object], *, authorization: str) -> dict[str, object]:
        ...

    def put_json(self, url: str, payload: dict[str, object], *, authorization: str) -> dict[str, object]:
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

    def put_json(self, url: str, payload: dict[str, object], *, authorization: str) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="PUT",
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        return _read_json_response(request, error_label="Bot Framework message update")


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
        return cls(
            tenant_id=(
                os.environ.get("AGENTIC_MESH_GRAPH_TENANT_ID")
                or os.environ.get("AGENTIC_MESH_TENANT_ID")
            )
        )

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
        reply_to_id = activity.get("replyToId") or activity.get("bot_framework_activity_id") or activity.get("id")
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

    def send_decision_card(
        self,
        *,
        role_id: str,
        activity: dict[str, object],
        card: dict[str, object],
    ) -> str:
        identity = _identity_from_env(role_id)
        service_url = _required_string(activity.get("serviceUrl"), "Teams activity serviceUrl").rstrip("/")
        conversation = activity.get("conversation")
        if not isinstance(conversation, dict):
            raise TeamsDeliveryError("Teams activity conversation is missing")
        conversation_id = _required_string(conversation.get("id"), "Teams conversation id")
        token = self._token(identity)
        endpoint = service_url + f"/v3/conversations/{urllib.parse.quote(conversation_id, safe='')}/activities"
        payload: dict[str, object] = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ],
        }
        response = self.transport.post_json(endpoint, payload, authorization=f"Bearer {token}")
        return _delivery_id(response, endpoint + json.dumps(card, sort_keys=True))

    def update_decision_card(
        self,
        *,
        role_id: str,
        activity: dict[str, object],
        activity_id: str,
        card: dict[str, object],
    ) -> str:
        identity = _identity_from_env(role_id)
        service_url = _required_string(activity.get("serviceUrl"), "Teams activity serviceUrl").rstrip("/")
        conversation = activity.get("conversation")
        if not isinstance(conversation, dict):
            raise TeamsDeliveryError("Teams activity conversation is missing")
        conversation_id = _required_string(conversation.get("id"), "Teams conversation id")
        activity_id = _required_string(activity_id, "Teams activity id")
        token = self._token(identity)
        endpoint = (
            service_url
            + f"/v3/conversations/{urllib.parse.quote(conversation_id, safe='')}/activities/"
            + urllib.parse.quote(activity_id, safe="")
        )
        payload: dict[str, object] = {
            "type": "message",
            "id": activity_id,
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ],
        }
        response = self.transport.put_json(endpoint, payload, authorization=f"Bearer {token}")
        return _delivery_id(response, endpoint + json.dumps(card, sort_keys=True))

    def add_processing_reaction(
        self,
        *,
        role_id: str,
        activity: dict[str, object],
    ) -> str | None:
        del role_id
        route = _graph_reaction_route(activity)
        token = _graph_delegated_token_from_env(self.transport, tenant_id=self.tenant_id)
        if route is None or token is None:
            return None
        self.transport.post_json(
            route.url,
            {"reactionType": PROCESSING_REACTION_GLYPH},
            authorization=f"Bearer {token}",
        )
        delivery_seed = route.url + PROCESSING_REACTION_GLYPH
        return f"graph-setReaction-{hashlib.sha256(delivery_seed.encode('utf-8')).hexdigest()[:16]}"

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


def _graph_delegated_token_from_env(transport: BotFrameworkTransport, *, tenant_id: str | None) -> str | None:
    client_id = os.environ.get("AGENTIC_MESH_GRAPH_CLIENT_ID")
    refresh_token = os.environ.get("AGENTIC_MESH_GRAPH_REFRESH_TOKEN")
    scopes = os.environ.get("AGENTIC_MESH_GRAPH_SCOPES")
    authority = tenant_id or os.environ.get("AGENTIC_MESH_GRAPH_TENANT_ID") or os.environ.get("AGENTIC_MESH_TENANT_ID")
    if client_id and refresh_token and scopes and authority:
        response = transport.post_form(
            f"https://login.microsoftonline.com/{urllib.parse.quote(authority, safe='')}/oauth2/v2.0/token",
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "scope": scopes,
            },
        )
        token = response.get("access_token") if isinstance(response, dict) else None
        if isinstance(token, str) and token.strip():
            return token.strip()
    for name in ("AGENTIC_MESH_TEAMS_TOKEN", "AGENTIC_MESH_GRAPH_TOKEN", "AGENTIC_MESH_GRAPH_DELEGATED_TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def processing_reaction_diagnostics(activity: dict[str, object]) -> ProcessingReactionDiagnostics:
    route = _graph_reaction_route(activity)
    if route is not None:
        return ProcessingReactionDiagnostics(
            route_type=route.route_type,
            message_id=route.message_id,
            unsupported_reason=None,
        )
    message_id = _activity_string(activity, "id")
    conversation = activity.get("conversation")
    conversation_type = ""
    if isinstance(conversation, dict):
        conversation_type = (_string_value(conversation.get("conversationType")) or "").casefold()
    if conversation_type == "personal":
        return ProcessingReactionDiagnostics(
            route_type="none",
            message_id=message_id,
            unsupported_reason=MISSING_GRAPH_ROUTE_REASON,
        )
    return ProcessingReactionDiagnostics(
        route_type="none",
        message_id=message_id,
        unsupported_reason="missing_graph_route",
    )


def _graph_reaction_route(activity: dict[str, object]) -> ProcessingReactionRoute | None:
    message_id = _activity_string(activity, "id")
    if message_id is None:
        return None
    base_url = (os.environ.get("AGENTIC_MESH_GRAPH_BASE_URL") or "https://graph.microsoft.com/v1.0").rstrip("/")
    channel_data = activity.get("channelData") if isinstance(activity.get("channelData"), dict) else {}
    conversation = activity.get("conversation")
    conversation_type = ""
    if isinstance(conversation, dict):
        conversation_type = (_string_value(conversation.get("conversationType")) or "").casefold()
    has_explicit_chat_id = (
        _activity_string(activity, "graph_chat_id") is not None
        or _activity_string(activity, "chat_id") is not None
    )
    team_id = (
        _nested_string(channel_data, "team", "id")
        or _activity_string(activity, "team_id")
        or (
            os.environ.get("AGENTIC_MESH_PROJECT_TEAM_ID")
            if conversation_type != "personal" and not has_explicit_chat_id
            else None
        )
    )
    channel_id = (
        _nested_string(channel_data, "channel", "id")
        or _activity_string(activity, "channel_id")
        or (
            os.environ.get("AGENTIC_MESH_PROJECT_CHANNEL_ID")
            if conversation_type != "personal" and not has_explicit_chat_id
            else None
        )
    )
    reply_to_id = _activity_string(activity, "replyToId")
    if team_id and channel_id:
        team = urllib.parse.quote(team_id.strip(), safe="")
        channel = urllib.parse.quote(channel_id.strip(), safe="")
        message = urllib.parse.quote(message_id.strip(), safe="")
        if reply_to_id and reply_to_id != message_id:
            parent = urllib.parse.quote(reply_to_id.strip(), safe="")
            return ProcessingReactionRoute(
                url=f"{base_url}/teams/{team}/channels/{channel}/messages/{parent}/replies/{message}/setReaction",
                route_type="channel_reply",
                message_id=message_id,
            )
        return ProcessingReactionRoute(
            url=f"{base_url}/teams/{team}/channels/{channel}/messages/{message}/setReaction",
            route_type="channel",
            message_id=message_id,
        )
    chat_id = _graph_chat_id(activity)
    if chat_id:
        chat = urllib.parse.quote(chat_id.strip(), safe="")
        message = urllib.parse.quote(message_id.strip(), safe="")
        return ProcessingReactionRoute(
            url=f"{base_url}/chats/{chat}/messages/{message}/setReaction",
            route_type="chat",
            message_id=message_id,
        )
    return None


def _activity_string(activity: dict[str, object], key: str) -> str | None:
    return _string_value(activity.get(key))


def _graph_chat_id(activity: dict[str, object]) -> str | None:
    explicit_chat_id = _activity_string(activity, "graph_chat_id") or _activity_string(activity, "chat_id")
    if explicit_chat_id:
        return explicit_chat_id
    conversation = activity.get("conversation")
    if not isinstance(conversation, dict):
        return None
    conversation_id = _string_value(conversation.get("id"))
    if conversation_id is None:
        return None
    conversation_type = (_string_value(conversation.get("conversationType")) or "").casefold()
    if conversation_type == "personal" and conversation_id.startswith("a:"):
        return None
    return conversation_id


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
