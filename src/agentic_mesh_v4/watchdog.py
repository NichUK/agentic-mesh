from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.auto_dispatch import ACTIVE_WORK_STATES
from agentic_mesh_v4.auto_dispatch import HUMAN_WAIT_WORK_STATES
from agentic_mesh_v4.auto_dispatch import TERMINAL_WORK_STATES
from agentic_mesh_v4.auto_dispatch import looks_actionable_next_action
from agentic_mesh_v4.auto_dispatch import requires_dispatch_path


ACTIVE_MESSAGE_STATES = {"active_turn", "delivering", "queued", "ready"}
ACTIVE_HANDOFF_STATES = {"open", "accepted", "blocked"}
@dataclass(frozen=True)
class WatchdogThresholds:
    queued_seconds: int = 30 * 60
    delivering_seconds: int = 10 * 60
    active_turn_seconds: int = 30 * 60
    failed_seconds: int = 0
    open_handoff_seconds: int = 10 * 60


@dataclass(frozen=True)
class WatchdogFinding:
    finding_key: str
    finding_type: str
    severity: str
    work_item_id: str | None = None
    handoff_id: str | None = None
    message_id: str | None = None
    target_role: str | None = None
    owner_role: str | None = None
    evidence: dict[str, Any] | None = None
    next_action: str = ""


