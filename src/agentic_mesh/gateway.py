from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any, Protocol

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import GatewayConfig
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import QueueItem
from agentic_mesh.work_queue import SourceAnchor


GATEWAY_SCHEMA_VERSION = "gateway-event-v0"
GATEWAY_RESULT_SCHEMA_VERSION = "gateway-result-v0"
GATEWAY_SESSION_SCHEMA_VERSION = "gateway-session-v0"

OUTCOME_QUICK_RESPONSE = "quick_response"
OUTCOME_STATUS_ANSWER = "status_answer"
OUTCOME_CLARIFICATION_NEEDED = "clarification_needed"
OUTCOME_ADD_CONTEXT = "add_context"
OUTCOME_CREATE_OR_UPDATE_QUEUE_ITEM = "create_or_update_queue_item"
OUTCOME_APPROVAL_RESPONSE = "approval_response"
OUTCOME_BLOCKER_REPORT = "blocker_report"
OUTCOME_ROLE_DIRECTED_HINT = "role_directed_hint"
OUTCOME_DIRECT_DELIVERY_ROUTED = "direct_delivery_routed"
OUTCOME_AUTHORIZATION_REQUIRED = "authorization_required"
OUTCOME_NOT_CAPTURED = "not_captured"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_FAILED = "failed"

GATEWAY_OUTCOMES = {
    OUTCOME_QUICK_RESPONSE,
    OUTCOME_STATUS_ANSWER,
    OUTCOME_CLARIFICATION_NEEDED,
    OUTCOME_ADD_CONTEXT,
    OUTCOME_CREATE_OR_UPDATE_QUEUE_ITEM,
    OUTCOME_APPROVAL_RESPONSE,
    OUTCOME_BLOCKER_REPORT,
    OUTCOME_ROLE_DIRECTED_HINT,
    OUTCOME_DIRECT_DELIVERY_ROUTED,
    OUTCOME_AUTHORIZATION_REQUIRED,
    OUTCOME_NOT_CAPTURED,
    OUTCOME_DUPLICATE,
    OUTCOME_FAILED,
}

FORBIDDEN_FIELD_NAMES = {
    "tenant_id",
    "team_id",
    "channel_id",
    "conversation_id",
    "activity_id",
    "user_id",
    "from_id",
    "bot_id",
    "service_url",
    "raw_payload",
    "raw_payload_ref",
    "credential_ref",
    "secret_ref",
    "mount_ref",
    "access_token",
    "refresh_token",
    "authorization",
    "prompt",
    "provider_output",
    "provider_error",
    "stdout",
    "stderr",
    "command_line",
    "absolute_path",
}

TOKEN_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]{12,}|token[=:]\S+|password[=:]\S+|secret[=:]\S+)"
)
ABSOLUTE_PATH_RE = re.compile(r"([A-Za-z]:\\|/(mesh|home|Users|tmp|var|opt)/)")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,180}$")
WORK_RE = re.compile(r"\bwork-[A-Za-z0-9][A-Za-z0-9._-]*\b")
QUEUE_RE = re.compile(r"\bqueue-[A-Za-z0-9][A-Za-z0-9._-]*\b")

WORK_ITEM_NATIVE_CONNECTORS = {
    "azure-devops",
    "azure_devops",
    "ado",
    "github",
    "github-issues",
    "github_issues",
    "jira",
    "linear",
}
WORK_ITEM_NATIVE_SOURCE_KINDS = {
    "backlog-item",
    "backlog_item",
    "bug",
    "feature",
    "issue",
    "pull-request",
    "pull_request",
    "story",
    "task",
    "ticket",
    "work-item",
    "work_item",
}
MESSAGING_CONNECTORS = {"discord", "slack", "teams"}
MESSAGING_SOURCE_KINDS = {"channel", "chat", "dm", "message", "thread"}


class GatewayError(ValueError):
    pass


