from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.parse import urlparse

from agentic_mesh.models import NotificationPolicyConfig
from agentic_mesh.models import NotificationSurfaceConfig
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso


MESSAGE_TYPE_NOTIFICATION_EVENT = "notification.event"
NOTIFICATION_EVENT_SCHEMA_VERSION = "notification-event-v0"
NOTIFICATION_POLICY_SCHEMA_VERSION = "notification-policy-v0"
SOURCE_ROUTE_SCHEMA_VERSION = "source-route-v0"
ROUTE_RESOLUTION_SCHEMA_VERSION = "route-resolution-v0"
NOTIFICATION_ATTEMPT_SCHEMA_VERSION = "notification-attempt-v0"
LINK_SCHEMA_VERSION = "notification-link-v0"

EVENT_QUEUE_CAPTURED = "queue.captured"
EVENT_QUEUE_READY_FOR_PROMOTION = "queue.ready_for_promotion"
EVENT_QUEUE_PROMOTED = "queue.promoted"
EVENT_QUEUE_BLOCKED = "queue.blocked"
EVENT_QUEUE_PROMOTION_FAILED = "queue.promotion_failed"
EVENT_QUEUE_REPLAY_DETECTED = "queue.replay_detected"

_SAFE_LOGICAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ROUTINE_ROUTE_KINDS = {
    "configured_handoff",
    "configured_consult",
    "configured_correction",
}
_ROUTINE_EVENT_KINDS = {
    "lifecycle.handoff_requested",
    "lifecycle.consult_requested",
    "lifecycle.review_loop_requested",
}
_ACTION_NEEDED_EVENT_KINDS = {
    "request.needs_clarification",
    "human_response.requested",
    "problem.blocked",
    "problem.failed",
    "problem.needs_runtime_recovery",
    EVENT_QUEUE_BLOCKED,
    EVENT_QUEUE_PROMOTION_FAILED,
    "notification.fallback_used",
    "notification.failed",
    "connector.unreachable",
    "activation.blocked",
    "activation.smoke_failed",
    "activation.stale_runtime",
}
_COMPLETION_EVENT_KINDS = {
    "work.completed",
    "release.released",
    "work.closed",
    "human_response.completed",
    EVENT_QUEUE_CAPTURED,
    EVENT_QUEUE_PROMOTED,
    "activation.verified",
}
_DASHBOARD_ONLY_EVENT_KINDS = {
    EVENT_QUEUE_READY_FOR_PROMOTION,
    EVENT_QUEUE_REPLAY_DETECTED,
}
_SOURCE_ROUTE_CAPABILITIES = {
    "notification_event",
    "thread_reply",
    "message",
}
_FORBIDDEN_VALUE_MARKERS = (
    "://",
    "\\",
    "..",
    "secret",
    "token",
    "bearer ",
    "oauth",
    "mount_ref",
    "raw_activity",
    "raw_payload",
    "serviceurl",
    "service_url",
    "graph.microsoft.com",
)


def validate_logical_id(value: str, *, field_name: str = "id") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field_name} must not be empty")
    if "/" in candidate or "\\" in candidate or ".." in candidate:
        raise ValueError(f"{field_name} must be a contained logical id")
    if ":" in candidate and not candidate.startswith("source:"):
        raise ValueError(f"{field_name} must not contain URI-style separators")
    if not _SAFE_LOGICAL_ID_RE.match(candidate):
        raise ValueError(f"{field_name} contains unsupported characters")
    return candidate


def safe_filename(value: str) -> str:
    safe_value = validate_logical_id(value)
    digest = hashlib.sha256(safe_value.encode("utf-8")).hexdigest()[:16]
    normalized = safe_value.replace(":", "_")
    return f"{normalized}-{digest}.json"


def redacted_error_class(error: BaseException | str | None) -> str | None:
    if error is None:
        return None
    if isinstance(error, BaseException):
        return error.__class__.__name__
    value = str(error).strip()
    if not value:
        return None
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:64]


def _safe_text(value: Any, *, max_length: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    for marker in _FORBIDDEN_VALUE_MARKERS:
        if marker in text.lower():
            return "[redacted]"
    if len(text) > max_length:
        return f"{text[: max_length - 1]}..."
    return text


@dataclass(frozen=True)
class StatusLink:
    label: str
    href: str | None
    available: bool = True
    schema_version: str = LINK_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "label": _safe_text(self.label, max_length=80) or "status",
            "href": self.href if self.available else None,
            "available": self.available,
        }


