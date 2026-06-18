import agentic_mesh_v3.broker as broker_module
from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.broker import NatsJetStreamAdapter
from agentic_mesh_v3.broker import build_broker_adapter


class FakeConsumerConfig:
    def __init__(
        self,
        *,
        durable_name: str,
        ack_policy: str,
        filter_subject: str | None = None,
        deliver_policy: str | None = None,
        ack_wait: float | None = None,
        max_deliver: int | None = None,
    ) -> None:
        self.durable_name = durable_name
        self.ack_policy = ack_policy
        self.filter_subject = filter_subject
        self.deliver_policy = deliver_policy
        self.ack_wait = ack_wait
        self.max_deliver = max_deliver


class FakeNatsMessage:
    subject = "agent.product-manager"
    data = b'{"text": "hello"}'

    async def ack(self) -> None:
        self.acked = True


class FakeJetStream:
    def __init__(
        self,
        *,
        consumer_exists: bool = False,
        existing_filter_subject: str | None = None,
        existing_deliver_policy: str = "new",
        existing_ack_wait: float = broker_module.NATS_CONSUMER_ACK_WAIT_SECONDS,
        existing_max_deliver: int = broker_module.NATS_CONSUMER_MAX_DELIVER,
    ) -> None:
        self.consumer_exists = consumer_exists
        self.existing_filter_subject = existing_filter_subject
        self.existing_deliver_policy = existing_deliver_policy
        self.existing_ack_wait = existing_ack_wait
        self.existing_max_deliver = existing_max_deliver
        self.added_config: object | None = None
        self.consumer_info_calls: list[tuple[str, str]] = []
        self.delete_consumer_calls: list[tuple[str, str]] = []

    async def consumer_info(self, stream: str, consumer: str) -> object:
        self.consumer_info_calls.append((stream, consumer))
        if self.consumer_exists:
            config = type(
                "FakeExistingConsumerConfig",
                (),
                {
                    "filter_subject": self.existing_filter_subject,
                    "deliver_policy": self.existing_deliver_policy,
                    "ack_wait": self.existing_ack_wait,
                    "max_deliver": self.existing_max_deliver,
                },
            )()
            return type("FakeExistingConsumerInfo", (), {"config": config})()
        raise RuntimeError("consumer not found")

    async def add_consumer(self, stream: str, *, config: object) -> None:
        self.added_stream = stream
        self.added_config = config

    async def delete_consumer(self, stream: str, consumer: str) -> None:
        self.delete_consumer_calls.append((stream, consumer))


class FakeNatsSequence:
    stream = 42


class FakeNatsMetadata:
    sequence = FakeNatsSequence()
    num_delivered = 1


class FakeNatsConnection:
    def __init__(self, jetstream: FakeJetStream) -> None:
        self._jetstream = jetstream
        self.closed = False

    def jetstream(self) -> FakeJetStream:
        return self._jetstream

    async def close(self) -> None:
        self.closed = True


class FakePullNatsMessage:
    subject = "agent.product-manager"
    data = b'{"text": "shape"}'
    metadata = FakeNatsMetadata()
    acked = False

    async def ack(self) -> None:
        self.acked = True


class FakePullSubscription:
    async def fetch(self, *, batch: int, timeout: int) -> list[FakePullNatsMessage]:
        assert batch == 1
        assert timeout == 1
        return [FakePullNatsMessage()]


class FakePullJetStream(FakeJetStream):
    def __init__(self) -> None:
        super().__init__(consumer_exists=True)
        self.pull_subscribe_calls: list[tuple[str, str, str]] = []

    async def pull_subscribe(self, subject: str, *, durable: str, stream: str) -> FakePullSubscription:
        self.pull_subscribe_calls.append((subject, durable, stream))
        return FakePullSubscription()


class FakeStreamJetStream(FakeJetStream):
    def __init__(self, *, existing_subjects: list[str] | None = None) -> None:
        super().__init__()
        self.existing_subjects = existing_subjects
        self.add_stream_calls: list[tuple[str, list[str]]] = []
        self.update_stream_calls: list[tuple[str, list[str]]] = []

    async def stream_info(self, stream: str) -> object:
        if self.existing_subjects is not None:
            config = type("FakeStreamConfig", (), {"subjects": self.existing_subjects})()
            return type("FakeStreamInfo", (), {"config": config})()
        raise RuntimeError(f"missing stream: {stream}")

    async def add_stream(self, *, name: str, subjects: list[str]) -> None:
        self.add_stream_calls.append((name, subjects))

    async def update_stream(self, *, name: str, subjects: list[str]) -> None:
        self.update_stream_calls.append((name, subjects))


