from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import Message
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso
from agentic_mesh.storage import FileMessageStore


QUEUE_SCHEMA_VERSION = "work-queue-v0"
QUEUE_NOTIFICATION_SCHEMA_VERSION = "queue-notification-summary-v0"

STATUS_CAPTURED = "captured"
STATUS_TRIAGING = "triaging"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_BLOCKED = "blocked"
STATUS_READY_FOR_PROMOTION = "ready_for_promotion"
STATUS_PROMOTED = "promoted"
STATUS_CLOSED = "closed"
STATUS_CANCELED = "canceled"

SUCCESSFUL_WORK_COMPLETION_STATUSES = {
    "completed",
    "completed_after_human_response",
}

QUEUE_STATUSES = {
    STATUS_CAPTURED,
    STATUS_TRIAGING,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_BLOCKED,
    STATUS_READY_FOR_PROMOTION,
    STATUS_PROMOTED,
    STATUS_CLOSED,
    STATUS_CANCELED,
}

ALLOWED_TRANSITIONS = {
    STATUS_CAPTURED: {
        STATUS_TRIAGING,
        STATUS_NEEDS_CLARIFICATION,
        STATUS_BLOCKED,
        STATUS_READY_FOR_PROMOTION,
        STATUS_CLOSED,
        STATUS_CANCELED,
    },
    STATUS_TRIAGING: {
        STATUS_NEEDS_CLARIFICATION,
        STATUS_BLOCKED,
        STATUS_READY_FOR_PROMOTION,
        STATUS_CLOSED,
        STATUS_CANCELED,
    },
    STATUS_NEEDS_CLARIFICATION: {
        STATUS_TRIAGING,
        STATUS_BLOCKED,
        STATUS_CLOSED,
        STATUS_CANCELED,
    },
    STATUS_BLOCKED: {STATUS_TRIAGING, STATUS_CLOSED, STATUS_CANCELED},
    STATUS_READY_FOR_PROMOTION: {STATUS_PROMOTED, STATUS_CANCELED},
    STATUS_PROMOTED: {STATUS_CLOSED},
    STATUS_CLOSED: set(),
    STATUS_CANCELED: set(),
}

READINESS_REQUIRED_FIELDS = {
    "requester",
    "outcome",
    "scope",
    "constraints",
    "priority_or_risk_signal",
    "recommended_work_item_type",
}

INTAKE_PROMOTION_STATUSES = {
    STATUS_CAPTURED,
    STATUS_TRIAGING,
    STATUS_NEEDS_CLARIFICATION,
}


class WorkQueueError(ValueError):
    pass