@dataclass(frozen=True)
class DocumentLink:
    label: str
    href: str | None
    available: bool = True
    schema_version: str = LINK_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "label": _safe_text(self.label, max_length=80) or "document",
            "href": self.href if self.available else None,
            "available": self.available,
        }


def _approved_status_base_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(str(value).strip())
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        return ""
    if parsed.path and parsed.path not in {"", "/"}:
        return ""
    return str(value).strip().rstrip("/")


class StatusLinkBuilder:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = _approved_status_base_url(
            base_url or os.environ.get("AGENTIC_MESH_STATUS_BASE_URL")
        )

    def work_item(self, work_item_id: str | None, *, label: str = "Work item") -> StatusLink:
        if not work_item_id:
            return StatusLink(label=label, href=None, available=False)
        try:
            safe_id = validate_logical_id(work_item_id, field_name="work_item_id")
        except ValueError:
            return StatusLink(label=label, href=None, available=False)
        href = f"/work-items/{quote(safe_id, safe='')}"
        return StatusLink(label=label, href=f"{self.base_url}{href}" if self.base_url else href)

    def queue(self, *, label: str = "Work queue") -> StatusLink:
        href = "/work-queue"
        return StatusLink(label=label, href=f"{self.base_url}{href}" if self.base_url else href)


class DocumentLinkBuilder:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = _approved_status_base_url(
            base_url or os.environ.get("AGENTIC_MESH_STATUS_BASE_URL")
        )

    def artifact(self, artifact_path: str | None, *, label: str = "Document") -> DocumentLink:
        if not artifact_path:
            return DocumentLink(label=label, href=None, available=False)
        path = str(artifact_path)
        path_parts = Path(path).parts
        if (
            path.startswith("/")
            or path.startswith("\\\\")
            or re.match(r"^[A-Za-z]:[\\/]", path)
            or ".." in path_parts
            or path.startswith("state/")
            or "://" in path
        ):
            return DocumentLink(label=label, href=None, available=False)
        href = f"/artifact-viewer/{quote(path, safe='')}"
        return DocumentLink(label=label, href=f"{self.base_url}{href}" if self.base_url else href)


