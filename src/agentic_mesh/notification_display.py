from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote
from urllib.parse import urlparse


DISPLAY_FACTS_SCHEMA_VERSION = "notification-display-v0"

TITLE_MAX = 80
SUMMARY_MAX = 240
ACTION_MAX = 160
DETAIL_MAX = 600
AUDIT_ITEM_MAX = 5
ARTIFACT_LINK_MAX = 2

FORBIDDEN_TEXT_MARKERS = (
    "tenant_id",
    "team_id",
    "channel_id",
    "conversation_id",
    "activity_id",
    "actor_id",
    "bot_id",
    "serviceurl",
    "service_url",
    "graph.microsoft.com",
    "raw_activity",
    "raw_route",
    "raw_payload",
    "credential_ref",
    "secret_ref",
    "bearer ",
    "token",
    "oauth",
    "mount_ref",
    "provider response",
    "c:\\",
    "\\\\",
)

DISPLAY_LABELS = {
    "queue_captured": "Request captured",
    "queue_promoted": "Work started",
    "lifecycle_work_created": "Work item created",
    "runtime_recovery_queued": "Operator action required: Runtime recovery queued",
    "approval_requested": "Approval requested",
    "approval_decision_recorded": "Decision recorded",
    "work_completed": "Completed",
    "publish_or_release_ready": "Release ready",
    "consolidated_audit_update": "Audit update",
    "notification_delivery_problem": "Fallback route used",
    "unknown_status_update": "Status update",
}

EVENT_CATEGORY_MAP = {
    "queue.captured": "queue_captured",
    "queue.promoted": "queue_promoted",
    "work.created": "lifecycle_work_created",
    "work.started": "lifecycle_work_created",
    "problem.blocked": "blocker_or_role_failure",
    "problem.failed": "blocker_or_role_failure",
    "problem.needs_runtime_recovery": "runtime_recovery_queued",
    "human_response.requested": "approval_requested",
    "human_response.received": "approval_decision_recorded",
    "human_response.completed": "approval_decision_recorded",
    "work.completed": "work_completed",
    "work.closed": "work_completed",
    "release.ready": "publish_or_release_ready",
    "release.released": "publish_or_release_ready",
    "publish.ready": "publish_or_release_ready",
    "audit.update": "consolidated_audit_update",
    "notification.digest": "consolidated_audit_update",
    "notification.fallback_used": "notification_delivery_problem",
    "notification.failed": "notification_delivery_problem",
    "connector.unreachable": "notification_delivery_problem",
}

ACTION_CATEGORIES = {
    "blocker_or_role_failure",
    "runtime_recovery_queued",
    "approval_requested",
}

ID_FACT_LABELS = (
    ("work_item_id", "Work item", True),
    ("queue_item_id", "Queue item", True),
    ("work_item_type", "Type", False),
    ("lifecycle_state", "State", True),
)

_SAFE_RELATIVE_ROUTE_RE = re.compile(r"^/[A-Za-z0-9/_.,:%+-]*$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def build_notification_display_facts(
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
) -> dict[str, Any]:
    event_kind = _safe_text(payload.get("event_kind"), max_length=96) or ""
    category = _category_for(payload, event_kind)
    action_needed = _action_needed_for(payload, category)
    display_label = _display_label_for(payload, category, event_kind, action_needed)
    title = _safe_text(
        payload.get("title") or payload.get("status_label") or event_kind or "Notification",
        max_length=TITLE_MAX,
    )
    summary, summary_truncated = _safe_text_with_truncation(
        payload.get("summary") or payload.get("status_detail") or "",
        max_length=SUMMARY_MAX,
    )
    facts = _build_facts(payload, category)
    status_links = _status_links_for(payload, category, base_url=base_url)
    artifact_links, artifact_overflow = _artifact_links_for(payload)
    detail_items = _detail_items_for(payload, category)
    truncation_notices = []
    if summary_truncated:
        truncation_notices.append("Summary truncated; open status for full details.")
    if artifact_overflow:
        truncation_notices.append(artifact_overflow["notice"])

    action_owner = _safe_text(payload.get("action_owner"), max_length=80)
    next_action = _safe_text(payload.get("next_action"), max_length=ACTION_MAX)
    if action_needed:
        action_owner = action_owner or "Action owner unknown"
        next_action = next_action or "Review work item status for next action"

    return {
        "schema_version": DISPLAY_FACTS_SCHEMA_VERSION,
        "display_category": category,
        "display_label": display_label,
        "event_kind": event_kind,
        "title": title or "Notification",
        "summary": summary or "",
        "action_needed": action_needed,
        "action_owner": action_owner,
        "next_action": next_action,
        "retryability_label": _retryability_label(payload),
        "facts": facts,
        "status_links": status_links,
        "artifact_links": artifact_links,
        "artifact_overflow": artifact_overflow,
        "detail_items": detail_items,
        "audit_window": _safe_text(payload.get("audit_window"), max_length=120),
        "source": {
            key: value
            for key, value in {
                "source_anchor_ref": _safe_text(payload.get("source_anchor_ref"), max_length=128),
                "source_summary": _safe_text(payload.get("source_anchor_summary"), max_length=160),
                "route_label": _safe_text(payload.get("route_label"), max_length=120),
                "fallback_reason": _safe_text(payload.get("fallback_reason"), max_length=120),
            }.items()
            if value
        },
        "occurred_at": _safe_text(payload.get("occurred_at"), max_length=80),
        "correlation_id": _safe_text(payload.get("correlation_id"), max_length=96),
        "truncation_notice": " ".join(truncation_notices) or None,
        "redaction_class": _safe_text(payload.get("redaction_class"), max_length=40) or "default",
    }


