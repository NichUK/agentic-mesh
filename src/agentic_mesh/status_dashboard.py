from __future__ import annotations

import json
from collections import Counter
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from agentic_mesh.activation_evidence import activation_attention_needed
from agentic_mesh.activation_evidence import label_for as activation_label_for
from agentic_mesh.capabilities import CapabilityReadinessStore
from agentic_mesh.capabilities import unknown_summary
from agentic_mesh.models import MeshConfig
from agentic_mesh.models import utc_now_iso
from agentic_mesh.threaded_context import FileThreadedContextStore
from agentic_mesh.recovery_alerts import FileRecoveryAlertStore
from agentic_mesh.recovery_observability import build_recovery_observability_view
from agentic_mesh.work_item_recovery import FileRecoveryStatusStore
from agentic_mesh.work_item_indexes import NOT_FOUND
from agentic_mesh.work_item_indexes import parse_global_index
from agentic_mesh.work_item_indexes import parse_local_index
from agentic_mesh.work_queue import QueueItem


SCHEMA_VERSION = "status-dashboard-v0"
REFRESH_MODE = "manual_browser_refresh"
GROUPS = ["attention_needed", "active", "promoted", "terminal", "unknown"]

APPROVAL_ATTENTION_STATUSES = {
    "waiting_for_response",
    "pending",
    "not_approved",
    "invalid_response",
    "timed_out",
    "blocked_before_gate",
    "failed_before_gate",
    "request_failed",
    "stale_waiting",
    "inconsistent",
    "unknown",
}

WORK_ITEM_DISPLAY_LABELS = {
    "blocked": "Blocked by role",
    "failed": "Role failed",
    "needs_runtime_recovery": "Needs runtime recovery",
    "stale_claim": "Stale claim",
    "correction_requested": "Correction requested",
    "waiting_for_human_response": "Waiting for human response",
    "human_response_recorded": "Human response recorded",
    "running": "Running",
    "pending": "Pending",
    "observed": "Observed",
    "completed": "Completed",
    "completed_after_human_response": "Completed after human response",
    "closed": "Closed",
    "canceled": "Canceled",
    "cancelled": "Canceled",
    "not_found": "Unknown",
    "unknown": "Unknown",
    "incomplete_record": "Incomplete record",
}

QUEUE_DISPLAY_LABELS = {
    "captured": "Captured",
    "triaging": "In triage",
    "needs_clarification": "Needs clarification",
    "blocked": "Blocked",
    "ready_for_promotion": "Ready to promote",
    "promoted": "Promoted",
    "closed": "Closed",
    "canceled": "Canceled",
    "unknown": "Unknown",
    "incomplete_record": "Incomplete record",
}

WORK_ITEM_GROUPS = {
    "blocked": "attention_needed",
    "failed": "attention_needed",
    "needs_runtime_recovery": "attention_needed",
    "stale_claim": "attention_needed",
    "correction_requested": "active",
    "waiting_for_human_response": "attention_needed",
    "human_response_recorded": "active",
    "running": "active",
    "pending": "active",
    "observed": "active",
    "completed": "terminal",
    "completed_after_human_response": "terminal",
    "closed": "terminal",
    "canceled": "terminal",
    "cancelled": "terminal",
}

TERMINAL_WORK_ITEM_STATUSES = {
    "closed",
    "canceled",
    "cancelled",
    "completed",
    "completed_after_human_response",
}

STOPPED_WORK_ITEM_STATUSES = {
    "blocked",
    "failed",
    "needs_runtime_recovery",
    "stale_claim",
}

TERMINAL_QUEUE_STATUSES = {
    "closed",
    "canceled",
}

TERMINAL_RECOVERY_STATES = {
    "superseded",
}

QUEUE_GROUPS = {
    "captured": "active",
    "triaging": "active",
    "needs_clarification": "attention_needed",
    "blocked": "attention_needed",
    "ready_for_promotion": "active",
    "promoted": "promoted",
    "closed": "terminal",
    "canceled": "terminal",
}


def work_item_status_group(status: str | None) -> str:
    return WORK_ITEM_GROUPS.get(str(status or "unknown"), "unknown")


def _approval_attention(status: str, approval_status: str | None) -> bool:
    if status != "waiting_for_human_response":
        return False
    return str(approval_status or "") in APPROVAL_ATTENTION_STATUSES


def _current_recovery(
    state_root: Path | None,
    project_id: str,
    work_item_id: str,
) -> dict[str, Any] | None:
    if state_root is None:
        return None
    try:
        status = FileRecoveryStatusStore(
            state_root,
            project_id,
            create_dirs=False,
        ).get_current(work_item_id)
        alert = FileRecoveryAlertStore(
            state_root,
            project_id,
            create_dirs=False,
        ).get_current(work_item_id)
    except Exception:
        return {
            "schema_version": "recovery-observability-view-v1",
            "work_item_id": work_item_id,
            "recovery_state": "unknown",
            "recovery_state_label": "Unknown",
            "attention_reason": "Recovery status could not be read",
        }
    if status is None:
        return None
    try:
        return build_recovery_observability_view(status, alert_state=alert).to_dict()
    except Exception as exc:
        payload = status.to_dict()
        payload.setdefault("schema_version", "recovery-observability-view-v1")
        payload.setdefault("recovery_state_label", str(payload.get("recovery_state") or "Unknown"))
        payload.setdefault("attention_reason", f"Recovery status read compatibility issue: {exc.__class__.__name__}")
        return payload


