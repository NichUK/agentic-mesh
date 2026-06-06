from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import NotificationPolicyConfig
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso
from agentic_mesh.notifications import EVENT_QUEUE_BLOCKED
from agentic_mesh.notifications import EVENT_QUEUE_CAPTURED
from agentic_mesh.notifications import EVENT_QUEUE_PROMOTED
from agentic_mesh.notifications import EVENT_QUEUE_PROMOTION_FAILED
from agentic_mesh.notifications import EVENT_QUEUE_READY_FOR_PROMOTION
from agentic_mesh.notifications import FileNotificationAttemptStore
from agentic_mesh.notifications import FileSourceRouteStore
from agentic_mesh.notifications import MESSAGE_TYPE_NOTIFICATION_EVENT
from agentic_mesh.notifications import NotificationEvent
from agentic_mesh.notifications import NotificationPolicyEvaluator
from agentic_mesh.notifications import NotificationRouteResolver
from agentic_mesh.notifications import RouteResolution
from agentic_mesh.notifications import StatusLinkBuilder
from agentic_mesh.notifications import redacted_error_class
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import QueueItem
from agentic_mesh.work_queue import QueueNotification
from agentic_mesh.work_queue import SourceAnchor
from agentic_mesh.work_queue import WorkQueueError
from agentic_mesh.work_queue import validate_logical_id


ACTION_SCHEMA_VERSION = "external-action-v0"
RECEIPT_SCHEMA_VERSION = "external-action-receipt-v0"
CONTROL_PLANE_ACTION_SCHEMA_VERSION = "control-plane-action-v0"
CONTROL_PLANE_RECEIPT_SCHEMA_VERSION = "control-plane-action-receipt-v0"
STATUS_LOCATION_SCHEMA_VERSION = "status-location-v0"

OUTCOME_CAPTURED = "captured"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_REJECTED = "rejected"
OUTCOME_NOT_CAPTURED = "not_captured"
OUTCOME_ACTION_RECORDED = "action_recorded"
OUTCOME_ACTION_DENIED = "action_denied"
OUTCOME_ACTION_FAILED = "action_failed"

ACTION_CAPTURE_WORK = "capture_work"
ACTION_SHOW_STATUS = "show_status"
ACTION_MARK_READY = "mark_ready"
ACTION_PROMOTE = "promote"
ACTION_TRANSITION = "transition"
ACTION_READ_RAW_REFERENCE = "read_raw_reference"
ACTION_PURGE_RAW = "purge_raw"
ACTION_AUTHENTICATE_PROFILE = "authenticate_profile"
ACTION_CAPTURE_QUEUE_ITEM = "capture_queue_item"
ACTION_READ_PROJECT_STATUS = "read_project_status"
ACTION_READ_QUEUE_ITEM = "read_queue_item"
ACTION_TRANSITION_QUEUE_ITEM = "transition_queue_item"
ACTION_MARK_QUEUE_READY = "mark_queue_ready"
ACTION_PROMOTE_QUEUE_ITEM = "promote_queue_item"
ACTION_READ_WORK_ITEM = "read_work_item"
ACTION_SUBMIT_APPROVAL_RESPONSE = "submit_approval_response"
ACTION_BIND_COMMENT_THREAD = "bind_comment_thread"
ACTION_RETRY_ROUTE_OR_NOTIFICATION = "retry_route_or_notification"
ACTION_UNBLOCK_ROUTE_OR_PROBLEM = "unblock_route_or_problem"
ACTION_RELOAD_CONFIG = "reload_config"
ACTION_READ_AUDIT = "read_audit"

CLI_SOURCE = "cli"
DENY_API_MCP_DEFAULTS = {"api", "mcp"}
SENSITIVE_ACTIONS = {
    ACTION_PROMOTE,
    ACTION_PROMOTE_QUEUE_ITEM,
    ACTION_READ_RAW_REFERENCE,
    ACTION_PURGE_RAW,
    ACTION_SUBMIT_APPROVAL_RESPONSE,
    ACTION_RETRY_ROUTE_OR_NOTIFICATION,
    ACTION_UNBLOCK_ROUTE_OR_PROBLEM,
    ACTION_RELOAD_CONFIG,
}
CAPTURE_ACTIONS = {ACTION_CAPTURE_WORK, ACTION_CAPTURE_QUEUE_ITEM}
STATUS_ACTIONS = {ACTION_SHOW_STATUS, ACTION_READ_PROJECT_STATUS, ACTION_READ_QUEUE_ITEM}
MARK_READY_ACTIONS = {ACTION_MARK_READY, ACTION_MARK_QUEUE_READY}
PROMOTE_ACTIONS = {ACTION_PROMOTE, ACTION_PROMOTE_QUEUE_ITEM}
TRANSITION_ACTIONS = {ACTION_TRANSITION, ACTION_TRANSITION_QUEUE_ITEM}
SUPPORTED_ACTIONS = (
    CAPTURE_ACTIONS
    | STATUS_ACTIONS
    | MARK_READY_ACTIONS
    | PROMOTE_ACTIONS
    | TRANSITION_ACTIONS
    | {ACTION_READ_RAW_REFERENCE, ACTION_PURGE_RAW}
)
ALLOWED_JOURNAL_FIELDS = {
    "project_id",
    "action_type",
    "receipt_outcome",
    "queue_item_id",
    "work_item_id",
    "work_item_type",
    "queue_status",
    "lifecycle_state",
    "connector_type",
    "connector_id",
    "source_anchor_ref",
    "actor_type",
    "role_id",
    "correlation_id",
    "schema_version",
    "failure_class",
    "notification_state",
    "notification_event_kind",
    "notification_attempt_id",
    "notification_route_kind",
    "notification_selected_surface_key",
    "notification_fallback_reason",
    "permission_decision",
    "decision_reason",
}
PROHIBITED_TEXT_MARKERS = {
    "secret_ref",
    "mount_ref",
    "access_token",
    "refresh_token",
    "service_url",
    "tenant_id",
    "channel_id",
    "conversation_id",
    "activity_id",
    "oauth",
}


