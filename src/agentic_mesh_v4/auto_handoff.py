from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from agentic_mesh_v4.auto_dispatch import DispatchResolution
from agentic_mesh_v4.auto_dispatch import DispatchTarget
from agentic_mesh_v4.auto_dispatch import WorkItemDispatchContext
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.db import utc_now


@dataclass(frozen=True)
class AutoDispatchHandoffResult:
    handoff_id: str
    message_id: str
    call_id: str
    work_item_id: str
    from_role: str
    to_role: str
    state: str
    next_action: str


@dataclass(frozen=True)
class AutoDispatchHandoffGroupResult:
    dispatch_group_id: str
    work_item_id: str
    from_role: str
    results: tuple[AutoDispatchHandoffResult, ...]


def create_auto_dispatch_handoff(
    *,
    db: V4Database,
    project_id: str,
    from_role: str,
    work_item: WorkItemDispatchContext,
    resolution: DispatchResolution,
    source_message_id: str | None = None,
    source_turn_id: str | None = None,
    source_thread_id: str | None = None,
    source_correlation_id: str | None = None,
) -> AutoDispatchHandoffResult | None:
    """Create one auto-dispatch handoff/message in one database transaction."""

    if resolution.should_dispatch and len(resolution.targets) != 1:
        raise ValueError("create_auto_dispatch_handoff requires exactly one target; use create_auto_dispatch_handoffs")
    group = create_auto_dispatch_handoffs(
        db=db,
        project_id=project_id,
        from_role=from_role,
        work_item=work_item,
        resolution=resolution,
        source_message_id=source_message_id,
        source_turn_id=source_turn_id,
        source_thread_id=source_thread_id,
        source_correlation_id=source_correlation_id,
    )
    if group is None:
        return None
    return group.results[0]


def create_auto_dispatch_handoffs(
    *,
    db: V4Database,
    project_id: str,
    from_role: str,
    work_item: WorkItemDispatchContext,
    resolution: DispatchResolution,
    source_message_id: str | None = None,
    source_turn_id: str | None = None,
    source_thread_id: str | None = None,
    source_correlation_id: str | None = None,
) -> AutoDispatchHandoffGroupResult | None:
    """Create one or more auto-dispatch handoffs/messages in one database transaction."""

    if not resolution.should_dispatch:
        return None
    if not resolution.targets:
        return None

    role_instance_id = f"{project_id}.{from_role}.1"
    dispatch_group_id = f"dispatch-group-{uuid4().hex}"
    now = utc_now()
    target_states = {target.target_state for target in resolution.targets if target.target_state}
    work_state = next(iter(target_states)) if len(target_states) == 1 else work_item.state
    owner_role = resolution.targets[0].target_role if len(resolution.targets) == 1 else (work_item.owner_role or from_role)
    grouped_next_action = _grouped_next_action(work_item=work_item, targets=resolution.targets)
    planned: list[dict[str, Any]] = []
    for index, target in enumerate(resolution.targets, start=1):
        state = target.target_state or work_state
        next_action = target.next_action or work_item.next_action
        if not next_action.strip():
            next_action = f"Continue work on {work_item.work_item_id}."
        handoff_id = f"handoff-{uuid4().hex}"
        message_id = f"msg-{uuid4().hex}"
        call_id = f"call-{uuid4().hex}"
        reason = target.reason or resolution.source or "auto_dispatch"
        message_text = (
            f"Auto-dispatch handoff for {work_item.work_item_id} from {from_role} to {target.target_role}. "
            f"Next action: {next_action}"
        )
        payload = _handoff_payload(
            work_item=work_item,
            target=target,
            from_role=from_role,
            state=state,
            next_action=next_action,
            reason=reason,
            handoff_id=handoff_id,
            resolution=resolution,
            source_message_id=source_message_id,
            source_turn_id=source_turn_id,
            dispatch_group_id=dispatch_group_id,
            dispatch_group_size=len(resolution.targets),
            dispatch_group_index=index,
        )
        planned.append(
            {
                "target": target,
                "state": state,
                "next_action": next_action,
                "handoff_id": handoff_id,
                "message_id": message_id,
                "call_id": call_id,
                "reason": reason,
                "message_text": message_text,
                "payload": payload,
                "message_payload": {**payload, "target_role": target.target_role, "text": message_text},
                "correlation_id": f"corr-{message_id}",
            }
        )
    group_payload = _group_payload(
        work_item=work_item,
        from_role=from_role,
        dispatch_group_id=dispatch_group_id,
        resolution=resolution,
        planned=planned,
        source_message_id=source_message_id,
        source_turn_id=source_turn_id,
        source_correlation_id=source_correlation_id,
    )

    with db.connection:
        for item in planned:
            target = item["target"]
            db.connection.execute(
                """
                INSERT INTO handoffs(handoff_id, work_item_id, from_role, to_role, reason, status, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (item["handoff_id"], work_item.work_item_id, from_role, target.target_role, item["reason"], "open", now),
            )
            db.connection.execute(
                """
                INSERT INTO safe_output_calls(
                  call_id, role_instance_id, tool_name, payload_json, durable,
                  message_id, turn_id, work_item_id, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    item["call_id"],
                    role_instance_id,
                    "handoff.require",
                    json.dumps(item["payload"], sort_keys=True),
                    1,
                    source_message_id,
                    source_turn_id,
                    work_item.work_item_id,
                    now,
                ),
            )
            db.connection.execute(
                """
                INSERT INTO message_queue(
                  message_id, correlation_id, source, target_role, conversation_ref,
                  thread_ref, text, payload_json, state, steering, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item["message_id"],
                    item["correlation_id"],
                    "auto-dispatch",
                    target.target_role,
                    None,
                    None,
                    item["message_text"],
                    json.dumps(item["message_payload"], sort_keys=True),
                    "queued",
                    0,
                    now,
                    now,
                ),
            )
            db.connection.execute(
                """
                INSERT INTO message_journal(
                  journal_id, message_id, correlation_id, role_instance_id,
                  stage, status, summary, payload_hash, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"journal-{uuid4().hex}",
                    item["message_id"],
                    item["correlation_id"],
                    role_instance_id,
                    "received",
                    "queued",
                    f"Queued auto-dispatch handoff for {target.target_role}",
                    _payload_hash(item["message_payload"]),
                    now,
                ),
            )
            db.connection.execute(
                """
                INSERT INTO message_journal(
                  journal_id, message_id, correlation_id, role_instance_id,
                  stage, status, summary, payload_hash, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"journal-{uuid4().hex}",
                    item["message_id"],
                    item["correlation_id"],
                    role_instance_id,
                    "auto_dispatch_handoff",
                    "created",
                    f"Created handoff {item['handoff_id']} from {from_role} to {target.target_role}",
                    _payload_hash(item["payload"]),
                    now,
                ),
            )
        work_update = db.connection.execute(
            """
            UPDATE work_items
            SET state=?, owner_role=?, next_action=?, updated_at=?
            WHERE work_item_id=?
            """,
            (work_state, owner_role, grouped_next_action, now, work_item.work_item_id),
        )
        if work_update.rowcount != 1:
            raise ValueError(f"unknown work item: {work_item.work_item_id}")
        event_type = "auto_dispatch/handoff_created" if len(planned) == 1 else "auto_dispatch/handoffs_created"
        event_content = (
            f"Created auto-dispatch handoff {planned[0]['handoff_id']} to {planned[0]['target'].target_role}."
            if len(planned) == 1
            else f"Created {len(planned)} auto-dispatch handoffs for {work_item.work_item_id}."
        )
        db.connection.execute(
            """
            INSERT INTO agent_events(
              event_id, role_instance_id, thread_id, turn_id, message_id,
              event_type, content, payload_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                f"event-{uuid4().hex}",
                role_instance_id,
                source_thread_id,
                source_turn_id,
                source_message_id,
                event_type,
                event_content,
                json.dumps(group_payload, sort_keys=True),
                now,
            ),
        )

    results = tuple(
        AutoDispatchHandoffResult(
            handoff_id=str(item["handoff_id"]),
            message_id=str(item["message_id"]),
            call_id=str(item["call_id"]),
            work_item_id=work_item.work_item_id,
            from_role=from_role,
            to_role=str(item["target"].target_role),
            state=str(item["state"]),
            next_action=str(item["next_action"]),
        )
        for item in planned
    )
    return AutoDispatchHandoffGroupResult(
        dispatch_group_id=dispatch_group_id,
        work_item_id=work_item.work_item_id,
        from_role=from_role,
        results=results,
    )