def _recovery_attention(recovery: dict[str, Any] | None) -> bool:
    if not recovery:
        return False
    return str(recovery.get("recovery_state") or "") in {
        "recovery_needed",
        "recovery_failed",
        "split_required",
        "runtime_fix_required",
        "sponsor_decision_required",
        "duplicate_active_work",
        "not_recoverable",
        "unknown",
    }


def _recovery_active(recovery: dict[str, Any] | None) -> bool:
    if not recovery:
        return False
    return str(recovery.get("recovery_state") or "") in {
        "recovery_queued",
        "recovery_running",
    }


def queue_status_group(
    status: str | None,
    *,
    notification_failure_reason: str | None = None,
) -> str:
    if str(status or "unknown") in TERMINAL_QUEUE_STATUSES:
        return "terminal"
    if notification_failure_reason:
        return "attention_needed"
    return QUEUE_GROUPS.get(str(status or "unknown"), "unknown")


def display_label(status: str | None, *, item_type: str) -> str:
    value = str(status or "unknown")
    labels = QUEUE_DISPLAY_LABELS if item_type == "queue" else WORK_ITEM_DISPLAY_LABELS
    return labels.get(value, value.replace("_", " ").title())


def route_links() -> dict[str, str]:
    return {
        "status_html": "/status",
        "status_json": "/status.json",
        "work_items_html": "/work-items",
        "work_items_json": "/work-items.json",
        "work_queue_html": "/work-queue",
        "work_queue_json": "/work-queue.json",
        "current_agents_html": "/agents/current",
        "current_agents_json": "/agents/current.json",
        "queue_html": "/queue",
        "queue_json": "/queue.json",
    }


def status_location(target_type: str, target_id: str) -> dict[str, Any]:
    from agentic_mesh.external_actions import status_location_for

    return status_location_for(target_type, target_id).to_safe_dict()


def build_status_dashboard(
    *,
    mesh_config: MeshConfig,
    state_root: Path,
    workspace_root: Path,
    work_item_status: Callable[[str], dict[str, Any]],
    artifact_exists: Callable[[str], bool],
) -> dict[str, Any]:
    project_id = mesh_config.project.project_id
    queue_items, queue_errors = _read_queue_items(state_root, project_id)
    queue_rows = [
        queue_row_from_item(item)
        for item in sorted(queue_items, key=lambda item: (item.created_at, item.queue_item_id))
    ]
    queue_rows.extend(_queue_error_row(error) for error in queue_errors)
    queue_by_work_item = {
        row["promoted_work_item_id"]: row
        for row in queue_rows
        if row.get("promoted_work_item_id")
    }
    queue_by_id = {
        row["queue_item_id"]: row
        for row in queue_rows
        if row.get("queue_item_id") and row.get("status") != "incomplete_record"
    }

    work_item_ids = _known_work_item_ids(
        mesh_config=mesh_config,
        state_root=state_root,
        workspace_root=workspace_root,
        queue_rows=queue_rows,
    )
    title_hints = _title_hints(mesh_config, workspace_root)
    work_item_rows = []
    for work_item_id in sorted(work_item_ids):
        try:
            payload = work_item_status(work_item_id)
            if payload.get("status") == "not_found" and work_item_id not in title_hints:
                continue
            row = work_item_row_from_status(
                work_item_id=work_item_id,
                payload=payload,
                queue_by_id=queue_by_id,
                queue_by_work_item=queue_by_work_item,
                title_hints=title_hints,
                workspace_root=workspace_root,
                mesh_config=mesh_config,
                artifact_exists=artifact_exists,
                state_root=state_root,
            )
        except Exception as exc:
            row = incomplete_work_item_row(work_item_id, exc.__class__.__name__)
        work_item_rows.append(row)
    work_item_rows = _sort_rows(work_item_rows)
    queue_rows = _sort_rows(queue_rows)

    generated_at = utc_now_iso()
    counts = _aggregate_counts(work_item_rows, queue_rows)
    notification_summary = _notification_summary(state_root, project_id)
    capability_readiness = _capability_readiness_rows(
        state_root=state_root,
        mesh_config=mesh_config,
        generated_at=generated_at,
    )
    grouped = {
        group: {
            "work_items": [row for row in work_item_rows if row["status_group"] == group],
            "queue_items": [row for row in queue_rows if row["status_group"] == group],
        }
        for group in GROUPS
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "project": {
            "project_id": mesh_config.project.project_id,
            "name": mesh_config.project.name,
            "default_repository": mesh_config.project.workspace.default_repository,
            "workspace_label": mesh_config.project.workspace.root,
        },
        "links": route_links(),
        "counts": counts,
        "notifications": notification_summary,
        "capability_readiness": capability_readiness,
        "groups": grouped,
        **grouped,
        "work_items": work_item_rows,
        "queue_items": queue_rows,
        "read_only": True,
        "refresh_mode": REFRESH_MODE,
    }


