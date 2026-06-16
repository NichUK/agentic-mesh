from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

import bleach

from agentic_mesh_v3.broker import BrokerAdapter


@dataclass(frozen=True)
class StakeholderMessage:
    connector: str
    message_id: str
    source_type: str
    sender_ref: str
    conversation_ref: str
    text: str
    mentioned_roles: tuple[str, ...] = ()
    thread_ref: str | None = None
    reply_target_ref: str | None = None
    reply_thread_ref: str | None = None


@dataclass(frozen=True)
class OutboundMessage:
    connector: str
    target_ref: str
    text_markdown: str
    thread_ref: str | None = None
    importance: str = "normal"


@dataclass(frozen=True)
class DeliveryReceipt:
    delivery_id: str
    connector: str
    target_ref: str
    thread_ref: str | None


class StakeholderBridge(Protocol):
    def route_inbound(self, message: StakeholderMessage) -> list[str]:
        """Route inbound stakeholder text to agent inbox subjects and shared context."""

    def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """Send a stakeholder-facing message."""


class LocalTeamsBridge:
    """Teams-shaped bridge with no Graph dependency.

    Real Teams wiring should adapt Bot Framework/Graph events into this
    connector-neutral shape and preserve DMs/thread refs.
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        *,
        stream: str = "agent-inbox",
        role_ids: tuple[str, ...] = (),
        team_wide_trigger: str = "@all-agents",
    ) -> None:
        self.broker = broker
        self.stream = stream
        self.role_ids = role_ids
        self.team_wide_trigger = team_wide_trigger
        self.deliveries: list[OutboundMessage] = []

    def route_inbound(self, message: StakeholderMessage) -> list[str]:
        subjects: list[str] = []
        if message.source_type == "dm":
            role = _role_from_conversation_ref(message.conversation_ref)
            if role is not None:
                _append_unique(subjects, f"agent.{role}")
        elif message.source_type == "channel":
            _append_unique(subjects, "project.context")
            if message.mentioned_roles:
                for role in message.mentioned_roles:
                    _append_unique(subjects, f"agent.{role}")
            else:
                for role in self.role_ids:
                    _append_unique(subjects, f"agent.{role}.relevance")
        for subject in subjects:
            self.broker.publish(
                self.stream,
                subject,
                {
                    "message_type": "stakeholder.message",
                    "connector": message.connector,
                    "source_message_id": message.message_id,
                    "source_type": message.source_type,
                    "route_type": _route_type(message, subject, self.team_wide_trigger),
                    "sender_ref": message.sender_ref,
                    "conversation_ref": message.conversation_ref,
                    "thread_ref": message.thread_ref,
                    "reply_target_ref": message.reply_target_ref,
                    "reply_thread_ref": message.reply_thread_ref,
                    "text": message.text,
                },
            )
        return subjects

    def send(self, message: OutboundMessage) -> DeliveryReceipt:
        self.deliveries.append(message)
        return DeliveryReceipt(
            delivery_id=f"delivery-{uuid4().hex}",
            connector=message.connector,
            target_ref=message.target_ref,
            thread_ref=message.thread_ref,
        )


def _role_from_conversation_ref(conversation_ref: str) -> str | None:
    if conversation_ref.startswith("dm:"):
        return conversation_ref.removeprefix("dm:")
    return None


def _append_unique(subjects: list[str], subject: str) -> None:
    if subject not in subjects:
        subjects.append(subject)


def _route_type(message: StakeholderMessage, subject: str, team_wide_trigger: str) -> str:
    if message.source_type == "dm":
        return "role_dm"
    if subject == "project.context":
        return "project_channel_context"
    if subject.endswith(".relevance"):
        return (
            "team_wide_relevance_check"
            if team_wide_trigger and team_wide_trigger.casefold() in message.text.casefold()
            else "project_channel_relevance_check"
        )
    return "mentioned_role_message"


class GraphTeamsBridge:
    """Microsoft Graph-backed outbound Teams bridge.

    Inbound Teams events normally arrive through Bot Framework or Graph
    subscriptions and are normalised into `StakeholderMessage` before routing.
    This class handles outbound markdown/HTML delivery with an injectable
    transport for tests and enterprise authentication.
    """

    def __init__(
        self,
        *,
        transport: "GraphTeamsTransport",
        graph_base_url: str = "https://graph.microsoft.com/v1.0",
        inbound_bridge: StakeholderBridge | None = None,
        inbound_broker: BrokerAdapter | None = None,
        inbound_stream: str = "agent-inbox",
        role_ids: tuple[str, ...] = (),
        team_wide_trigger: str = "@all-agents",
    ) -> None:
        self.transport = transport
        self.graph_base_url = graph_base_url.rstrip("/")
        if inbound_bridge is not None and inbound_broker is not None:
            raise ValueError("configure either inbound_bridge or inbound_broker, not both")
        self.inbound_bridge = inbound_bridge
        if inbound_broker is not None:
            self.inbound_bridge = LocalTeamsBridge(
                inbound_broker,
                stream=inbound_stream,
                role_ids=role_ids,
                team_wide_trigger=team_wide_trigger,
            )

    def route_inbound(self, message: StakeholderMessage) -> list[str]:
        if self.inbound_bridge is None:
            raise RuntimeError("GraphTeamsBridge inbound routing requires inbound_bridge or inbound_broker")
        return self.inbound_bridge.route_inbound(message)

    def send(self, message: OutboundMessage) -> DeliveryReceipt:
        endpoint = self._endpoint_for(message)
        response = self.transport.post_json(
            endpoint,
            {
                "body": {
                    "contentType": "html",
                    "content": _markdown_to_teams_html(message.text_markdown),
                },
                "importance": message.importance,
            },
        )
        return DeliveryReceipt(
            delivery_id=str(response.get("id") or f"delivery-{uuid4().hex}"),
            connector=message.connector,
            target_ref=message.target_ref,
            thread_ref=message.thread_ref,
        )

    def _endpoint_for(self, message: OutboundMessage) -> str:
        if message.target_ref.startswith("chat:"):
            chat_id = message.target_ref.removeprefix("chat:")
            if message.thread_ref:
                return f"{self.graph_base_url}/chats/{chat_id}/messages/{message.thread_ref}/replies"
            return f"{self.graph_base_url}/chats/{chat_id}/messages"
        if message.target_ref.startswith("team:") and "/channel:" in message.target_ref:
            team_part, channel_part = message.target_ref.split("/channel:", 1)
            team_id = team_part.removeprefix("team:")
            channel_id = channel_part
            if message.thread_ref:
                return (
                    f"{self.graph_base_url}/teams/{team_id}/channels/{channel_id}"
                    f"/messages/{message.thread_ref}/replies"
                )
            return f"{self.graph_base_url}/teams/{team_id}/channels/{channel_id}/messages"
        raise ValueError(f"unsupported Teams target_ref: {message.target_ref}")


class GraphTeamsTransport(Protocol):
    def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        """POST a JSON payload and return parsed response data."""


class UrlLibGraphTeamsTransport:
    def __init__(self, *, access_token: str) -> None:
        self.access_token = access_token

    def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        import json
        import urllib.request

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))


def _markdown_to_teams_html(markdown: str) -> str:
    import markdown as markdown_lib

    cleaned_source = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", "", markdown)
    rendered = markdown_lib.markdown(cleaned_source, extensions=["fenced_code", "tables"])
    return bleach.clean(
        rendered,
        tags={
            "a",
            "blockquote",
            "br",
            "code",
            "em",
            "h1",
            "h2",
            "h3",
            "h4",
            "hr",
            "li",
            "ol",
            "p",
            "pre",
            "strong",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "ul",
        },
        attributes={"a": ["href", "title"], "code": ["class"]},
        strip=True,
        strip_comments=True,
    )