def _handoff_payload(
    *,
    work_item: WorkItemDispatchContext,
    target: DispatchTarget,
    from_role: str,
    state: str,
    next_action: str,
    reason: str,
    handoff_id: str,
    resolution: DispatchResolution,
    source_message_id: str | None,
    source_turn_id: str | None,
    dispatch_group_id: str,
    dispatch_group_size: int,
    dispatch_group_index: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "auto_dispatch": True,
        "work_item_id": work_item.work_item_id,
        "from_role": from_role,
        "to_role": target.target_role,
        "state": state,
        "next_action": next_action,
        "reason": reason,
        "handoff_id": handoff_id,
        "resolution_source": resolution.source,
        "dispatch_group_id": dispatch_group_id,
        "dispatch_group_size": dispatch_group_size,
        "dispatch_group_index": dispatch_group_index,
    }
    if source_message_id is not None:
        payload["source_message_id"] = source_message_id
    if source_turn_id is not None:
        payload["source_turn_id"] = source_turn_id
    if resolution.evidence:
        payload["dispatch_evidence"] = resolution.evidence
    return payload


def _group_payload(
    *,
    work_item: WorkItemDispatchContext,
    from_role: str,
    dispatch_group_id: str,
    resolution: DispatchResolution,
    planned: list[dict[str, Any]],
    source_message_id: str | None,
    source_turn_id: str | None,
    source_correlation_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "auto_dispatch": True,
        "dispatch_group_id": dispatch_group_id,
        "work_item_id": work_item.work_item_id,
        "from_role": from_role,
        "resolution_source": resolution.source,
        "source_message_id": source_message_id,
        "source_turn_id": source_turn_id,
        "source_correlation_id": source_correlation_id,
        "targets": [
            {
                "to_role": item["target"].target_role,
                "handoff_id": item["handoff_id"],
                "message_id": item["message_id"],
                "call_id": item["call_id"],
                "state": item["state"],
                "next_action": item["next_action"],
                "reason": item["reason"],
            }
            for item in planned
        ],
    }
    if resolution.evidence:
        payload["dispatch_evidence"] = resolution.evidence
    return payload


def _grouped_next_action(*, work_item: WorkItemDispatchContext, targets: tuple[DispatchTarget, ...]) -> str:
    if len(targets) == 1:
        return targets[0].next_action or work_item.next_action
    parts = [
        f"{target.target_role}: {target.next_action or work_item.next_action or f'Continue work on {work_item.work_item_id}.'}"
        for target in targets
    ]
    return "Parallel auto-dispatch: " + "; ".join(parts)


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