@dataclass(frozen=True)
class GatewayActor:
    actor_ref: str
    display_label: str
    actor_kind: str = "user"
    capability_refs: tuple[str, ...] = ()

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "actor_ref": _safe_id(self.actor_ref, "actor_ref"),
            "display_label": _redact_text(self.display_label),
            "actor_kind": _safe_id(self.actor_kind, "actor_kind"),
            "capability_refs": [_safe_id(item, "capability_ref") for item in self.capability_refs],
        }


@dataclass(frozen=True)
class GatewayTarget:
    target_type: str
    target_ref: str
    owner_role: str | None = None
    lifecycle_state: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "target_type": _safe_id(self.target_type, "target_type"),
            "target_ref": _safe_id(self.target_ref, "target_ref"),
            "owner_role": _safe_optional_id(self.owner_role, "owner_role"),
            "lifecycle_state": _safe_optional_id(self.lifecycle_state, "lifecycle_state"),
        }


@dataclass(frozen=True)
class GatewayEvent:
    gateway_event_id: str
    project_id: str
    gateway_id: str
    connector_type: str
    connector_id: str
    source_anchor: SourceAnchor
    actor: GatewayActor
    text_summary: str
    source_kind: str
    idempotency_key: str
    received_at: str = field(default_factory=utc_now_iso)
    role_hints: tuple[str, ...] = ()
    explicit_refs: tuple[str, ...] = ()
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def to_safe_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "gateway_event_id": _safe_id(self.gateway_event_id, "gateway_event_id"),
            "project_id": _safe_id(self.project_id, "project_id"),
            "gateway_id": _safe_id(self.gateway_id, "gateway_id"),
            "connector_type": _safe_id(self.connector_type, "connector_type"),
            "connector_id": _safe_id(self.connector_id, "connector_id"),
            "source_anchor": self.source_anchor.redacted_summary(),
            "actor": self.actor.to_safe_dict(),
            "text_summary": _redact_text(self.text_summary, limit=500),
            "source_kind": _safe_id(self.source_kind, "source_kind"),
            "idempotency_ref": idempotency_ref(self.idempotency_key),
            "received_at": self.received_at,
            "role_hints": [_safe_id(item, "role_hint") for item in self.role_hints],
            "explicit_refs": [_safe_id(item, "explicit_ref") for item in self.explicit_refs],
        }
        assert_no_forbidden_fields(payload)
        return payload


@dataclass(frozen=True)
class GatewayResult:
    gateway_result_id: str
    gateway_event_id: str
    project_id: str
    gateway_id: str
    outcome: str
    correlation_id: str
    created_at: str = field(default_factory=utc_now_iso)
    queue_item_id: str | None = None
    work_item_id: str | None = None
    owner_role: str | None = None
    lifecycle_state: str | None = None
    target: GatewayTarget | None = None
    reason_class: str | None = None
    receipt: dict[str, Any] = field(default_factory=dict)
    schema_version: str = GATEWAY_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.outcome not in GATEWAY_OUTCOMES:
            raise GatewayError(f"unsupported gateway outcome `{self.outcome}`")

    def to_safe_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "gateway_result_id": _safe_id(self.gateway_result_id, "gateway_result_id"),
            "gateway_event_id": _safe_id(self.gateway_event_id, "gateway_event_id"),
            "project_id": _safe_id(self.project_id, "project_id"),
            "gateway_id": _safe_id(self.gateway_id, "gateway_id"),
            "outcome": self.outcome,
            "correlation_id": _safe_id(self.correlation_id, "correlation_id"),
            "created_at": self.created_at,
            "queue_item_id": _safe_optional_id(self.queue_item_id, "queue_item_id"),
            "work_item_id": _safe_optional_id(self.work_item_id, "work_item_id"),
            "owner_role": _safe_optional_id(self.owner_role, "owner_role"),
            "lifecycle_state": _safe_optional_id(self.lifecycle_state, "lifecycle_state"),
            "target": self.target.to_safe_dict() if self.target else None,
            "reason_class": _safe_optional_id(self.reason_class, "reason_class"),
            "receipt": _safe_receipt(self.receipt),
        }
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(frozen=True)
class GatewaySession:
    session_id: str
    project_id: str
    gateway_id: str
    actor_ref: str
    source_ref: str
    privacy_level: str
    status: str = "active"
    target: GatewayTarget | None = None
    updated_at: str = field(default_factory=utc_now_iso)
    schema_version: str = GATEWAY_SESSION_SCHEMA_VERSION

    def to_safe_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "session_id": _safe_id(self.session_id, "session_id"),
            "project_id": _safe_id(self.project_id, "project_id"),
            "gateway_id": _safe_id(self.gateway_id, "gateway_id"),
            "actor_ref": _safe_id(self.actor_ref, "actor_ref"),
            "source_ref": _safe_id(self.source_ref, "source_ref"),
            "privacy_level": _safe_id(self.privacy_level, "privacy_level"),
            "status": _safe_id(self.status, "status"),
            "target": self.target.to_safe_dict() if self.target else None,
            "updated_at": self.updated_at,
        }
        assert_no_forbidden_fields(payload)
        return payload


