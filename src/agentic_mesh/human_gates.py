from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

from agentic_mesh.models import FlowGate
from agentic_mesh.models import MeshConfig
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso


SCHEMA_VERSION = "human-gate-summary-v0"
REQUEST_SCHEMA_VERSION = "human-gate-request-v0"

STATUS_LABELS = {
    "not_configured": "No human approval configured",
    "not_yet_reached": "Approval not reached yet",
    "skipped_by_policy": "Approval skipped by policy",
    "waiting_for_response": "Waiting for human response",
    "pending": "Waiting for approval",
    "completed": "Human response completed",
    "not_approved": "Not approved",
    "invalid_response": "Invalid human response",
    "timed_out": "Human response timed out",
    "blocked_before_gate": "Blocked before approval",
    "failed_before_gate": "Failed before approval",
    "request_failed": "Approval request failed",
    "stale_waiting": "Approval needs recovery",
    "superseded": "Approval request superseded",
    "inconsistent": "Approval state inconsistent",
    "unknown": "Approval state unknown",
}

ATTENTION_STATUSES = {
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

SAFE_REQUEST_FAILURE_REASONS = {
    "approval_request_outbox_unavailable",
    "approval_surface_unmapped",
    "approval_route_unmapped",
    "approval_request_persistence_failed",
    "approval_request_enqueue_failed",
    "approval_notification_failed",
    "approval_request_state_corrupt",
    "approval_request_recovery_required",
}

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,160}$")


@dataclass(frozen=True)
class HumanGateRequest:
    schema_version: str
    response_request_id: str
    project_id: str
    work_item_id: str
    work_item_type: str | None
    lifecycle_state: str
    gate_id: str
    gate_attempt: str
    response_type: str
    requested_from: str | None
    channel: str | None
    prompt: str | None
    accepted_values: list[Any] = field(default_factory=list)
    status: str = "request_pending_delivery"
    status_reason: str | None = None
    response_value: Any | None = None
    responder: str | None = None
    connector_message_id: str | None = None
    requested_at: str = field(default_factory=utc_now_iso)
    completed_at: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)
    active: bool = True
    approval_request_id: str | None = None
    current_notification_attempt_id: str | None = None
    notification_attempts: list[dict[str, Any]] = field(default_factory=list)
    timeout: str | None = None
    on_timeout: str | None = None
    decision_context_schema_version: str | None = None
    decision_context: dict[str, Any] | None = None
    decision_context_created_at: str | None = None
    decision_context_completeness: str | None = None
    decision_context_lookup_status: str | None = None
    decision_context_lookup_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HumanGateRequest":
        return cls(
            schema_version=str(data.get("schema_version") or REQUEST_SCHEMA_VERSION),
            response_request_id=str(data["response_request_id"]),
            project_id=str(data["project_id"]),
            work_item_id=str(data["work_item_id"]),
            work_item_type=(
                str(data["work_item_type"]) if data.get("work_item_type") else None
            ),
            lifecycle_state=str(data["lifecycle_state"]),
            gate_id=str(data["gate_id"]),
            gate_attempt=str(data.get("gate_attempt") or "attempt-1"),
            response_type=str(data["response_type"]),
            requested_from=(
                str(data["requested_from"]) if data.get("requested_from") else None
            ),
            channel=str(data["channel"]) if data.get("channel") else None,
            prompt=str(data["prompt"]) if data.get("prompt") else None,
            accepted_values=list(data.get("accepted_values") or []),
            status=str(data.get("status") or "unknown"),
            status_reason=(
                str(data["status_reason"]) if data.get("status_reason") else None
            ),
            response_value=data.get("response_value"),
            responder=str(data["responder"]) if data.get("responder") else None,
            connector_message_id=(
                str(data["connector_message_id"])
                if data.get("connector_message_id")
                else None
            ),
            requested_at=str(data.get("requested_at") or utc_now_iso()),
            completed_at=(
                str(data["completed_at"]) if data.get("completed_at") else None
            ),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            active=bool(data.get("active", True)),
            approval_request_id=(
                str(data["approval_request_id"])
                if data.get("approval_request_id")
                else str(data["response_request_id"])
            ),
            current_notification_attempt_id=(
                str(data["current_notification_attempt_id"])
                if data.get("current_notification_attempt_id")
                else None
            ),
            notification_attempts=list(data.get("notification_attempts") or []),
            timeout=str(data["timeout"]) if data.get("timeout") else None,
            on_timeout=str(data["on_timeout"]) if data.get("on_timeout") else None,
            decision_context_schema_version=(
                str(data["decision_context_schema_version"])
                if data.get("decision_context_schema_version")
                else None
            ),
            decision_context=(
                dict(data["decision_context"])
                if isinstance(data.get("decision_context"), dict)
                else None
            ),
            decision_context_created_at=(
                str(data["decision_context_created_at"])
                if data.get("decision_context_created_at")
                else None
            ),
            decision_context_completeness=(
                str(data["decision_context_completeness"])
                if data.get("decision_context_completeness")
                else None
            ),
            decision_context_lookup_status=(
                str(data["decision_context_lookup_status"])
                if data.get("decision_context_lookup_status")
                else None
            ),
            decision_context_lookup_reason=(
                str(data["decision_context_lookup_reason"])
                if data.get("decision_context_lookup_reason")
                else None
            ),
        )


