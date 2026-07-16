from __future__ import annotations

import fcntl
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from agentic_mesh_v4.db import TERMINAL_MESSAGE_STATES


CONTROL_SCHEMA = "agentic-mesh-v4-detached-turn-control-v1"
MAX_CONTROL_SECONDS = 15 * 60


@dataclass(frozen=True)
class DetachedTurnControlEvaluation:
    requested: bool
    authorized: bool
    reason: str
    handoff_id: str | None = None
    target_message_id: str | None = None


def evaluate_detached_turn_control(
    *,
    db: Any,
    role_instance_id: str,
    message_id: str,
    turn_id: str | None,
    now: datetime | None = None,
) -> DetachedTurnControlEvaluation:
    """Authorize the single detached Platform control case without weakening completion."""

    if not turn_id:
        return DetachedTurnControlEvaluation(False, False, "missing_turn_id")
    calls = db.list_safe_output_calls(
        role_instance_id=role_instance_id,
        message_id=message_id,
        turn_id=turn_id,
    )
    controls: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for call in calls:
        if str(call.get("tool_name") or "") != "handoff.require":
            continue
        payload = _json_mapping(call.get("payload_json"))
        control = payload.get("detached_turn_control")
        if isinstance(control, dict):
            controls.append((call, payload))
    if not controls:
        return DetachedTurnControlEvaluation(False, False, "not_requested")
    if len(controls) != 1:
        return DetachedTurnControlEvaluation(True, False, "duplicate_control")

    call, payload = controls[0]
    control = payload["detached_turn_control"]
    current = now or datetime.now(UTC)
    expires_at = _parse_timestamp(control.get("expires_at"))
    created_at = _parse_timestamp(call.get("created_at"))
    if expires_at is None or created_at is None:
        return DetachedTurnControlEvaluation(True, False, "invalid_control_time")
    if created_at > current or expires_at <= current:
        return DetachedTurnControlEvaluation(True, False, "expired_control")
    if expires_at <= created_at or (expires_at - created_at).total_seconds() > MAX_CONTROL_SECONDS:
        return DetachedTurnControlEvaluation(True, False, "invalid_control_window")

    source = db.connection.execute(
        """
        SELECT message_id, target_role, state, locked_by
        FROM message_queue WHERE message_id=?
        """,
        (message_id,),
    ).fetchone()
    expected_role_instance = str(control.get("role_instance_id") or "")
    if (
        source is None
        or str(source["target_role"]) != "platform-engineer"
        or str(source["state"]) != "active_turn"
        or str(source["locked_by"] or "") != role_instance_id
        or expected_role_instance != role_instance_id
        or str(payload.get("from_role") or "") != "platform-engineer"
        or str(payload.get("to_role") or "") != "project-manager"
        or str(call.get("work_item_id") or "") != str(payload.get("work_item_id") or "")
    ):
        return DetachedTurnControlEvaluation(True, False, "source_identity_mismatch")

    active_turn = db.connection.execute(
        """
        SELECT turn_id, message_id, status FROM codex_turns WHERE turn_id=?
        """,
        (turn_id,),
    ).fetchone()
    if (
        active_turn is None
        or str(active_turn["message_id"] or "") != message_id
        or str(active_turn["status"] or "") != "active"
    ):
        return DetachedTurnControlEvaluation(True, False, "turn_identity_mismatch")

    handoff_id = str(payload.get("handoff_id") or "")
    continuation = db.connection.execute(
        """
        SELECT h.handoff_id, h.work_item_id, h.from_role, h.to_role, h.status,
               h.source_message_id, h.target_message_id,
               m.target_role, m.state, m.delivery_attempts, m.locked_by, m.locked_at
        FROM handoffs h
        JOIN message_queue m ON m.message_id=h.target_message_id
        WHERE h.handoff_id=?
        """,
        (handoff_id,),
    ).fetchone()
    if (
        continuation is None
        or str(continuation["work_item_id"]) != str(payload.get("work_item_id") or "")
        or str(continuation["from_role"]) != "platform-engineer"
        or str(continuation["to_role"]) != "project-manager"
        or str(continuation["status"]) not in {"open", "accepted"}
        or str(continuation["source_message_id"] or "") != message_id
        or str(continuation["target_role"]) != "project-manager"
        or str(continuation["state"]) != "queued"
        or int(continuation["delivery_attempts"] or 0) != 0
        or continuation["locked_by"] is not None
        or continuation["locked_at"] is not None
    ):
        return DetachedTurnControlEvaluation(True, False, "continuation_mismatch")

    target_message_id = str(continuation["target_message_id"])
    rows = db.connection.execute("SELECT message_id, state FROM message_queue").fetchall()
    nonterminal_ids = {
        str(row["message_id"])
        for row in rows
        if str(row["state"] or "") not in TERMINAL_MESSAGE_STATES
    }
    if nonterminal_ids != {message_id, target_message_id}:
        return DetachedTurnControlEvaluation(
            True,
            False,
            "additional_nonterminal_traffic",
            handoff_id=handoff_id,
            target_message_id=target_message_id,
        )

    lease_error = _validate_live_lease(
        control=control,
        message_id=message_id,
        turn_id=turn_id,
        work_item_id=str(payload.get("work_item_id") or ""),
        current=current,
    )
    if lease_error:
        return DetachedTurnControlEvaluation(
            True,
            False,
            lease_error,
            handoff_id=handoff_id,
            target_message_id=target_message_id,
        )
    return DetachedTurnControlEvaluation(
        True,
        True,
        "authorized",
        handoff_id=handoff_id,
        target_message_id=target_message_id,
    )


def _validate_live_lease(
    *,
    control: dict[str, Any],
    message_id: str,
    turn_id: str,
    work_item_id: str,
    current: datetime,
) -> str | None:
    raw_path = str(control.get("lease_path") or "")
    if not raw_path or not os.path.isabs(raw_path):
        return "invalid_lease_path"
    path = Path(raw_path)
    try:
        metadata = path.lstat()
    except OSError:
        return "process_lease_missing"
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return "invalid_lease_file"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return "process_lease_unreadable"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            process_alive = True
        else:
            process_alive = False
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        if not process_alive:
            return "detached_process_lost"
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
            lease = json.load(handle)
    except (OSError, ValueError, TypeError):
        return "invalid_process_lease"
    finally:
        os.close(descriptor)
    if not isinstance(lease, dict):
        return "invalid_process_lease"
    if (
        lease.get("schema") != CONTROL_SCHEMA
        or lease.get("state") != "waiting"
        or lease.get("dispatcher_state") != "held"
        or str(lease.get("dispatcher_hold_id") or "") != str(control.get("dispatcher_hold_id") or "")
        or str(lease.get("process_id") or "") != str(control.get("process_id") or "")
        or str(lease.get("message_id") or "") != message_id
        or str(lease.get("turn_id") or "") != turn_id
        or str(lease.get("work_item_id") or "") != work_item_id
    ):
        return "dispatcher_or_process_mismatch"
    lease_expiry = _parse_timestamp(lease.get("expires_at"))
    control_expiry = _parse_timestamp(control.get("expires_at"))
    if lease_expiry is None or lease_expiry != control_expiry or lease_expiry <= current:
        return "stale_process_lease"
    return None


def _json_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)
