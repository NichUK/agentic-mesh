from __future__ import annotations

from collections import defaultdict
from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True)
class BrokerMessage:
    message_id: str
    subject: str
    payload: dict[str, object]
    created_at: str
    delivery_count: int = 0


@dataclass(frozen=True)
class BrokerConsumer:
    stream: str
    consumer: str
    filter_subject: str | None = None


@dataclass(frozen=True)
class BrokerDepth:
    stream: str
    pending: int
    consumers: dict[str, int] = field(default_factory=dict)


class BrokerAdapter(Protocol):
    """Durable inbox/outbox adapter boundary for v3 agents."""

    def ensure_stream(self, stream: str, subjects: list[str]) -> None:
        """Create or update a stream/topic namespace."""

    def ensure_consumer(
        self, stream: str, consumer: str, *, filter_subject: str | None = None
    ) -> BrokerConsumer:
        """Create or update a durable consumer."""

    def publish(
        self, stream: str, subject: str, payload: dict[str, object], *, message_id: str | None = None
    ) -> BrokerMessage:
        """Publish a message into a stream."""

    def fetch(self, stream: str, consumer: str, *, batch: int = 1) -> list[BrokerMessage]:
        """Fetch messages for a durable consumer without acknowledging them."""

    def ack(self, stream: str, consumer: str, message_id: str) -> None:
        """Acknowledge successful processing."""

    def nack(self, stream: str, consumer: str, message_id: str, *, reason: str) -> None:
        """Reject processing and leave the message available for retry/dead-letter policy."""

    def depth(self, stream: str) -> BrokerDepth:
        """Return inspectable pending depth for reporting and hibernation decisions."""


class InMemoryBrokerAdapter:
    """Contract-test adapter with NATS-like durable consumer semantics."""

    def __init__(self) -> None:
        self._subjects: dict[str, set[str]] = defaultdict(set)
        self._messages: dict[str, deque[BrokerMessage]] = defaultdict(deque)
        self._consumers: dict[tuple[str, str], BrokerConsumer] = {}
        self._inflight: dict[tuple[str, str], dict[str, BrokerMessage]] = defaultdict(dict)

    def ensure_stream(self, stream: str, subjects: list[str]) -> None:
        if not stream:
            raise ValueError("stream is required")
        self._subjects[stream].update(subjects)
        self._messages.setdefault(stream, deque())

    def ensure_consumer(
        self, stream: str, consumer: str, *, filter_subject: str | None = None
    ) -> BrokerConsumer:
        if stream not in self._messages:
            raise ValueError(f"stream does not exist: {stream}")
        if not consumer:
            raise ValueError("consumer is required")
        broker_consumer = BrokerConsumer(stream=stream, consumer=consumer, filter_subject=filter_subject)
        self._consumers[(stream, consumer)] = broker_consumer
        self._inflight.setdefault((stream, consumer), {})
        return broker_consumer

    def publish(
        self, stream: str, subject: str, payload: dict[str, object], *, message_id: str | None = None
    ) -> BrokerMessage:
        if stream not in self._messages:
            raise ValueError(f"stream does not exist: {stream}")
        allowed_subjects = self._subjects.get(stream, set())
        if allowed_subjects and subject not in allowed_subjects and "*" not in allowed_subjects:
            raise ValueError(f"subject {subject!r} is not configured for stream {stream!r}")
        message = BrokerMessage(
            message_id=message_id or f"msg-{uuid4().hex}",
            subject=subject,
            payload=dict(payload),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._messages[stream].append(message)
        return message

    def fetch(self, stream: str, consumer: str, *, batch: int = 1) -> list[BrokerMessage]:
        broker_consumer = self._consumers.get((stream, consumer))
        if broker_consumer is None:
            raise ValueError(f"consumer does not exist: {stream}/{consumer}")
        if batch < 1:
            raise ValueError("batch must be positive")
        fetched: list[BrokerMessage] = []
        kept: deque[BrokerMessage] = deque()
        while self._messages[stream]:
            message = self._messages[stream].popleft()
            if len(fetched) < batch and self._matches(broker_consumer, message):
                fetched.append(message)
                self._inflight[(stream, consumer)][message.message_id] = message
            else:
                kept.append(message)
        self._messages[stream].extendleft(reversed(kept))
        return fetched

    def ack(self, stream: str, consumer: str, message_id: str) -> None:
        self._inflight[(stream, consumer)].pop(message_id, None)

    def nack(self, stream: str, consumer: str, message_id: str, *, reason: str) -> None:
        message = self._inflight[(stream, consumer)].pop(message_id, None)
        if message is None:
            return
        retried = BrokerMessage(
            message_id=message.message_id,
            subject=message.subject,
            payload={**message.payload, "last_nack_reason": reason},
            created_at=message.created_at,
            delivery_count=message.delivery_count + 1,
        )
        self._messages[stream].appendleft(retried)

    def depth(self, stream: str) -> BrokerDepth:
        if stream not in self._messages:
            raise ValueError(f"stream does not exist: {stream}")
        consumer_depths = {
            consumer: len(messages)
            for (candidate_stream, consumer), messages in self._inflight.items()
            if candidate_stream == stream
        }
        return BrokerDepth(stream=stream, pending=len(self._messages[stream]), consumers=consumer_depths)

    @staticmethod
    def _matches(consumer: BrokerConsumer, message: BrokerMessage) -> bool:
        return consumer.filter_subject is None or consumer.filter_subject == message.subject


class NatsJetStreamAdapter:
    """NATS JetStream adapter placeholder behind the v3 broker port."""

    def __init__(self, servers: str) -> None:
        self.servers = servers

    def ensure_stream(self, stream: str, subjects: list[str]) -> None:
        raise NotImplementedError("NATS JetStream client wiring belongs in the adapter slice")

    def ensure_consumer(
        self, stream: str, consumer: str, *, filter_subject: str | None = None
    ) -> BrokerConsumer:
        raise NotImplementedError("NATS JetStream client wiring belongs in the adapter slice")

    def publish(
        self, stream: str, subject: str, payload: dict[str, object], *, message_id: str | None = None
    ) -> BrokerMessage:
        raise NotImplementedError("NATS JetStream client wiring belongs in the adapter slice")

    def fetch(self, stream: str, consumer: str, *, batch: int = 1) -> list[BrokerMessage]:
        raise NotImplementedError("NATS JetStream client wiring belongs in the adapter slice")

    def ack(self, stream: str, consumer: str, message_id: str) -> None:
        raise NotImplementedError("NATS JetStream client wiring belongs in the adapter slice")

    def nack(self, stream: str, consumer: str, message_id: str, *, reason: str) -> None:
        raise NotImplementedError("NATS JetStream client wiring belongs in the adapter slice")

    def depth(self, stream: str) -> BrokerDepth:
        raise NotImplementedError("NATS JetStream client wiring belongs in the adapter slice")