@dataclass(frozen=True)
class ExternalActor:
    actor_type: str
    display_label: str
    role_id: str | None = None
    service_id: str | None = None
    subject_ref: str | None = None
    auth_context_ref: str | None = None
    identity_provider: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "actor_type": safe_token(self.actor_type),
            "display_label": safe_text(self.display_label, 120),
            "role_id": safe_token(self.role_id) if self.role_id else None,
            "service_id": safe_token(self.service_id) if self.service_id else None,
            "subject_ref": keyed_ref(self.subject_ref) if self.subject_ref else None,
            "auth_context_ref": (
                safe_token(self.auth_context_ref) if self.auth_context_ref else None
            ),
            "identity_provider": (
                safe_token(self.identity_provider) if self.identity_provider else None
            ),
        }


@dataclass(frozen=True)
class ExternalActionTarget:
    queue_item_id: str | None = None
    work_item_id: str | None = None
    work_item_type: str | None = None
    lifecycle_state: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "queue_item_id": safe_token(self.queue_item_id) if self.queue_item_id else None,
            "work_item_id": safe_token(self.work_item_id) if self.work_item_id else None,
            "work_item_type": safe_token(self.work_item_type) if self.work_item_type else None,
            "lifecycle_state": (
                safe_token(self.lifecycle_state) if self.lifecycle_state else None
            ),
        }


@dataclass(frozen=True)
class StatusLocation:
    target_type: str
    target_id: str
    display_label: str
    relative_path: str | None = None
    json_path: str | None = None
    html_path: str | None = None
    external_url: str | None = None
    generated_by: str = "status-read-model"
    generated_at: str = field(default_factory=utc_now_iso)
    schema_version: str = STATUS_LOCATION_SCHEMA_VERSION

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target_type": safe_token(self.target_type),
            "target_id": safe_token(self.target_id),
            "display_label": safe_text(self.display_label, 120),
            "relative_path": safe_route(self.relative_path),
            "json_path": safe_route(self.json_path),
            "html_path": safe_route(self.html_path),
            "external_url": self.external_url if is_safe_external_url(self.external_url) else None,
            "generated_by": safe_token(self.generated_by),
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True)
class ExternalActionRequest:
    action_type: str
    source_type: str
    source_anchor: SourceAnchor
    actor: ExternalActor
    target: ExternalActionTarget = field(default_factory=ExternalActionTarget)
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    idempotency_key: str | None = None
    action_id: str = field(default_factory=lambda: new_id("act"))
    correlation_id: str = field(default_factory=lambda: new_id("corr"))
    requested_at: str = field(default_factory=utc_now_iso)
    response_preference: str = "synchronous"
    profile_ref: str | None = None
    project_id: str | None = None
    schema_version: str = ACTION_SCHEMA_VERSION

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action_id": safe_token(self.action_id),
            "action_type": safe_token(self.action_type),
            "source_type": safe_token(self.source_type),
            "source_anchor_summary": safe_source_summary(self.source_anchor),
            "actor": self.actor.to_safe_dict(),
            "profile_ref": safe_token(self.profile_ref) if self.profile_ref else None,
            "project_id": safe_token(self.project_id) if self.project_id else None,
            "target": self.target.to_safe_dict(),
            "payload": safe_payload(self.payload),
            "reason": safe_text(self.reason, 200) if self.reason else None,
            "idempotency_key_ref": (
                keyed_ref(self.idempotency_key) if self.idempotency_key else None
            ),
            "correlation_id": safe_token(self.correlation_id),
            "requested_at": self.requested_at,
            "response_preference": safe_token(self.response_preference),
        }


@dataclass(frozen=True)
class ExternalActionReceipt:
    receipt_id: str
    action_id: str
    action_type: str
    outcome: str
    target: ExternalActionTarget
    current_state: dict[str, Any]
    status_location: StatusLocation
    actor_summary: dict[str, Any]
    source_anchor_summary: dict[str, Any]
    correlation_id: str
    journal_ref: str | None = None
    permission_decision: str = "allowed"
    decision_reason: str = "allowed"
    profile_summary: dict[str, Any] = field(default_factory=dict)
    audit_ref: str | None = None
    safe_reason: str | None = None
    next_allowed_actions: list[str] = field(default_factory=list)
    notification_state: str = "not_required"
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = RECEIPT_SCHEMA_VERSION

    def to_safe_dict(self) -> dict[str, Any]:
        target = self.target.to_safe_dict()
        payload = {
            "schema_version": self.schema_version,
            "receipt_id": safe_token(self.receipt_id),
            "action_id": safe_token(self.action_id),
            "action_type": safe_token(self.action_type),
            "outcome": safe_token(self.outcome),
            "target": target,
            "current_state": safe_payload(self.current_state),
            "status_location": self.status_location.to_safe_dict(),
            "actor_summary": safe_payload(self.actor_summary),
            "source_anchor_summary": safe_payload(self.source_anchor_summary),
            "correlation_id": safe_token(self.correlation_id),
            "journal_ref": safe_token(self.journal_ref) if self.journal_ref else None,
            "audit_ref": safe_token(self.audit_ref) if self.audit_ref else None,
            "permission_decision": safe_token(self.permission_decision),
            "decision_reason": safe_token(self.decision_reason),
            "profile_summary": safe_payload(self.profile_summary),
            "safe_reason": safe_text(self.safe_reason, 200) if self.safe_reason else None,
            "next_allowed_actions": [safe_token(item) for item in self.next_allowed_actions],
            "notification_state": safe_token(self.notification_state),
            "created_at": self.created_at,
        }
        # Compatibility aliases keep current CLI callers working while the
        # receipt remains the external response boundary.
        payload.update(
            {
                "queue_item_id": target.get("queue_item_id"),
                "work_item_id": target.get("work_item_id"),
                "work_item_type": target.get("work_item_type"),
                "lifecycle_state": target.get("lifecycle_state"),
                "status": payload["current_state"].get("queue_status")
                or payload["current_state"].get("status"),
            }
        )
        payload.update(safe_extra(self.extra))
        return payload


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    permission_decision: str = "allowed"