def _capability_readiness_rows(
    *,
    state_root: Path,
    mesh_config: MeshConfig,
    generated_at: str,
) -> dict[str, Any]:
    store = CapabilityReadinessStore(
        state_root,
        mesh_config.project.project_id,
        create_dirs=False,
    )
    rows = []
    for instance in sorted(mesh_config.instances.values(), key=lambda item: item.instance_id):
        current = store.read_current(instance.instance_id)
        summary = (current or {}).get("summary")
        if not isinstance(summary, dict):
            summary = unknown_summary(
                project_id=mesh_config.project.project_id,
                role_id=instance.role_id,
                role_instance_id=instance.instance_id,
                generated_at=generated_at,
                diagnostic_class="missing_evidence",
            )
        rows.append(summary)
    priority = {
        "startup_blocked": 0,
        "not_ready_for_capability_dependent_work": 1,
        "unknown": 2,
        "ready_with_warnings": 3,
        "ready": 4,
    }
    rows.sort(
        key=lambda row: (
            priority.get(str(row.get("overall_readiness")), 9),
            str(row.get("role_instance_id")),
        )
    )
    counts = Counter(str(row.get("overall_readiness") or "unknown") for row in rows)
    return {
        "schema_version": "capability-readiness-dashboard-v0",
        "rows": rows,
        "counts_by_overall_readiness": dict(counts),
        "read_only": True,
        "redaction_applied": True,
    }


def _notification_summary(state_root: Path, project_id: str) -> dict[str, Any]:
    root = state_root / "projects" / project_id / "notification_attempts"
    rows: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append(_notification_attempt_row(data))
    dead_letter_root = root / "dead_letters"
    dead_letters = 0
    if dead_letter_root.exists():
        dead_letters = len(list(dead_letter_root.glob("*.json")))
    by_status = Counter(row["status"] for row in rows)
    return {
        "schema_version": "notification-ops-summary-v0",
        "total_attempts": len(rows),
        "pending_attempts": int(by_status.get("pending", 0) + by_status.get("queued", 0)),
        "failed_attempts": int(by_status.get("failed", 0)),
        "retry_backlog": int(by_status.get("retry_scheduled", 0)),
        "dead_letters": dead_letters,
        "fallback_count": sum(1 for row in rows if row.get("fallback_used")),
        "unroutable_count": int(by_status.get("blocked_unroutable", 0)),
        "attempts": rows,
    }


def _notification_attempt_row(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": str(data.get("attempt_id") or "unknown"),
        "event_id": str(data.get("event_id") or "unknown"),
        "status": str(data.get("status") or "unknown"),
        "work_item_id": data.get("work_item_id"),
        "queue_item_id": data.get("queue_item_id"),
        "connector_id": data.get("connector_id"),
        "connector_type": data.get("connector_type"),
        "route_kind": data.get("route_kind"),
        "selected_surface_key": data.get("selected_surface_key"),
        "route_label": data.get("route_label"),
        "retry_count": int(data.get("retry_count", 0)),
        "fallback_used": bool(data.get("fallback_used", False)),
        "fallback_reason": data.get("fallback_reason"),
        "redacted_error_class": data.get("redacted_error_class"),
        "next_operator_action": _notification_next_action(data),
        "updated_at": data.get("updated_at") or data.get("created_at"),
    }


def _notification_next_action(data: dict[str, Any]) -> str | None:
    status = str(data.get("status") or "")
    if status == "blocked_unroutable":
        return "Configure an explicit notification surface or inspect support route state."
    if status == "dead_lettered":
        return "Inspect connector health and retry or purge through operator tooling."
    if status == "failed":
        return "Inspect connector health and retry policy."
    return None


