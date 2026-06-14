from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.observability import span


@dataclass(frozen=True)
class TeamsSyncResult:
    channels_checked: int
    messages_seen: int
    messages_replayed: int
    duplicates: int
    skipped_bot_messages: int


def sync_project_channel_messages(
    *,
    db: V2Database,
    config: ConnectorConfig,
    graph_client: Any,
    max_messages: int = 25,
    include_replies: bool = True,
) -> TeamsSyncResult:
    adapter = LocalTeamsTestAdapter(db, config)
    adapter.install()
    totals = {
        "channels_checked": 0,
        "messages_seen": 0,
        "messages_replayed": 0,
        "duplicates": 0,
        "skipped_bot_messages": 0,
    }
    for channel_id in _configured_channel_ids(config):
        totals["channels_checked"] += 1
        for message in _list_channel_messages(
            graph_client,
            team_id=config.project_team_ref,
            channel_id=channel_id,
            max_messages=max_messages,
        ):
            _replay_graph_message(
                adapter=adapter,
                config=config,
                channel_id=channel_id,
                graph_message=message,
                totals=totals,
                thread_ref=None,
            )
            if include_replies:
                message_id = str(message.get("id") or "").strip()
                if not message_id:
                    continue
                for reply in _list_channel_replies(
                    graph_client,
                    team_id=config.project_team_ref,
                    channel_id=channel_id,
                    message_id=message_id,
                    max_messages=max_messages,
                ):
                    _replay_graph_message(
                        adapter=adapter,
                        config=config,
                        channel_id=channel_id,
                        graph_message=reply,
                        totals=totals,
                        thread_ref=message_id,
                    )
    return TeamsSyncResult(**totals)


def _replay_graph_message(
    *,
    adapter: LocalTeamsTestAdapter,
    config: ConnectorConfig,
    channel_id: str,
    graph_message: dict[str, Any],
    totals: dict[str, int],
    thread_ref: str | None,
) -> None:
    message_id = str(graph_message.get("id") or "").strip()
    if not message_id:
        return
    totals["messages_seen"] += 1
    sender_ref, sender_display, sender_is_bot = _sender(graph_message)
    if sender_is_bot or _looks_like_agent_sender(sender_display):
        totals["skipped_bot_messages"] += 1
        return
    body = _message_body(graph_message)
    mentioned_roles = _mentioned_roles(graph_message, config=config, body=body)
    event = {
        "event_type": "message.created",
        "message_id": message_id,
        "conversation_ref": channel_id,
        "sender_ref": sender_ref,
        "source_type": "channel",
        "body": body,
        "thread_ref": thread_ref or message_id,
        "reply_to_id": message_id,
        "mentioned_roles": mentioned_roles,
    }
    with span("v2.teams_graph_sync_replay", connector_id=config.connector_id, channel_id=channel_id):
        replayed = adapter.replay_event(event)
    if replayed.duplicate:
        totals["duplicates"] += 1
    else:
        totals["messages_replayed"] += 1


def _configured_channel_ids(config: ConnectorConfig) -> tuple[str, ...]:
    channels = [config.default_project_channel_ref]
    channels.extend(config.channel_bindings)
    return tuple(dict.fromkeys(channel for channel in channels if channel))


def _list_channel_messages(
    graph_client: Any,
    *,
    team_id: str,
    channel_id: str,
    max_messages: int,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"$top": str(max_messages)})
    path = f"/teams/{urllib.parse.quote(team_id, safe='')}/channels/{urllib.parse.quote(channel_id, safe='')}/messages?{query}"
    payload = graph_client.request("GET", path)
    return _value_list(payload)


def _list_channel_replies(
    graph_client: Any,
    *,
    team_id: str,
    channel_id: str,
    message_id: str,
    max_messages: int,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"$top": str(max_messages)})
    path = (
        f"/teams/{urllib.parse.quote(team_id, safe='')}/channels/{urllib.parse.quote(channel_id, safe='')}"
        f"/messages/{urllib.parse.quote(message_id, safe='')}/replies?{query}"
    )
    payload = graph_client.request("GET", path)
    return _value_list(payload)


def _value_list(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    value = payload.get("value")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _sender(graph_message: dict[str, Any]) -> tuple[str, str, bool]:
    sender = graph_message.get("from") if isinstance(graph_message.get("from"), dict) else {}
    application = sender.get("application") if isinstance(sender.get("application"), dict) else None
    user = sender.get("user") if isinstance(sender.get("user"), dict) else None
    if application is not None:
        display = str(application.get("displayName") or application.get("id") or "teams-application")
        return str(application.get("id") or display), display, True
    if user is not None:
        display = str(user.get("displayName") or user.get("id") or "teams-user")
        return str(user.get("id") or display), display, False
    return "unknown-human", "unknown-human", False


def _message_body(graph_message: dict[str, Any]) -> str:
    raw_body = graph_message.get("body") if isinstance(graph_message.get("body"), dict) else {}
    content = str(raw_body.get("content") or "")
    return _plain_text_from_html(content)


def _plain_text_from_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _mentioned_roles(graph_message: dict[str, Any], *, config: ConnectorConfig, body: str) -> list[str]:
    roles: list[str] = []
    mentions = graph_message.get("mentions")
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_text = str(mention.get("mentionText") or "")
            roles.extend(_roles_from_text(mention_text, config=config))
            mentioned = mention.get("mentioned") if isinstance(mention.get("mentioned"), dict) else {}
            application = mentioned.get("application") if isinstance(mentioned.get("application"), dict) else {}
            user = mentioned.get("user") if isinstance(mentioned.get("user"), dict) else {}
            roles.extend(_roles_from_text(str(application.get("displayName") or ""), config=config))
            roles.extend(_roles_from_text(str(user.get("displayName") or ""), config=config))
    roles.extend(_roles_from_text(body, config=config))
    return list(dict.fromkeys(roles))


def _roles_from_text(value: str, *, config: ConnectorConfig) -> list[str]:
    normalized = value.casefold()
    roles: list[str] = []
    for role_id, identity in config.role_identities.items():
        candidates = {
            identity.display_name.casefold(),
            identity.mention_handle.casefold(),
            identity.alias.casefold(),
        }
        if any(candidate and candidate in normalized for candidate in candidates):
            roles.append(role_id)
    return roles


def _looks_like_agent_sender(display_name: str) -> bool:
    normalized = display_name.strip().casefold()
    return normalized.startswith("am-") or normalized.startswith("agentic mesh")