def _category_for(payload: dict[str, Any], event_kind: str) -> str:
    supplied = _safe_text(payload.get("display_category"), max_length=80)
    if supplied in set(DISPLAY_LABELS) | {"blocker_or_role_failure"}:
        return supplied
    return EVENT_CATEGORY_MAP.get(event_kind, "unknown_status_update")


def _action_needed_for(payload: dict[str, Any], category: str) -> bool:
    if category == "approval_decision_recorded":
        return bool(payload.get("action_needed") and payload.get("action_owner") and payload.get("next_action"))
    if category == "notification_delivery_problem":
        return bool(payload.get("action_needed") or payload.get("event_kind") in {"notification.failed", "connector.unreachable"})
    return bool(payload.get("action_needed") or category in ACTION_CATEGORIES)


def _display_label_for(
    payload: dict[str, Any],
    category: str,
    event_kind: str,
    action_needed: bool,
) -> str:
    supplied = _safe_text(payload.get("display_label"), max_length=80)
    if supplied:
        return supplied
    if category == "blocker_or_role_failure":
        if event_kind == "problem.failed":
            return "Action required: Role failed"
        return "Action required: Blocked by role"
    if category == "approval_decision_recorded" and _looks_not_approved(payload):
        return "Decision recorded: Not approved"
    if category == "notification_delivery_problem" and action_needed:
        return "Action required: Notification failed"
    return DISPLAY_LABELS.get(category, "Status update")


def _build_facts(payload: dict[str, Any], category: str) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for key, label, code in ID_FACT_LABELS:
        _append_fact(facts, label, payload.get(key), code=code, kind=key)
    if category == "runtime_recovery_queued":
        _append_fact(facts, "Runtime component", payload.get("runtime_component"), kind="runtime_component")
    elif payload.get("affected_role"):
        _append_fact(facts, "Affected role", payload.get("affected_role"), kind="affected_role")
    else:
        _append_fact(facts, "Owner", payload.get("owner_role") or payload.get("role_id"), kind="owner_role")
    _append_fact(facts, "Gate", payload.get("gate_id"), code=True, kind="gate_id")
    _append_fact(
        facts,
        "Response request",
        payload.get("response_request_id") or payload.get("approval_request_id"),
        code=True,
        kind="response_request_id",
    )
    source = payload.get("source_anchor_summary") or payload.get("source_anchor_ref")
    _append_fact(facts, "Source", source, kind="source")
    _append_fact(facts, "Correlation", payload.get("correlation_id"), code=True, kind="correlation_id")
    return facts


def _append_fact(
    facts: list[dict[str, Any]],
    label: str,
    value: Any,
    *,
    code: bool = False,
    kind: str | None = None,
) -> None:
    safe_value = _safe_text(value, max_length=160)
    if not safe_value:
        return
    facts.append({"label": label, "value": safe_value, "code": code, "kind": kind or label.lower()})


