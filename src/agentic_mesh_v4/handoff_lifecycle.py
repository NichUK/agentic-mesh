from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from agentic_mesh_v4.auto_dispatch import requires_dispatch_path
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.db import utc_now


ACTIVE_HANDOFF_STATES = {"open", "accepted", "blocked"}
TERMINAL_HANDOFF_STATES = {"completed", "superseded", "cancelled"}
GOVERNANCE_ROLES = {"project-manager", "delivery-manager", "platform-engineer"}


class HandoffLifecycleError(ValueError):
    pass


def create_handoff(
    *,
    db: V4Database,
    from_role: str,
    to_role: str,
    work_item_id: str,
    state: str,
    next_action: str,
    reason: str,
    source_message_id: str | None = None,
    safe_output_call_id: str | None = None,
    message_text: str | None = None,
) -> dict[str, str]:
    handoff_id = f"handoff-{uuid4().hex}"
    now = utc_now()
    with db.connection:
        prior = _active_handoffs(db=db, work_item_id=work_item_id)
        db.connection.execute(
            """
            INSERT INTO handoffs(
              handoff_id, work_item_id, from_role, to_role, reason, status, state,
              actor_role, source_message_id, next_action, updated_at, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                handoff_id,
                work_item_id,
                from_role,
                to_role,
                reason,
                "open",
                "open",
                from_role,
                source_message_id,
                next_action,
                now,
                now,
            ),
        )
        _record_transition(
            db=db,
            handoff_id=handoff_id,
            work_item_id=work_item_id,
            actor_role=from_role,
            from_state=None,
            to_state="open",
            reason=reason,
            message_id=source_message_id,
            safe_output_call_id=safe_output_call_id,
        )
        message_id = db.enqueue_message(
            target_role=to_role,
            text=message_text or f"Handoff for {work_item_id} from {from_role} to {to_role}. Next action: {next_action}",
            source="safe-output",
            payload={
                "work_item_id": work_item_id,
                "from_role": from_role,
                "to_role": to_role,
                "state": state,
                "next_action": next_action,
                "reason": reason,
                "handoff_id": handoff_id,
            },
        )
        db.connection.execute(
            "UPDATE handoffs SET target_message_id=?, updated_at=? WHERE handoff_id=?",
            (message_id, now, handoff_id),
        )
        db.upsert_work_item(work_item_id=work_item_id, state=state, owner_role=to_role, next_action=next_action)
        for item in prior:
            _transition_handoff(
                db=db,
                handoff_id=str(item["handoff_id"]),
                actor_role=from_role,
                to_state="superseded",
                reason=f"Superseded by {handoff_id}: {reason}",
                superseded_by_handoff_id=handoff_id,
                safe_output_call_id=safe_output_call_id,
            )
            _suppress_queued_messages_for_handoff(db=db, handoff_id=str(item["handoff_id"]), state="superseded")
    return {"handoff_id": handoff_id, "message_id": message_id}


def accept_handoff(*, db: V4Database, handoff_id: str, actor_role: str, reason: str, safe_output_call_id: str | None = None) -> dict[str, str]:
    handoff = _handoff(db, handoff_id)
    if actor_role != handoff["to_role"]:
        raise HandoffLifecycleError("only target role can accept handoff")
    _transition_handoff(db=db, handoff_id=handoff_id, actor_role=actor_role, to_state="accepted", reason=reason, safe_output_call_id=safe_output_call_id)
    return {"handoff_id": handoff_id, "state": "accepted"}


def complete_handoff(
    *,
    db: V4Database,
    handoff_id: str,
    actor_role: str,
    next_state: str,
    next_owner_role: str,
    next_action: str,
    reason: str,
    safe_output_call_id: str | None = None,
) -> dict[str, str]:
    handoff = _handoff(db, handoff_id)
    if actor_role not in {handoff["to_role"], handoff["from_role"], *GOVERNANCE_ROLES}:
        raise HandoffLifecycleError("actor is not authorized to complete handoff")
    result = {"handoff_id": handoff_id, "state": "completed"}
    with db.connection:
        _transition_handoff(db=db, handoff_id=handoff_id, actor_role=actor_role, to_state="completed", reason=reason, safe_output_call_id=safe_output_call_id)
        if next_owner_role != actor_role and requires_dispatch_path(state=next_state, next_action=next_action, owner_role=next_owner_role):
            next_handoff = create_handoff(
                db=db,
                from_role=actor_role,
                to_role=next_owner_role,
                work_item_id=str(handoff["work_item_id"]),
                state=next_state,
                next_action=next_action,
                reason=f"Continuation after {handoff_id}: {reason}",
                safe_output_call_id=safe_output_call_id,
            )
            result["next_handoff_id"] = next_handoff["handoff_id"]
            result["next_message_id"] = next_handoff["message_id"]
        else:
            db.upsert_work_item(work_item_id=str(handoff["work_item_id"]), state=next_state, owner_role=next_owner_role, next_action=next_action)
    return result


def supersede_handoff(
    *,
    db: V4Database,
    handoff_id: str,
    actor_role: str,
    superseded_by_handoff_id: str,
    reason: str,
    safe_output_call_id: str | None = None,
) -> dict[str, str]:
    if actor_role not in GOVERNANCE_ROLES:
        raise HandoffLifecycleError("only governance roles can supersede directly")
    _transition_handoff(
        db=db,
        handoff_id=handoff_id,
        actor_role=actor_role,
        to_state="superseded",
        reason=reason,
        superseded_by_handoff_id=superseded_by_handoff_id,
        safe_output_call_id=safe_output_call_id,
    )
    _suppress_queued_messages_for_handoff(db=db, handoff_id=handoff_id, state="superseded")
    return {"handoff_id": handoff_id, "state": "superseded"}


def cancel_handoff(
    *,
    db: V4Database,
    handoff_id: str,
    actor_role: str,
    reason: str,
    owner_role: str,
    next_action: str,
    safe_output_call_id: str | None = None,
) -> dict[str, str]:
    handoff = _handoff(db, handoff_id)
    if actor_role not in {handoff["from_role"], *GOVERNANCE_ROLES}:
        raise HandoffLifecycleError("actor is not authorized to cancel handoff")
    with db.connection:
        _transition_handoff(db=db, handoff_id=handoff_id, actor_role=actor_role, to_state="cancelled", reason=reason, safe_output_call_id=safe_output_call_id)
        _suppress_queued_messages_for_handoff(db=db, handoff_id=handoff_id, state="cancelled")
        db.upsert_work_item(work_item_id=str(handoff["work_item_id"]), owner_role=owner_role, next_action=next_action)
    return {"handoff_id": handoff_id, "state": "cancelled"}


def block_handoff(
    *,
    db: V4Database,
    handoff_id: str,
    actor_role: str,
    reason: str,
    next_action: str,
    safe_output_call_id: str | None = None,
) -> dict[str, str]:
    handoff = _handoff(db, handoff_id)
    if actor_role != handoff["to_role"]:
        raise HandoffLifecycleError("only target role can block handoff")
    with db.connection:
        _transition_handoff(db=db, handoff_id=handoff_id, actor_role=actor_role, to_state="blocked", reason=reason, safe_output_call_id=safe_output_call_id)
        db.connection.execute(
            "UPDATE handoffs SET next_action=?, reason=?, updated_at=? WHERE handoff_id=?",
            (next_action, reason, utc_now(), handoff_id),
        )
        db.upsert_work_item(work_item_id=str(handoff["work_item_id"]), owner_role=actor_role, next_action=next_action)
    return {"handoff_id": handoff_id, "state": "blocked"}


def suppress_terminal_handoff_message(*, db: V4Database, message: Any) -> str | None:
    payload = getattr(message, "payload", None)
    if not isinstance(payload, dict):
        return None
    handoff_id = payload.get("handoff_id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return None
    row = db.connection.execute("SELECT state, status FROM handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
    if row is None:
        return None
    state = str(row["state"] or row["status"] or "")
    if state not in TERMINAL_HANDOFF_STATES:
        return None
    message_state = "cancelled" if state == "cancelled" else "superseded"
    db.mark_message_state(getattr(message, "message_id"), state=message_state, summary=f"Skipped stale handoff message for {handoff_id}; handoff is {state}.")
    return message_state


def _transition_handoff(
    *,
    db: V4Database,
    handoff_id: str,
    actor_role: str,
    to_state: str,
    reason: str,
    superseded_by_handoff_id: str | None = None,
    safe_output_call_id: str | None = None,
) -> None:
    handoff = _handoff(db, handoff_id)
    from_state = str(handoff["state"] or handoff["status"] or "")
    if from_state in TERMINAL_HANDOFF_STATES:
        raise HandoffLifecycleError(f"handoff is already terminal: {from_state}")
    now = utc_now()
    timestamp_column = {
        "accepted": "accepted_at",
        "completed": "completed_at",
        "superseded": "closed_at",
        "cancelled": "closed_at",
    }.get(to_state)
    assignments = "state=?, status=?, actor_role=?, superseded_by_handoff_id=?, updated_at=?"
    values: list[object] = [to_state, to_state, actor_role, superseded_by_handoff_id, now]
    if timestamp_column:
        assignments += f", {timestamp_column}=?"
        values.append(now)
    values.append(handoff_id)
    db.connection.execute(f"UPDATE handoffs SET {assignments} WHERE handoff_id=?", tuple(values))
    _record_transition(
        db=db,
        handoff_id=handoff_id,
        work_item_id=handoff["work_item_id"],
        actor_role=actor_role,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        superseded_by_handoff_id=superseded_by_handoff_id,
        message_id=handoff["target_message_id"],
        safe_output_call_id=safe_output_call_id,
    )


def _record_transition(
    *,
    db: V4Database,
    handoff_id: str,
    work_item_id: str | None,
    actor_role: str,
    from_state: str | None,
    to_state: str,
    reason: str,
    superseded_by_handoff_id: str | None = None,
    message_id: str | None = None,
    safe_output_call_id: str | None = None,
) -> str:
    transition_id = f"transition-{uuid4().hex}"
    db.connection.execute(
        """
        INSERT INTO handoff_transitions(
          transition_id, handoff_id, work_item_id, actor_role, from_state, to_state,
          reason, superseded_by_handoff_id, message_id, safe_output_call_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            transition_id,
            handoff_id,
            work_item_id,
            actor_role,
            from_state,
            to_state,
            reason,
            superseded_by_handoff_id,
            message_id,
            safe_output_call_id,
            utc_now(),
        ),
    )
    return transition_id