class ExternalActionPolicy:
    def __init__(self, *, allow_cli_promotion: bool = False) -> None:
        self.allow_cli_promotion = allow_cli_promotion

    def authorize(self, request: ExternalActionRequest) -> PolicyDecision:
        if request.actor.actor_type in {"unauthenticated", "anonymous"}:
            return PolicyDecision(False, "unauthenticated", "denied")
        if request.source_type in DENY_API_MCP_DEFAULTS:
            return PolicyDecision(False, "source_disabled_by_default", "denied")
        if request.action_type in CAPTURE_ACTIONS | STATUS_ACTIONS:
            return PolicyDecision(True, "allowed_low_risk_action", "allowed")
        if request.action_type in PROMOTE_ACTIONS and not self.allow_cli_promotion:
            return PolicyDecision(False, "promotion_denied_by_default", "denied")
        if request.source_type != CLI_SOURCE:
            return PolicyDecision(False, "source_not_authorized_for_mutation", "denied")
        if request.action_type in SENSITIVE_ACTIONS and not (request.reason or "").strip():
            return PolicyDecision(False, "sensitive_action_requires_reason", "denied")
        if request.action_type == ACTION_READ_RAW_REFERENCE and not (
            request.actor.display_label and request.target.queue_item_id and request.correlation_id
        ):
            return PolicyDecision(
                False,
                "support_action_requires_actor_target_correlation",
                "denied",
            )
        return PolicyDecision(True, "authorized_local_cli_action", "allowed")