@dataclass(frozen=True)
class NotificationEvent:
    event_kind: str
    event_group: str
    visibility: str
    project_id: str
    event_id: str
    action_needed: bool = False
    urgency: str = "normal"
    action_owner: str | None = None
    next_action: str | None = None
    retryable: bool | None = None
    work_item_id: str | None = None
    work_item_type: str | None = None
    lifecycle_state: str | None = None
    queue_item_id: str | None = None
    source_message_id: str | None = None
    source_anchor_ref: str | None = None
    source_anchor_summary: str | None = None
    title: str | None = None
    summary: str | None = None
    status_label: str | None = None
    status_detail: str | None = None
    status_links: tuple[StatusLink, ...] = ()
    document_links: tuple[DocumentLink, ...] = ()
    display_category: str | None = None
    display_label: str | None = None
    owner_role: str | None = None
    affected_role: str | None = None
    runtime_component: str | None = None
    reason_summary: str | None = None
    failure_class: str | None = None
    retryability_label: str | None = None
    detail_items: tuple[str, ...] = ()
    audit_window: str | None = None
    route_hint: str | None = None
    redaction_class: str = "default"
    occurred_at: str = ""
    correlation_id: str | None = None
    trace_context: dict[str, str] | None = None
    dedupe_key: str | None = None
    schema_version: str = NOTIFICATION_EVENT_SCHEMA_VERSION

    @staticmethod
    def create(
        *,
        event_kind: str,
        event_group: str,
        visibility: str,
        project_id: str,
        work_item_id: str | None = None,
        queue_item_id: str | None = None,
        lifecycle_state: str | None = None,
        source_message_id: str | None = None,
        correlation_id: str | None = None,
        **kwargs: Any,
    ) -> "NotificationEvent":
        raw_key = "|".join(
            str(part or "")
            for part in (
                project_id,
                event_kind,
                work_item_id,
                queue_item_id,
                lifecycle_state,
                source_message_id,
                correlation_id,
            )
        )
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]
        dedupe_key = kwargs.pop("dedupe_key", f"dedupe-{digest}")
        return NotificationEvent(
            event_kind=event_kind,
            event_group=event_group,
            visibility=visibility,
            project_id=project_id,
            event_id=f"notifevt-{digest}",
            work_item_id=work_item_id,
            queue_item_id=queue_item_id,
            lifecycle_state=lifecycle_state,
            source_message_id=source_message_id,
            correlation_id=correlation_id,
            occurred_at=kwargs.pop("occurred_at", utc_now_iso()),
            dedupe_key=dedupe_key,
            **kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "event_id": validate_logical_id(self.event_id, field_name="event_id"),
            "event_kind": _safe_text(self.event_kind, max_length=96),
            "event_group": _safe_text(self.event_group, max_length=64),
            "visibility": self.visibility,
            "action_needed": self.action_needed,
            "urgency": self.urgency,
            "action_owner": _safe_text(self.action_owner, max_length=80),
            "next_action": _safe_text(self.next_action),
            "retryable": self.retryable,
            "project_id": _safe_text(self.project_id, max_length=80),
            "work_item_id": _safe_text(self.work_item_id, max_length=96),
            "work_item_type": _safe_text(self.work_item_type, max_length=64),
            "lifecycle_state": _safe_text(self.lifecycle_state, max_length=80),
            "queue_item_id": _safe_text(self.queue_item_id, max_length=96),
            "source_message_id": _safe_text(self.source_message_id, max_length=96),
            "source_anchor_ref": _safe_text(self.source_anchor_ref, max_length=128),
            "source_anchor_summary": _safe_text(self.source_anchor_summary),
            "title": _safe_text(self.title),
            "summary": _safe_text(self.summary),
            "status_label": _safe_text(self.status_label, max_length=80),
            "status_detail": _safe_text(self.status_detail),
            "status_links": [link.to_dict() for link in self.status_links],
            "document_links": [link.to_dict() for link in self.document_links],
            "display_category": _safe_text(self.display_category, max_length=80),
            "display_label": _safe_text(self.display_label, max_length=80),
            "owner_role": _safe_text(self.owner_role, max_length=80),
            "affected_role": _safe_text(self.affected_role, max_length=80),
            "runtime_component": _safe_text(self.runtime_component, max_length=80),
            "reason_summary": _safe_text(self.reason_summary),
            "failure_class": _safe_text(self.failure_class, max_length=120),
            "retryability_label": _safe_text(self.retryability_label, max_length=120),
            "detail_items": [
                text
                for text in (
                    _safe_text(item, max_length=160) for item in self.detail_items
                )
                if text is not None
            ],
            "audit_window": _safe_text(self.audit_window, max_length=120),
            "route_hint": _safe_text(self.route_hint, max_length=80),
            "redaction_class": self.redaction_class,
            "occurred_at": self.occurred_at,
            "correlation_id": _safe_text(self.correlation_id, max_length=96),
            "dedupe_key": _safe_text(self.dedupe_key, max_length=128),
        }
        return {key: value for key, value in data.items() if value is not None}


def readiness_notification_event(
    *,
    readiness_result: dict[str, Any],
    summary: dict[str, Any],
    work_item_id: str | None = None,
    work_item_type: str | None = None,
    lifecycle_state: str | None = None,
    correlation_id: str | None = None,
) -> NotificationEvent | None:
    status = str(readiness_result.get("status") or "")
    if status not in {
        "missing_required",
        "validation_failed",
        "unavailable_with_fallback",
    } and str(summary.get("overall_readiness") or "") != "startup_blocked":
        return None
    return NotificationEvent.create(
        event_kind="capability.readiness.action_needed",
        event_group="action_needed_events",
        visibility="notify",
        project_id=str(readiness_result.get("project_id") or summary.get("project_id")),
        work_item_id=work_item_id,
        work_item_type=work_item_type,
        lifecycle_state=lifecycle_state,
        correlation_id=correlation_id,
        action_needed=True,
        urgency="high" if summary.get("overall_readiness") == "startup_blocked" else "normal",
        action_owner=str(
            readiness_result.get("action_owner")
            or summary.get("action_owner")
            or "platform-engineer"
        ),
        next_action=str(
            readiness_result.get("next_action")
            or summary.get("next_action")
            or "Review capability provisioning."
        ),
        title="Capability readiness needs attention",
        summary=(
            f"{readiness_result.get('role_instance_id')} "
            f"{readiness_result.get('display_name')} is "
            f"{readiness_result.get('user_label') or status}."
        ),
        status_label=str(readiness_result.get("user_label") or status),
        status_detail=str(readiness_result.get("impact") or summary.get("blocking_impact") or ""),
        status_links=(
            StatusLink(
                label="Status JSON",
                href=str(summary.get("status_url") or "/agents/current.json"),
            ),
        ),
        display_category="capability_readiness",
        display_label=str(readiness_result.get("display_name") or "Capability"),
        owner_role=str(summary.get("role_id") or readiness_result.get("role_id") or ""),
        affected_role=str(summary.get("role_id") or readiness_result.get("role_id") or ""),
        runtime_component="capability-readiness",
        reason_summary=str(readiness_result.get("impact") or ""),
        detail_items=(
            f"role_instance={readiness_result.get('role_instance_id')}",
            f"capability={readiness_result.get('capability_id')}",
            f"status={status}",
        ),
        redaction_class="capability_readiness",
    )


