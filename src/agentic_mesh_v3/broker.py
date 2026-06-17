from __future__ import annotations

from collections import defaultdict
from collections import deque
import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
import inspect
import re
from typing import Protocol
from uuid import uuid4


NATS_CONSUMER_ACK_WAIT_SECONDS = 3600.0
NATS_CONSUMER_MAX_DELIVER = 3


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

    def dead_letter(self, stream: str, consumer: str, message_id: str, *, reason: str) -> None:
        """Move an in-flight message to dead letter storage."""

    def pending(self, stream: str, consumer: str | None = None, *, limit: int = 20) -> list[BrokerMessage]:
        """Inspect queued and in-flight pending messages without claiming more work."""

    def dead_letters(self, stream: str, *, limit: int = 20) -> list[BrokerMessage]:
        """Inspect dead-lettered messages for operator/agent recovery."""

    def depth(self, stream: str) -> BrokerDepth:
        """Return inspectable pending depth for reporting and hibernation decisions."""


class InMemoryBrokerAdapter:
    """Contract-test adapter with NATS-like durable consumer semantics."""

    def __init__(self) -> None:
        self._subjects: dict[str, set[str]] = defaultdict(set)
        self._messages: dict[str, deque[BrokerMessage]] = defaultdict(deque)
        self._consumers: dict[tuple[str, str], BrokerConsumer] = {}
        self._inflight: dict[tuple[str, str], dict[str, BrokerMessage]] = defaultdict(dict)
        self._dead_letters: dict[str, deque[BrokerMessage]] = defaultdict(deque)

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
        if allowed_subjects and not any(_subject_matches(pattern, subject) for pattern in allowed_subjects):
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

    def dead_letter(self, stream: str, consumer: str, message_id: str, *, reason: str) -> None:
        message = self._inflight[(stream, consumer)].pop(message_id, None)
        if message is None:
            return
        dead = BrokerMessage(
            message_id=message.message_id,
            subject=message.subject,
            payload={**message.payload, "dead_letter_reason": reason},
            created_at=message.created_at,
            delivery_count=message.delivery_count,
        )
        self._dead_letters[stream].append(dead)

    def pending(self, stream: str, consumer: str | None = None, *, limit: int = 20) -> list[BrokerMessage]:
        if stream not in self._messages:
            raise ValueError(f"stream does not exist: {stream}")
        if limit < 1:
            raise ValueError("limit must be positive")
        messages: list[BrokerMessage] = []
        if consumer is None:
            messages.extend(list(self._messages[stream]))
            for (candidate_stream, _), inflight in self._inflight.items():
                if candidate_stream == stream:
                    messages.extend(inflight.values())
        else:
            broker_consumer = self._consumers.get((stream, consumer))
            if broker_consumer is None:
                raise ValueError(f"consumer does not exist: {stream}/{consumer}")
            messages.extend(message for message in self._messages[stream] if self._matches(broker_consumer, message))
            messages.extend(self._inflight[(stream, consumer)].values())
        return messages[:limit]

    def dead_letters(self, stream: str, *, limit: int = 20) -> list[BrokerMessage]:
        if limit < 1:
            raise ValueError("limit must be positive")
        return list(self._dead_letters[stream])[:limit]

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
        return consumer.filter_subject is None or _subject_matches(consumer.filter_subject, message.subject)


def _subject_matches(pattern: str, subject: str) -> bool:
    """Return whether a NATS-style subject pattern matches a concrete subject."""

    pattern_tokens = pattern.split(".") if pattern else []
    subject_tokens = subject.split(".") if subject else []
    for index, token in enumerate(pattern_tokens):
        if token == ">":
            return index == len(pattern_tokens) - 1
        if index >= len(subject_tokens):
            return False
        if token != "*" and token != subject_tokens[index]:
            return False
    return len(pattern_tokens) == len(subject_tokens)