def work_items_payload(aggregate: dict[str, Any]) -> dict[str, Any]:
    rows = list(aggregate["work_items"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": aggregate["generated_at"],
        "project": aggregate["project"],
        "links": aggregate["links"],
        "counts": {
            "total": len(rows),
            "by_status": dict(Counter(row["status"] for row in rows)),
            "by_status_group": _count_groups(rows),
        },
        "items": rows,
        "read_only": True,
        "refresh_mode": REFRESH_MODE,
    }


def work_queue_payload(aggregate: dict[str, Any]) -> dict[str, Any]:
    rows = list(aggregate["queue_items"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": aggregate["generated_at"],
        "project": aggregate["project"],
        "links": aggregate["links"],
        "counts": {
            "total": len(rows),
            "by_status": dict(Counter(row["status"] for row in rows)),
            "by_status_group": _count_groups(rows),
        },
        "items": rows,
        "read_only": True,
        "refresh_mode": REFRESH_MODE,
    }


def queue_row_from_item(item: QueueItem) -> dict[str, Any]:
    summary = item.redacted_summary()
    notification = summary.get("notification") or {}
    notification_failure = summary.get("notification_failure_reason")
    status = str(summary.get("status") or "unknown")
    group = queue_status_group(
        status,
        notification_failure_reason=(
            str(notification_failure) if notification_failure else None
        ),
    )
    promoted_work_item_id = summary.get("promoted_work_item_id")
    blocker = summary.get("blocker_reason")
    return {
        "queue_item_id": summary["queue_item_id"],
        "title": summary.get("title") or summary["queue_item_id"],
        "title_source": (
            "queue_item_title" if summary.get("title") else "fallback_queue_item_id"
        ),
        "summary": item.summary or None,
        "status": status,
        "display_label": (
            "Notification failed"
            if notification_failure and status not in TERMINAL_QUEUE_STATUSES
            else display_label(status, item_type="queue")
        ),
        "status_group": group,
        "owner_role": summary.get("owner_role") or None,
        "recommended_work_item_type": summary.get("recommended_work_item_type") or None,
        "source_anchor_summary": summary.get("source_anchor"),
        "promoted_work_item_id": promoted_work_item_id,
        "blocker_reason": blocker,
        "notification_state": notification.get("status") or "not_required",
        "notification_event_kind": notification.get("event_kind"),
        "notification_attempt_id": notification.get("attempt_id"),
        "notification_dedupe_ref": notification.get("dedupe_ref"),
        "notification_route_kind": notification.get("route_kind"),
        "notification_selected_surface_key": notification.get("selected_surface_key"),
        "notification_route_label": notification.get("route_label"),
        "notification_fallback_reason": notification.get("fallback_reason"),
        "notification_failure_class": notification.get("failure_class"),
        "notification_retry_count": notification.get("retry_count", 0),
        "notification_dead_lettered": bool(notification.get("dead_lettered", False)),
        "notification_recorded_at": notification.get("recorded_at"),
        "notification_failure_reason": notification_failure,
        "created_at": item.created_at,
        "updated_at": summary.get("updated_at") or item.updated_at,
        "next_action": _queue_next_action(status, summary),
        "attention_reason": _queue_attention_reason(
            status,
            blocker,
            None if status in TERMINAL_QUEUE_STATUSES else notification_failure,
        ),
        "work_item_status_url": (
            f"/work-items/{quote(str(promoted_work_item_id), safe='')}"
            if promoted_work_item_id
            else None
        ),
        "work_item_json_url": (
            f"/work-items/{quote(str(promoted_work_item_id), safe='')}.json"
            if promoted_work_item_id
            else None
        ),
        "extraction_error": None,
    }


def work_item_row_from_status(
    *,
    work_item_id: str,
    payload: dict[str, Any],
    queue_by_id: dict[str, dict[str, Any]],
    queue_by_work_item: dict[str, dict[str, Any]],
    title_hints: dict[str, tuple[str, str]],
    workspace_root: Path,
    mesh_config: MeshConfig,
    artifact_exists: Callable[[str], bool],
    state_root: Path | None = None,
) -> dict[str, Any]:
    current = payload.get("current") or {}
    activation_summary = payload.get("activation_summary") or payload.get("activation_evidence")
    current_recovery = _current_recovery(state_root, mesh_config.project.project_id, work_item_id)
    status = str(payload.get("status") or current.get("status") or "unknown")
    human_gate = payload.get("human_gate_summary") or {}
    approval_status = human_gate.get("status")
    approval_attention = _approval_attention(status, approval_status)
    live_human_gate_active = (
        status == "waiting_for_human_response"
        and approval_status in {"waiting_for_response", "pending"}
    )
    recovery_state = (
        str(current_recovery.get("recovery_state") or "")
        if current_recovery
        else ""
    )
    if status in TERMINAL_WORK_ITEM_STATUSES or live_human_gate_active:
        current_recovery = None
    activation_attention = activation_attention_needed(activation_summary)
    if activation_attention:
        group = "attention_needed"
    elif recovery_state in TERMINAL_RECOVERY_STATES:
        group = "terminal"
    elif approval_attention:
        group = "attention_needed"
    elif status in STOPPED_WORK_ITEM_STATUSES:
        group = "attention_needed"
    elif _recovery_attention(current_recovery):
        group = "attention_needed"
    elif _recovery_active(current_recovery):
        group = "active"
    else:
        group = work_item_status_group(status)
    queue_item_id = _linked_queue_item_id(payload)
    queue_row = (
        queue_by_id.get(queue_item_id or "")
        or queue_by_work_item.get(work_item_id)
    )
    title, title_source = _work_item_title(
        work_item_id=work_item_id,
        current=current,
        queue_row=queue_row,
        title_hints=title_hints,
        workspace_root=workspace_root,
        mesh_config=mesh_config,
    )
    summary = _work_item_summary(
        current=current,
        queue_row=queue_row,
        title=title,
    )
    artifact_records = _safe_artifact_records(
        payload.get("artifact_records") or [],
        artifact_exists=artifact_exists,
    )
    artifact_links = [
        {
            "path": record["path"],
            "label": record["label"],
            "viewer_url": f"/artifact-viewer/{quote(record['path'], safe='')}",
            "source_url": f"/artifacts/{quote(record['path'], safe='')}",
        }
        for record in artifact_records
        if record["exists"]
    ][:5]
    started_at = _first_present(
        queue_row.get("created_at") if queue_row else None,
        _oldest_entry_time(payload.get("queue_entries") or []),
        _oldest_event_time(payload.get("timeline") or []),
    )
    updated_at = _first_present(
        current.get("since"),
        _latest_event_time(payload.get("timeline") or []),
        queue_row.get("updated_at") if queue_row else None,
    )
    attention_reason = (
        _activation_attention_reason(activation_summary)
        if activation_attention
        else _work_item_attention_reason(status, current, payload)
    )
    if current_recovery and _recovery_attention(current_recovery):
        attention_reason = str(
            current_recovery.get("next_action")
            or current_recovery.get("recovery_state_label")
            or "Recovery needs attention"
        )
    if not attention_reason and status in STOPPED_WORK_ITEM_STATUSES:
        attention_reason = (
            str(current.get("reason_summary") or current.get("result_message"))
            if current.get("reason_summary") or current.get("result_message")
            else None
        )
    if not attention_reason and approval_attention:
        attention_reason = str(
            human_gate.get("attention_reason")
            or human_gate.get("display_label")
            or "Approval needs attention"
        )
    next_action = (
        activation_summary.get("next_action")
        if activation_attention
        and isinstance(activation_summary, dict)
        and activation_summary.get("next_action")
        else str(
            human_gate.get("attention_reason")
            or human_gate.get("display_label")
            or "Approval needs attention"
        )
        if approval_attention
        else
        current_recovery.get("next_action")
        if current_recovery and _recovery_attention(current_recovery)
        else current.get("next_action") or _work_item_next_action(current)
    )
    blocker_summary = _blocker_summary(
        current_problem=current
        if status in {"blocked", "failed", "needs_runtime_recovery"}
        else None,
        current_recovery=current_recovery,
        attention_reason=attention_reason,
        next_action=next_action,
    )
    threaded_context = (
        _threaded_context_summary(state_root, mesh_config.project.project_id, work_item_id)
        if state_root is not None
        else {"count": 0, "pending_owner_attention": 0, "needs_triage": 0, "items": []}
    )
    return {
        "work_item_id": work_item_id,
        "title": title,
        "title_source": title_source,
        "summary": summary,
        "status": status,
        "display_label": (
            _activation_display_label(activation_summary)
            if activation_attention
            else "Superseded"
            if recovery_state == "superseded"
            else
            str(current_recovery.get("recovery_state_label"))
            if current_recovery and _recovery_attention(current_recovery)
            else display_label(status, item_type="work_item")
        ),
        "status_group": group,
        "lifecycle_state": current.get("lifecycle_state") or None,
        "owner_role": current.get("role_id") or current.get("action_owner") or None,
        "role_instance_id": current.get("role_instance_id") or None,
        "queue_item_id": queue_item_id,
        "source_anchor_summary": (
            queue_row.get("source_anchor_summary") if queue_row else None
        ),
        "started_at": started_at,
        "updated_at": updated_at,
        "next_action": next_action,
        "attention_reason": attention_reason,
        "blocker_summary": blocker_summary,
        "approval_status": approval_status,
        "approval_status_label": human_gate.get("display_label"),
        "approval_request_id": human_gate.get("approval_request_id"),
        "response_request_id": human_gate.get("response_request_id"),
        "notification_attempt_id": human_gate.get("notification_attempt_id"),
        "current_human_gate_id": human_gate.get("current_human_gate_id"),
        "current_recovery": current_recovery,
        "current_problem": current if status in {"blocked", "failed", "needs_runtime_recovery"} else None,
        "activation_summary": activation_summary,
        "deployment_impact_labels": _activation_list(activation_summary, "deployment_impact_labels"),
        "activation_path_labels": _activation_list(activation_summary, "activation_path_labels"),
        "target_labels": _activation_list(activation_summary, "target_labels"),
        "source_status": _activation_value(activation_summary, "source_status"),
        "activation_status": _activation_value(activation_summary, "activation_status"),
        "smoke_status": _activation_value(activation_summary, "smoke_status"),
        "notification_state": _activation_value(activation_summary, "notification_state"),
        "failure_class": _activation_value(activation_summary, "failure_class") or current.get("failure_class"),
        "activation_action_owner": _activation_value(activation_summary, "action_owner"),
        "activation_updated_at": _activation_value(activation_summary, "activation_updated_at"),
        "live_smoke_required": bool(_activation_value(activation_summary, "live_smoke_required")),
        "activation_extraction_error": _activation_value(activation_summary, "extraction_error"),
        "next_human_gate_id": human_gate.get("next_human_gate_id"),
        "approval_attention_reason": human_gate.get("attention_reason"),
        "approval_required_before_implementation": bool(
            human_gate.get("approval_required_before_implementation", False)
        ),
        "artifact_count": len(artifact_records),
        "missing_artifact_count": len(
            [record for record in artifact_records if not record["exists"]]
        ),
        "status_url": f"/work-items/{quote(work_item_id, safe='')}",
        "json_url": f"/work-items/{quote(work_item_id, safe='')}.json",
        "artifact_links": artifact_links,
        "threaded_context": threaded_context,
        "extraction_error": None,
    }


def _blocker_summary(
    *,
    current_problem: dict[str, Any] | None,
    current_recovery: dict[str, Any] | None,
    attention_reason: str | None,
    next_action: Any,
) -> dict[str, Any] | None:
    if not current_problem and not current_recovery and not attention_reason:
        return None
    problem = current_problem or {}
    recovery = current_recovery or {}
    return {
        "status": problem.get("status") or recovery.get("recovery_state"),
        "status_label": (
            recovery.get("recovery_state_label")
            or problem.get("status_label")
            or problem.get("problem_label")
        ),
        "reason": problem.get("reason") or attention_reason,
        "reason_summary": (
            problem.get("reason_summary")
            or attention_reason
            or recovery.get("recovery_reason_label")
        ),
        "failure_class": problem.get("failure_class"),
        "recovery_state": recovery.get("recovery_state"),
        "recovery_reason_class": recovery.get("recovery_reason_class"),
        "recoverability_class": recovery.get("recoverability_class"),
        "next_action": next_action or recovery.get("next_action") or problem.get("next_action"),
        "action_owner": recovery.get("action_owner") or problem.get("action_owner"),
        "artifact_paths": problem.get("artifact_paths") or [],
    }


def _activation_value(summary: Any, key: str) -> Any:
    if not isinstance(summary, dict):
        return None
    return summary.get(key)


def _activation_list(summary: Any, key: str) -> list[str]:
    value = _activation_value(summary, key)
    return list(value) if isinstance(value, (list, tuple)) else []


def _activation_display_label(summary: Any) -> str:
    if not isinstance(summary, dict):
        return "Activation needs attention"
    failure_class = summary.get("failure_class")
    if failure_class:
        return activation_label_for(str(failure_class))
    if summary.get("smoke_status") in {"failed", "blocked"}:
        return f"Activation smoke {summary.get('smoke_status')}"
    return activation_label_for(str(summary.get("activation_status") or "unknown"))


def _activation_attention_reason(summary: Any) -> str | None:
    if not isinstance(summary, dict):
        return None
    if summary.get("extraction_error"):
        return "Activation evidence could not be read."
    failure_class = summary.get("failure_class")
    target = ", ".join(summary.get("target_labels") or []) or "target_unknown"
    if failure_class == "source_changed_running_service_not_updated":
        return f"Stale runtime detected on {target}."
    if failure_class:
        return f"{activation_label_for(str(failure_class))} on {target}."
    if summary.get("notification_state") in {"failed", "blocked_unroutable"}:
        return "Activation notification needs attention."
    if summary.get("smoke_status") in {"failed", "blocked"}:
        return f"Activation smoke {summary.get('smoke_status')} on {target}."
    if summary.get("activation_status") in {"blocked", "failed", "unknown"}:
        return f"Activation {summary.get('activation_status')} on {target}."
    return None


def incomplete_work_item_row(work_item_id: str, error_class: str) -> dict[str, Any]:
    return {
        "work_item_id": work_item_id,
        "title": work_item_id,
        "title_source": "fallback_work_item_id",
        "summary": None,
        "status": "incomplete_record",
        "display_label": "Incomplete record",
        "status_group": "unknown",
        "lifecycle_state": None,
        "owner_role": None,
        "role_instance_id": None,
        "queue_item_id": None,
        "source_anchor_summary": None,
        "started_at": None,
        "updated_at": None,
        "next_action": None,
        "attention_reason": "Incomplete record",
        "artifact_count": 0,
        "missing_artifact_count": 0,
        "status_url": f"/work-items/{quote(work_item_id, safe='')}",
        "json_url": f"/work-items/{quote(work_item_id, safe='')}.json",
        "artifact_links": [],
        "extraction_error": error_class,
    }


def _known_work_item_ids(
    *,
    mesh_config: MeshConfig,
    state_root: Path,
    workspace_root: Path,
    queue_rows: list[dict[str, Any]],
) -> set[str]:
    project_id = mesh_config.project.project_id
    ids: set[str] = set()
    def add_work_item_id(value: Any) -> None:
        if not isinstance(value, str):
            return
        cleaned = value.strip()
        if not cleaned or cleaned in {"*", "global"}:
            return
        ids.add(cleaned)

    for event in _safe_journal_events(state_root, project_id):
        if event.get("work_item_id"):
            add_work_item_id(str(event["work_item_id"]))
    queue_root = state_root / "projects" / project_id / "queues"
    for path in sorted(queue_root.glob("*/*/*.json")) + sorted(queue_root.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        payload = data.get("payload") or {}
        if isinstance(payload, dict) and payload.get("work_item_id"):
            add_work_item_id(str(payload["work_item_id"]))
    for row in queue_rows:
        if row.get("promoted_work_item_id"):
            add_work_item_id(str(row["promoted_work_item_id"]))
    recovery_store = FileRecoveryStatusStore(state_root, project_id, create_dirs=False)
    for recovery, _ in [(row, None) for row in recovery_store.list_current()[0]]:
        add_work_item_id(recovery.work_item_id)
    threaded_context_root = state_root / "projects" / project_id / "threaded_context" / "contexts"
    if threaded_context_root.exists():
        store = FileThreadedContextStore(state_root, project_id, _NullJournal())
        for context in store.list_contexts():
            add_work_item_id(context.parent_work_item_id)
    work_items_root = _document_library_root(mesh_config, workspace_root) / "work-items"
    if work_items_root.exists():
        for path in sorted(child for child in work_items_root.iterdir() if child.is_dir()):
            add_work_item_id(path.name)
    return ids


def _read_queue_items(
    state_root: Path,
    project_id: str,
) -> tuple[list[QueueItem], list[dict[str, str]]]:
    items_dir = state_root / "projects" / project_id / "work_queue" / "items"
    items: list[QueueItem] = []
    errors: list[dict[str, str]] = []
    if not items_dir.exists():
        return items, errors
    for path in sorted(items_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items.append(QueueItem.from_dict(data))
        except Exception as exc:
            errors.append(
                {
                    "queue_item_id": path.stem,
                    "error_class": exc.__class__.__name__,
                }
            )
    return items, errors


def _queue_error_row(error: dict[str, str]) -> dict[str, Any]:
    queue_item_id = error.get("queue_item_id") or "unknown"
    return {
        "queue_item_id": queue_item_id,
        "title": queue_item_id,
        "title_source": "fallback_queue_item_id",
        "summary": None,
        "status": "incomplete_record",
        "display_label": "Incomplete record",
        "status_group": "unknown",
        "owner_role": None,
        "recommended_work_item_type": None,
        "source_anchor_summary": None,
        "promoted_work_item_id": None,
        "blocker_reason": None,
        "notification_failure_reason": None,
        "created_at": None,
        "updated_at": None,
        "next_action": None,
        "attention_reason": "Incomplete record",
        "work_item_status_url": None,
        "work_item_json_url": None,
        "extraction_error": error.get("error_class") or "ParseError",
    }


def _safe_journal_events(state_root: Path, project_id: str) -> list[dict[str, Any]]:
    journal_path = state_root / "projects" / project_id / "journal" / "events.jsonl"
    if not journal_path.exists():
        return []
    events = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _title_hints(
    mesh_config: MeshConfig,
    workspace_root: Path,
) -> dict[str, tuple[str, str]]:
    hints: dict[str, tuple[str, str]] = {}
    root = _document_library_root(mesh_config, workspace_root)
    global_index = root / "work-items" / "index.md"
    if global_index.exists():
        try:
            for row in parse_global_index(global_index.read_text(encoding="utf-8")):
                if row.summary and row.summary != NOT_FOUND:
                    hints[row.work_item_id] = (row.summary, "maintained_global_index")
        except Exception:
            pass
    work_items_root = root / "work-items"
    if not work_items_root.exists():
        return hints
    for item_dir in sorted(path for path in work_items_root.iterdir() if path.is_dir()):
        local = item_dir / "00-index.md"
        if local.exists():
            try:
                index = parse_local_index(
                    local.read_text(encoding="utf-8"),
                    work_item_id=item_dir.name,
                )
                if index.summary and index.summary != NOT_FOUND:
                    hints[item_dir.name] = (index.summary, "maintained_local_index")
                    continue
            except Exception:
                pass
        heading = _first_heading(item_dir / "10-business-brief.md") or _first_heading(
            item_dir / "20-product-definition.md"
        )
        if heading:
            hints[item_dir.name] = (heading, "first_artifact_heading")
    return hints


def _work_item_title(
    *,
    work_item_id: str,
    current: dict[str, Any],
    queue_row: dict[str, Any] | None,
    title_hints: dict[str, tuple[str, str]],
    workspace_root: Path,
    mesh_config: MeshConfig,
) -> tuple[str, str]:
    if current.get("title"):
        return human_title(str(current["title"])), "runtime_payload_title"
    if queue_row and queue_row.get("title"):
        return human_title(str(queue_row["title"])), "promoted_queue_item_title"
    if work_item_id in title_hints:
        hinted_title, title_source = title_hints[work_item_id]
        return human_title(hinted_title), title_source
    item_dir = _document_library_root(mesh_config, workspace_root) / "work-items" / work_item_id
    heading = _first_heading(item_dir / "10-business-brief.md") or _first_heading(
        item_dir / "20-product-definition.md"
    )
    if heading:
        return human_title(heading), "first_artifact_heading"
    return work_item_id, "fallback_work_item_id"


def _work_item_summary(
    *,
    current: dict[str, Any],
    queue_row: dict[str, Any] | None,
    title: str,
) -> str | None:
    for value in [
        current.get("summary"),
        queue_row.get("summary") if queue_row else None,
    ]:
        summary = human_summary(value)
        if summary and summary != title:
            return summary
    return None


def human_title(value: Any, *, max_words: int = 8, max_length: int = 72) -> str:
    text = _single_line(value)
    if not text:
        return "Untitled work item"
    text = re.sub(r"^@?AM-[A-Za-z -]+\s+", "", text).strip()
    text = re.sub(r"^(please\s+)?(create|run|implement|build|fix|add|update)\s+(and\s+run\s+)?", "", text, flags=re.IGNORECASE).strip()
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(" ,.;:")
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip(" ,.;:") + "..."
    return text[:1].upper() + text[1:] if text else "Untitled work item"


def human_summary(value: Any, *, max_length: int = 420) -> str | None:
    text = _single_line(value)
    if not text:
        return None
    text = re.sub(r"^@?AM-[A-Za-z -]+\s+", "", text).strip()
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_length:
        return text
    boundary = text.rfind(". ", 0, max_length)
    if boundary < 120:
        boundary = text.rfind(" ", 0, max_length)
    if boundary < 120:
        boundary = max_length
    return text[:boundary].rstrip(" ,.;:") + "..."


def _single_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _document_library_root(mesh_config: MeshConfig, workspace_root: Path) -> Path:
    configured = Path(mesh_config.project.document_library.root)
    if configured.is_absolute():
        return configured
    effective_workspace_root = Path(mesh_config.project.workspace.root)
    if not effective_workspace_root.is_absolute():
        effective_workspace_root = workspace_root / effective_workspace_root
    return (effective_workspace_root / configured).resolve()


def _first_heading(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped.removeprefix("# ").strip() or None
    except Exception:
        return None
    return None


def _linked_queue_item_id(payload: dict[str, Any]) -> str | None:
    for entry in payload.get("queue_entries") or []:
        if entry.get("queue_item_id"):
            return str(entry["queue_item_id"])
    return None


def _safe_artifact_records(
    records: list[dict[str, Any]],
    *,
    artifact_exists: Callable[[str], bool],
) -> list[dict[str, Any]]:
    safe = []
    for record in records:
        path = str(record.get("path") or "")
        if not _is_safe_artifact_path(path):
            continue
        safe.append(
            {
                "path": path,
                "label": str(record.get("label") or path),
                "verification": str(record.get("verification") or "unknown"),
                "exists": bool(record.get("exists") and artifact_exists(path)),
            }
        )
    return safe


def _threaded_context_summary(
    state_root: Path,
    project_id: str,
    work_item_id: str,
) -> dict[str, Any]:
    try:
        store = FileThreadedContextStore(state_root, project_id, _NullJournal())
        contexts = store.list_contexts(work_item_id)
    except Exception:
        return {
            "count": 0,
            "pending_owner_attention": 0,
            "needs_triage": 0,
            "items": [],
        }
    rows = [context.to_safe_dict() for context in contexts]
    return {
        "count": len(rows),
        "pending_owner_attention": sum(
            1 for row in rows if row.get("attention_state") == "pending"
        ),
        "needs_triage": sum(
            1 for row in rows if row.get("action_state") == "needs_triage"
        ),
        "linked_new_work": sum(
            1 for row in rows if row.get("context_kind") == "linked_new_work"
        ),
        "items": rows[:20],
    }


class _NullJournal:
    def append(self, event_type: str, **fields: Any) -> dict[str, Any]:
        return {"event_type": event_type, **fields}


def _is_safe_artifact_path(path: str) -> bool:
    candidate = Path(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        return False
    blocked_parts = {"state", "secrets", "worker_mounts", "raw"}
    if any(part in blocked_parts for part in candidate.parts):
        return False
    return path.startswith("work-items/") or path.startswith("documents/analysis/")


def _work_item_attention_reason(
    status: str,
    current: dict[str, Any],
    payload: dict[str, Any],
) -> str | None:
    if work_item_status_group(status) != "attention_needed":
        return None
    if current.get("reason_summary"):
        return str(current["reason_summary"])
    notification = payload.get("notification_state") or {}
    if notification.get("status") == "failed":
        return "Notification failed"
    if status == "stale_claim":
        age = current.get("claim_age_seconds")
        return f"Stale claim age {int(float(age))}s" if isinstance(age, int | float) else "Stale claim"
    return "Reason unavailable"


def _queue_attention_reason(
    status: str,
    blocker_reason: Any,
    notification_failure_reason: Any,
) -> str | None:
    if notification_failure_reason:
        return str(notification_failure_reason)
    if queue_status_group(status) != "attention_needed":
        return None
    if blocker_reason:
        return str(blocker_reason)
    if status == "needs_clarification":
        return "Needs clarification"
    if status == "blocked":
        return "Reason unavailable"
    return None


def _work_item_next_action(current: dict[str, Any]) -> str | None:
    owner = current.get("action_owner") or current.get("role_id")
    lifecycle = current.get("lifecycle_state")
    if owner and lifecycle:
        return f"Fallback: awaiting {owner} in {lifecycle}"
    return None


def _queue_next_action(status: str, summary: dict[str, Any]) -> str | None:
    owner = summary.get("owner_role")
    if status == "needs_clarification":
        return f"Fallback: awaiting clarification for {owner or 'owner'}"
    if status == "blocked":
        return f"Fallback: awaiting {owner or 'owner'} to resolve blocker"
    if status == "ready_for_promotion":
        return "Fallback: awaiting promotion"
    if status in {"captured", "triaging"} and owner:
        return f"Fallback: awaiting {owner} triage"
    return None


def _aggregate_counts(
    work_item_rows: list[dict[str, Any]],
    queue_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    work_groups = _count_groups(work_item_rows)
    queue_groups = _count_groups(queue_rows)
    return {
        "total_work_items": len(work_item_rows),
        "total_queue_items": len(queue_rows),
        "work_items_by_status": dict(Counter(row["status"] for row in work_item_rows)),
        "work_items_by_status_group": work_groups,
        "queue_items_by_status": dict(Counter(row["status"] for row in queue_rows)),
        "queue_items_by_status_group": queue_groups,
        "attention_needed": work_groups["attention_needed"] + queue_groups["attention_needed"],
        "active": work_groups["active"] + queue_groups["active"],
        "promoted": work_groups["promoted"] + queue_groups["promoted"],
        "terminal": work_groups["terminal"] + queue_groups["terminal"],
        "unknown": work_groups["unknown"] + queue_groups["unknown"],
    }


def _count_groups(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(row.get("status_group") or "unknown" for row in rows)
    return {group: int(counts.get(group, 0)) for group in GROUPS}


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("started_at") or row.get("created_at") or row.get("updated_at") or ""),
            str(row.get("work_item_id") or row.get("queue_item_id") or ""),
        ),
    )


def _first_present(*values: Any) -> Any:
    for value in values:
        if value:
            return value
    return None


def _oldest_entry_time(entries: list[dict[str, Any]]) -> str | None:
    values = sorted(str(item.get("created_at") or item.get("claimed_at") or "") for item in entries)
    return next((value for value in values if value), None)


def _oldest_event_time(events: list[dict[str, Any]]) -> str | None:
    values = sorted(str(item.get("timestamp") or "") for item in events)
    return next((value for value in values if value), None)


def _latest_event_time(events: list[dict[str, Any]]) -> str | None:
    values = sorted(str(item.get("timestamp") or "") for item in events if item.get("timestamp"))
    return values[-1] if values else None