def activation_notification_event(
    *,
    project_id: str,
    activation_summary: dict[str, Any],
    event_kind: str | None = None,
    correlation_id: str | None = None,
    status_link_builder: StatusLinkBuilder | None = None,
) -> NotificationEvent:
    work_item_id = str(activation_summary.get("work_item_id") or "unknown")
    failure_class = activation_summary.get("failure_class")
    kind = event_kind or _activation_event_kind(activation_summary)
    builder = status_link_builder or StatusLinkBuilder()
    target = ", ".join(activation_summary.get("target_labels") or []) or "target_unknown"
    summary = _safe_text(
        activation_summary.get("next_action")
        or activation_summary.get("attention_reason")
        or "Review activation evidence.",
        max_length=220,
    )
    return NotificationEvent.create(
        event_kind=kind,
        event_group=(
            "completion_events" if kind == "activation.verified" else "action_needed_events"
        ),
        visibility="notify",
        project_id=project_id,
        work_item_id=work_item_id,
        lifecycle_state=activation_summary.get("lifecycle_state"),
        correlation_id=correlation_id,
        action_needed=kind != "activation.verified",
        urgency="high" if failure_class else "normal",
        action_owner=str(activation_summary.get("action_owner") or "runtime/operator"),
        next_action=str(summary or "Review activation evidence."),
        retryable=activation_summary.get("retryable"),
        title=_activation_title(activation_summary),
        summary=(
            f"{work_item_id} activation for {target}: "
            f"{activation_summary.get('activation_status') or 'unknown'} / "
            f"{activation_summary.get('smoke_status') or 'unknown'}."
        ),
        status_label=str(
            activation_summary.get("failure_class_label")
            or activation_summary.get("activation_status_label")
            or "Activation"
        ),
        status_detail=str(
            activation_summary.get("latest_smoke", {}).get("actual_summary")
            if isinstance(activation_summary.get("latest_smoke"), dict)
            else activation_summary.get("smoke_status") or ""
        ),
        status_links=(builder.work_item(work_item_id, label="Work item status"),),
        display_category="activation",
        display_label=str(
            activation_summary.get("failure_class_label")
            or activation_summary.get("activation_status_label")
            or "Activation"
        ),
        runtime_component="activation-evidence",
        reason_summary=str(summary or ""),
        failure_class=str(failure_class) if failure_class else None,
        detail_items=(
            f"target={target}",
            f"source_status={activation_summary.get('source_status') or 'unknown'}",
            f"activation_status={activation_summary.get('activation_status') or 'unknown'}",
            f"smoke_status={activation_summary.get('smoke_status') or 'unknown'}",
            f"notification_state={activation_summary.get('notification_state') or 'not_required'}",
        ),
        redaction_class="activation_evidence",
    )


def _activation_event_kind(summary: dict[str, Any]) -> str:
    failure_class = summary.get("failure_class")
    if failure_class == "source_changed_running_service_not_updated":
        return "activation.stale_runtime"
    if summary.get("smoke_status") == "failed":
        return "activation.smoke_failed"
    if summary.get("activation_status") == "complete" and summary.get("smoke_status") in {
        "passed",
        "not_required",
    }:
        return "activation.verified"
    return "activation.blocked"