class HumanGateStoreError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FileHumanGateRequestStore:
    def __init__(self, state_root: Path, project_id: str) -> None:
        _validate_id(project_id, "project_id")
        self.project_id = project_id
        self.root = state_root / "projects" / project_id / "human_gates"
        self.requests_root = self.root / "requests"
        self.index_root = self.root / "index"
        self.requests_root.mkdir(parents=True, exist_ok=True)
        self.index_root.mkdir(parents=True, exist_ok=True)

    def ensure_request(
        self,
        *,
        work_item_id: str,
        work_item_type: str | None,
        lifecycle_state: str,
        gate: FlowGate,
        gate_attempt: str = "attempt-1",
        response_request_id: str | None = None,
    ) -> tuple[HumanGateRequest, bool]:
        _validate_id(work_item_id, "work_item_id")
        _validate_id(lifecycle_state, "lifecycle_state")
        _validate_id(gate.gate_id, "gate_id")
        _validate_id(gate_attempt, "gate_attempt")
        response_type = str(gate.response_type or "")
        _validate_id(response_type, "response_type")
        key = _idempotency_key(
            self.project_id,
            work_item_id,
            lifecycle_state,
            gate.gate_id,
            gate_attempt,
        )
        existing_id = self._read_index("idempotency", key)
        if existing_id:
            existing = self.read(existing_id)
            if existing and existing.active and existing.status in {
                "request_pending_delivery",
                "waiting_for_response",
                "invalid_response",
            }:
                return existing, False

        request_id = response_request_id or new_id("human-response")
        _validate_id(request_id, "response_request_id")
        attempt_id = _notification_attempt_id(request_id, 1)
        request = HumanGateRequest(
            schema_version=REQUEST_SCHEMA_VERSION,
            response_request_id=request_id,
            approval_request_id=request_id,
            project_id=self.project_id,
            work_item_id=work_item_id,
            work_item_type=work_item_type,
            lifecycle_state=lifecycle_state,
            gate_id=gate.gate_id,
            gate_attempt=gate_attempt,
            response_type=response_type,
            requested_from=gate.requested_from,
            channel=gate.channel,
            prompt=gate.prompt,
            accepted_values=list(
                (gate.completion_criteria or {}).get("accepted_values") or []
            ),
            timeout=gate.timeout,
            on_timeout=gate.on_timeout,
            current_notification_attempt_id=attempt_id,
            notification_attempts=[
                {
                    "notification_attempt_id": attempt_id,
                    "approval_request_id": request_id,
                    "response_request_id": request_id,
                    "attempt_number": 1,
                    "status": "created",
                    "logical_route": gate.channel,
                    "route_label": gate.channel,
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                }
            ],
        )
        self._write_request(request)
        self._write_index("idempotency", key, request.response_request_id)
        self._write_index(
            "work-items",
            _work_item_index_key(work_item_id, gate.gate_id),
            request.response_request_id,
        )
        return request, True

    def read(self, response_request_id: str) -> HumanGateRequest | None:
        _validate_id(response_request_id, "response_request_id")
        path = self._request_path(response_request_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return HumanGateRequest.from_dict(json.load(handle))

    def read_by_work_item(self, work_item_id: str) -> list[HumanGateRequest]:
        _validate_id(work_item_id, "work_item_id")
        rows: list[HumanGateRequest] = []
        for path in sorted(self.requests_root.glob("*.json")):
            try:
                request = HumanGateRequest.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except Exception:
                continue
            if request.work_item_id == work_item_id:
                rows.append(request)
        return sorted(rows, key=lambda item: item.updated_at)

    def mark_enqueue_succeeded(
        self,
        response_request_id: str,
        *,
        connector_message_id: str,
    ) -> HumanGateRequest:
        request = self._require(response_request_id)
        attempt_id = request.current_notification_attempt_id or _notification_attempt_id(
            request.response_request_id,
            len(request.notification_attempts) + 1,
        )
        updated_attempts = _upsert_attempt(
            request.notification_attempts,
            {
                "notification_attempt_id": attempt_id,
                "approval_request_id": request.approval_request_id
                or request.response_request_id,
                "response_request_id": request.response_request_id,
                "attempt_number": _attempt_number(
                    request.notification_attempts,
                    attempt_id,
                ),
                "status": "queued",
                "connector_message_id": connector_message_id,
                "logical_route": request.channel,
                "route_label": request.channel,
                "queued_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        )
        updated = _replace_request(
            request,
            status="waiting_for_response",
            status_reason=None,
            connector_message_id=connector_message_id,
            current_notification_attempt_id=attempt_id,
            notification_attempts=updated_attempts,
            updated_at=utc_now_iso(),
            active=True,
        )
        self._write_request(updated)
        return updated

    def record_decision_context(
        self,
        response_request_id: str,
        *,
        decision_context: dict[str, Any],
        schema_version: str = "approval-decision-context-snapshot-v1",
    ) -> HumanGateRequest:
        request = self._require(response_request_id)
        completeness = str(decision_context.get("context_completeness") or "complete")
        updated = _replace_request(
            request,
            decision_context_schema_version=schema_version,
            decision_context=decision_context,
            decision_context_created_at=utc_now_iso(),
            decision_context_completeness=completeness,
            updated_at=utc_now_iso(),
        )
        self._write_request(updated)
        return updated

    def read_decision_context(self, response_request_id: str) -> dict[str, Any] | None:
        request = self.read(response_request_id)
        if request is None:
            return None
        return dict(request.decision_context) if request.decision_context else None

    def record_context_lookup(
        self,
        response_request_id: str,
        *,
        result: str,
        reason: str | None = None,
    ) -> HumanGateRequest | None:
        request = self.read(response_request_id)
        if request is None:
            return None
        updated = _replace_request(
            request,
            decision_context_lookup_status=result[:80],
            decision_context_lookup_reason=reason[:160] if reason else None,
            updated_at=utc_now_iso(),
        )
        self._write_request(updated)
        return updated

    def mark_failed(
        self,
        response_request_id: str,
        *,
        reason: str,
    ) -> HumanGateRequest:
        if reason not in SAFE_REQUEST_FAILURE_REASONS:
            reason = "approval_request_recovery_required"
        request = self._require(response_request_id)
        attempt_id = request.current_notification_attempt_id or _notification_attempt_id(
            request.response_request_id,
            len(request.notification_attempts) + 1,
        )
        updated_attempts = _upsert_attempt(
            request.notification_attempts,
            {
                "notification_attempt_id": attempt_id,
                "approval_request_id": request.approval_request_id
                or request.response_request_id,
                "response_request_id": request.response_request_id,
                "attempt_number": _attempt_number(
                    request.notification_attempts,
                    attempt_id,
                ),
                "status": "failed",
                "logical_route": request.channel,
                "route_label": request.channel,
                "failure_class": reason,
                "safe_reason": reason,
                "failed_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        )
        updated = _replace_request(
            request,
            status="request_failed",
            status_reason=reason,
            current_notification_attempt_id=attempt_id,
            notification_attempts=updated_attempts,
            updated_at=utc_now_iso(),
            active=False,
        )
        self._write_request(updated)
        return updated

    def mark_stale_waiting(
        self,
        response_request_id: str,
        *,
        reason: str = "historical_waiting_without_active_request",
    ) -> HumanGateRequest:
        request = self._require(response_request_id)
        updated = _replace_request(
            request,
            status="stale_waiting",
            status_reason=reason,
            updated_at=utc_now_iso(),
            active=False,
        )
        self._write_request(updated)
        return updated

    def mark_superseded(
        self,
        response_request_id: str,
        *,
        reason: str,
        actor: str | None = None,
    ) -> HumanGateRequest:
        request = self._require(response_request_id)
        updated = _replace_request(
            request,
            status="superseded",
            status_reason=reason[:160],
            responder=actor,
            updated_at=utc_now_iso(),
            active=False,
        )
        self._write_request(updated)
        return updated

    def prepare_resend_attempt(
        self,
        response_request_id: str,
        *,
        actor: str,
        reason: str,
    ) -> HumanGateRequest:
        request = self._require(response_request_id)
        if request.status in {"completed", "not_approved", "timed_out", "superseded"}:
            raise HumanGateStoreError("terminal_approval_request")
        attempt_number = len(request.notification_attempts) + 1
        attempt_id = _notification_attempt_id(request.response_request_id, attempt_number)
        attempts = list(request.notification_attempts)
        attempts.append(
            {
                "notification_attempt_id": attempt_id,
                "approval_request_id": request.approval_request_id
                or request.response_request_id,
                "response_request_id": request.response_request_id,
                "attempt_number": attempt_number,
                "status": "created",
                "logical_route": request.channel,
                "route_label": request.channel,
                "safe_reason": reason[:160],
                "actor_label": actor[:120],
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
        )
        updated = _replace_request(
            request,
            status="waiting_for_response",
            status_reason=None,
            current_notification_attempt_id=attempt_id,
            notification_attempts=attempts,
            updated_at=utc_now_iso(),
            active=True,
        )
        self._write_request(updated)
        return updated

    def validate_response(
        self,
        *,
        project_id: str,
        work_item_id: str,
        work_item_type: str | None,
        lifecycle_state: str,
        gate_id: str,
        response_type: str,
        response_request_id: str,
        responder: str | None,
        response_value: Any,
        authenticated: bool = False,
        authoritative: bool = False,
    ) -> tuple[bool, str, HumanGateRequest | None]:
        try:
            request = self.read(response_request_id)
        except ValueError:
            return False, "malformed_response_request_id", None
        if request is None:
            return False, "stale_or_unknown_request", None
        reason = _response_mismatch_reason(
            request=request,
            project_id=project_id,
            work_item_id=work_item_id,
            work_item_type=work_item_type,
            lifecycle_state=lifecycle_state,
            gate_id=gate_id,
            response_type=response_type,
        )
        if reason is None and request.status in {"completed", "not_approved"}:
            if request.response_value == _safe_response_value(response_value):
                return request.status == "completed", "duplicate_same_value", request
            return False, "terminal_request_conflict", request
        if reason is None and request.status in {"request_failed", "timed_out"}:
            return False, "terminal_request", request
        if reason is None and authoritative and not authenticated:
            reason = "connector_origin_unauthenticated"
        if reason is None and response_value not in _allowed_values(request):
            reason = "unrecognized_response_value"
        if reason is not None:
            updated = _replace_request(
                request,
                status="invalid_response",
                status_reason=reason,
                response_value=_safe_response_value(response_value),
                responder=responder,
                updated_at=utc_now_iso(),
                active=True,
            )
            self._write_request(updated)
            return False, reason, updated

        accepted = set(request.accepted_values or [])
        if response_value in accepted:
            updated = _replace_request(
                request,
                status="completed",
                status_reason=None,
                response_value=_safe_response_value(response_value),
                responder=responder,
                completed_at=utc_now_iso(),
                updated_at=utc_now_iso(),
                active=False,
            )
            self._write_request(updated)
            return True, "accepted", updated

        updated = _replace_request(
            request,
            status="not_approved",
            status_reason="response_did_not_satisfy_gate",
            response_value=_safe_response_value(response_value),
            responder=responder,
            completed_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            active=False,
        )
        self._write_request(updated)
        return False, "not_approved", updated

    def rebuild_indexes(self) -> None:
        for path in sorted((self.index_root / "idempotency").glob("*.json")):
            path.unlink(missing_ok=True)
        for path in sorted((self.index_root / "work-items").glob("*.json")):
            path.unlink(missing_ok=True)
        for request in self.read_all():
            key = _idempotency_key(
                request.project_id,
                request.work_item_id,
                request.lifecycle_state,
                request.gate_id,
                request.gate_attempt,
            )
            self._write_index("idempotency", key, request.response_request_id)
            self._write_index(
                "work-items",
                _work_item_index_key(request.work_item_id, request.gate_id),
                request.response_request_id,
            )

    def read_all(self) -> list[HumanGateRequest]:
        rows: list[HumanGateRequest] = []
        for path in sorted(self.requests_root.glob("*.json")):
            try:
                rows.append(
                    HumanGateRequest.from_dict(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except Exception:
                continue
        return rows

    def _require(self, response_request_id: str) -> HumanGateRequest:
        request = self.read(response_request_id)
        if request is None:
            raise HumanGateStoreError("approval_request_recovery_required")
        return request

    def _request_path(self, response_request_id: str) -> Path:
        _validate_id(response_request_id, "response_request_id")
        return _contained_path(self.requests_root, f"{response_request_id}.json")

    def _write_request(self, request: HumanGateRequest) -> None:
        self._atomic_write_json(
            self._request_path(request.response_request_id),
            request.to_dict(),
        )

    def _read_index(self, category: str, key: str) -> str | None:
        path = self._index_path(category, key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        value = data.get("response_request_id")
        return str(value) if value else None

    def _write_index(self, category: str, key: str, response_request_id: str) -> None:
        self._atomic_write_json(
            self._index_path(category, key),
            {"response_request_id": response_request_id},
        )

    def _index_path(self, category: str, key: str) -> Path:
        _validate_id(category, "index_category")
        _validate_id(key, "index_key")
        return _contained_path(self.index_root / category, f"{key}.json")

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        tmp_path.replace(path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


def derive_human_gate_summary(
    *,
    mesh_config: MeshConfig,
    state_root: Path,
    work_item_id: str,
    current: dict[str, Any],
    problem_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = FileHumanGateRequestStore(state_root, mesh_config.project.project_id)
    requests = store.read_by_work_item(work_item_id)
    latest_request = requests[-1] if requests else None
    gates = _configured_human_gates(mesh_config, current=current)
    current_lifecycle = str(current.get("lifecycle_state") or "")
    next_gate = _next_gate(gates, current_lifecycle)
    current_gate = _gate_for_state(gates, current_lifecycle)

    if latest_request is not None:
        return _summary_from_request(latest_request, gates)

    problem_state = str((problem_status or {}).get("status") or current.get("status") or "")
    if problem_state in {"failed", "needs_runtime_recovery"} and next_gate is not None:
        return _summary(
            status="failed_before_gate",
            current_gate=None,
            next_gate=next_gate,
            attention_reason=_safe_reason(problem_status, current),
        )
    if problem_state == "blocked" and next_gate is not None:
        return _summary(
            status="blocked_before_gate",
            current_gate=None,
            next_gate=next_gate,
            attention_reason=_safe_reason(problem_status, current),
        )
    if current_gate is not None and str(current.get("status")) == "waiting_for_human_response":
        return _summary(
            status="request_failed",
            current_gate=current_gate,
            next_gate=None,
            attention_reason="approval_request_recovery_required",
        )
    if next_gate is not None:
        return _summary(status="not_yet_reached", current_gate=None, next_gate=next_gate)
    if current_gate is not None:
        return _summary(status="not_yet_reached", current_gate=current_gate, next_gate=None)
    return _summary(status="not_configured", current_gate=None, next_gate=None)


def _summary_from_request(
    request: HumanGateRequest,
    gates: list[tuple[str, FlowGate]],
) -> dict[str, Any]:
    current_gate = _safe_gate_dict(request=request)
    future = [
        gate
        for state_id, gate in gates
        if _gate_sort_key(gates, state_id) > _gate_sort_key(gates, request.lifecycle_state)
    ]
    status = request.status
    if status == "request_pending_delivery":
        status = "waiting_for_response"
    timeout_at = _derived_timeout_at(request)
    if status == "waiting_for_response" and timeout_at is not None and _is_past(timeout_at):
        status = "timed_out"
    return _summary(
        status=status if status in STATUS_LABELS else "unknown",
        current_gate=current_gate,
        next_gate=future[0] if future else None,
        attention_reason=request.status_reason,
        requested_at=request.requested_at,
        completed_at=request.completed_at,
        timeout=request.timeout,
        timeout_at=timeout_at if status == "timed_out" else None,
        on_timeout=request.on_timeout,
        response_request_id=request.response_request_id,
        approval_request_id=request.approval_request_id or request.response_request_id,
        notification_attempt_id=request.current_notification_attempt_id,
    )


def _summary(
    *,
    status: str,
    current_gate: dict[str, Any] | FlowGate | None,
    next_gate: FlowGate | None,
    attention_reason: str | None = None,
    requested_at: str | None = None,
    completed_at: str | None = None,
    timeout: str | None = None,
    timeout_at: str | None = None,
    on_timeout: str | None = None,
    response_request_id: str | None = None,
    approval_request_id: str | None = None,
    notification_attempt_id: str | None = None,
) -> dict[str, Any]:
    current_gate_dict = (
        current_gate
        if isinstance(current_gate, dict)
        else _safe_gate_dict(gate=current_gate)
        if current_gate is not None
        else None
    )
    next_gate_dict = _safe_gate_dict(gate=next_gate) if next_gate is not None else None
    configured = [item for item in [current_gate_dict, next_gate_dict] if item]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "display_label": STATUS_LABELS.get(status, STATUS_LABELS["unknown"]),
        "approval_strategy": "configured_human_response_gate"
        if (current_gate_dict or next_gate_dict)
        else "not_configured",
        "current_human_gate": current_gate_dict,
        "next_human_gate": next_gate_dict,
        "configured_human_gates": configured,
        "current_human_gate_id": (current_gate_dict or {}).get("gate_id"),
        "next_human_gate_id": (next_gate_dict or {}).get("gate_id"),
        "approval_request_status": status,
        "approval_request_status_label": STATUS_LABELS.get(
            status,
            STATUS_LABELS["unknown"],
        ),
        "approval_request_id": approval_request_id,
        "response_request_id": response_request_id,
        "notification_attempt_id": notification_attempt_id,
        "requested_at": requested_at,
        "completed_at": completed_at,
        "timeout": timeout,
        "timeout_at": timeout_at,
        "on_timeout": on_timeout,
        "attention_reason": attention_reason
        or ("Human approval needs attention." if status in ATTENTION_STATUSES else None),
        "approval_required_before_implementation": False,
    }


def _safe_gate_dict(
    gate: FlowGate | None = None,
    request: HumanGateRequest | None = None,
) -> dict[str, Any]:
    if request is not None:
        status = request.status
        return {
            "gate_id": request.gate_id,
            "lifecycle_state": request.lifecycle_state,
            "status": status,
            "display_label": STATUS_LABELS.get(status, status),
            "response_type": request.response_type,
            "prompt": request.prompt,
            "requested_from": request.requested_from,
            "channel": request.channel,
            "accepted_values": request.accepted_values,
            "timeout": request.timeout,
            "on_timeout": request.on_timeout,
            "approval_request_id": request.approval_request_id
            or request.response_request_id,
            "response_request_id": request.response_request_id,
            "notification_attempt_id": request.current_notification_attempt_id,
            "requested_at": request.requested_at,
            "completed_at": request.completed_at,
            "status_reason": request.status_reason,
        }
    if gate is None:
        return {}
    return {
        "gate_id": gate.gate_id,
        "response_type": gate.response_type,
        "prompt": gate.prompt,
        "requested_from": gate.requested_from,
        "channel": gate.channel,
        "accepted_values": list(
            (gate.completion_criteria or {}).get("accepted_values") or []
        ),
        "timeout": gate.timeout,
        "on_timeout": gate.on_timeout,
    }


def _configured_human_gates(
    mesh_config: MeshConfig,
    current: dict[str, Any] | None = None,
) -> list[tuple[str, FlowGate]]:
    rows: list[tuple[str, FlowGate]] = []
    policy = mesh_config.project.human_gate_policy
    work_item_type = str((current or {}).get("work_item_type") or "")
    if (
        policy.sponsor_approval_before_build == "required_before_implementation"
        and (not policy.work_item_types or work_item_type in policy.work_item_types)
    ):
        rows.append(
            (
                "quality_planning",
                FlowGate(
                    gate_id=policy.gate_id,
                    type="human_response",
                    response_type=policy.response_type,
                    prompt="Approve progression into implementation for this work item.",
                    requested_from=policy.requested_from,
                    channel=policy.channel,
                    completion_criteria={"accepted_values": ["approved"]},
                ),
            )
        )
    for state_id, state in mesh_config.project.flow.states.items():
        for gate in state.gates:
            if gate.type == "human_response":
                rows.append((state_id, gate))
    return rows


def _gate_for_state(
    gates: list[tuple[str, FlowGate]],
    lifecycle_state: str,
) -> FlowGate | None:
    for state_id, gate in gates:
        if state_id == lifecycle_state:
            return gate
    return None


def _next_gate(
    gates: list[tuple[str, FlowGate]],
    lifecycle_state: str,
) -> FlowGate | None:
    if not gates:
        return None
    state_ids = [state_id for state_id, _ in gates]
    if lifecycle_state in state_ids:
        index = state_ids.index(lifecycle_state)
        return gates[index + 1][1] if index + 1 < len(gates) else None
    flow_states = list(gates[0][0] for _ in [0])
    all_states = list({state_id: None for state_id, _ in gates}.keys())
    if lifecycle_state in all_states:
        return _gate_for_state(gates, lifecycle_state)
    return gates[0][1]


def _gate_sort_key(gates: list[tuple[str, FlowGate]], lifecycle_state: str) -> int:
    for index, (state_id, _) in enumerate(gates):
        if state_id == lifecycle_state:
            return index
    return -1


def _response_mismatch_reason(
    *,
    request: HumanGateRequest,
    project_id: str,
    work_item_id: str,
    work_item_type: str | None,
    lifecycle_state: str,
    gate_id: str,
    response_type: str,
) -> str | None:
    checks = {
        "project_mismatch": request.project_id == project_id,
        "work_item_mismatch": request.work_item_id == work_item_id,
        "work_item_type_mismatch": (
            request.work_item_type is None
            or work_item_type is None
            or request.work_item_type == work_item_type
        ),
        "lifecycle_state_mismatch": request.lifecycle_state == lifecycle_state,
        "gate_id_mismatch": request.gate_id == gate_id,
        "response_type_mismatch": request.response_type == response_type,
    }
    for reason, matched in checks.items():
        if not matched:
            return reason
    return None


def _allowed_values(request: HumanGateRequest) -> set[Any]:
    values = set(request.accepted_values)
    if request.response_type == "approve_not_approve":
        values.update({"approved", "not_approved"})
    return values


def _safe_response_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:120]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return "[redacted]"


def _derived_timeout_at(request: HumanGateRequest) -> str | None:
    duration = _parse_iso_duration(request.timeout)
    if duration is None:
        return None
    try:
        requested_at = datetime.fromisoformat(request.requested_at)
    except ValueError:
        return None
    return (requested_at + duration).isoformat()


def _is_past(timestamp: str) -> bool:
    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    now = datetime.fromisoformat(utc_now_iso())
    if value.tzinfo is None:
        value = value.replace(tzinfo=now.tzinfo)
    return value <= now


def _parse_iso_duration(value: str | None) -> timedelta | None:
    if not value:
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        value,
    )
    if not match:
        return None
    parts = {key: int(raw or 0) for key, raw in match.groupdict().items()}
    duration = timedelta(**parts)
    return duration if duration.total_seconds() > 0 else None


def _notification_attempt_id(response_request_id: str, attempt_number: int) -> str:
    digest = hashlib.sha256(
        f"{response_request_id}\0{attempt_number}".encode("utf-8")
    ).hexdigest()[:16]
    return f"approval-attempt-{digest}"


def _attempt_number(
    attempts: list[dict[str, Any]],
    notification_attempt_id: str,
) -> int:
    for attempt in attempts:
        if attempt.get("notification_attempt_id") == notification_attempt_id:
            value = attempt.get("attempt_number")
            if isinstance(value, int):
                return value
            try:
                return int(str(value))
            except ValueError:
                return 1
    return len(attempts) + 1


def _upsert_attempt(
    attempts: list[dict[str, Any]],
    update: dict[str, Any],
) -> list[dict[str, Any]]:
    attempt_id = str(update.get("notification_attempt_id") or "")
    rows: list[dict[str, Any]] = []
    replaced = False
    for attempt in attempts:
        if attempt.get("notification_attempt_id") == attempt_id:
            merged = dict(attempt)
            merged.update({key: value for key, value in update.items() if value is not None})
            rows.append(merged)
            replaced = True
        else:
            rows.append(dict(attempt))
    if not replaced:
        rows.append({key: value for key, value in update.items() if value is not None})
    return rows


def _safe_reason(
    problem_status: dict[str, Any] | None,
    current: dict[str, Any],
) -> str | None:
    value = (problem_status or {}).get("reason_summary") or current.get("reason_summary")
    return str(value)[:160] if value else None


def _replace_request(request: HumanGateRequest, **changes: Any) -> HumanGateRequest:
    data = request.to_dict()
    data.update(changes)
    return HumanGateRequest.from_dict(data)


def _validate_id(value: str, field_name: str) -> None:
    if not _SAFE_ID_RE.match(str(value or "")):
        raise ValueError(f"{field_name} contains unsafe characters")


def _contained_path(root: Path, name: str) -> Path:
    root = root.resolve()
    path = (root / name).resolve()
    if root != path.parent and root not in path.parents:
        raise ValueError("human gate state path escapes root")
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root.parent):
        raise ValueError("human gate state path crosses a symlink")
    return path


def _idempotency_key(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"idem-{digest}"


def _work_item_index_key(work_item_id: str, gate_id: str) -> str:
    digest = hashlib.sha256(f"{work_item_id}\0{gate_id}".encode("utf-8")).hexdigest()
    return f"work-gate-{digest}"