def _status_links_for(
    payload: dict[str, Any],
    category: str,
    *,
    base_url: str | None,
) -> list[dict[str, Any]]:
    links = [_safe_link(link, default_label="Open status") for link in payload.get("status_links") or []]
    links = [link for link in links if link]
    if links:
        return links
    work_item_id = _safe_logical_id(payload.get("work_item_id"))
    if work_item_id:
        return [_link("Open work item status", _with_base(f"/work-items/{quote(work_item_id, safe='')}", base_url))]
    if payload.get("queue_item_id") or category == "queue_captured":
        return [_link("Open work queue", _with_base("/work-queue", base_url))]
    return [_link("Open project status", _with_base("/status", base_url))]


def _artifact_links_for(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    links = []
    for raw_link in payload.get("document_links") or []:
        link = _safe_link(raw_link, default_label="Open evidence", artifact=True)
        if link:
            links.append(link)
    visible = links[:ARTIFACT_LINK_MAX]
    hidden_count = max(0, len(links) - len(visible))
    overflow = (
        {
            "hidden_count": hidden_count,
            "notice": "More artifacts available in work item status.",
        }
        if hidden_count
        else None
    )
    return visible, overflow


def _safe_link(
    raw_link: Any,
    *,
    default_label: str,
    artifact: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(raw_link, dict) or raw_link.get("available") is False:
        return None
    label = _safe_text(raw_link.get("label"), max_length=80) or default_label
    href = raw_link.get("href")
    if href is None:
        return None
    href_text = str(href).strip()
    if artifact and not _safe_artifact_href(href_text):
        return None
    if not _safe_href(href_text):
        return None
    return _link(label, href_text)


def _safe_href(href: str) -> bool:
    lower = href.lower()
    if any(marker in lower for marker in ("graph.microsoft.com", "serviceurl", "service_url", "token=", "bearer")):
        return False
    if href.startswith("/"):
        return bool(_SAFE_RELATIVE_ROUTE_RE.match(href)) and ".." not in href and not href.startswith("//")
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return ".." not in parsed.path and "\\" not in href


def _safe_artifact_href(href: str) -> bool:
    parsed = urlparse(href)
    path = parsed.path if parsed.scheme else href
    if _WINDOWS_DRIVE_RE.match(href) or href.startswith("\\\\"):
        return False
    if path.startswith("/artifact-viewer/"):
        return True
    if path.startswith("/") and not path.startswith("/artifact-viewer/"):
        return False
    parts = [part for part in path.split("/") if part]
    return bool(parts) and parts[0] != "state" and ".." not in parts


def _link(label: str, href: str) -> dict[str, Any]:
    return {
        "schema_version": "notification-link-v0",
        "label": label,
        "href": href,
        "available": True,
    }


def _with_base(path: str, base_url: str | None) -> str:
    base = str(base_url or "").rstrip("/")
    return f"{base}{path}" if base else path


def _detail_items_for(payload: dict[str, Any], category: str) -> list[str]:
    raw_items = payload.get("detail_items")
    if raw_items is None and category == "consolidated_audit_update":
        raw_items = payload.get("audit_items")
    if not isinstance(raw_items, list):
        return []
    items: list[str] = []
    remaining = DETAIL_MAX
    for item in raw_items[:AUDIT_ITEM_MAX]:
        safe = _safe_text(item, max_length=min(160, remaining))
        if not safe:
            continue
        items.append(safe)
        remaining -= len(safe)
        if remaining <= 0:
            break
    return items


def _retryability_label(payload: dict[str, Any]) -> str | None:
    supplied = _safe_text(payload.get("retryability_label"), max_length=120)
    if supplied:
        return supplied
    retryable = payload.get("retryable")
    if retryable is True:
        return "Yes"
    if retryable is False:
        return "No"
    return None


def _looks_not_approved(payload: dict[str, Any]) -> bool:
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("response_value", "decision", "status_label", "status_detail", "summary")
    ).lower()
    return "not_approved" in text or "not approved" in text or "rejected" in text


def _safe_logical_id(value: Any) -> str | None:
    text = _safe_text(value, max_length=128)
    if not text or "/" in text or "\\" in text or ".." in text or "://" in text:
        return None
    return text


def _safe_text(value: Any, *, max_length: int) -> str | None:
    text, _ = _safe_text_with_truncation(value, max_length=max_length)
    return text


def _safe_text_with_truncation(value: Any, *, max_length: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return None, False
    lowered = text.lower()
    if any(marker in lowered for marker in FORBIDDEN_TEXT_MARKERS):
        return "[redacted]", False
    if len(text) > max_length:
        return f"{text[: max_length - 1]}...", True
    return text, False