def _activation_title(summary: dict[str, Any]) -> str:
    failure_class = summary.get("failure_class")
    if failure_class == "source_changed_running_service_not_updated":
        return "Stale runtime detected"
    if summary.get("smoke_status") == "failed":
        return "Activation smoke failed"
    if summary.get("activation_status") == "complete":
        return "Activation verified"
    return "Activation needs attention"


@dataclass(frozen=True)
class SourceRouteRecord:
    source_anchor_ref: str
    connector_type: str
    connector_id: str
    route_label: str
    route_kind: str
    raw_route: dict[str, Any]
    capabilities: tuple[str, ...] = ()
    expires_at: str | None = None
    created_at: str = ""
    last_verified_at: str | None = None
    last_failure_class: str | None = None
    correlation_id: str | None = None
    schema_version: str = SOURCE_ROUTE_SCHEMA_VERSION

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "source_anchor_ref": validate_logical_id(
                self.source_anchor_ref,
                field_name="source_anchor_ref",
            ),
            "connector_type": _safe_text(self.connector_type, max_length=64),
            "connector_id": _safe_text(self.connector_id, max_length=80),
            "route_label": _safe_text(self.route_label, max_length=120),
            "route_kind": _safe_text(self.route_kind, max_length=80),
            "capabilities": list(self.capabilities),
            "expires_at": self.expires_at,
            "created_at": self.created_at or utc_now_iso(),
            "last_verified_at": self.last_verified_at,
            "last_failure_class": _safe_text(self.last_failure_class, max_length=80),
            "correlation_id": _safe_text(self.correlation_id, max_length=96),
        }
        if include_raw:
            data["raw_route"] = dict(self.raw_route)
        return {key: value for key, value in data.items() if value is not None}


@dataclass(frozen=True)
class RouteResolution:
    route_resolution_id: str
    event_id: str
    result: str
    connector_id: str | None = None
    connector_type: str | None = None
    surface_id: str | None = None
    selected_surface_key: str | None = None
    route_kind: str | None = None
    dispatch_route: str | None = None
    route_label: str | None = None
    fallback_reason: str | None = None
    source_anchor_ref: str | None = None
    occurred_at: str = ""
    correlation_id: str | None = None
    schema_version: str = ROUTE_RESOLUTION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "route_resolution_id": validate_logical_id(
                self.route_resolution_id,
                field_name="route_resolution_id",
            ),
            "event_id": validate_logical_id(self.event_id, field_name="event_id"),
            "result": self.result,
            "connector_id": _safe_text(self.connector_id, max_length=80),
            "connector_type": _safe_text(self.connector_type, max_length=64),
            "surface_id": _safe_text(self.surface_id, max_length=80),
            "selected_surface_key": _safe_text(self.selected_surface_key, max_length=80),
            "route_kind": _safe_text(self.route_kind, max_length=80),
            "route_label": _safe_text(self.route_label, max_length=120),
            "fallback_reason": _safe_text(self.fallback_reason, max_length=120),
            "source_anchor_ref": _safe_text(self.source_anchor_ref, max_length=128),
            "occurred_at": self.occurred_at or utc_now_iso(),
            "correlation_id": _safe_text(self.correlation_id, max_length=96),
        }
        return {key: value for key, value in data.items() if value is not None}


