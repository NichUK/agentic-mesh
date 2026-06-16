from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.broker import NatsJetStreamAdapter
from agentic_mesh_v3.broker import build_broker_adapter


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


def test_build_broker_adapter_requires_nats_servers() -> None:
    try:
        build_broker_adapter(adapter="nats-jetstream")
    except ValueError as exc:
        assert "requires servers" in str(exc)
    else:
        raise AssertionError("NATS adapter should require servers")
