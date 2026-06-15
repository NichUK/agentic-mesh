from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

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

    def __init__(self, broker: BrokerAdapter, *, stream: str = "agent-inbox") -> None:
        self.broker = broker
        self.stream = stream
        self.deliveries: list[OutboundMessage] = []

    def route_inbound(self, message: StakeholderMessage) -> list[str]:
        subjects: list[str] = []
        if message.source_type == "dm":
            role = _role_from_conversation_ref(message.conversation_ref)
            if role is not None:
                subjects.append(f"agent.{role}")
        subjects.extend(f"agent.{role}" for role in message.mentioned_roles)
        if message.source_type == "channel" and not subjects:
            subjects.append("project.context")
        for subject in subjects:
            self.broker.publish(
                self.stream,
                subject,
                {
                    "connector": message.connector,
                    "source_message_id": message.message_id,
                    "source_type": message.source_type,
                    "sender_ref": message.sender_ref,
                    "conversation_ref": message.conversation_ref,
                    "thread_ref": message.thread_ref,
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
    ) -> None:
        self.transport = transport
        self.graph_base_url = graph_base_url.rstrip("/")

    def route_inbound(self, message: StakeholderMessage) -> list[str]:
        raise NotImplementedError("GraphTeamsBridge receives already-normalised inbound events from the listener")

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


def _markdown_to_teams_html(markdown: str) -> str:
    import markdown as markdown_lib

    return markdown_lib.markdown(markdown, extensions=["fenced_code", "tables"])