class FileExternalActionStore:
    def __init__(
        self,
        state_root: Path,
        project_id: str,
        *,
        namespace: str = "external_actions",
    ) -> None:
        self.project_id = project_id
        validate_logical_id(namespace, field_name="namespace")
        self.root = state_root / "projects" / project_id / namespace
        self.actions_dir = self.root / "actions"
        self.receipts_dir = self.root / "receipts"
        self.idempotency_dir = self.root / "index" / "idempotency"
        self.target_dir = self.root / "index" / "targets"
        for path in [
            self.actions_dir,
            self.receipts_dir,
            self.idempotency_dir,
            self.target_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def receipt_for_idempotency_key(
        self,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        path = self.idempotency_dir / f"{keyed_ref(idempotency_key)}.json"
        self._assert_within(path, self.idempotency_dir)
        if not path.exists():
            return None
        index = self._read_json(path)
        receipt_id = str(index.get("receipt_id") or "")
        if not receipt_id:
            return None
        return self.get_receipt(receipt_id)

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        validate_logical_id(receipt_id, field_name="receipt_id")
        path = self.receipts_dir / f"{receipt_id}.json"
        self._assert_within(path, self.receipts_dir)
        if not path.exists():
            return None
        return self._read_json(path)

    def record(
        self,
        *,
        request: ExternalActionRequest,
        receipt: ExternalActionReceipt,
    ) -> None:
        validate_logical_id(request.action_id, field_name="action_id")
        validate_logical_id(receipt.receipt_id, field_name="receipt_id")
        action_path = self.actions_dir / f"{request.action_id}.json"
        receipt_path = self.receipts_dir / f"{receipt.receipt_id}.json"
        self._assert_within(action_path, self.actions_dir)
        self._assert_within(receipt_path, self.receipts_dir)
        self._atomic_write_json(action_path, request.to_safe_dict())
        self._atomic_write_json(receipt_path, receipt.to_safe_dict())
        if request.idempotency_key:
            index_path = self.idempotency_dir / f"{keyed_ref(request.idempotency_key)}.json"
            self._assert_within(index_path, self.idempotency_dir)
            self._atomic_write_json(
                index_path,
                {
                    "receipt_id": receipt.receipt_id,
                    "action_id": request.action_id,
                    "target": receipt.target.to_safe_dict(),
                },
            )
        target_id = receipt.target.queue_item_id or receipt.target.work_item_id
        if target_id:
            validate_logical_id(target_id, field_name="target_id")
            target_path = self.target_dir / f"{target_id}.json"
            self._assert_within(target_path, self.target_dir)
            self._atomic_write_json(
                target_path,
                {
                    "receipt_id": receipt.receipt_id,
                    "action_id": request.action_id,
                    "action_type": request.action_type,
                    "updated_at": receipt.created_at,
                },
            )

    def list_receipts(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        receipts: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for path in sorted(self.receipts_dir.glob("*.json")):
            try:
                receipts.append(self._read_json(path))
            except Exception as exc:
                errors.append(
                    {
                        "receipt_id": path.stem,
                        "error_class": exc.__class__.__name__,
                    }
                )
        return receipts, errors

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise WorkQueueError("Expected JSON object")
        return data

    def _atomic_write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        self._assert_within(tmp, path.parent)
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(path)

    @staticmethod
    def _assert_within(path: Path, root: Path) -> None:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        if resolved_root != resolved_path and resolved_root not in resolved_path.parents:
            raise WorkQueueError("Path escaped external action root")


class QueueReceiptNotificationService:
    def __init__(
        self,
        *,
        project_id: str,
        policy: NotificationPolicyConfig,
        work_queue: FileWorkQueueStore,
        attempt_store: FileNotificationAttemptStore,
        connector_outbox: FileConnectorOutbox,
        source_route_store: FileSourceRouteStore | None = None,
    ) -> None:
        self.project_id = project_id
        self.policy = policy
        self.work_queue = work_queue
        self.attempt_store = attempt_store
        self.connector_outbox = connector_outbox
        self.evaluator = NotificationPolicyEvaluator(policy)
        self.route_resolver = NotificationRouteResolver(
            policy=policy,
            source_route_store=source_route_store,
        )

    def record(
        self,
        *,
        request: ExternalActionRequest,
        item: QueueItem,
        event_kind: str,
        target: ExternalActionTarget,
        failure_class: str | None = None,
        reason: str | None = None,
    ) -> QueueItem:
        visibility = self.evaluator.visibility_for(
            event_kind=event_kind,
            role_id=item.owner_role,
        )
        event = self._event_for(
            request=request,
            item=item,
            event_kind=event_kind,
            target=target,
            visibility=visibility,
            failure_class=failure_class,
            reason=reason,
        )
        if visibility in {"dashboard_only", "suppress"}:
            return self.work_queue.record_notification(
                item.queue_item_id,
                status="dashboard_only" if visibility == "dashboard_only" else "suppressed_by_policy",
                reason=visibility,
                event_kind=event.event_kind,
                dedupe_ref=keyed_ref(event.dedupe_key),
                correlation_id=request.correlation_id,
            )
        try:
            resolution = self.route_resolver.resolve(event)
            attempt = self.attempt_store.create_or_dedupe(event, resolution)
        except Exception as exc:
            return self.work_queue.record_notification(
                item.queue_item_id,
                status="failed",
                reason="attempt_store_failed",
                event_kind=event.event_kind,
                failure_class=redacted_error_class(exc),
                correlation_id=request.correlation_id,
            )
        if attempt.status == "deduplicated":
            return self._record_attempt_summary(
                item=item,
                event=event,
                attempt=attempt,
                resolution=resolution,
                status="deduplicated",
                reason="existing_notification_attempt",
            )
        if resolution.result == "unroutable":
            return self._record_attempt_summary(
                item=item,
                event=event,
                attempt=attempt,
                resolution=resolution,
                status="blocked_unroutable",
                reason=resolution.fallback_reason,
            )
        if not resolution.dispatch_route:
            dead = self.attempt_store.dead_letter(attempt, "MissingDispatchRoute")
            return self._record_attempt_summary(
                item=item,
                event=event,
                attempt=dead,
                resolution=resolution,
                status="dead_lettered",
                reason="dispatch_route_missing",
                failure_class="MissingDispatchRoute",
                dead_lettered=True,
            )
        recorded = self._record_attempt_summary(
            item=item,
            event=event,
            attempt=attempt,
            resolution=resolution,
            status=attempt.status,
            reason=resolution.fallback_reason,
        )
        try:
            self.connector_outbox.enqueue(
                ConnectorMessage.create(
                    channel=resolution.dispatch_route,
                    message_type=MESSAGE_TYPE_NOTIFICATION_EVENT,
                    payload={
                        **event.to_dict(),
                        "notification_attempt_id": attempt.attempt_id,
                        "route_kind": resolution.route_kind,
                        "selected_surface_key": resolution.selected_surface_key,
                        "route_label": resolution.route_label,
                        "fallback_reason": resolution.fallback_reason,
                    },
                    source=f"notification-policy:{self.project_id}",
                    correlation_id=request.correlation_id,
                )
            )
        except Exception as exc:
            dead = self.attempt_store.dead_letter(attempt, exc.__class__.__name__)
            return self._record_attempt_summary(
                item=recorded,
                event=event,
                attempt=dead,
                resolution=resolution,
                status="dead_lettered",
                reason="connector_outbox_failed",
                failure_class=redacted_error_class(exc),
                dead_lettered=True,
            )
        return recorded

    def _event_for(
        self,
        *,
        request: ExternalActionRequest,
        item: QueueItem,
        event_kind: str,
        target: ExternalActionTarget,
        visibility: str,
        failure_class: str | None,
        reason: str | None,
    ) -> NotificationEvent:
        promotion = item.promotion
        work_item_id = target.work_item_id or (promotion.work_item_id if promotion else None)
        work_item_type = (
            target.work_item_type
            or (promotion.work_item_type if promotion else item.recommended_work_item_type)
        )
        lifecycle_state = target.lifecycle_state or (
            promotion.lifecycle_state if promotion else None
        )
        action_needed = event_kind in {EVENT_QUEUE_BLOCKED, EVENT_QUEUE_PROMOTION_FAILED}
        return NotificationEvent.create(
            event_kind=event_kind,
            event_group="work_queue",
            visibility=visibility,
            project_id=self.project_id,
            action_needed=action_needed,
            action_owner=item.owner_role if action_needed else None,
            next_action=(
                "Review the queue item and clear the blocker."
                if event_kind == EVENT_QUEUE_BLOCKED
                else "Review promotion failure before retrying."
                if event_kind == EVENT_QUEUE_PROMOTION_FAILED
                else None
            ),
            retryable=event_kind == EVENT_QUEUE_PROMOTION_FAILED,
            work_item_id=work_item_id,
            work_item_type=work_item_type,
            lifecycle_state=lifecycle_state,
            queue_item_id=item.queue_item_id,
            source_anchor_ref=item.source_anchor.source_anchor_ref(),
            source_anchor_summary=item.source_anchor.display_label,
            title=item.title,
            summary=item.summary,
            status_label=_status_label_for_event(event_kind),
            status_detail=reason or item.blocker_reason or item.status,
            status_links=(StatusLinkBuilder().queue(),),
            display_category=_display_category_for_event(event_kind),
            owner_role=item.owner_role,
            reason_summary=reason or item.blocker_reason,
            failure_class=failure_class,
            retryability_label="retry after resolving failure" if failure_class else None,
            occurred_at=utc_now_iso(),
            correlation_id=request.correlation_id or item.correlation_id,
            dedupe_key=_notification_dedupe_key(
                self.project_id,
                event_kind,
                item.queue_item_id,
                work_item_id,
            ),
        )

    def _record_attempt_summary(
        self,
        *,
        item: QueueItem,
        event: NotificationEvent,
        attempt,
        resolution: RouteResolution,
        status: str,
        reason: str | None,
        failure_class: str | None = None,
        dead_lettered: bool = False,
    ) -> QueueItem:
        return self.work_queue.record_notification(
            item.queue_item_id,
            status=status,
            reason=reason,
            event_kind=event.event_kind,
            attempt_id=attempt.attempt_id,
            dedupe_ref=keyed_ref(attempt.dedupe_key),
            route_kind=attempt.route_kind or resolution.route_kind,
            selected_surface_key=(
                attempt.selected_surface_key or resolution.selected_surface_key
            ),
            route_label=attempt.route_label or resolution.route_label,
            fallback_reason=attempt.fallback_reason or resolution.fallback_reason,
            failure_class=failure_class or attempt.redacted_error_class,
            retry_count=attempt.retry_count,
            dead_lettered=dead_lettered or attempt.status == "dead_lettered",
            correlation_id=event.correlation_id,
        )


class ExternalActionService:
    def __init__(
        self,
        *,
        project_id: str,
        work_queue: FileWorkQueueStore,
        store: FileExternalActionStore,
        journal: EventJournal,
        policy: ExternalActionPolicy | None = None,
        message_store: FileMessageStore | None = None,
        notification_service: QueueReceiptNotificationService | None = None,
    ) -> None:
        self.project_id = project_id
        self.work_queue = work_queue
        self.store = store
        self.journal = journal
        self.policy = policy or ExternalActionPolicy()
        self.message_store = message_store
        self.notification_service = notification_service

    def execute(self, request: ExternalActionRequest) -> ExternalActionReceipt:
        attrs = external_action_attrs(self.project_id, request)
        with telemetry.start_span(
            f"external_action.{request.action_type}",
            correlation_id=request.correlation_id,
            attributes=attrs,
        ):
            self._journal("external_action_received", request)
            duplicate = self.store.receipt_for_idempotency_key(request.idempotency_key)
            if duplicate is not None:
                receipt = receipt_from_safe_dict(duplicate, outcome=OUTCOME_DUPLICATE)
                self._journal(
                    "external_action_replay_detected",
                    request,
                    receipt=receipt,
                )
                return receipt
            decision = self.policy.authorize(request)
            if not decision.allowed:
                receipt = self._denied_receipt(request, decision.reason)
                self._record_receipt(request, receipt, event_type="external_action_denied")
                return receipt
            self._journal("external_action_authorized", request)
            try:
                receipt = self._execute_authorized(request)
            except Exception as exc:
                receipt = self._failed_receipt(request, exc.__class__.__name__)
            event_type = (
                "external_action_executed"
                if receipt.outcome
                in {OUTCOME_CAPTURED, OUTCOME_ACTION_RECORDED, OUTCOME_DUPLICATE}
                else "external_action_failed"
            )
            return self._record_receipt(request, receipt, event_type=event_type)

    def _execute_authorized(
        self,
        request: ExternalActionRequest,
    ) -> ExternalActionReceipt:
        if request.action_type in CAPTURE_ACTIONS:
            return self._capture_work(request)
        if request.action_type in STATUS_ACTIONS:
            return self._show_status(request)
        if request.action_type in MARK_READY_ACTIONS:
            return self._mark_ready(request)
        if request.action_type in TRANSITION_ACTIONS:
            return self._transition(request)
        if request.action_type in PROMOTE_ACTIONS:
            return self._promote(request)
        if request.action_type == ACTION_READ_RAW_REFERENCE:
            return self._support_read(request)
        if request.action_type == ACTION_PURGE_RAW:
            return self._purge_raw(request)
        raise WorkQueueError(f"Unsupported external action `{request.action_type}`")

    def _capture_work(self, request: ExternalActionRequest) -> ExternalActionReceipt:
        existing = self.work_queue.get_by_idempotency_key(request.idempotency_key)
        item = self.work_queue.capture(
            title=str(request.payload.get("title") or ""),
            summary=str(request.payload.get("summary") or ""),
            owner_role=str(request.payload.get("owner_role") or "business-analyst"),
            source_anchor=request.source_anchor,
            recommended_work_item_type=str(
                request.payload.get("recommended_work_item_type") or "spike"
            ),
            correlation_id=request.correlation_id,
            idempotency_key=request.idempotency_key,
            metadata={"source": request.source_type},
            raw_payload=(
                request.payload.get("raw_payload")
                if isinstance(request.payload.get("raw_payload"), dict)
                else None
            ),
            retain_raw_payload=bool(request.payload.get("retain_raw_payload", False)),
        )
        receipt = receipt_for_queue_item(
            request,
            item,
            outcome=OUTCOME_DUPLICATE if existing else OUTCOME_CAPTURED,
            journal_ref="external_action_receipt_recorded",
        )
        if existing:
            return receipt
        return self._with_queue_notification(
            request,
            receipt,
            item,
            event_kind=EVENT_QUEUE_CAPTURED,
        )

    def _show_status(self, request: ExternalActionRequest) -> ExternalActionReceipt:
        item = self._require_target_item(request)
        return receipt_for_queue_item(
            request,
            item,
            outcome=OUTCOME_ACTION_RECORDED,
            journal_ref="external_action_receipt_recorded",
        )

    def _mark_ready(self, request: ExternalActionRequest) -> ExternalActionReceipt:
        item = self.work_queue.mark_readiness(
            request.target.queue_item_id,
            actor_role=request.actor.role_id or request.actor.display_label,
            evidence=dict(request.payload.get("evidence") or {}),
            correlation_id=request.correlation_id,
        )
        receipt = receipt_for_queue_item(
            request,
            item,
            outcome=OUTCOME_ACTION_RECORDED,
            journal_ref="external_action_receipt_recorded",
        )
        return self._with_queue_notification(
            request,
            receipt,
            item,
            event_kind=EVENT_QUEUE_READY_FOR_PROMOTION,
        )

    def _transition(self, request: ExternalActionRequest) -> ExternalActionReceipt:
        item = self.work_queue.transition(
            request.target.queue_item_id,
            str(request.payload.get("status") or ""),
            actor_role=request.actor.role_id or request.actor.display_label,
            reason=request.reason,
            correlation_id=request.correlation_id,
        )
        receipt = receipt_for_queue_item(
            request,
            item,
            outcome=OUTCOME_ACTION_RECORDED,
            journal_ref="external_action_receipt_recorded",
        )
        if item.status != "blocked":
            return receipt
        return self._with_queue_notification(
            request,
            receipt,
            item,
            event_kind=EVENT_QUEUE_BLOCKED,
            reason=request.reason,
        )

    def _promote(self, request: ExternalActionRequest) -> ExternalActionReceipt:
        if self.message_store is None:
            raise WorkQueueError("message_store is required for promotion")
        promotion = self.work_queue.promote(
            request.target.queue_item_id,
            actor_role=request.actor.role_id or request.actor.display_label,
            message_store=self.message_store,
            target_role=str(request.payload["target_role"]),
            lifecycle_state=str(request.payload["lifecycle_state"]),
            work_item_id=request.payload.get("work_item_id"),
            work_item_type=request.payload.get("work_item_type"),
            message_type=str(request.payload.get("message_type") or "sdlc.intake"),
            idempotency_key=request.idempotency_key,
        )
        item = self._require_target_item(request)
        target = ExternalActionTarget(
            queue_item_id=item.queue_item_id,
            work_item_id=promotion.work_item_id,
            work_item_type=promotion.work_item_type,
            lifecycle_state=promotion.lifecycle_state,
        )
        receipt = receipt_for_queue_item(
            request,
            item,
            outcome=OUTCOME_ACTION_RECORDED,
            target=target,
            journal_ref="external_action_receipt_recorded",
        )
        return self._with_queue_notification(
            request,
            receipt,
            item,
            event_kind=EVENT_QUEUE_PROMOTED,
        )

    def _support_read(self, request: ExternalActionRequest) -> ExternalActionReceipt:
        support = self.work_queue.support_read(
            request.target.queue_item_id,
            actor=request.actor.display_label,
            reason=request.reason or "",
            correlation_id=request.correlation_id,
        )
        item = self._require_target_item(request)
        receipt = receipt_for_queue_item(
            request,
            item,
            outcome=OUTCOME_ACTION_RECORDED,
            journal_ref="external_action_support_access_granted",
        )
        return replace(
            receipt,
            extra={
                "raw_ref_count": support["raw_ref_count"],
                "raw_refs": support["raw_refs"],
                "metadata_keys": support["metadata_keys"],
            },
        )

    def _purge_raw(self, request: ExternalActionRequest) -> ExternalActionReceipt:
        result = self.work_queue.purge_raw(
            queue_item_id=request.target.queue_item_id,
            dry_run=not bool(request.payload.get("execute", False)),
            actor=request.actor.display_label or None,
            reason=request.reason,
            correlation_id=request.correlation_id,
        )
        item = (
            self.work_queue.get(request.target.queue_item_id)
            if request.target.queue_item_id
            else None
        )
        target = ExternalActionTarget(queue_item_id=request.target.queue_item_id)
        receipt = self._base_receipt(
            request,
            outcome=OUTCOME_ACTION_RECORDED,
            target=target,
            current_state={"purge": safe_payload(result)},
            status_location=status_location_for("work_queue", "work-queue"),
            journal_ref="external_action_receipt_recorded",
        )
        if item is not None:
            receipt = receipt_for_queue_item(
                request,
                item,
                outcome=OUTCOME_ACTION_RECORDED,
                journal_ref="external_action_receipt_recorded",
            )
        return receipt

    def _require_target_item(self, request: ExternalActionRequest) -> QueueItem:
        if not request.target.queue_item_id:
            raise WorkQueueError("queue_item_id is required")
        item = self.work_queue.get(request.target.queue_item_id)
        if item is None:
            raise WorkQueueError(f"Unknown queue item `{request.target.queue_item_id}`")
        return item

    def _denied_receipt(
        self,
        request: ExternalActionRequest,
        reason: str,
    ) -> ExternalActionReceipt:
        item = (
            self.work_queue.get(request.target.queue_item_id)
            if request.target.queue_item_id
            else None
        )
        if item is not None:
            return receipt_for_queue_item(
                request,
                item,
                outcome=OUTCOME_ACTION_DENIED,
                journal_ref="external_action_denied",
                safe_reason=reason,
                permission_decision="denied",
                decision_reason=reason,
            )
        return self._base_receipt(
            request,
            outcome=OUTCOME_ACTION_DENIED,
            current_state={"status": "denied"},
            safe_reason=reason,
            journal_ref="external_action_denied",
            permission_decision="denied",
            decision_reason=reason,
        )

    def _failed_receipt(
        self,
        request: ExternalActionRequest,
        reason: str,
    ) -> ExternalActionReceipt:
        outcome = OUTCOME_NOT_CAPTURED if request.action_type in CAPTURE_ACTIONS else OUTCOME_ACTION_FAILED
        item = (
            self.work_queue.get(request.target.queue_item_id)
            if request.target.queue_item_id
            else None
        )
        if item is not None and request.action_type in PROMOTE_ACTIONS:
            receipt = receipt_for_queue_item(
                request,
                item,
                outcome=outcome,
                safe_reason=reason,
                journal_ref="external_action_failed",
            )
            return self._with_queue_notification(
                request,
                receipt,
                item,
                event_kind=EVENT_QUEUE_PROMOTION_FAILED,
                failure_class=reason,
                reason="promotion_failed",
            )
        return self._base_receipt(
            request,
            outcome=outcome,
            current_state={"failure_class": safe_token(reason)},
            safe_reason=reason,
            journal_ref="external_action_failed",
        )

    def _base_receipt(
        self,
        request: ExternalActionRequest,
        *,
        outcome: str,
        target: ExternalActionTarget | None = None,
        current_state: dict[str, Any] | None = None,
        status_location: StatusLocation | None = None,
        safe_reason: str | None = None,
        journal_ref: str | None = None,
        permission_decision: str = "allowed",
        decision_reason: str = "allowed",
    ) -> ExternalActionReceipt:
        resolved_target = target or request.target
        return ExternalActionReceipt(
            receipt_id=new_id("receipt"),
            action_id=request.action_id,
            action_type=request.action_type,
            outcome=outcome,
            target=resolved_target,
            current_state=current_state or {},
            status_location=status_location
            or status_location_for(
                "queue_item",
                resolved_target.queue_item_id or resolved_target.work_item_id or "status",
            ),
            actor_summary=request.actor.to_safe_dict(),
            source_anchor_summary=safe_source_summary(request.source_anchor),
            correlation_id=request.correlation_id,
            journal_ref=journal_ref,
            audit_ref=journal_ref,
            permission_decision=permission_decision,
            decision_reason=decision_reason,
            profile_summary=profile_summary_for(request),
            safe_reason=safe_reason,
            notification_state="not_required",
        )

    def _with_queue_notification(
        self,
        request: ExternalActionRequest,
        receipt: ExternalActionReceipt,
        item: QueueItem,
        *,
        event_kind: str,
        failure_class: str | None = None,
        reason: str | None = None,
    ) -> ExternalActionReceipt:
        if self.notification_service is None:
            return receipt
        notified = self.notification_service.record(
            request=request,
            item=item,
            event_kind=event_kind,
            target=receipt.target,
            failure_class=failure_class,
            reason=reason,
        )
        notification = notified.notification
        if notification is None:
            return receipt
        current_state = {
            **receipt.current_state,
            "notification_state": notification.status,
        }
        return replace(
            receipt,
            current_state=current_state,
            notification_state=notification.status,
            extra={
                **receipt.extra,
                "notification": notification.to_safe_dict(),
            },
        )

    def _record_receipt(
        self,
        request: ExternalActionRequest,
        receipt: ExternalActionReceipt,
        *,
        event_type: str,
    ) -> ExternalActionReceipt:
        try:
            self.store.record(request=request, receipt=receipt)
            self._journal(event_type, request, receipt=receipt)
            self._journal("external_action_receipt_recorded", request, receipt=receipt)
        except Exception as exc:
            self._journal(
                "external_action_failed",
                request,
                receipt=receipt,
                failure_class=exc.__class__.__name__,
            )
            if request.action_type == ACTION_CAPTURE_WORK:
                receipt = self._failed_receipt(request, exc.__class__.__name__)
            else:
                raise
        return receipt

    def _journal(
        self,
        event_type: str,
        request: ExternalActionRequest,
        *,
        receipt: ExternalActionReceipt | None = None,
        failure_class: str | None = None,
    ) -> dict[str, Any]:
        fields = {
            "project_id": self.project_id,
            "action_type": request.action_type,
            "receipt_outcome": receipt.outcome if receipt else None,
            "queue_item_id": (
                receipt.target.queue_item_id
                if receipt
                else request.target.queue_item_id
            ),
            "work_item_id": receipt.target.work_item_id if receipt else None,
            "work_item_type": receipt.target.work_item_type if receipt else None,
            "lifecycle_state": receipt.target.lifecycle_state if receipt else None,
            "queue_status": (
                (receipt.current_state or {}).get("queue_status") if receipt else None
            ),
            "connector_type": request.source_anchor.connector_type,
            "connector_id": request.source_anchor.connector_id,
            "source_anchor_ref": request.source_anchor.source_anchor_ref(),
            "actor_type": request.actor.actor_type,
            "role_id": request.actor.role_id,
            "correlation_id": request.correlation_id,
            "schema_version": request.schema_version,
            "failure_class": failure_class,
            "permission_decision": receipt.permission_decision if receipt else None,
            "decision_reason": receipt.decision_reason if receipt else None,
            "notification_state": (
                receipt.notification_state if receipt else "not_required"
            ),
            "notification_event_kind": (
                ((receipt.extra or {}).get("notification") or {}).get("event_kind")
                if receipt
                else None
            ),
            "notification_attempt_id": (
                ((receipt.extra or {}).get("notification") or {}).get("attempt_id")
                if receipt
                else None
            ),
            "notification_route_kind": (
                ((receipt.extra or {}).get("notification") or {}).get("route_kind")
                if receipt
                else None
            ),
            "notification_selected_surface_key": (
                ((receipt.extra or {}).get("notification") or {}).get("selected_surface_key")
                if receipt
                else None
            ),
            "notification_fallback_reason": (
                ((receipt.extra or {}).get("notification") or {}).get("fallback_reason")
                if receipt
                else None
            ),
        }
        return self.journal.append(
            event_type,
            **{
                key: value
                for key, value in fields.items()
                if key in ALLOWED_JOURNAL_FIELDS and value is not None
            },
        )


def receipt_for_queue_item(
    request: ExternalActionRequest,
    item: QueueItem,
    *,
    outcome: str,
    target: ExternalActionTarget | None = None,
    journal_ref: str | None = None,
    safe_reason: str | None = None,
    permission_decision: str = "allowed",
    decision_reason: str = "allowed",
) -> ExternalActionReceipt:
    summary = item.redacted_summary()
    resolved_target = target or ExternalActionTarget(
        queue_item_id=item.queue_item_id,
        work_item_id=item.promotion.work_item_id if item.promotion else None,
        work_item_type=(
            item.promotion.work_item_type
            if item.promotion
            else item.recommended_work_item_type
        ),
        lifecycle_state=item.promotion.lifecycle_state if item.promotion else None,
    )
    return ExternalActionReceipt(
        receipt_id=new_id("receipt"),
        action_id=request.action_id,
        action_type=request.action_type,
        outcome=outcome,
        target=resolved_target,
        current_state={
            "queue_status": item.status,
            "title": item.title,
            "owner_role": item.owner_role,
            "notification_state": (
                item.notification.status if item.notification else "not_required"
            ),
        },
        status_location=status_location_for("queue_item", item.queue_item_id),
        actor_summary=request.actor.to_safe_dict(),
        source_anchor_summary=safe_source_summary(item.source_anchor),
        correlation_id=item.correlation_id or request.correlation_id,
        journal_ref=journal_ref,
        audit_ref=journal_ref,
        permission_decision=permission_decision,
        decision_reason=decision_reason,
        profile_summary=profile_summary_for(request),
        safe_reason=safe_reason,
        next_allowed_actions=next_allowed_actions(item, outcome),
        notification_state=item.notification.status if item.notification else "not_required",
    )


def receipt_from_safe_dict(
    data: dict[str, Any],
    *,
    outcome: str | None = None,
) -> ExternalActionReceipt:
    target_data = data.get("target") or {}
    status_data = data.get("status_location") or {}
    return ExternalActionReceipt(
        receipt_id=str(data["receipt_id"]),
        action_id=str(data["action_id"]),
        action_type=str(data["action_type"]),
        outcome=outcome or str(data["outcome"]),
        target=ExternalActionTarget(
            queue_item_id=target_data.get("queue_item_id") or data.get("queue_item_id"),
            work_item_id=target_data.get("work_item_id") or data.get("work_item_id"),
            work_item_type=target_data.get("work_item_type") or data.get("work_item_type"),
            lifecycle_state=target_data.get("lifecycle_state") or data.get("lifecycle_state"),
        ),
        current_state=dict(data.get("current_state") or {}),
        status_location=StatusLocation(
            target_type=str(status_data.get("target_type") or "status_view"),
            target_id=str(status_data.get("target_id") or "status"),
            display_label=str(status_data.get("display_label") or "Status"),
            relative_path=status_data.get("relative_path"),
            json_path=status_data.get("json_path"),
            html_path=status_data.get("html_path"),
            external_url=status_data.get("external_url"),
            generated_by=str(status_data.get("generated_by") or "status-read-model"),
            generated_at=str(status_data.get("generated_at") or utc_now_iso()),
        ),
        actor_summary=dict(data.get("actor_summary") or {}),
        source_anchor_summary=dict(data.get("source_anchor_summary") or {}),
        correlation_id=str(data["correlation_id"]),
        journal_ref=data.get("journal_ref"),
        audit_ref=data.get("audit_ref"),
        permission_decision=str(data.get("permission_decision") or "allowed"),
        decision_reason=str(data.get("decision_reason") or "allowed"),
        profile_summary=dict(data.get("profile_summary") or {}),
        safe_reason=data.get("safe_reason"),
        next_allowed_actions=list(data.get("next_allowed_actions") or []),
        notification_state=str(data.get("notification_state") or "not_required"),
        extra={
            key: value
            for key, value in data.items()
            if key
            not in {
                "schema_version",
                "receipt_id",
                "action_id",
                "action_type",
                "outcome",
                "target",
                "current_state",
                "status_location",
                "actor_summary",
                "source_anchor_summary",
                "correlation_id",
                "journal_ref",
                "audit_ref",
                "permission_decision",
                "decision_reason",
                "profile_summary",
                "safe_reason",
                "next_allowed_actions",
                "notification_state",
                "created_at",
                "queue_item_id",
                "work_item_id",
                "work_item_type",
                "lifecycle_state",
                "status",
            }
        },
        created_at=str(data.get("created_at") or utc_now_iso()),
        schema_version=str(data.get("schema_version") or RECEIPT_SCHEMA_VERSION),
    )


def status_location_for(target_type: str, target_id: str) -> StatusLocation:
    safe_id = safe_token(target_id)
    if target_type == "queue_item":
        route_id = quote(safe_id, safe="")
        return StatusLocation(
            target_type="queue_item",
            target_id=safe_id,
            display_label=f"Queue item {safe_id}",
            relative_path=f"/work-queue/{route_id}",
            html_path="/work-queue",
            json_path="/work-queue.json",
        )
    if target_type == "work_item":
        route_id = quote(safe_id, safe="")
        return StatusLocation(
            target_type="work_item",
            target_id=safe_id,
            display_label=f"Work item {safe_id}",
            relative_path=f"/work-items/{route_id}",
            html_path=f"/work-items/{route_id}",
            json_path=f"/work-items/{route_id}.json",
        )
    return StatusLocation(
        target_type="status_view",
        target_id=safe_id,
        display_label="Work queue status",
        relative_path="/work-queue",
        html_path="/work-queue",
        json_path="/work-queue.json",
    )


def _notification_dedupe_key(
    project_id: str,
    event_kind: str,
    queue_item_id: str,
    work_item_id: str | None,
) -> str:
    raw = "|".join([project_id, event_kind, queue_item_id, work_item_id or ""])
    return f"dedupe-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _status_label_for_event(event_kind: str) -> str:
    return {
        EVENT_QUEUE_CAPTURED: "Queue item captured",
        EVENT_QUEUE_READY_FOR_PROMOTION: "Queue item ready for promotion",
        EVENT_QUEUE_PROMOTED: "Queue item promoted",
        EVENT_QUEUE_BLOCKED: "Queue item blocked",
        EVENT_QUEUE_PROMOTION_FAILED: "Queue promotion failed",
    }.get(event_kind, "Queue status changed")


def _display_category_for_event(event_kind: str) -> str:
    return {
        EVENT_QUEUE_CAPTURED: "queue_captured",
        EVENT_QUEUE_PROMOTED: "queue_promoted",
        EVENT_QUEUE_BLOCKED: "blocker_or_role_failure",
        EVENT_QUEUE_PROMOTION_FAILED: "notification_delivery_problem",
    }.get(event_kind, "unknown_status_update")


def derive_idempotency_key(
    *parts: str,
    secret_material: str | bytes | None = None,
    sensitive: bool = True,
    allow_local_fallback: bool = False,
) -> str:
    value = "\x1f".join(str(part) for part in parts if part is not None)
    if secret_material:
        key = (
            secret_material
            if isinstance(secret_material, bytes)
            else str(secret_material).encode("utf-8")
        )
        digest = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"
    if sensitive and not allow_local_fallback:
        raise WorkQueueError("sensitive idempotency derivation requires key material")
    return f"sha256-local:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def external_action_attrs(
    project_id: str,
    request: ExternalActionRequest,
    *,
    outcome: str | None = None,
    failure_class: str | None = None,
) -> dict[str, Any]:
    return telemetry.span_attributes(
        project_id=project_id,
        action_type=request.action_type,
        receipt_outcome=outcome,
        connector_type=request.source_anchor.connector_type,
        connector_id=request.source_anchor.connector_id,
        source_anchor_ref=request.source_anchor.source_anchor_ref(),
        actor_type=request.actor.actor_type,
        role_id=request.actor.role_id,
        correlation_id=request.correlation_id,
        schema_version=request.schema_version,
        failure_class=failure_class,
        notification_state="not_required",
    )


def next_allowed_actions(item: QueueItem, outcome: str) -> list[str]:
    if outcome == OUTCOME_ACTION_DENIED:
        return []
    if item.status == "captured":
        return ["show_status", "mark_ready", "transition"]
    if item.status == "ready_for_promotion":
        return ["show_status", "promote"]
    return ["show_status"]


def safe_source_summary(anchor: SourceAnchor) -> dict[str, Any]:
    return safe_payload(anchor.redacted_summary())


def profile_summary_for(request: ExternalActionRequest) -> dict[str, Any]:
    if not request.profile_ref:
        return {}
    return {
        "profile_ref": safe_token(request.profile_ref),
        "project_id": safe_token(request.project_id or ""),
        "actor_type": safe_token(request.actor.actor_type),
        "identity_provider": (
            safe_token(request.actor.identity_provider)
            if request.actor.identity_provider
            else "local_dev"
        ),
        "token_status": "opaque_ref_only",
    }


def safe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            safe_token(str(key)): safe_payload(item)
            for key, item in value.items()
            if not is_prohibited_key(str(key))
        }
    if isinstance(value, list):
        return [safe_payload(item) for item in value[:20]]
    if isinstance(value, str):
        return safe_text(value, 500)
    if isinstance(value, bool | int | float) or value is None:
        return value
    return safe_text(str(value), 200)


def safe_extra(value: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if key == "raw_refs" and isinstance(item, list):
            safe[key] = [safe_token(str(ref)) for ref in item]
        elif key in {"raw_ref_count", "metadata_keys"}:
            safe[key] = safe_payload(item)
        elif not is_prohibited_key(str(key)):
            safe[safe_token(str(key))] = safe_payload(item)
    return safe


def safe_text(value: str | None, max_length: int) -> str:
    text = html.escape(str(value or ""), quote=True)
    for marker in PROHIBITED_TEXT_MARKERS:
        text = text.replace(marker, "[redacted]")
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3].rstrip()}..."


def safe_token(value: str | None) -> str:
    text = str(value or "unknown").strip()
    if not text:
        return "unknown"
    return "".join(ch if ch.isalnum() or ch in {"-", "_", ".", ":"} else "-" for ch in text)[:160]


def safe_route(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value)
    if text.startswith("/") and ".." not in text and "://" not in text:
        return text
    return None


def is_safe_external_url(value: str | None) -> bool:
    return bool(value and str(value).startswith("https://status."))


def keyed_ref(value: str | None) -> str:
    if not value:
        raise WorkQueueError("idempotency key is required")
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def is_prohibited_key(key: str) -> bool:
    lower = key.lower()
    return any(marker in lower for marker in PROHIBITED_TEXT_MARKERS) or lower in {
        "raw_payload",
        "raw_refs",
        "external_url",
        "actor",
        "source_message_id",
    }


def receipt_output(receipt: ExternalActionReceipt, **extra: Any) -> dict[str, Any]:
    payload = receipt.to_safe_dict()
    payload.update(safe_payload(extra))
    return payload
