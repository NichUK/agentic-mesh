from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.observability import span


DURABLE_CLASSIFICATIONS = {
    "decision",
    "requirement",
    "risk",
    "constraint",
    "approval",
    "instruction",
    "blocker",
    "release_fact",
}


@dataclass(frozen=True)
class ContextSummaryRequest:
    summary_id: str
    source_event_ids: tuple[str, ...]
    summary: str
    classification: str
    visibility_scope: str
    created_by_role: str
    durable_refs: tuple[str, ...] = ()
    target_ref: str | None = None


class ContextRetentionService:
    def __init__(self, db: V2Database, config: ConnectorConfig) -> None:
        self.db = db
        self.config = config

    def retention_days(self, key: str) -> int | None:
        return self.config.retention.get(key)

    def retention_key_for_conversation_event(self, event: dict[str, Any]) -> str:
        if event.get("visibility_scope") == "private":
            return "private_dm_days"
        payload = event.get("payload")
        channel_scope = payload.get("channel_scope") if isinstance(payload, dict) else None
        if isinstance(channel_scope, dict) and channel_scope.get("work_scope"):
            return "focus_channel_days" if "focus_channel_days" in self.config.retention else "project_channel_days"
        return "project_channel_days"

    def retention_key_for_receipt(self) -> str:
        return "idempotency_receipt_days"

    def retention_key_for_delivery(self) -> str:
        return "delivery_record_days"

    def retention_key_for_summary(self) -> str:
        return "compacted_summary_days"

    def compact_events(self, request: ContextSummaryRequest) -> dict[str, Any]:
        with span("v2.teams.compaction", connector_id=self.config.connector_id, visibility_scope=request.visibility_scope):
            if not request.source_event_ids:
                raise ValueError("context summary requires at least one source event")
            events_by_id = {
                str(event["conversation_event_id"]): event
                for event in self.db.list_conversation_events()
            }
            events = []
            for event_id in request.source_event_ids:
                event = events_by_id.get(event_id)
                if event is None:
                    raise ValueError(f"unknown conversation event `{event_id}`")
                events.append(event)
            conversation_ids = {str(event["conversation_id"]) for event in events}
            if len(conversation_ids) != 1:
                raise ValueError("context summary sources must belong to one conversation")
            connector_ids = {str(event["connector_id"]) for event in events}
            if connector_ids != {self.config.connector_id}:
                raise ValueError("context summary sources must belong to the configured connector")
            private_event_ids = {
                str(event["conversation_event_id"])
                for event in events
                if event.get("visibility_scope") == "private"
            }
            if private_event_ids and request.visibility_scope != "private":
                promoted = {
                    str(proposal.get("source_conversation_event_id"))
                    for proposal in self.db.list_work_proposals()
                    if proposal.get("source_conversation_event_id")
                }
                unpromoted = private_event_ids - promoted
                if unpromoted:
                    raise ValueError(
                        "private conversation context cannot be compacted into shared visibility without explicit promotion"
                    )
            if _requires_durable_target(request.classification) and not request.durable_refs and not request.target_ref:
                raise ValueError(
                    "durable context classifications require a durable reference or target"
                )
            retention_key = (
                "private_dm_days"
                if request.visibility_scope == "private"
                else self.retention_key_for_summary()
            )
            self.db.record_context_summary(
                summary_id=request.summary_id,
                connector_id=self.config.connector_id,
                conversation_id=conversation_ids.pop(),
                visibility_scope=request.visibility_scope,
                classification=request.classification,
                retention_key=retention_key,
                summary=request.summary,
                source_refs=list(request.source_event_ids),
                durable_refs=list(request.durable_refs),
                target_ref=request.target_ref,
                created_by_role=request.created_by_role,
            )
            summary = self.db.get_context_summary(request.summary_id)
            if summary is None:
                raise ValueError(f"context summary `{request.summary_id}` was not recorded")
            return summary

    def expire_conversation_event_raw(self, conversation_event_id: str) -> str:
        with span("v2.teams.retention_expiry", connector_id=self.config.connector_id, source_table="conversation_events"):
            existing = self.db.get_retention_expiry_record_by_source(
                source_table="conversation_events",
                source_id=conversation_event_id,
            )
            if existing is not None:
                return str(existing["expiry_id"])
            event = self._event_by_id(conversation_event_id)
            retention_key = self.retention_key_for_conversation_event(event)
            content_sha256 = _content_sha256(
                {
                    "body_preview": event.get("body_preview"),
                    "payload": event.get("payload"),
                }
            )
            expiry_id = f"retention-{_short_hash(f'conversation_events:{conversation_event_id}')}"
            self.db.record_retention_expiry(
                expiry_id=expiry_id,
                connector_id=self.config.connector_id,
                source_table="conversation_events",
                source_id=conversation_event_id,
                retention_key=retention_key,
                content_sha256=content_sha256,
                metadata={
                    "conversation_id": event.get("conversation_id"),
                    "event_type": event.get("event_type"),
                    "visibility_scope": event.get("visibility_scope"),
                    "role_id": event.get("role_id"),
                },
            )
            self.db.expire_conversation_event_raw(
                conversation_event_id=conversation_event_id,
                content_sha256=content_sha256,
                retention_key=retention_key,
            )
            return expiry_id

    def expire_external_receipt_raw(self, receipt_id: str) -> str:
        with span("v2.teams.retention_expiry", connector_id=self.config.connector_id, source_table="external_event_receipts"):
            existing = self.db.get_retention_expiry_record_by_source(
                source_table="external_event_receipts",
                source_id=receipt_id,
            )
            if existing is not None:
                return str(existing["expiry_id"])
            receipt = self._receipt_by_id(receipt_id)
            retention_key = self.retention_key_for_receipt()
            content_sha256 = _content_sha256(receipt.get("payload"))
            expiry_id = f"retention-{_short_hash(f'external_event_receipts:{receipt_id}')}"
            self.db.record_retention_expiry(
                expiry_id=expiry_id,
                connector_id=self.config.connector_id,
                source_table="external_event_receipts",
                source_id=receipt_id,
                retention_key=retention_key,
                content_sha256=content_sha256,
                metadata={
                    "external_event_id": receipt.get("external_event_id"),
                    "event_type": receipt.get("event_type"),
                    "idempotency_key": receipt.get("idempotency_key"),
                },
            )
            self.db.expire_external_receipt_raw(
                receipt_id=receipt_id,
                content_sha256=content_sha256,
                retention_key=retention_key,
            )
            return expiry_id

    def expire_delivery_record_raw(self, delivery_id: str) -> str:
        with span("v2.teams.retention_expiry", connector_id=self.config.connector_id, source_table="delivery_records"):
            existing = self.db.get_retention_expiry_record_by_source(
                source_table="delivery_records",
                source_id=delivery_id,
            )
            if existing is not None:
                return str(existing["expiry_id"])
            delivery = self.db.get_delivery_record(delivery_id)
            if delivery is None:
                raise ValueError(f"unknown delivery `{delivery_id}`")
            retention_key = self.retention_key_for_delivery()
            content_sha256 = _content_sha256(delivery.get("payload"))
            expiry_id = f"retention-{_short_hash(f'delivery_records:{delivery_id}')}"
            self.db.record_retention_expiry(
                expiry_id=expiry_id,
                connector_id=self.config.connector_id,
                source_table="delivery_records",
                source_id=delivery_id,
                retention_key=retention_key,
                content_sha256=content_sha256,
                metadata={
                    "destination_type": delivery.get("destination_type"),
                    "purpose": delivery.get("purpose"),
                    "status": delivery.get("status"),
                    "role_id": delivery.get("role_id"),
                },
            )
            self.db.expire_delivery_record_raw(
                delivery_id=delivery_id,
                content_sha256=content_sha256,
                retention_key=retention_key,
            )
            return expiry_id

    def _event_by_id(self, conversation_event_id: str) -> dict[str, Any]:
        event = self.db.get_conversation_event(conversation_event_id)
        if event is None:
            raise ValueError(f"unknown conversation event `{conversation_event_id}`")
        return event

    def _receipt_by_id(self, receipt_id: str) -> dict[str, Any]:
        for receipt in self.db.list_external_event_receipts():
            if receipt.get("receipt_id") == receipt_id:
                return receipt
        raise ValueError(f"unknown external receipt `{receipt_id}`")


def _content_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _requires_durable_target(classification: str) -> bool:
    normalized = str(classification or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized in DURABLE_CLASSIFICATIONS
