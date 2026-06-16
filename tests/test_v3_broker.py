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