def run_watchdog_sweep(
    *,
    db: V4Database,
    initiator_role: str,
    mode: str = "manual",
    thresholds: WatchdogThresholds | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or WatchdogThresholds()
    now = now or datetime.now(UTC)
    sweep_run_id = db.start_watchdog_sweep(initiator_role=initiator_role, mode=mode)
    findings = collect_watchdog_findings(db=db, thresholds=thresholds, now=now)
    active_keys = {finding.finding_key for finding in findings}
    for finding in findings:
        db.upsert_watchdog_finding(
            sweep_run_id=sweep_run_id,
            finding_key=finding.finding_key,
            finding_type=finding.finding_type,
            severity=finding.severity,
            work_item_id=finding.work_item_id,
            handoff_id=finding.handoff_id,
            message_id=finding.message_id,
            target_role=finding.target_role,
            owner_role=finding.owner_role,
            evidence=finding.evidence,
            next_action=finding.next_action,
        )
    resolved = db.resolve_watchdog_findings(active_finding_keys=active_keys, sweep_run_id=sweep_run_id)
    summary = _summary(findings=findings, resolved=resolved)
    db.finish_watchdog_sweep(sweep_run_id=sweep_run_id, status="completed", summary=summary)
    return {"sweep_run_id": sweep_run_id, **summary}


def collect_watchdog_findings(
    *,
    db: V4Database,
    thresholds: WatchdogThresholds | None = None,
    now: datetime | None = None,
) -> list[WatchdogFinding]:
    thresholds = thresholds or WatchdogThresholds()
    now = now or datetime.now(UTC)
    messages = db.list_messages()
    handoffs = [_row_dict(row) for row in db.connection.execute("SELECT * FROM handoffs ORDER BY created_at ASC")]
    work_items = [_row_dict(row) for row in db.connection.execute("SELECT * FROM work_items ORDER BY updated_at DESC")]
    completion_diagnostics = [
        _row_dict(row)
        for row in db.connection.execute(
            "SELECT * FROM turn_completion_diagnostics ORDER BY created_at DESC"
        )
    ]
    findings: list[WatchdogFinding] = []
    findings.extend(_handoff_findings(handoffs=handoffs, messages=messages, thresholds=thresholds, now=now))
    findings.extend(_message_findings(messages=messages, thresholds=thresholds, now=now))
    findings.extend(_planned_not_dispatched_findings(work_items=work_items, handoffs=handoffs, messages=messages))
    findings.extend(_completion_diagnostic_findings(completion_diagnostics))
    return findings


def collect_dispatch_invariant_findings(
    *,
    db: V4Database,
    exclude_message_ids: set[str] | None = None,
) -> list[WatchdogFinding]:
    exclude_message_ids = exclude_message_ids or set()
    messages = [
        item
        for item in db.list_messages()
        if str(item.get("message_id") or "") not in exclude_message_ids
    ]
    handoffs = [_row_dict(row) for row in db.connection.execute("SELECT * FROM handoffs ORDER BY created_at ASC")]
    work_items = [_row_dict(row) for row in db.connection.execute("SELECT * FROM work_items ORDER BY updated_at DESC")]
    return _planned_not_dispatched_findings(work_items=work_items, handoffs=handoffs, messages=messages)


def _handoff_findings(
    *,
    handoffs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    thresholds: WatchdogThresholds,
    now: datetime,
) -> list[WatchdogFinding]:
    findings: list[WatchdogFinding] = []
    active_by_work_item: dict[str, list[dict[str, Any]]] = {}
    for handoff in handoffs:
        status = str(handoff.get("status") or "")
        if status not in ACTIVE_HANDOFF_STATES:
            continue
        work_item_id = _string(handoff.get("work_item_id"))
        if work_item_id:
            active_by_work_item.setdefault(work_item_id, []).append(handoff)
        age_seconds = int(_age_seconds(handoff.get("created_at"), now=now))
        if age_seconds < thresholds.open_handoff_seconds:
            continue
        target_role = _string(handoff.get("to_role"))
        if _has_live_message_for_handoff(messages=messages, handoff=handoff, target_role=target_role):
            continue
        findings.append(
            WatchdogFinding(
                finding_key=f"open_handoff_without_message:{handoff['handoff_id']}",
                finding_type="open_handoff_without_message",
                severity="high",
                work_item_id=work_item_id,
                handoff_id=str(handoff["handoff_id"]),
                target_role=target_role,
                owner_role=target_role,
                evidence={
                    "handoff_id": handoff["handoff_id"],
                    "from_role": handoff.get("from_role"),
                    "to_role": handoff.get("to_role"),
                    "status": status,
                    "created_at": handoff.get("created_at"),
                    "age_seconds": age_seconds,
                    "threshold_seconds": thresholds.open_handoff_seconds,
                },
                next_action="Reissue or correct the handoff so the target role has queued work.",
            )
        )
    for work_item_id, items in active_by_work_item.items():
        if len(items) <= 1:
            continue
        findings.append(
            WatchdogFinding(
                finding_key=f"multiple_active_handoffs:{work_item_id}",
                finding_type="multiple_active_handoffs",
                severity="high",
                work_item_id=work_item_id,
                evidence={"handoff_ids": [item["handoff_id"] for item in items]},
                next_action="Resolve conflicting handoffs so the work item has one active owner path.",
            )
        )
    return findings


def _message_findings(
    *,
    messages: list[dict[str, Any]],
    thresholds: WatchdogThresholds,
    now: datetime,
) -> list[WatchdogFinding]:
    findings: list[WatchdogFinding] = []
    thresholds_by_state = {
        "queued": thresholds.queued_seconds,
        "ready": thresholds.queued_seconds,
        "delivering": thresholds.delivering_seconds,
        "active_turn": thresholds.active_turn_seconds,
        "failed": thresholds.failed_seconds,
        "dead_lettered": thresholds.failed_seconds,
    }
    for message in messages:
        state = str(message.get("state") or "")
        if state not in thresholds_by_state:
            continue
        observed_at = message.get("updated_at") or message.get("created_at")
        age_seconds = int(_age_seconds(observed_at, now=now))
        threshold_seconds = thresholds_by_state[state]
        if age_seconds < threshold_seconds:
            continue
        payload = _json_mapping(message.get("payload_json"))
        finding_type = "failed_message" if state in {"failed", "dead_lettered"} else "stuck_message"
        findings.append(
            WatchdogFinding(
                finding_key=f"{finding_type}:{message['message_id']}",
                finding_type=finding_type,
                severity="high" if state in {"delivering", "active_turn", "failed", "dead_lettered"} else "medium",
                work_item_id=_string(payload.get("work_item_id")),
                handoff_id=_string(payload.get("handoff_id")),
                message_id=str(message["message_id"]),
                target_role=_string(message.get("target_role")),
                owner_role=_string(message.get("target_role")),
                evidence={
                    "message_id": message["message_id"],
                    "state": state,
                    "target_role": message.get("target_role"),
                    "locked_by": message.get("locked_by"),
                    "locked_at": message.get("locked_at"),
                    "updated_at": message.get("updated_at"),
                    "observed_at": observed_at,
                    "age_seconds": age_seconds,
                    "threshold_seconds": threshold_seconds,
                    "delivery_attempts": message.get("delivery_attempts"),
                },
                next_action="Inspect and recover the stuck or failed message before assuming work is active.",
            )
        )
    return findings


def _planned_not_dispatched_findings(
    *,
    work_items: list[dict[str, Any]],
    handoffs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> list[WatchdogFinding]:
    findings: list[WatchdogFinding] = []
    for work_item in work_items:
        state = str(work_item.get("state") or "")
        work_item_id = _string(work_item.get("work_item_id"))
        owner_role = _string(work_item.get("owner_role"))
        next_action = _string(work_item.get("next_action"))
        if work_item_id is None or not _requires_dispatch_path(state=state, next_action=next_action, owner_role=owner_role):
            continue
        if _has_live_message_for_work_item(messages=messages, work_item_id=work_item_id):
            continue
        if _has_active_handoff_for_work_item(handoffs=handoffs, work_item_id=work_item_id, owner_role=owner_role):
            continue
        findings.append(
            WatchdogFinding(
                finding_key=f"planned_not_dispatched:{work_item_id}",
                finding_type="planned_not_dispatched",
                severity="high",
                work_item_id=work_item_id,
                target_role=owner_role,
                owner_role=owner_role,
                evidence={
                    "work_item_id": work_item_id,
                    "state": state,
                    "owner_role": owner_role,
                    "next_action": next_action,
                    "updated_at": work_item.get("updated_at"),
                    "age_seconds": int(_age_seconds(work_item.get("updated_at"), now=datetime.now(UTC))),
                },
                next_action="Create a next-agent handoff/message or explicitly mark the work as waiting for human decision/review.",
            )
        )
    return findings


def _completion_diagnostic_findings(diagnostics: list[dict[str, Any]]) -> list[WatchdogFinding]:
    findings: list[WatchdogFinding] = []
    for diagnostic in diagnostics:
        if diagnostic.get("state") != "completed_with_missing_output":
            continue
        diagnostic_id = str(diagnostic["diagnostic_id"])
        findings.append(
            WatchdogFinding(
                finding_key=f"missing_required_output:{diagnostic_id}",
                finding_type="missing_required_output",
                severity="high",
                work_item_id=_string(diagnostic.get("work_item_id")),
                message_id=_string(diagnostic.get("message_id")),
                owner_role=_role_from_instance_id(_string(diagnostic.get("role_instance_id"))),
                evidence={
                    "diagnostic_id": diagnostic_id,
                    "message_id": diagnostic.get("message_id"),
                    "turn_id": diagnostic.get("turn_id"),
                    "role_instance_id": diagnostic.get("role_instance_id"),
                    "missing_predicates": _json_value(diagnostic.get("missing_predicates_json")),
                    "observed_outputs": _json_value(diagnostic.get("observed_outputs_json")),
                },
                next_action=str(diagnostic.get("next_action") or "Record required durable output."),
            )
        )
    return findings


def _has_live_message_for_handoff(
    *,
    messages: list[dict[str, Any]],
    handoff: dict[str, Any],
    target_role: str | None,
) -> bool:
    work_item_id = _string(handoff.get("work_item_id"))
    handoff_id = _string(handoff.get("handoff_id"))
    for message in messages:
        if message.get("state") not in ACTIVE_MESSAGE_STATES:
            continue
        if target_role is not None and message.get("target_role") != target_role:
            continue
        payload = _json_mapping(message.get("payload_json"))
        if handoff_id is not None and payload.get("handoff_id") == handoff_id:
            return True
        if work_item_id is not None and payload.get("work_item_id") == work_item_id:
            return True
    return False


def _has_live_message_for_work_item(*, messages: list[dict[str, Any]], work_item_id: str) -> bool:
    for message in messages:
        if message.get("state") not in ACTIVE_MESSAGE_STATES:
            continue
        payload = _json_mapping(message.get("payload_json"))
        if payload.get("work_item_id") == work_item_id:
            return True
    return False


def _has_active_handoff_for_work_item(
    *,
    handoffs: list[dict[str, Any]],
    work_item_id: str,
    owner_role: str | None,
) -> bool:
    for handoff in handoffs:
        if handoff.get("status") not in ACTIVE_HANDOFF_STATES:
            continue
        if handoff.get("work_item_id") != work_item_id:
            continue
        if owner_role is not None and handoff.get("to_role") != owner_role:
            continue
        return True
    return False


def _requires_dispatch_path(*, state: str, next_action: str | None, owner_role: str | None) -> bool:
    return requires_dispatch_path(state=state, next_action=next_action, owner_role=owner_role)


def _looks_actionable_next_action(next_action: str | None) -> bool:
    return looks_actionable_next_action(next_action)


def _summary(*, findings: list[WatchdogFinding], resolved: int) -> dict[str, Any]:
    counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.finding_type] = counts.get(finding.finding_type, 0) + 1
        severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1
    return {
        "findings": len(findings),
        "resolved": resolved,
        "counts_by_type": counts,
        "counts_by_severity": severity_counts,
    }


def _age_seconds(value: object, *, now: datetime) -> float:
    timestamp = _parse_time(value)
    if timestamp is None:
        return 0
    return (now - timestamp).total_seconds()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _json_mapping(value: object) -> dict[str, Any]:
    parsed = _json_value(value)
    return parsed if isinstance(parsed, dict) else {}


def _json_value(value: object) -> object:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _role_from_instance_id(role_instance_id: str | None) -> str | None:
    if role_instance_id is None:
        return None
    parts = role_instance_id.split(".")
    return parts[-2] if len(parts) >= 2 else role_instance_id
