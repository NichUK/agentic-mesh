from __future__ import annotations

from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.broker import BrokerMessage


def broker_message_dict(message: BrokerMessage) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "subject": message.subject,
        "payload": message.payload,
        "created_at": message.created_at,
        "delivery_count": message.delivery_count,
    }


def broker_inspection_payload(
    broker: BrokerAdapter,
    *,
    stream: str,
    consumer: str | None = None,
    limit: int = 20,
    role_ids: tuple[str, ...] = (),
    instance_id: str = "1",
) -> dict[str, object]:
    if limit < 1:
        raise ValueError("limit must be positive")
    payload: dict[str, object] = {
        "stream": stream,
        "consumer": consumer,
        "pending": [broker_message_dict(message) for message in broker.pending(stream, consumer, limit=limit)],
        "dead_letters": [broker_message_dict(message) for message in broker.dead_letters(stream, limit=limit)],
    }
    if role_ids:
        payload["role_consumers"] = [
            {
                "role_id": role_id,
                "consumer": role_consumer_name(role_id, instance_id),
                **role_consumer_depth(broker, stream=stream, role_id=role_id, instance_id=instance_id, limit=limit),
            }
            for role_id in role_ids
        ]
    return payload


def role_consumer_name(role_id: str, instance_id: str) -> str:
    role_id = role_id.strip()
    instance_id = instance_id.strip()
    if not role_id:
        raise ValueError("role id is required")
    if not instance_id:
        raise ValueError("instance id is required")
    return f"{role_id}.{instance_id}"


def role_consumer_depth(
    broker: BrokerAdapter,
    *,
    stream: str,
    role_id: str,
    instance_id: str,
    limit: int,
) -> dict[str, object]:
    consumer = role_consumer_name(role_id, instance_id)
    try:
        priority_consumer = f"{consumer}.priority"
        broker.ensure_consumer(stream, priority_consumer, filter_subject=f"agent.{role_id}.priority")
        broker.ensure_consumer(stream, consumer, filter_subject=f"agent.{role_id}")
        pending = [
            *broker.pending(stream, priority_consumer, limit=max(limit, 10_000)),
            *broker.pending(stream, consumer, limit=max(limit, 10_000)),
        ]
    except Exception as exc:  # pragma: no cover - exercised by live adapters.
        return {"pending_count": None, "error": str(exc)}
    return {"pending_count": len(pending)}