@dataclass(frozen=True)
class NotificationAttempt:
    attempt_id: str
    event_id: str
    dedupe_key: str
    status: str
    project_id: str
    work_item_id: str | None = None
    queue_item_id: str | None = None
    connector_id: str | None = None
    connector_type: str | None = None
    route_kind: str | None = None
    selected_surface_key: str | None = None
    route_label: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    next_retry_at: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    redacted_error_class: str | None = None
    created_at: str = ""
    updated_at: str = ""
    correlation_id: str | None = None
    schema_version: str = NOTIFICATION_ATTEMPT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "attempt_id": validate_logical_id(self.attempt_id, field_name="attempt_id"),
            "event_id": validate_logical_id(self.event_id, field_name="event_id"),
            "dedupe_key": _safe_text(self.dedupe_key, max_length=128),
            "status": self.status,
            "project_id": _safe_text(self.project_id, max_length=80),
            "work_item_id": _safe_text(self.work_item_id, max_length=96),
            "queue_item_id": _safe_text(self.queue_item_id, max_length=96),
            "connector_id": _safe_text(self.connector_id, max_length=80),
            "connector_type": _safe_text(self.connector_type, max_length=64),
            "route_kind": _safe_text(self.route_kind, max_length=80),
            "selected_surface_key": _safe_text(self.selected_surface_key, max_length=80),
            "route_label": _safe_text(self.route_label, max_length=120),
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "next_retry_at": self.next_retry_at,
            "fallback_used": self.fallback_used,
            "fallback_reason": _safe_text(self.fallback_reason, max_length=120),
            "redacted_error_class": _safe_text(self.redacted_error_class, max_length=80),
            "created_at": self.created_at or utc_now_iso(),
            "updated_at": self.updated_at or self.created_at or utc_now_iso(),
            "correlation_id": _safe_text(self.correlation_id, max_length=96),
        }
        return {key: value for key, value in data.items() if value is not None}


class FileSourceRouteStore:
    def __init__(self, state_root: Path, project_id: str) -> None:
        self.root = state_root / "projects" / project_id / "source-routes"
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, record: SourceRouteRecord) -> None:
        path = self._path(record.source_anchor_ref)
        self._atomic_write(path, record.to_dict(include_raw=True), mode=0o600)

    def read(self, source_anchor_ref: str) -> SourceRouteRecord | None:
        path = self._path(source_anchor_ref)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return SourceRouteRecord(
            source_anchor_ref=str(data["source_anchor_ref"]),
            connector_type=str(data["connector_type"]),
            connector_id=str(data["connector_id"]),
            route_label=str(data["route_label"]),
            route_kind=str(data["route_kind"]),
            raw_route=dict(data.get("raw_route", {})),
            capabilities=tuple(data.get("capabilities", [])),
            expires_at=data.get("expires_at"),
            created_at=str(data.get("created_at", "")),
            last_verified_at=data.get("last_verified_at"),
            last_failure_class=data.get("last_failure_class"),
            correlation_id=data.get("correlation_id"),
        )

    def _path(self, source_anchor_ref: str) -> Path:
        return contained_json_path(self.root, safe_filename(source_anchor_ref))

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
        atomic_write_json(path, data, mode=mode)