def _active_handoffs(*, db: V4Database, work_item_id: str) -> list[dict[str, Any]]:
    return [
        {key: row[key] for key in row.keys()}
        for row in db.connection.execute(
            "SELECT * FROM handoffs WHERE work_item_id=? AND COALESCE(state,status) IN ('open','accepted','blocked') ORDER BY updated_at ASC",
            (work_item_id,),
        )
    ]


def _handoff(db: V4Database, handoff_id: str) -> dict[str, Any]:
    row = db.connection.execute("SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
    if row is None:
        raise HandoffLifecycleError(f"unknown handoff_id: {handoff_id}")
    return {key: row[key] for key in row.keys()}


def _suppress_queued_messages_for_handoff(*, db: V4Database, handoff_id: str, state: str) -> None:
    target_state = "cancelled" if state == "cancelled" else "superseded"
    rows = [
        {key: row[key] for key in row.keys()}
        for row in db.connection.execute("SELECT * FROM message_queue WHERE state IN ('queued','ready') ORDER BY created_at ASC")
    ]
    for row in rows:
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            continue
        if payload.get("handoff_id") != handoff_id:
            continue
        db.connection.execute(
            """
            UPDATE message_queue
            SET state=?, locked_by=NULL, locked_at=NULL, updated_at=?
            WHERE message_id=?
            """,
            (target_state, utc_now(), row["message_id"]),
        )