class NatsJetStreamAdapter:
    """NATS JetStream adapter behind the v3 broker port.

    The adapter keeps product code behind the synchronous `BrokerAdapter`
    boundary while using `nats-py` internally. It connects per operation for a
    simple local-first implementation; a later enterprise tuning slice can add
    connection pooling without changing the port.
    """

    def __init__(self, servers: str) -> None:
        self.servers = servers
        self._acked_messages: dict[tuple[str, str, str], object] = {}
        self._dead_letters: dict[str, deque[BrokerMessage]] = defaultdict(deque)
        self._loop = asyncio.new_event_loop()

    def _run(self, operation):  # noqa: ANN001
        if self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(operation)

    def ensure_stream(self, stream: str, subjects: list[str]) -> None:
        self._run(self._ensure_stream(stream, subjects))

    def ensure_consumer(
        self, stream: str, consumer: str, *, filter_subject: str | None = None
    ) -> BrokerConsumer:
        self._run(self._ensure_consumer(stream, consumer, filter_subject=filter_subject))
        return BrokerConsumer(stream=stream, consumer=consumer, filter_subject=filter_subject)

    def publish(
        self, stream: str, subject: str, payload: dict[str, object], *, message_id: str | None = None
    ) -> BrokerMessage:
        return self._run(self._publish(stream, subject, payload, message_id=message_id))

    def fetch(self, stream: str, consumer: str, *, batch: int = 1) -> list[BrokerMessage]:
        return self._run(self._fetch(stream, consumer, batch=batch))

    def ack(self, stream: str, consumer: str, message_id: str) -> None:
        entry = self._acked_messages.pop((stream, consumer, message_id), None)
        if entry is not None:
            self._run(_ack_nats_entry(entry))

    def nack(self, stream: str, consumer: str, message_id: str, *, reason: str) -> None:
        entry = self._acked_messages.pop((stream, consumer, message_id), None)
        if entry is not None:
            self._run(_nak_nats_entry(entry, reason=reason))

    def dead_letter(self, stream: str, consumer: str, message_id: str, *, reason: str) -> None:
        entry = self._acked_messages.pop((stream, consumer, message_id), None)
        if entry is not None:
            message = _nats_entry_message(entry)
            self._run(self._dead_letter(stream, consumer, message_id, entry, reason=reason))
            self._dead_letters[stream].append(
                _nats_dead_letter_record(message_id=message_id, message=message, reason=reason)
            )

    def pending(self, stream: str, consumer: str | None = None, *, limit: int = 20) -> list[BrokerMessage]:
        if limit < 1:
            raise ValueError("limit must be positive")
        return self._run(self._pending(stream, consumer, limit=limit))

    def dead_letters(self, stream: str, *, limit: int = 20) -> list[BrokerMessage]:
        if limit < 1:
            raise ValueError("limit must be positive")
        return list(self._dead_letters[stream])[:limit]

    def depth(self, stream: str) -> BrokerDepth:
        return self._run(self._depth(stream))

    async def _connect(self):
        nats = _import_nats()
        return await nats.connect(servers=[self.servers])

    async def _ensure_stream(self, stream: str, subjects: list[str]) -> None:
        nc = await self._connect()
        try:
            js = nc.jetstream()
            stream_subjects = list(dict.fromkeys([*subjects, f"deadletter.{stream}.>"]))
            try:
                info = await js.stream_info(stream)
                existing_subjects = list(getattr(getattr(info, "config", None), "subjects", None) or [])
                merged_subjects = list(dict.fromkeys([*existing_subjects, *stream_subjects]))
                await js.update_stream(name=stream, subjects=merged_subjects)
            except Exception:
                await js.add_stream(name=stream, subjects=stream_subjects)
        finally:
            await nc.close()

    async def _ensure_consumer(self, stream: str, consumer: str, *, filter_subject: str | None) -> None:
        nc = await self._connect()
        try:
            js = nc.jetstream()
            durable_name = _nats_consumer_name(consumer)
            ConsumerConfig = _import_nats_consumer_config()
            config = ConsumerConfig(
                durable_name=durable_name,
                ack_policy="explicit",
                filter_subject=filter_subject,
                ack_wait=NATS_CONSUMER_ACK_WAIT_SECONDS,
                max_deliver=NATS_CONSUMER_MAX_DELIVER,
            )
            try:
                info = await js.consumer_info(stream, durable_name)
                existing_config = getattr(info, "config", None)
                existing_filter = getattr(existing_config, "filter_subject", None)
                existing_ack_wait = getattr(existing_config, "ack_wait", None)
                existing_max_deliver = getattr(existing_config, "max_deliver", None)
                if (
                    existing_filter == filter_subject
                    and existing_ack_wait == NATS_CONSUMER_ACK_WAIT_SECONDS
                    and existing_max_deliver == NATS_CONSUMER_MAX_DELIVER
                ):
                    return
                await js.delete_consumer(stream, durable_name)
            except Exception:
                pass
            await js.add_consumer(stream, config=config)
        finally:
            await nc.close()

    async def _publish(
        self, stream: str, subject: str, payload: dict[str, object], *, message_id: str | None
    ) -> BrokerMessage:
        del stream
        import json

        nc = await self._connect()
        try:
            js = nc.jetstream()
            headers = {"Nats-Msg-Id": message_id} if message_id else None
            ack = await js.publish(subject, json.dumps(payload).encode("utf-8"), headers=headers)
            return BrokerMessage(
                message_id=message_id or f"{subject}:{ack.seq}",
                subject=subject,
                payload=dict(payload),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            await nc.close()

    async def _fetch(self, stream: str, consumer: str, *, batch: int) -> list[BrokerMessage]:
        import json

        nc = await self._connect()
        keep_connection_open = False
        try:
            js = nc.jetstream()
            subscription = await js.pull_subscribe("", durable=_nats_consumer_name(consumer), stream=stream)
            try:
                raw_messages = await subscription.fetch(batch=batch, timeout=1)
            except Exception as exc:
                if type(exc).__name__ == "TimeoutError":
                    return []
                raise
            keep_connection_open = bool(raw_messages)
            messages: list[BrokerMessage] = []
            for raw in raw_messages:
                payload = json.loads(raw.data.decode("utf-8")) if raw.data else {}
                metadata_value = raw.metadata
                metadata = await metadata_value if inspect.isawaitable(metadata_value) else metadata_value
                message_id = f"{raw.subject}:{metadata.sequence.stream}"
                self._acked_messages[(stream, consumer, message_id)] = (raw, nc)
                messages.append(
                    BrokerMessage(
                        message_id=message_id,
                        subject=raw.subject,
                        payload=payload if isinstance(payload, dict) else {"value": payload},
                        created_at=datetime.now(timezone.utc).isoformat(),
                        delivery_count=max(metadata.num_delivered - 1, 0),
                    )
                )
            return messages
        finally:
            if not keep_connection_open:
                await nc.close()

    async def _dead_letter(
        self,
        stream: str,
        consumer: str,
        message_id: str,
        entry: object,
        *,
        reason: str,
    ) -> None:
        import json

        message = _nats_entry_message(entry)
        nc = await self._connect()
        try:
            js = nc.jetstream()
            await js.publish(
                f"deadletter.{stream}.{_nats_consumer_name(consumer)}",
                json.dumps({"message_id": message_id, "reason": reason}).encode("utf-8"),
            )
            await message.ack()  # type: ignore[attr-defined]
        finally:
            await nc.close()
            await _nats_entry_close(entry)

    async def _depth(self, stream: str) -> BrokerDepth:
        nc = await self._connect()
        try:
            js = nc.jetstream()
            info = await js.stream_info(stream)
            return BrokerDepth(stream=stream, pending=int(info.state.messages), consumers={})
        finally:
            await nc.close()

    async def _pending(self, stream: str, consumer: str | None, *, limit: int) -> list[BrokerMessage]:
        nc = await self._connect()
        try:
            js = nc.jetstream()
            if consumer is None:
                info = await js.stream_info(stream)
                return _synthetic_pending_messages(
                    count=int(getattr(info.state, "messages", 0) or 0),
                    limit=limit,
                    stream=stream,
                    subject="",
                )
            info = await js.consumer_info(stream, _nats_consumer_name(consumer))
            config = getattr(info, "config", None)
            subject = str(getattr(config, "filter_subject", "") or "")
            count = int(getattr(info, "num_pending", 0) or 0) + int(
                getattr(info, "num_ack_pending", 0) or 0
            )
            return _synthetic_pending_messages(
                count=count,
                limit=limit,
                stream=stream,
                subject=subject,
                consumer=consumer,
            )
        finally:
            await nc.close()


def build_broker_adapter(*, adapter: str, servers: str | None = None) -> BrokerAdapter:
    if adapter == "in-memory":
        return InMemoryBrokerAdapter()
    if adapter == "nats-jetstream":
        if not servers:
            raise ValueError("NATS JetStream broker requires servers")
        return NatsJetStreamAdapter(servers)
    raise ValueError(f"unsupported broker adapter: {adapter}")


def _import_nats():
    try:
        import nats
    except ImportError as exc:
        raise RuntimeError("NATS JetStream adapter requires the optional `nats-py` package") from exc
    return nats


def _import_nats_consumer_config():
    try:
        from nats.js.api import ConsumerConfig
    except ImportError as exc:
        raise RuntimeError("NATS JetStream adapter requires the optional `nats-py` package") from exc
    return ConsumerConfig


def _nats_consumer_name(consumer: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", consumer.strip()).strip("_")
    if not normalized:
        raise ValueError("consumer is required")
    return normalized


def _nats_dead_letter_record(*, message_id: str, message: object, reason: str) -> BrokerMessage:
    record = _nats_broker_message_record(message_id=message_id, message=message)
    return BrokerMessage(
        message_id=record.message_id,
        subject=record.subject,
        payload={**record.payload, "dead_letter_reason": reason},
        created_at=record.created_at,
        delivery_count=record.delivery_count,
    )


def _nats_broker_message_record(*, message_id: str, message: object) -> BrokerMessage:
    import json

    payload: object = {}
    data = getattr(message, "data", b"")
    if data:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {"raw": repr(data)}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return BrokerMessage(
        message_id=message_id,
        subject=getattr(message, "subject", ""),
        payload=payload,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _synthetic_pending_messages(
    *,
    count: int,
    limit: int,
    stream: str,
    subject: str,
    consumer: str | None = None,
) -> list[BrokerMessage]:
    return [
        BrokerMessage(
            message_id=f"{stream}:{consumer or 'stream'}:pending:{index}",
            subject=subject,
            payload={"pending": True},
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        for index in range(min(count, limit))
    ]


def _nats_entry_message(entry: object) -> object:
    if isinstance(entry, tuple) and entry:
        return entry[0]
    return entry


async def _nats_entry_close(entry: object) -> None:
    if isinstance(entry, tuple) and len(entry) > 1:
        connection = entry[1]
        await connection.close()  # type: ignore[attr-defined]


async def _ack_nats_entry(entry: object) -> None:
    try:
        await _ack_nats_message(_nats_entry_message(entry))
    finally:
        await _nats_entry_close(entry)


async def _nak_nats_entry(entry: object, *, reason: str) -> None:
    try:
        await _nak_nats_message(_nats_entry_message(entry), reason=reason)
    finally:
        await _nats_entry_close(entry)


async def _ack_nats_message(message: object) -> None:
    await message.ack()  # type: ignore[attr-defined]


async def _nak_nats_message(message: object, *, reason: str) -> None:
    del reason
    await message.nak()  # type: ignore[attr-defined]