class FileNotificationAttemptStore:
    def __init__(self, state_root: Path, project_id: str) -> None:
        self.root = state_root / "projects" / project_id / "notification_attempts"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "index" / "dedupe").mkdir(parents=True, exist_ok=True)
        (self.root / "dead_letters").mkdir(parents=True, exist_ok=True)

    def create_or_dedupe(
        self,
        event: NotificationEvent,
        resolution: RouteResolution | None = None,
    ) -> NotificationAttempt:
        dedupe_key = validate_logical_id(
            event.dedupe_key or event.event_id,
            field_name="dedupe_key",
        )
        dedupe_path = contained_json_path(
            self.root / "index" / "dedupe",
            safe_filename(dedupe_key),
        )
        if dedupe_path.exists():
            data = json.loads(dedupe_path.read_text(encoding="utf-8"))
            attempt = self.read(str(data["attempt_id"]))
            if attempt is not None:
                return NotificationAttempt(
                    **{
                        **attempt.to_dict(),
                        "status": "deduplicated",
                        "updated_at": utc_now_iso(),
                    }
                )
        attempt = NotificationAttempt(
            attempt_id=new_id("notift"),
            event_id=event.event_id,
            dedupe_key=dedupe_key,
            status=self._initial_attempt_status(event, resolution),
            project_id=event.project_id,
            work_item_id=event.work_item_id,
            queue_item_id=event.queue_item_id,
            connector_id=resolution.connector_id if resolution else None,
            connector_type=resolution.connector_type if resolution else None,
            route_kind=resolution.route_kind if resolution else None,
            selected_surface_key=resolution.selected_surface_key if resolution else None,
            route_label=resolution.route_label if resolution else None,
            fallback_used=bool(
                resolution and resolution.result == "fallback_selected"
            ),
            fallback_reason=resolution.fallback_reason if resolution else None,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            correlation_id=event.correlation_id,
        )
        self.write(attempt)
        atomic_write_json(
            dedupe_path,
            {"dedupe_key": dedupe_key, "attempt_id": attempt.attempt_id},
        )
        return attempt

    def read(self, attempt_id: str) -> NotificationAttempt | None:
        path = self._attempt_path(attempt_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return NotificationAttempt(
            attempt_id=str(data["attempt_id"]),
            event_id=str(data["event_id"]),
            dedupe_key=str(data["dedupe_key"]),
            status=str(data["status"]),
            project_id=str(data["project_id"]),
            work_item_id=data.get("work_item_id"),
            queue_item_id=data.get("queue_item_id"),
            connector_id=data.get("connector_id"),
            connector_type=data.get("connector_type"),
            route_kind=data.get("route_kind"),
            selected_surface_key=data.get("selected_surface_key"),
            route_label=data.get("route_label"),
            retry_count=int(data.get("retry_count", 0)),
            max_retries=int(data.get("max_retries", 3)),
            next_retry_at=data.get("next_retry_at"),
            fallback_used=bool(data.get("fallback_used", False)),
            fallback_reason=data.get("fallback_reason"),
            redacted_error_class=data.get("redacted_error_class"),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            correlation_id=data.get("correlation_id"),
        )

    def write(self, attempt: NotificationAttempt) -> None:
        atomic_write_json(self._attempt_path(attempt.attempt_id), attempt.to_dict())

    def dead_letter(self, attempt: NotificationAttempt, error_class: str) -> NotificationAttempt:
        dead = NotificationAttempt(
            **{
                **attempt.to_dict(),
                "status": "dead_lettered",
                "redacted_error_class": redacted_error_class(error_class),
                "updated_at": utc_now_iso(),
            }
        )
        self.write(dead)
        atomic_write_json(
            contained_json_path(self.root / "dead_letters", safe_filename(dead.attempt_id)),
            dead.to_dict(),
        )
        return dead

    def _attempt_path(self, attempt_id: str) -> Path:
        return contained_json_path(self.root, safe_filename(attempt_id))

    @staticmethod
    def _initial_attempt_status(
        event: NotificationEvent,
        resolution: RouteResolution | None,
    ) -> str:
        if event.visibility != "notify":
            return "suppressed_by_policy" if event.visibility == "suppress" else event.visibility
        if resolution and resolution.result == "unroutable":
            return "blocked_unroutable"
        return "queued"


class NotificationPolicyEvaluator:
    def __init__(self, policy: NotificationPolicyConfig) -> None:
        self.policy = policy

    def visibility_for(
        self,
        *,
        event_kind: str,
        route_kind: str | None = None,
        role_id: str | None = None,
    ) -> str:
        if role_id and role_id in self.policy.role_overrides:
            override = self.policy.role_overrides[role_id].get(event_kind)
            if override:
                return override.visibility
        if event_kind in self.policy.event_overrides:
            return self.policy.event_overrides[event_kind].visibility
        if event_kind in _DASHBOARD_ONLY_EVENT_KINDS:
            return "dashboard_only"
        if route_kind in _ROUTINE_ROUTE_KINDS or event_kind in _ROUTINE_EVENT_KINDS:
            return self.policy.defaults.get("routine_lifecycle_events", "dashboard_only")
        if event_kind in _COMPLETION_EVENT_KINDS:
            return self.policy.defaults.get("completion_events", "notify")
        if event_kind in _ACTION_NEEDED_EVENT_KINDS:
            return self.policy.defaults.get("action_needed_events", "notify")
        return "dashboard_only"

    def preferred_surface_for(self, event_kind: str) -> NotificationSurfaceConfig | None:
        override = self.policy.event_overrides.get(event_kind)
        if override and override.preferred_surface:
            return self.policy.surfaces.get(override.preferred_surface)
        if event_kind == "human_response.requested":
            return self.policy.surfaces.get("approvals")
        return self.policy.surfaces.get("status_fallback")


class NotificationRouteResolver:
    def __init__(
        self,
        *,
        policy: NotificationPolicyConfig,
        source_route_store: FileSourceRouteStore | None = None,
    ) -> None:
        self.policy = policy
        self.source_route_store = source_route_store
        self.evaluator = NotificationPolicyEvaluator(policy)

    def resolve(self, event: NotificationEvent) -> RouteResolution:
        if event.visibility != "notify":
            return RouteResolution(
                route_resolution_id=new_id("routeres"),
                event_id=event.event_id,
                result=event.visibility,
                source_anchor_ref=event.source_anchor_ref,
                correlation_id=event.correlation_id,
            )
        source_resolution = self._source_resolution(event)
        if source_resolution is not None and source_resolution.result == "resolved":
            return source_resolution
        fallback_reason = (
            source_resolution.fallback_reason
            if source_resolution is not None
            else "source_route_missing"
        )
        return route_resolution_for_surface(
            event=event,
            surface=self.evaluator.preferred_surface_for(event.event_kind),
            fallback_reason=fallback_reason,
        )

    def _source_resolution(self, event: NotificationEvent) -> RouteResolution | None:
        if not self.source_route_store or not event.source_anchor_ref:
            return None
        record = self.source_route_store.read(event.source_anchor_ref)
        if record is None:
            return self._unusable_source(event, "source_route_missing")
        if record.expires_at and _is_past(record.expires_at):
            return self._unusable_source(event, "source_route_expired")
        if record.last_failure_class:
            return self._unusable_source(event, "source_route_unavailable")
        capabilities = set(record.capabilities)
        if capabilities and not capabilities.intersection(_SOURCE_ROUTE_CAPABILITIES):
            return self._unusable_source(event, "source_route_unsupported")
        dispatch_route = _safe_dispatch_route(record.raw_route)
        if dispatch_route is None:
            return self._unusable_source(event, "source_route_inaccessible")
        return RouteResolution(
            route_resolution_id=new_id("routeres"),
            event_id=event.event_id,
            result="resolved",
            connector_id=record.connector_id,
            connector_type=record.connector_type,
            surface_id=dispatch_route,
            selected_surface_key="source_thread",
            route_kind=record.route_kind,
            dispatch_route=dispatch_route,
            route_label=record.route_label,
            source_anchor_ref=event.source_anchor_ref,
            correlation_id=event.correlation_id,
        )

    @staticmethod
    def _unusable_source(event: NotificationEvent, reason: str) -> RouteResolution:
        return RouteResolution(
            route_resolution_id=new_id("routeres"),
            event_id=event.event_id,
            result="source_unavailable",
            fallback_reason=reason,
            source_anchor_ref=event.source_anchor_ref,
            correlation_id=event.correlation_id,
        )


def route_resolution_for_surface(
    *,
    event: NotificationEvent,
    surface: NotificationSurfaceConfig | None,
    fallback_reason: str | None = None,
) -> RouteResolution:
    if surface is None:
        return RouteResolution(
            route_resolution_id=new_id("routeres"),
            event_id=event.event_id,
            result="unroutable",
            fallback_reason=fallback_reason or "no_explicit_notification_surface",
            source_anchor_ref=event.source_anchor_ref,
            correlation_id=event.correlation_id,
        )
    return RouteResolution(
        route_resolution_id=new_id("routeres"),
        event_id=event.event_id,
        result="fallback_selected" if fallback_reason else "resolved",
        connector_id=surface.connector,
        connector_type=surface.connector,
        surface_id=surface.surface_id,
        selected_surface_key=surface.surface_id,
        route_kind="configured_surface",
        dispatch_route=surface.route,
        route_label=surface.label,
        fallback_reason=fallback_reason,
        source_anchor_ref=event.source_anchor_ref,
        correlation_id=event.correlation_id,
    )


def _safe_dispatch_route(raw_route: dict[str, Any]) -> str | None:
    for key in ("route", "channel", "source_scope"):
        value = raw_route.get(key)
        if not value:
            continue
        text = str(value)
        try:
            validate_logical_id(text, field_name="dispatch_route")
        except ValueError:
            continue
        return text
    return None


def _is_past(value: str) -> bool:
    try:
        candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=timezone.utc)
        return candidate < datetime.now(timezone.utc)
    except ValueError:
        return True


def contained_json_path(root: Path, filename: str) -> Path:
    if not filename.endswith(".json"):
        raise ValueError("notification store files must be JSON")
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    path = (root / filename).resolve()
    if root_resolved not in path.parents:
        raise ValueError("notification store path escapes configured root")
    if path.exists() and path.is_symlink():
        raise ValueError("notification store refuses symlink targets")
    return path


def atomic_write_json(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("notification store refuses symlink parent directories")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