@dataclass(frozen=True)
class GatewayPolicyDecision:
    outcome: str
    reason_class: str
    target_ref: str | None = None
    owner_role: str | None = None


class SafeStatusReader(Protocol):
    def answer_status(self, ref: str) -> dict[str, Any] | None:
        ...


class GatewayPolicy:
    def __init__(
        self,
        *,
        gateway_config: GatewayConfig,
        role_ids: set[str] | None = None,
    ) -> None:
        self.gateway_config = gateway_config
        self.role_ids = set(role_ids or set())

    def decide(self, event: GatewayEvent) -> GatewayPolicyDecision:
        text = event.text_summary.casefold()
        explicit_refs = list(event.explicit_refs)
        work_item_native = _is_work_item_native_source(event)
        messaging_source = _is_messaging_source(event)
        if _is_status_question(text) and explicit_refs:
            return GatewayPolicyDecision(
                outcome=OUTCOME_STATUS_ANSWER,
                reason_class="safe_status_ref",
                target_ref=explicit_refs[0],
            )
        if _is_approval_response(text) and explicit_refs:
            return GatewayPolicyDecision(
                outcome=OUTCOME_APPROVAL_RESPONSE,
                reason_class="approval_response_with_ref",
                target_ref=explicit_refs[0],
            )
        if _is_blocker_report(text) and explicit_refs:
            return GatewayPolicyDecision(
                outcome=OUTCOME_BLOCKER_REPORT,
                reason_class="blocker_report_with_ref",
                target_ref=explicit_refs[0],
            )
        if _is_context_update(text) and explicit_refs:
            return GatewayPolicyDecision(
                outcome=OUTCOME_ADD_CONTEXT,
                reason_class="context_update_with_ref",
                target_ref=explicit_refs[0],
            )
        if messaging_source:
            if event.role_hints:
                return GatewayPolicyDecision(
                    outcome=OUTCOME_ROLE_DIRECTED_HINT,
                    reason_class="messaging_role_hint_requires_agent_decision",
                    owner_role=_owner_from_role_hints(event.role_hints, self.role_ids),
                )
            if _is_direct_delivery_request(text) or _is_meaningful_work(text):
                return GatewayPolicyDecision(
                    outcome=OUTCOME_CLARIFICATION_NEEDED,
                    reason_class="messaging_work_requires_agent_proposal",
                    owner_role=self.gateway_config.default_owner_role,
                )
        if _is_direct_delivery_request(text):
            return GatewayPolicyDecision(
                outcome=(
                    OUTCOME_CREATE_OR_UPDATE_QUEUE_ITEM
                    if work_item_native
                    else OUTCOME_DIRECT_DELIVERY_ROUTED
                ),
                reason_class=(
                    "work_item_native_delivery_request"
                    if work_item_native
                    else "no_delivery_work_boundary"
                ),
                owner_role=self.gateway_config.default_owner_role,
            )
        if _is_meaningful_work(text):
            return GatewayPolicyDecision(
                outcome=OUTCOME_CREATE_OR_UPDATE_QUEUE_ITEM,
                reason_class=(
                    "work_item_native_meaningful_work_request"
                    if work_item_native
                    else "meaningful_work_request"
                ),
                owner_role=_owner_from_role_hints(event.role_hints, self.role_ids)
                or self.gateway_config.default_owner_role,
            )
        if event.role_hints:
            return GatewayPolicyDecision(
                outcome=OUTCOME_ROLE_DIRECTED_HINT,
                reason_class="role_hint_without_actionable_work",
                owner_role=_owner_from_role_hints(event.role_hints, self.role_ids),
            )
        if _looks_ambiguous_followup(text):
            return GatewayPolicyDecision(
                outcome=OUTCOME_CLARIFICATION_NEEDED,
                reason_class="ambiguous_followup",
            )
        return GatewayPolicyDecision(
            outcome=OUTCOME_QUICK_RESPONSE,
            reason_class="informational_or_acknowledgement",
        )