class InvalidTransitionError(WorkQueueError):
    def __init__(
        self,
        *,
        queue_item_id: str,
        current_status: str,
        attempted_status: str,
        actor_role: str,
        correlation_id: str,
    ) -> None:
        super().__init__(
            "Invalid queue transition "
            f"{current_status} -> {attempted_status} for {queue_item_id}"
        )
        self.queue_item_id = queue_item_id
        self.current_status = current_status
        self.attempted_status = attempted_status
        self.actor_role = actor_role
        self.correlation_id = correlation_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue_item_id": self.queue_item_id,
            "current_status": self.current_status,
            "attempted_status": self.attempted_status,
            "actor_role": self.actor_role,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class SourceAnchor:
    connector_type: str
    connector_id: str
    source_scope: str
    source_message_id: str | None
    actor: str | None
    received_at: str
    display_label: str
    external_url: str | None = None
    extensions: dict[str, Any] = field(default_factory=dict)

    def source_anchor_ref(self) -> str:
        value = "|".join(
            [
                self.connector_type,
                self.connector_id,
                self.source_scope,
                self.source_message_id or "",
                self.actor or "",
            ]
        )
        return f"source:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"

    def redacted_summary(self) -> dict[str, Any]:
        return {
            "connector_type": self.connector_type,
            "connector_id": self.connector_id,
            "source_scope": self.source_scope,
            "source_anchor_ref": self.source_anchor_ref(),
            "display_label": _truncate(self.display_label, 120),
            "received_at": self.received_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SourceAnchor":
        return SourceAnchor(
            connector_type=str(data["connector_type"]),
            connector_id=str(data["connector_id"]),
            source_scope=str(data["source_scope"]),
            source_message_id=(
                str(data["source_message_id"])
                if data.get("source_message_id") is not None
                else None
            ),
            actor=str(data["actor"]) if data.get("actor") is not None else None,
            received_at=str(data["received_at"]),
            display_label=str(data.get("display_label") or ""),
            external_url=(
                str(data["external_url"]) if data.get("external_url") else None
            ),
            extensions=dict(data.get("extensions") or {}),
        )


@dataclass(frozen=True)
class QueueReadiness:
    ready_by_role: str
    evidence: dict[str, Any]
    marked_at: str

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "QueueReadiness":
        return QueueReadiness(
            ready_by_role=str(data["ready_by_role"]),
            evidence=dict(data.get("evidence") or {}),
            marked_at=str(data["marked_at"]),
        )


@dataclass(frozen=True)
class QueuePromotion:
    work_item_id: str
    work_item_type: str
    lifecycle_state: str
    target_role: str
    message_id: str
    promoted_by: str
    promoted_at: str
    idempotency_key: str

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "QueuePromotion":
        return QueuePromotion(
            work_item_id=str(data["work_item_id"]),
            work_item_type=str(data["work_item_type"]),
            lifecycle_state=str(data["lifecycle_state"]),
            target_role=str(data["target_role"]),
            message_id=str(data["message_id"]),
            promoted_by=str(data["promoted_by"]),
            promoted_at=str(data["promoted_at"]),
            idempotency_key=str(data["idempotency_key"]),
        )


@dataclass(frozen=True)
class QueueNotification:
    status: str
    reason: str | None
    recorded_at: str
    event_kind: str | None = None
    attempt_id: str | None = None
    dedupe_ref: str | None = None
    route_kind: str | None = None
    selected_surface_key: str | None = None
    route_label: str | None = None
    fallback_reason: str | None = None
    failure_class: str | None = None
    retry_count: int = 0
    dead_lettered: bool = False
    correlation_id: str | None = None
    schema_version: str = QUEUE_NOTIFICATION_SCHEMA_VERSION

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "QueueNotification":
        return QueueNotification(
            status=str(data["status"]),
            reason=str(data["reason"]) if data.get("reason") is not None else None,
            recorded_at=str(data["recorded_at"]),
            event_kind=(
                str(data["event_kind"]) if data.get("event_kind") is not None else None
            ),
            attempt_id=(
                str(data["attempt_id"]) if data.get("attempt_id") is not None else None
            ),
            dedupe_ref=(
                str(data["dedupe_ref"]) if data.get("dedupe_ref") is not None else None
            ),
            route_kind=(
                str(data["route_kind"]) if data.get("route_kind") is not None else None
            ),
            selected_surface_key=(
                str(data["selected_surface_key"])
                if data.get("selected_surface_key") is not None
                else None
            ),
            route_label=(
                str(data["route_label"]) if data.get("route_label") is not None else None
            ),
            fallback_reason=(
                str(data["fallback_reason"])
                if data.get("fallback_reason") is not None
                else None
            ),
            failure_class=(
                str(data["failure_class"]) if data.get("failure_class") is not None else None
            ),
            retry_count=int(data.get("retry_count", 0)),
            dead_lettered=bool(data.get("dead_lettered", False)),
            correlation_id=(
                str(data["correlation_id"])
                if data.get("correlation_id") is not None
                else None
            ),
            schema_version=str(
                data.get("schema_version") or QUEUE_NOTIFICATION_SCHEMA_VERSION
            ),
        )

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "event_kind": self.event_kind,
            "attempt_id": self.attempt_id,
            "dedupe_ref": self.dedupe_ref,
            "route_kind": self.route_kind,
            "selected_surface_key": self.selected_surface_key,
            "route_label": self.route_label,
            "fallback_reason": self.fallback_reason,
            "failure_class": self.failure_class,
            "retry_count": self.retry_count,
            "dead_lettered": self.dead_lettered,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class QueueItem:
    queue_item_id: str
    title: str
    summary: str
    status: str
    owner_role: str
    recommended_work_item_type: str
    source_anchor: SourceAnchor
    correlation_id: str
    created_at: str
    updated_at: str
    last_status_at: str
    schema_version: str = QUEUE_SCHEMA_VERSION
    readiness: QueueReadiness | None = None
    promotion: QueuePromotion | None = None
    notification: QueueNotification | None = None
    blocker_reason: str | None = None
    raw_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def redacted_summary(self) -> dict[str, Any]:
        return {
            "queue_item_id": self.queue_item_id,
            "title": self.title,
            "status": self.status,
            "owner_role": self.owner_role,
            "recommended_work_item_type": self.recommended_work_item_type,
            "source_anchor": self.source_anchor.redacted_summary(),
            "promoted_work_item_id": (
                self.promotion.work_item_id if self.promotion else None
            ),
            "updated_at": self.updated_at,
            "blocker_reason": self.blocker_reason,
            "notification": (
                self.notification.to_safe_dict() if self.notification else None
            ),
            "notification_failure_reason": (
                self.notification.reason or self.notification.failure_class
                if self.notification
                and self.notification.status
                in {"failed", "dead_lettered", "blocked_unroutable"}
                else None
            ),
            "correlation_id": self.correlation_id,
            "schema_version": self.schema_version,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "QueueItem":
        readiness = data.get("readiness")
        promotion = data.get("promotion")
        notification = data.get("notification")
        return QueueItem(
            queue_item_id=str(data["queue_item_id"]),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            status=str(data["status"]),
            owner_role=str(data["owner_role"]),
            recommended_work_item_type=str(data["recommended_work_item_type"]),
            source_anchor=SourceAnchor.from_dict(data["source_anchor"]),
            correlation_id=str(data["correlation_id"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            last_status_at=str(data["last_status_at"]),
            schema_version=str(data.get("schema_version") or QUEUE_SCHEMA_VERSION),
            readiness=(
                QueueReadiness.from_dict(readiness) if isinstance(readiness, dict) else None
            ),
            promotion=(
                QueuePromotion.from_dict(promotion) if isinstance(promotion, dict) else None
            ),
            notification=(
                QueueNotification.from_dict(notification)
                if isinstance(notification, dict)
                else None
            ),
            blocker_reason=(
                str(data["blocker_reason"])
                if data.get("blocker_reason") is not None
                else None
            ),
            raw_refs=[str(item) for item in data.get("raw_refs") or []],
            metadata=dict(data.get("metadata") or {}),
        )


class FileWorkQueueStore:
    def __init__(self, state_root: Path, project_id: str, journal: EventJournal) -> None:
        self.project_id = project_id
        self.journal = journal
        self.root = state_root / "projects" / project_id / "work_queue"
        self.items_dir = self.root / "items"
        self.source_index_dir = self.root / "index" / "source-keys"
        self.promotion_index_dir = self.root / "index" / "promotions"
        self.raw_dir = self.root / "raw"
        self.locks_dir = self.root / "locks"
        for path in [
            self.items_dir,
            self.source_index_dir,
            self.promotion_index_dir,
            self.raw_dir,
            self.locks_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def capture(
        self,
        *,
        title: str,
        summary: str,
        owner_role: str,
        source_anchor: SourceAnchor,
        recommended_work_item_type: str,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        raw_payload: dict[str, Any] | None = None,
        retain_raw_payload: bool = False,
    ) -> QueueItem:
        correlation = correlation_id or new_id("corr")
        index_path = (
            self.source_index_dir / f"{self.safe_index_key(idempotency_key)}.json"
            if idempotency_key
            else None
        )
        if index_path is not None and index_path.exists():
            index = self._read_json(index_path)
            existing = self.get(str(index["queue_item_id"]))
            if existing is None:
                raise WorkQueueError("source idempotency index points to missing item")
            return existing

        now = utc_now_iso()
        queue_item_id = new_id("queue")
        raw_refs: list[str] = []
        if raw_payload is not None and retain_raw_payload:
            raw_refs.append(self._write_raw_payload(queue_item_id, raw_payload))
        item = QueueItem(
            queue_item_id=queue_item_id,
            title=title,
            summary=summary,
            status=STATUS_CAPTURED,
            owner_role=owner_role,
            recommended_work_item_type=recommended_work_item_type,
            source_anchor=source_anchor,
            correlation_id=correlation,
            created_at=now,
            updated_at=now,
            last_status_at=now,
            raw_refs=raw_refs,
            metadata=dict(metadata or {}),
        )
        self._write_item(item)
        if index_path is not None:
            self._atomic_write_json(index_path, {"queue_item_id": queue_item_id})
        self._append_queue_event("queue_item_created", item)
        self._record_depth_gauges()
        return item

    def get(self, queue_item_id: str) -> QueueItem | None:
        validate_logical_id(queue_item_id, field_name="queue_item_id")
        path = self._item_path(queue_item_id)
        if not path.exists():
            return None
        return self._read_item(path)

    def get_by_idempotency_key(self, idempotency_key: str | None) -> QueueItem | None:
        if idempotency_key is None or not str(idempotency_key).strip():
            return None
        index_path = self.source_index_dir / f"{self.safe_index_key(idempotency_key)}.json"
        if not index_path.exists():
            return None
        index = self._read_json(index_path)
        return self.get(str(index["queue_item_id"]))

    def list_items(self) -> list[QueueItem]:
        items: list[QueueItem] = []
        for path in sorted(self.items_dir.glob("*.json")):
            try:
                items.append(self._read_item(path))
            except json.JSONDecodeError:
                self.journal.append(
                    "queue_item_corrupt",
                    project_id=self.project_id,
                    queue_item_id=path.stem,
                    reason="invalid_json",
                )
        return items

    def transition(
        self,
        queue_item_id: str,
        status: str,
        *,
        actor_role: str,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> QueueItem:
        if status not in QUEUE_STATUSES:
            raise WorkQueueError(f"Unknown queue status `{status}`")
        with self._locked(queue_item_id):
            item = self._require_item(queue_item_id)
            correlation = correlation_id or item.correlation_id
            if status not in ALLOWED_TRANSITIONS[item.status]:
                error = InvalidTransitionError(
                    queue_item_id=queue_item_id,
                    current_status=item.status,
                    attempted_status=status,
                    actor_role=actor_role,
                    correlation_id=correlation,
                )
                self.journal.append(
                    "queue_item_invalid_transition",
                    project_id=self.project_id,
                    queue_item_id=queue_item_id,
                    queue_status=item.status,
                    attempted_status=status,
                    owner_role=item.owner_role,
                    actor_role=actor_role,
                    correlation_id=correlation,
                    schema_version=item.schema_version,
                )
                raise error
            now = utc_now_iso()
            updated = replace(
                item,
                status=status,
                updated_at=now,
                last_status_at=now,
                blocker_reason=reason if status == STATUS_BLOCKED else item.blocker_reason,
            )
            self._write_item(updated)
        event_type = {
            STATUS_BLOCKED: "queue_item_blocked",
            STATUS_CLOSED: "queue_item_closed",
            STATUS_CANCELED: "queue_item_canceled",
        }.get(status, "queue_item_status_changed")
        self._append_queue_event(
            event_type,
            updated,
            actor_role=actor_role,
            reason=reason,
        )
        self._record_depth_gauges()
        return updated

    def mark_readiness(
        self,
        queue_item_id: str,
        *,
        actor_role: str,
        evidence: dict[str, Any],
        correlation_id: str | None = None,
    ) -> QueueItem:
        with self._locked(queue_item_id):
            item = self._require_item(queue_item_id)
            if actor_role == "promotion-service":
                raise WorkQueueError("promotion service cannot mark queue readiness")
            work_type = str(
                evidence.get("recommended_work_item_type")
                or item.recommended_work_item_type
            )
            if work_type in {"slice", "feature"} and actor_role != "product-manager":
                raise WorkQueueError("Product Manager readiness is required")
            if work_type == "spike" and actor_role != "business-analyst":
                raise WorkQueueError("Business Analyst readiness is required for spikes")
            missing = [
                field_name
                for field_name in sorted(READINESS_REQUIRED_FIELDS)
                if not evidence.get(field_name)
            ]
            if missing:
                raise WorkQueueError(
                    "Readiness evidence missing: " + ", ".join(missing)
                )
            now = utc_now_iso()
            readiness = QueueReadiness(
                ready_by_role=actor_role,
                evidence=dict(evidence),
                marked_at=now,
            )
            updated = replace(
                item,
                status=STATUS_READY_FOR_PROMOTION,
                readiness=readiness,
                recommended_work_item_type=work_type,
                updated_at=now,
                last_status_at=now,
            )
            self._write_item(updated)
        self._append_queue_event(
            "queue_item_readiness_changed",
            updated,
            actor_role=actor_role,
            correlation_id=correlation_id or updated.correlation_id,
        )
        self._record_depth_gauges()
        return updated

    def record_notification(
        self,
        queue_item_id: str,
        *,
        status: str,
        reason: str | None,
        event_kind: str | None = None,
        attempt_id: str | None = None,
        dedupe_ref: str | None = None,
        route_kind: str | None = None,
        selected_surface_key: str | None = None,
        route_label: str | None = None,
        fallback_reason: str | None = None,
        failure_class: str | None = None,
        retry_count: int = 0,
        dead_lettered: bool = False,
        correlation_id: str | None = None,
    ) -> QueueItem:
        with self._locked(queue_item_id):
            item = self._require_item(queue_item_id)
            notification = QueueNotification(
                status=status,
                reason=reason,
                recorded_at=utc_now_iso(),
                event_kind=event_kind,
                attempt_id=attempt_id,
                dedupe_ref=dedupe_ref,
                route_kind=route_kind,
                selected_surface_key=selected_surface_key,
                route_label=route_label,
                fallback_reason=fallback_reason,
                failure_class=failure_class,
                retry_count=retry_count,
                dead_lettered=dead_lettered,
                correlation_id=correlation_id or item.correlation_id,
            )
            updated = replace(
                item,
                notification=notification,
                updated_at=utc_now_iso(),
            )
            self._write_item(updated)
        event_type = (
            "queue_item_notification_failed"
            if status == "failed"
            else f"queue_item_notification_{status}"
        )
        self._append_queue_event(
            event_type,
            updated,
            reason=reason,
            notification_status=status,
            notification_event_kind=event_kind,
            notification_attempt_id=attempt_id,
            notification_dedupe_ref=dedupe_ref,
            notification_route_kind=route_kind,
            notification_selected_surface_key=selected_surface_key,
            notification_route_label=route_label,
            notification_fallback_reason=fallback_reason,
            notification_failure_class=failure_class,
            notification_retry_count=retry_count,
            notification_dead_lettered=dead_lettered,
            correlation_id=correlation_id or updated.correlation_id,
        )
        return updated

    def promote(
        self,
        queue_item_id: str,
        *,
        actor_role: str,
        message_store: FileMessageStore,
        target_role: str,
        lifecycle_state: str,
        work_item_id: str | None = None,
        work_item_type: str | None = None,
        message_type: str = "sdlc.intake",
        idempotency_key: str | None = None,
    ) -> QueuePromotion:
        with self._locked(queue_item_id):
            item = self._require_item(queue_item_id)
            if item.promotion is not None:
                return item.promotion
            promotion_kind = "delivery"
            if item.status != STATUS_READY_FOR_PROMOTION:
                intake_role = _intake_role_for(item)
                if item.status not in INTAKE_PROMOTION_STATUSES:
                    raise WorkQueueError(
                        "queue item is not ready for delivery promotion "
                        f"and cannot be routed for intake from `{item.status}`"
                    )
                if target_role != intake_role:
                    raise WorkQueueError(
                        "queue item is not ready for delivery promotion; "
                        f"route it to `{intake_role}` for intake/readiness"
                    )
                promotion_kind = "intake"
            key = idempotency_key or f"{item.queue_item_id}:{item.correlation_id}"
            promotion_index = self.promotion_index_dir / f"{self.safe_index_key(key)}.json"
            if promotion_index.exists():
                promotion = QueuePromotion.from_dict(self._read_json(promotion_index))
                updated = replace(
                    item,
                    status=STATUS_PROMOTED,
                    promotion=promotion,
                    updated_at=utc_now_iso(),
                    last_status_at=utc_now_iso(),
                )
                self._write_item(updated)
                self._append_queue_event(
                    "queue_item_promoted",
                    updated,
                    actor_role=actor_role,
                    recovered=True,
                )
                self._record_depth_gauges()
                return promotion

            work_type = work_item_type or item.recommended_work_item_type
            work_id = work_item_id or new_id("work")
            self._append_queue_event(
                "queue_item_promotion_started",
                item,
                actor_role=actor_role,
                work_item_id=work_id,
                work_item_type=work_type,
            )
            message = Message.create(
                role_id=target_role,
                message_type=message_type,
                payload={
                    "title": item.title,
                    "summary": item.summary,
                    "work_item_id": work_id,
                    "work_item_type": work_type,
                    "lifecycle_state": lifecycle_state,
                    "queue_item_id": item.queue_item_id,
                    "source_anchor": item.source_anchor.redacted_summary(),
                    "queue_promotion_kind": promotion_kind,
                    "queue_status_at_promotion": item.status,
                    "readiness": (
                        asdict(item.readiness) if item.readiness is not None else None
                    ),
                },
                source=f"work-queue:{item.queue_item_id}",
                correlation_id=item.correlation_id,
            )
            message = message_store.enqueue(message)
            promotion = QueuePromotion(
                work_item_id=work_id,
                work_item_type=work_type,
                lifecycle_state=lifecycle_state,
                target_role=target_role,
                message_id=message.message_id,
                promoted_by=actor_role,
                promoted_at=utc_now_iso(),
                idempotency_key=self.safe_index_key(key),
            )
            self._atomic_write_json(promotion_index, asdict(promotion))
            now = utc_now_iso()
            updated = replace(
                item,
                status=STATUS_PROMOTED,
                promotion=promotion,
                updated_at=now,
                last_status_at=now,
            )
            self._write_item(updated)
        self._append_queue_event(
            "queue_item_promoted",
            updated,
            actor_role=actor_role,
            work_item_id=promotion.work_item_id,
            work_item_type=promotion.work_item_type,
        )
        self._record_depth_gauges()
        return promotion

    def reconcile_promoted_closures(
        self,
        *,
        message_store: FileMessageStore,
        actor_role: str = "work-queue-reconciler",
    ) -> list[QueueItem]:
        closed: list[QueueItem] = []
        for item in self.list_items():
            if item.status != STATUS_PROMOTED or item.promotion is None:
                continue
            work_item_id = item.promotion.work_item_id
            if _has_active_work_message(message_store, work_item_id):
                continue
            completion = _latest_work_completion(self.journal, work_item_id)
            if completion is None:
                continue
            if completion.get("status") not in SUCCESSFUL_WORK_COMPLETION_STATUSES:
                continue
            closed.append(
                self.transition(
                    item.queue_item_id,
                    STATUS_CLOSED,
                    actor_role=actor_role,
                    reason="Promoted lifecycle work completed with no active messages.",
                    correlation_id=item.correlation_id,
                )
            )
            self.journal.append(
                "queue_item_closed_by_reconciliation",
                project_id=self.project_id,
                queue_item_id=item.queue_item_id,
                work_item_id=work_item_id,
                work_item_type=item.promotion.work_item_type,
                completion_status=completion.get("status"),
                completion_event_timestamp=completion.get("timestamp"),
                actor_role=actor_role,
                correlation_id=item.correlation_id,
                schema_version=item.schema_version,
            )
        return closed

    def support_read(
        self,
        queue_item_id: str,
        *,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        if not actor or not reason or not correlation_id:
            raise WorkQueueError("support mode requires actor, reason, and correlation id")
        item = self._require_item(queue_item_id)
        self.journal.append(
            "queue_item_support_accessed",
            project_id=self.project_id,
            queue_item_id=item.queue_item_id,
            queue_status=item.status,
            owner_role=item.owner_role,
            actor=actor,
            reason=_truncate(reason, 200),
            correlation_id=correlation_id,
            schema_version=item.schema_version,
        )
        return {
            "queue_item_id": item.queue_item_id,
            "source_anchor": item.source_anchor.redacted_summary(),
            "raw_ref_count": len(item.raw_refs),
            "raw_refs": list(item.raw_refs),
            "metadata_keys": sorted(item.metadata),
        }

    def purge_raw(
        self,
        *,
        queue_item_id: str | None,
        dry_run: bool,
        actor: str | None = None,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        if not dry_run and (not actor or not reason):
            raise WorkQueueError("purge execute requires actor and reason")
        items = [self._require_item(queue_item_id)] if queue_item_id else self.list_items()
        refs_removed = 0
        files_removed = 0
        for item in items:
            item_raw_dir = self.raw_dir / item.queue_item_id
            files = list(item_raw_dir.glob("*.json")) if item_raw_dir.exists() else []
            refs_removed += len(item.raw_refs)
            files_removed += len(files)
            if dry_run:
                continue
            for path in files:
                path.unlink(missing_ok=True)
            if item_raw_dir.exists():
                try:
                    item_raw_dir.rmdir()
                except OSError:
                    pass
            updated = replace(item, raw_refs=[], updated_at=utc_now_iso())
            self._write_item(updated)
        event_type = "queue_item_purge_dry_run" if dry_run else "queue_item_purged"
        self.journal.append(
            event_type,
            project_id=self.project_id,
            queue_item_id=queue_item_id,
            item_count=len(items),
            raw_ref_count=refs_removed,
            raw_file_count=files_removed,
            actor=actor,
            reason=_truncate(reason or "", 200) if reason else None,
            correlation_id=correlation_id,
        )
        return {
            "dry_run": dry_run,
            "item_count": len(items),
            "raw_ref_count": refs_removed,
            "raw_file_count": files_removed,
        }

    def status_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for item in self.list_items():
            owner_counts = counts.setdefault(item.owner_role, {})
            owner_counts[item.status] = owner_counts.get(item.status, 0) + 1
        return counts

    @staticmethod
    def safe_index_key(value: str | None) -> str:
        if value is None or not str(value).strip():
            raise WorkQueueError("idempotency key is required")
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    def _write_raw_payload(self, queue_item_id: str, raw_payload: dict[str, Any]) -> str:
        validate_logical_id(queue_item_id, field_name="queue_item_id")
        payload_id = new_id("payload")
        path = self.raw_dir / queue_item_id / f"{payload_id}.json"
        self._assert_within(path, self.raw_dir)
        self._atomic_write_json(path, raw_payload)
        return f"raw:{hashlib.sha256(str(path.name).encode('utf-8')).hexdigest()[:16]}"

    def _require_item(self, queue_item_id: str | None) -> QueueItem:
        if not queue_item_id:
            raise WorkQueueError("queue_item_id is required")
        item = self.get(queue_item_id)
        if item is None:
            raise WorkQueueError(f"Unknown queue item `{queue_item_id}`")
        return item

    def _append_queue_event(
        self,
        event_type: str,
        item: QueueItem,
        **fields: Any,
    ) -> None:
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            queue_item_id=item.queue_item_id,
            queue_status=item.status,
            owner_role=item.owner_role,
            work_item_id=fields.get("work_item_id")
            or (item.promotion.work_item_id if item.promotion else None),
            work_item_type=fields.get("work_item_type")
            or item.recommended_work_item_type,
            connector_type=item.source_anchor.connector_type,
            connector_id=item.source_anchor.connector_id,
            source_anchor_ref=item.source_anchor.source_anchor_ref(),
            correlation_id=fields.get("correlation_id") or item.correlation_id,
            schema_version=item.schema_version,
        )
        with telemetry.start_span(
            event_type.replace("_", "."),
            correlation_id=attrs.get("correlation_id"),
            attributes=attrs,
        ):
            self.journal.append(
                event_type,
                project_id=self.project_id,
                queue_item_id=item.queue_item_id,
                queue_status=item.status,
                owner_role=item.owner_role,
                work_item_id=attrs.get("work_item_id"),
                work_item_type=attrs.get("work_item_type"),
                connector_type=item.source_anchor.connector_type,
                connector_id=item.source_anchor.connector_id,
                source_anchor_ref=item.source_anchor.source_anchor_ref(),
                correlation_id=attrs.get("correlation_id"),
                schema_version=item.schema_version,
                **{
                    key: value
                    for key, value in fields.items()
                    if key not in {"correlation_id", "work_item_id", "work_item_type"}
                },
            )

    def _record_depth_gauges(self) -> None:
        for owner_role, statuses in self.status_counts().items():
            for status, count in statuses.items():
                telemetry.set_gauge(
                    "agentic_mesh.work_queue.depth",
                    count,
                    {
                        "project_id": self.project_id,
                        "owner_role": owner_role,
                        "queue_status": status,
                    },
                )

    def _item_path(self, queue_item_id: str) -> Path:
        validate_logical_id(queue_item_id, field_name="queue_item_id")
        path = self.items_dir / f"{queue_item_id}.json"
        self._assert_within(path, self.items_dir)
        return path

    def _write_item(self, item: QueueItem) -> None:
        self._atomic_write_json(self._item_path(item.queue_item_id), item.to_dict())

    def _read_item(self, path: Path) -> QueueItem:
        self._assert_within(path, self.items_dir)
        return QueueItem.from_dict(self._read_json(path))

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
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(path)

    @contextmanager
    def _locked(self, queue_item_id: str):
        validate_logical_id(queue_item_id, field_name="queue_item_id")
        lock_path = self.locks_dir / f"{queue_item_id}.lock"
        self._assert_within(lock_path, self.locks_dir)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError as exc:
            raise WorkQueueError(f"Queue item `{queue_item_id}` is locked") from exc
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)

    @staticmethod
    def _assert_within(path: Path, root: Path) -> None:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        if resolved_root != resolved_path and resolved_root not in resolved_path.parents:
            raise WorkQueueError("Path escaped work queue root")


def validate_logical_id(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkQueueError(f"{field_name} is required")
    path = Path(value)
    if path.is_absolute():
        raise WorkQueueError(f"{field_name} must not be an absolute path")
    if value in {".", ".."} or ".." in path.parts:
        raise WorkQueueError(f"{field_name} must not contain parent traversal")
    if "/" in value or "\\" in value:
        raise WorkQueueError(f"{field_name} must not contain path separators")
    if value.startswith("file:") or value.startswith("http:") or value.startswith("https:"):
        raise WorkQueueError(f"{field_name} must be a logical id, not a URI")


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 3].rstrip()}..."


def _intake_role_for(item: QueueItem) -> str:
    work_type = item.recommended_work_item_type
    if work_type == "spike":
        return "business-analyst"
    if work_type in {"slice", "feature"}:
        return "product-manager"
    return item.owner_role


def _has_active_work_message(message_store: FileMessageStore, work_item_id: str) -> bool:
    for role_dir in sorted(message_store.root.iterdir() if message_store.root.exists() else []):
        if not role_dir.is_dir():
            continue
        pending = role_dir / "pending"
        if pending.exists():
            for path in pending.glob("*.json"):
                message = message_store._read_message(path)
                if message.payload.get("work_item_id") == work_item_id:
                    return True
        claimed = role_dir / "claimed"
        if claimed.exists():
            for path in claimed.glob("*/*.json"):
                message = message_store._read_message(path)
                if message.payload.get("work_item_id") == work_item_id:
                    return True
    return False


def _latest_work_completion(
    journal: EventJournal,
    work_item_id: str,
) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for event in journal.read_all():
        if event.get("event_type") != "work_completed":
            continue
        if event.get("work_item_id") != work_item_id:
            continue
        latest = event
    return latest


def source_anchor_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    value = payload.get("source_anchor")
    if isinstance(value, dict):
        return dict(value)
    return None
