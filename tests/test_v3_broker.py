from agentic_mesh_v3.broker import InMemoryBrokerAdapter


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