class GatewayStore:
    def __init__(self, state_root: Path, project_id: str) -> None:
        _safe_id(project_id, "project_id")
        self.project_id = project_id
        self.root = state_root / "projects" / project_id / "gateway"
        self.events_dir = self.root / "events"
        self.results_dir = self.root / "results"
        self.sessions_dir = self.root / "sessions"
        self.idempotency_dir = self.root / "idempotency"
        self.audit_path = self.root / "support-access.jsonl"
        for path in [
            self.events_dir,
            self.results_dir,
            self.sessions_dir,
            self.idempotency_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def result_for_key(self, idempotency_key: str) -> GatewayResult | None:
        path = self.idempotency_dir / f"{idempotency_ref(idempotency_key)}.json"
        if not path.exists():
            return None
        data = self._read_json(path)
        return self.get_result(str(data["gateway_result_id"]))

    def record_event(self, event: GatewayEvent) -> None:
        self._write_json(
            self.events_dir / f"{_safe_id(event.gateway_event_id, 'gateway_event_id')}.json",
            event.to_safe_dict(),
        )

    def record_result(self, result: GatewayResult, *, idempotency_key: str) -> GatewayResult:
        self._write_json(
            self.results_dir
            / f"{_safe_id(result.gateway_result_id, 'gateway_result_id')}.json",
            result.to_safe_dict(),
        )
        self._write_json(
            self.idempotency_dir / f"{idempotency_ref(idempotency_key)}.json",
            {"gateway_result_id": result.gateway_result_id},
        )
        return result

    def get_result(self, gateway_result_id: str) -> GatewayResult | None:
        path = self.results_dir / f"{_safe_id(gateway_result_id, 'gateway_result_id')}.json"
        if not path.exists():
            return None
        data = self._read_json(path)
        target = data.get("target")
        return GatewayResult(
            gateway_result_id=str(data["gateway_result_id"]),
            gateway_event_id=str(data["gateway_event_id"]),
            project_id=str(data["project_id"]),
            gateway_id=str(data["gateway_id"]),
            outcome=str(data["outcome"]),
            correlation_id=str(data["correlation_id"]),
            created_at=str(data["created_at"]),
            queue_item_id=data.get("queue_item_id"),
            work_item_id=data.get("work_item_id"),
            owner_role=data.get("owner_role"),
            lifecycle_state=data.get("lifecycle_state"),
            target=(
                GatewayTarget(
                    target_type=str(target["target_type"]),
                    target_ref=str(target["target_ref"]),
                    owner_role=target.get("owner_role"),
                    lifecycle_state=target.get("lifecycle_state"),
                )
                if isinstance(target, dict)
                else None
            ),
            reason_class=data.get("reason_class"),
            receipt=dict(data.get("receipt") or {}),
            schema_version=str(data.get("schema_version") or GATEWAY_RESULT_SCHEMA_VERSION),
        )

    def record_session(self, session: GatewaySession) -> None:
        self._write_json(
            self.sessions_dir / f"{_safe_id(session.session_id, 'session_id')}.json",
            session.to_safe_dict(),
        )

    def record_support_access(
        self,
        *,
        actor_ref: str,
        reason_class: str,
        target_ref: str,
        correlation_id: str,
    ) -> None:
        payload = {
            "recorded_at": utc_now_iso(),
            "actor_ref": _safe_id(actor_ref, "actor_ref"),
            "reason_class": _safe_id(reason_class, "reason_class"),
            "target_ref": _safe_id(target_ref, "target_ref"),
            "correlation_id": _safe_id(correlation_id, "correlation_id"),
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        _ensure_under(path, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)

    def _read_json(self, path: Path) -> dict[str, Any]:
        _ensure_under(path, self.root)
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise GatewayError(f"gateway state {path.name} must be a mapping")
        return data


class GatewayService:
    def __init__(
        self,
        *,
        project_id: str,
        gateway_config: GatewayConfig,
        store: GatewayStore,
        journal: EventJournal,
        work_queue: FileWorkQueueStore | None = None,
        status_reader: SafeStatusReader | None = None,
        role_ids: set[str] | None = None,
    ) -> None:
        self.project_id = project_id
        self.gateway_config = gateway_config
        self.store = store
        self.journal = journal
        self.work_queue = work_queue
        self.status_reader = status_reader
        self.policy = GatewayPolicy(gateway_config=gateway_config, role_ids=role_ids)

    def handle_event(self, event: GatewayEvent) -> GatewayResult:
        if not self.gateway_config.enabled:
            return self._record_result(
                event,
                outcome=OUTCOME_NOT_CAPTURED,
                reason_class="gateway_disabled",
                receipt={"label": "Gateway disabled"},
            )
        duplicate = self.store.result_for_key(event.idempotency_key)
        if duplicate is not None:
            self.journal.append(
                "gateway.duplicate",
                project_id=self.project_id,
                gateway_id=self.gateway_config.gateway_id,
                gateway_result_id=duplicate.gateway_result_id,
                outcome=duplicate.outcome,
                correlation_id=duplicate.correlation_id,
            )
            return duplicate

        self.store.record_event(event)
        decision = self.policy.decide(event)
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            gateway_id=self.gateway_config.gateway_id,
            gateway_event_id=event.gateway_event_id,
            outcome=decision.outcome,
            correlation_id=new_id("corr"),
        )
        with telemetry.start_span("gateway.handle", attributes=attrs):
            if decision.outcome == OUTCOME_STATUS_ANSWER:
                return self._status_answer(event, decision)
            if decision.outcome in {
                OUTCOME_CREATE_OR_UPDATE_QUEUE_ITEM,
                OUTCOME_DIRECT_DELIVERY_ROUTED,
            }:
                return self._capture_queue_item(event, decision)
            if decision.outcome in {
                OUTCOME_APPROVAL_RESPONSE,
                OUTCOME_BLOCKER_REPORT,
                OUTCOME_ADD_CONTEXT,
                OUTCOME_ROLE_DIRECTED_HINT,
                OUTCOME_CLARIFICATION_NEEDED,
                OUTCOME_QUICK_RESPONSE,
            }:
                return self._record_result(
                    event,
                    outcome=decision.outcome,
                    reason_class=decision.reason_class,
                    owner_role=decision.owner_role,
                    target_ref=decision.target_ref,
                    receipt=receipt_for_outcome(
                        decision.outcome,
                        reason_class=decision.reason_class,
                        target_ref=decision.target_ref,
                        owner_role=decision.owner_role,
                    ),
                )
        return self._record_result(
            event,
            outcome=OUTCOME_FAILED,
            reason_class="unhandled_policy_outcome",
            receipt={"label": "Gateway failed safely"},
        )

    def _status_answer(
        self,
        event: GatewayEvent,
        decision: GatewayPolicyDecision,
    ) -> GatewayResult:
        target_ref = decision.target_ref or ""
        status_payload = (
            self.status_reader.answer_status(target_ref)
            if self.status_reader is not None
            else None
        )
        if status_payload is None:
            return self._record_result(
                event,
                outcome=OUTCOME_CLARIFICATION_NEEDED,
                reason_class="status_ref_not_found",
                target_ref=target_ref,
                receipt=receipt_for_outcome(
                    OUTCOME_CLARIFICATION_NEEDED,
                    reason_class="status_ref_not_found",
                    target_ref=target_ref,
                ),
            )
        return self._record_result(
            event,
            outcome=OUTCOME_STATUS_ANSWER,
            reason_class=decision.reason_class,
            target_ref=target_ref,
            receipt={"label": "Status found", "status": _safe_receipt(status_payload)},
        )

    def _capture_queue_item(
        self,
        event: GatewayEvent,
        decision: GatewayPolicyDecision,
    ) -> GatewayResult:
        if self.work_queue is None:
            return self._record_result(
                event,
                outcome=OUTCOME_CLARIFICATION_NEEDED,
                reason_class="work_queue_unavailable",
                receipt={"label": "Queue unavailable", "next_action": "Ask an operator"},
            )
        owner_role = decision.owner_role or self.gateway_config.default_owner_role
        queue_item = self.work_queue.capture(
            title=_title_from_text(event.text_summary),
            summary=event.text_summary,
            owner_role=owner_role,
            source_anchor=event.source_anchor,
            recommended_work_item_type=_recommended_work_item_type(event.text_summary),
            idempotency_key=event.idempotency_key,
            metadata={
                "gateway_id": event.gateway_id,
                "gateway_event_id": event.gateway_event_id,
                "gateway_outcome": decision.outcome,
                "reason_class": decision.reason_class,
                "role_hints": list(event.role_hints),
                "explicit_refs": list(event.explicit_refs),
            },
            retain_raw_payload=False,
        )
        return self._record_result(
            event,
            outcome=decision.outcome,
            reason_class=decision.reason_class,
            queue_item_id=queue_item.queue_item_id,
            owner_role=queue_item.owner_role,
            receipt=queue_receipt(queue_item, outcome=decision.outcome),
        )

    def _record_result(
        self,
        event: GatewayEvent,
        *,
        outcome: str,
        reason_class: str | None,
        receipt: dict[str, Any],
        queue_item_id: str | None = None,
        work_item_id: str | None = None,
        owner_role: str | None = None,
        lifecycle_state: str | None = None,
        target_ref: str | None = None,
    ) -> GatewayResult:
        correlation_id = new_id("corr")
        result = GatewayResult(
            gateway_result_id=new_id("gw-result"),
            gateway_event_id=event.gateway_event_id,
            project_id=self.project_id,
            gateway_id=self.gateway_config.gateway_id,
            outcome=outcome,
            correlation_id=correlation_id,
            queue_item_id=queue_item_id,
            work_item_id=work_item_id,
            owner_role=owner_role,
            lifecycle_state=lifecycle_state,
            target=(
                GatewayTarget(
                    target_type=_target_type(target_ref),
                    target_ref=target_ref,
                    owner_role=owner_role,
                    lifecycle_state=lifecycle_state,
                )
                if target_ref
                else None
            ),
            reason_class=reason_class,
            receipt=receipt,
        )
        self.store.record_result(result, idempotency_key=event.idempotency_key)
        self.journal.append(
            "gateway.result_recorded",
            project_id=self.project_id,
            gateway_id=self.gateway_config.gateway_id,
            gateway_event_id=event.gateway_event_id,
            gateway_result_id=result.gateway_result_id,
            outcome=outcome,
            queue_item_id=queue_item_id,
            work_item_id=work_item_id,
            owner_role=owner_role,
            lifecycle_state=lifecycle_state,
            reason_class=reason_class,
            source_anchor_ref=event.source_anchor.source_anchor_ref(),
            correlation_id=correlation_id,
        )
        return result


def event_from_message(
    *,
    project_id: str,
    gateway_id: str,
    connector_type: str,
    connector_id: str,
    source_kind: str,
    source_anchor: SourceAnchor,
    actor_label: str,
    actor_source_id: str | None,
    text: str,
    idempotency_key: str,
    role_hints: list[str] | None = None,
) -> GatewayEvent:
    text_summary = _redact_text(text, limit=1200)
    return GatewayEvent(
        gateway_event_id=new_id("gw-event"),
        project_id=project_id,
        gateway_id=gateway_id,
        connector_type=connector_type,
        connector_id=connector_id,
        source_anchor=source_anchor,
        actor=GatewayActor(
            actor_ref=actor_ref(actor_source_id or actor_label),
            display_label=actor_label or "gateway-user",
        ),
        text_summary=text_summary,
        source_kind=source_kind,
        idempotency_key=idempotency_key,
        role_hints=tuple(role_hints or []),
        explicit_refs=tuple(explicit_refs(text_summary)),
    )


def receipt_for_outcome(
    outcome: str,
    *,
    reason_class: str,
    target_ref: str | None = None,
    owner_role: str | None = None,
) -> dict[str, Any]:
    labels = {
        OUTCOME_QUICK_RESPONSE: "Acknowledged",
        OUTCOME_STATUS_ANSWER: "Status found",
        OUTCOME_CLARIFICATION_NEEDED: "Clarification needed",
        OUTCOME_ADD_CONTEXT: "Context captured",
        OUTCOME_APPROVAL_RESPONSE: "Approval response captured",
        OUTCOME_BLOCKER_REPORT: "Blocker captured",
        OUTCOME_ROLE_DIRECTED_HINT: "Role hint captured",
        OUTCOME_DIRECT_DELIVERY_ROUTED: "Request queued for governed work",
        OUTCOME_AUTHORIZATION_REQUIRED: "Authorization required",
        OUTCOME_NOT_CAPTURED: "Not captured",
    }
    payload = {
        "label": labels.get(outcome, outcome.replace("_", " ").title()),
        "outcome": outcome,
        "reason_class": reason_class,
        "target_ref": target_ref,
        "owner_role": owner_role,
    }
    return _safe_receipt(payload)


def queue_receipt(queue_item: QueueItem, *, outcome: str) -> dict[str, Any]:
    return _safe_receipt(
        {
            "label": "Queue item captured",
            "outcome": outcome,
            "queue_item_id": queue_item.queue_item_id,
            "title": queue_item.title,
            "status": queue_item.status,
            "owner_role": queue_item.owner_role,
            "recommended_work_item_type": queue_item.recommended_work_item_type,
            "next_action": "Lifecycle intake will continue through the configured flow.",
        }
    )


def assert_no_forbidden_fields(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).casefold() in FORBIDDEN_FIELD_NAMES:
                raise GatewayError(f"forbidden gateway field `{key}`")
            assert_no_forbidden_fields(value)
    elif isinstance(payload, list):
        for item in payload:
            assert_no_forbidden_fields(item)
    elif isinstance(payload, str):
        if TOKEN_RE.search(payload):
            raise GatewayError("forbidden token-like value in gateway output")
        if ABSOLUTE_PATH_RE.search(payload):
            raise GatewayError("forbidden absolute path in gateway output")


def actor_ref(value: str) -> str:
    return "actor-" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def idempotency_ref(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def explicit_refs(text: str) -> list[str]:
    seen: list[str] = []
    for match in [*WORK_RE.findall(text), *QUEUE_RE.findall(text)]:
        if match not in seen:
            seen.append(match)
    return seen


def _safe_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        safe_key = str(key)
        if safe_key.casefold() in FORBIDDEN_FIELD_NAMES:
            continue
        if isinstance(value, dict):
            safe[safe_key] = _safe_receipt(value)
        elif isinstance(value, list):
            safe[safe_key] = [
                _redact_text(str(item), limit=160)
                if not isinstance(item, dict)
                else _safe_receipt(item)
                for item in value
            ]
        else:
            safe[safe_key] = _redact_text(str(value), limit=300)
    assert_no_forbidden_fields(safe)
    return safe


def _safe_id(value: str, field_name: str) -> str:
    value = str(value or "")
    if not ID_RE.match(value) or "/" in value or "\\" in value or ".." in value:
        raise GatewayError(f"{field_name} must be a safe logical id")
    return value


def _safe_optional_id(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _safe_id(value, field_name)


def _redact_text(text: str, *, limit: int = 240) -> str:
    value = TOKEN_RE.sub("[redacted]", str(text or ""))
    value = ABSOLUTE_PATH_RE.sub("[redacted-path]", value)
    return value.replace("\r", " ").replace("\n", " ").strip()[:limit]


def _title_from_text(text: str) -> str:
    words = _redact_text(text, limit=80).strip()
    return words or "Gateway request"


def _recommended_work_item_type(text: str) -> str:
    value = text.casefold()
    if any(term in value for term in {"implement", "build", "change", "fix", "deploy"}):
        return "slice"
    if any(term in value for term in {"investigate", "research", "spike"}):
        return "spike"
    return "slice"


def _target_type(target_ref: str | None) -> str:
    if not target_ref:
        return "unknown"
    if target_ref.startswith("work-"):
        return "work_item"
    if target_ref.startswith("queue-"):
        return "queue_item"
    return "reference"


def _is_status_question(text: str) -> bool:
    return any(term in text for term in {"status", "where is", "what happened", "progress"})


def _is_approval_response(text: str) -> bool:
    return any(term in text for term in {"approved", "not approved", "approve", "reject"})


def _is_blocker_report(text: str) -> bool:
    return any(term in text for term in {"blocked", "blocker", "cannot proceed", "stuck"})


def _is_context_update(text: str) -> bool:
    return any(term in text for term in {"add context", "more context", "update", "note for"})


def _is_direct_delivery_request(text: str) -> bool:
    delivery_terms = {
        "implement",
        "write the code",
        "run qa",
        "deploy",
        "release",
        "bypass review",
        "just do it",
        "write the artifact",
    }
    return any(term in text for term in delivery_terms)


def _is_meaningful_work(text: str) -> bool:
    terms = {
        "investigate",
        "create a slice",
        "start a slice",
        "make a change",
        "architecture review",
        "security review",
        "project change",
        "new work",
        "fix",
        "build",
    }
    return any(term in text for term in terms)


def _looks_ambiguous_followup(text: str) -> bool:
    return len(text.split()) <= 8 and any(term in text for term in {"also", "one more", "that", "same"})


def _owner_from_role_hints(role_hints: tuple[str, ...], role_ids: set[str]) -> str | None:
    for hint in role_hints:
        if hint in role_ids:
            return hint
    return None


def _is_work_item_native_source(event: GatewayEvent) -> bool:
    connector = event.connector_type.casefold().replace(" ", "-")
    connector_id = event.connector_id.casefold().replace(" ", "-")
    source_kind = event.source_kind.casefold().replace(" ", "-")
    return (
        connector in WORK_ITEM_NATIVE_CONNECTORS
        or connector_id in WORK_ITEM_NATIVE_CONNECTORS
        or source_kind in WORK_ITEM_NATIVE_SOURCE_KINDS
    )


def _is_messaging_source(event: GatewayEvent) -> bool:
    connector = event.connector_type.casefold().replace(" ", "-")
    source_kind = event.source_kind.casefold().replace(" ", "-")
    return connector in MESSAGING_CONNECTORS or source_kind in MESSAGING_SOURCE_KINDS


def _ensure_under(path: Path, root: Path) -> None:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved_root not in [resolved, *resolved.parents]:
        raise GatewayError("gateway state path escaped gateway root")