class FakeDeadLetterJetStream(FakeJetStream):
    def __init__(self) -> None:
        super().__init__()
        self.publish_calls: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.publish_calls.append((subject, data))


def test_in_memory_broker_publish_fetch_ack() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")
    published = broker.publish("agent-inbox", "agent.product-manager", {"text": "hello"})

    fetched = broker.fetch("agent-inbox", "pm-1")

    assert [message.message_id for message in fetched] == [published.message_id]
    assert broker.depth("agent-inbox").consumers["pm-1"] == 1

    broker.ack("agent-inbox", "pm-1", published.message_id)

    assert broker.depth("agent-inbox").consumers["pm-1"] == 0


def test_in_memory_broker_nack_requeues_with_reason() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")
    published = broker.publish("agent-inbox", "agent.product-manager", {"text": "hello"})
    fetched = broker.fetch("agent-inbox", "pm-1")[0]

    broker.nack("agent-inbox", "pm-1", fetched.message_id, reason="try again")
    retried = broker.fetch("agent-inbox", "pm-1")[0]

    assert retried.message_id == published.message_id
    assert retried.delivery_count == 1
    assert retried.payload["last_nack_reason"] == "try again"


def test_in_memory_broker_inspects_pending_without_claiming() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "agent.engineering"])
    broker.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")
    first = broker.publish("agent-inbox", "agent.product-manager", {"text": "shape"})
    second = broker.publish("agent-inbox", "agent.engineering", {"text": "build"})

    pending_for_pm = broker.pending("agent-inbox", "pm-1")
    pending_all = broker.pending("agent-inbox")

    assert [message.message_id for message in pending_for_pm] == [first.message_id]
    assert [message.message_id for message in pending_all] == [first.message_id, second.message_id]
    assert broker.depth("agent-inbox").pending == 2


def test_in_memory_broker_supports_wildcard_stream_subjects() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.*", "project.>"])

    product = broker.publish("agent-inbox", "agent.product-manager", {"text": "shape"})
    project = broker.publish("agent-inbox", "project.agentic-mesh.context", {"text": "shared"})

    assert [message.message_id for message in broker.pending("agent-inbox")] == [
        product.message_id,
        project.message_id,
    ]
    try:
        broker.publish("agent-inbox", "system.product-manager.internal", {"text": "not configured"})
    except ValueError as exc:
        assert "not configured" in str(exc)
    else:
        raise AssertionError("wildcard stream subjects should still reject unmatched subjects")


def test_in_memory_broker_supports_wildcard_consumer_filters() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.>", "system.>"])
    broker.ensure_consumer("agent-inbox", "all-agents", filter_subject="agent.*")
    broker.ensure_consumer("agent-inbox", "all-project", filter_subject="agent.project.>")
    product = broker.publish("agent-inbox", "agent.product-manager", {"text": "shape"})
    project_context = broker.publish("agent-inbox", "agent.project.context", {"text": "shared"})
    broker.publish("agent-inbox", "system.operator", {"text": "ignore"})

    agent_messages = broker.fetch("agent-inbox", "all-agents", batch=10)
    project_messages = broker.fetch("agent-inbox", "all-project", batch=10)

    assert [message.message_id for message in agent_messages] == [product.message_id]
    assert [message.message_id for message in project_messages] == [project_context.message_id]


def test_in_memory_broker_dead_letters_inflight_message() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")
    published = broker.publish("agent-inbox", "agent.product-manager", {"text": "hello"})
    fetched = broker.fetch("agent-inbox", "pm-1")[0]

    broker.dead_letter("agent-inbox", "pm-1", fetched.message_id, reason="poison message")

    assert broker.depth("agent-inbox").pending == 0
    assert broker.depth("agent-inbox").consumers["pm-1"] == 0
    dead = broker.dead_letters("agent-inbox")[0]
    assert dead.message_id == published.message_id
    assert dead.payload["dead_letter_reason"] == "poison message"


def test_build_broker_adapter_supports_in_memory_and_nats() -> None:
    assert isinstance(build_broker_adapter(adapter="in-memory"), InMemoryBrokerAdapter)
    assert isinstance(
        build_broker_adapter(adapter="nats-jetstream", servers="nats://localhost:4222"),
        NatsJetStreamAdapter,
    )


def test_nats_ensure_consumer_uses_nats_consumer_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakeJetStream()
    fake_connection = FakeNatsConnection(fake_js)

    async def fake_connect() -> FakeNatsConnection:
        return fake_connection

    monkeypatch.setattr(adapter, "_connect", fake_connect)
    monkeypatch.setattr(broker_module, "_import_nats_consumer_config", lambda: FakeConsumerConfig)

    consumer = adapter.ensure_consumer(
        "agent-inbox",
        "pm-1",
        filter_subject="agent.product-manager",
    )

    assert consumer.consumer == "pm-1"
    assert fake_js.consumer_info_calls == [("agent-inbox", "pm-1")]
    assert isinstance(fake_js.added_config, FakeConsumerConfig)
    assert fake_js.added_config.durable_name == "pm-1"
    assert fake_js.added_config.ack_policy == "explicit"
    assert fake_js.added_config.filter_subject == "agent.product-manager"
    assert fake_js.added_config.deliver_policy == "new"
    assert fake_js.added_config.ack_wait == broker_module.NATS_CONSUMER_ACK_WAIT_SECONDS
    assert fake_js.added_config.max_deliver == broker_module.NATS_CONSUMER_MAX_DELIVER
    assert fake_connection.closed is True


def test_nats_ensure_consumer_normalizes_role_instance_consumer_names(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakeJetStream()

    async def fake_connect() -> FakeNatsConnection:
        return FakeNatsConnection(fake_js)

    monkeypatch.setattr(adapter, "_connect", fake_connect)
    monkeypatch.setattr(broker_module, "_import_nats_consumer_config", lambda: FakeConsumerConfig)

    consumer = adapter.ensure_consumer(
        "agent-inbox",
        "agentic-mesh-dev.product-manager.1",
        filter_subject="agent.product-manager",
    )

    assert consumer.consumer == "agentic-mesh-dev.product-manager.1"
    assert fake_js.consumer_info_calls == [("agent-inbox", "agentic-mesh-dev_product-manager_1")]
    assert isinstance(fake_js.added_config, FakeConsumerConfig)
    assert fake_js.added_config.durable_name == "agentic-mesh-dev_product-manager_1"


def test_nats_ensure_consumer_keeps_existing_durable_consumer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakeJetStream(consumer_exists=True, existing_filter_subject="agent.product-manager")
    fake_connection = FakeNatsConnection(fake_js)

    async def fake_connect() -> FakeNatsConnection:
        return fake_connection

    monkeypatch.setattr(adapter, "_connect", fake_connect)
    monkeypatch.setattr(broker_module, "_import_nats_consumer_config", lambda: FakeConsumerConfig)

    adapter.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")

    assert fake_js.consumer_info_calls == [("agent-inbox", "pm-1")]
    assert fake_js.added_config is None
    assert fake_connection.closed is True


def test_nats_ensure_consumer_recreates_wrong_filter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakeJetStream(consumer_exists=True, existing_filter_subject="agent.product-manager.relevance")
    fake_connection = FakeNatsConnection(fake_js)

    async def fake_connect() -> FakeNatsConnection:
        return fake_connection

    monkeypatch.setattr(adapter, "_connect", fake_connect)
    monkeypatch.setattr(broker_module, "_import_nats_consumer_config", lambda: FakeConsumerConfig)

    adapter.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")

    assert fake_js.delete_consumer_calls == [("agent-inbox", "pm-1")]
    assert isinstance(fake_js.added_config, FakeConsumerConfig)
    assert fake_js.added_config.filter_subject == "agent.product-manager"


def test_nats_ensure_consumer_recreates_short_ack_wait(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakeJetStream(
        consumer_exists=True,
        existing_filter_subject="agent.product-manager",
        existing_ack_wait=30.0,
        existing_max_deliver=broker_module.NATS_CONSUMER_MAX_DELIVER,
    )
    fake_connection = FakeNatsConnection(fake_js)

    async def fake_connect() -> FakeNatsConnection:
        return fake_connection

    monkeypatch.setattr(adapter, "_connect", fake_connect)
    monkeypatch.setattr(broker_module, "_import_nats_consumer_config", lambda: FakeConsumerConfig)

    adapter.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")

    assert fake_js.delete_consumer_calls == [("agent-inbox", "pm-1")]
    assert isinstance(fake_js.added_config, FakeConsumerConfig)
    assert fake_js.added_config.ack_wait == broker_module.NATS_CONSUMER_ACK_WAIT_SECONDS


def test_nats_ensure_consumer_recreates_historical_replay_policy(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakeJetStream(
        consumer_exists=True,
        existing_filter_subject="agent.product-manager",
        existing_deliver_policy="all",
    )
    fake_connection = FakeNatsConnection(fake_js)

    async def fake_connect() -> FakeNatsConnection:
        return fake_connection

    monkeypatch.setattr(adapter, "_connect", fake_connect)
    monkeypatch.setattr(broker_module, "_import_nats_consumer_config", lambda: FakeConsumerConfig)

    adapter.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")

    assert fake_js.delete_consumer_calls == [("agent-inbox", "pm-1")]
    assert isinstance(fake_js.added_config, FakeConsumerConfig)
    assert fake_js.added_config.deliver_policy == "new"


def test_nats_fetch_accepts_property_metadata_and_uses_normalized_durable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakePullJetStream()
    fake_connection = FakeNatsConnection(fake_js)

    async def fake_connect() -> FakeNatsConnection:
        return fake_connection

    monkeypatch.setattr(adapter, "_connect", fake_connect)

    messages = adapter.fetch("agent-inbox", "agentic-mesh-dev.product-manager.1")

    assert fake_js.pull_subscribe_calls == [
        ("", "agentic-mesh-dev_product-manager_1", "agent-inbox")
    ]
    assert len(messages) == 1
    assert messages[0].message_id == "agent.product-manager:42"
    assert messages[0].payload == {"text": "shape"}
    assert messages[0].delivery_count == 0
    assert fake_connection.closed is False
    adapter.ack("agent-inbox", "agentic-mesh-dev.product-manager.1", "agent.product-manager:42")
    assert fake_connection.closed is True


def test_nats_ensure_stream_allows_nested_dead_letter_subjects(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakeStreamJetStream()

    async def fake_connect() -> FakeNatsConnection:
        return FakeNatsConnection(fake_js)

    monkeypatch.setattr(adapter, "_connect", fake_connect)

    adapter.ensure_stream("agent-inbox", ["agent.>"])

    assert fake_js.add_stream_calls == [
        ("agent-inbox", ["agent.>", "deadletter.agent-inbox.>"])
    ]


def test_nats_ensure_stream_merges_existing_subjects(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakeStreamJetStream(existing_subjects=["agent.product-manager"])

    async def fake_connect() -> FakeNatsConnection:
        return FakeNatsConnection(fake_js)

    monkeypatch.setattr(adapter, "_connect", fake_connect)

    adapter.ensure_stream("agent-inbox", ["agent.engineering"])

    assert fake_js.add_stream_calls == []
    assert fake_js.update_stream_calls == [
        ("agent-inbox", ["agent.product-manager", "agent.engineering", "deadletter.agent-inbox.>"])
    ]


def test_nats_dead_letter_uses_normalized_nested_subject(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = NatsJetStreamAdapter("nats://localhost:4222")
    fake_js = FakeDeadLetterJetStream()

    async def fake_connect() -> FakeNatsConnection:
        return FakeNatsConnection(fake_js)

    monkeypatch.setattr(adapter, "_connect", fake_connect)

    adapter._acked_messages[
        ("agent-inbox", "agentic-mesh-dev.product-manager.1", "agent.product-manager:1")
    ] = FakeNatsMessage()
    adapter.dead_letter(
        "agent-inbox",
        "agentic-mesh-dev.product-manager.1",
        "agent.product-manager:1",
        reason="bad payload",
    )

    assert fake_js.publish_calls
    assert fake_js.publish_calls[0][0] == (
        "deadletter.agent-inbox.agentic-mesh-dev_product-manager_1"
    )


def test_nats_broker_dead_letters_are_inspectable_without_claiming_more_work() -> None:
    adapter = NatsJetStreamAdapter("nats://localhost:4222")

    async def fake_dead_letter(stream, consumer, message_id, message, *, reason):  # noqa: ANN001
        assert stream == "agent-inbox"
        assert consumer == "pm-1"
        assert message_id == "agent.product-manager:1"
        assert reason == "poison message"
        await message.ack()

    adapter._dead_letter = fake_dead_letter  # type: ignore[method-assign]
    adapter._acked_messages[("agent-inbox", "pm-1", "agent.product-manager:1")] = FakeNatsMessage()

    adapter.dead_letter("agent-inbox", "pm-1", "agent.product-manager:1", reason="poison message")

    assert ("agent-inbox", "pm-1", "agent.product-manager:1") not in adapter._acked_messages
    dead = adapter.dead_letters("agent-inbox")
    assert len(dead) == 1
    assert dead[0].message_id == "agent.product-manager:1"
    assert dead[0].subject == "agent.product-manager"
    assert dead[0].payload == {"text": "hello", "dead_letter_reason": "poison message"}


def test_nats_broker_pending_decodes_inflight_payload() -> None:
    pending = [
        broker_module._nats_broker_message_record(  # type: ignore[attr-defined]
            message_id="agent.product-manager:1",
            message=FakeNatsMessage(),
        )
    ]

    assert len(pending) == 1
    assert pending[0].message_id == "agent.product-manager:1"
    assert pending[0].subject == "agent.product-manager"
    assert pending[0].payload == {"text": "hello"}


def test_build_broker_adapter_requires_nats_servers() -> None:
    try:
        build_broker_adapter(adapter="nats-jetstream")
    except ValueError as exc:
        assert "requires servers" in str(exc)
    else:
        raise AssertionError("NATS adapter should require servers")
