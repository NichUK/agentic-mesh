from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.release import ReleaseEvidence
from agentic_mesh_v2.release import ReleaseEvidenceLink
from agentic_mesh_v2.release import ReleaseService


TERMINAL_TOOLS: frozenset[str] = frozenset(
    {
        "status.reply",
        "status.complete",
        "sponsor.ask_question",
        "human_response.request",
        "handoff.request",
        "consult.request",
        "queue.propose_item",
        "release.request_approval",
        "release.close",
        "noop",
        "report.blocked",
        "report.incomplete",
    }
)

COMMON_TOOLS: frozenset[str] = frozenset(
    {
        "status.reply",
        "status.progress",
        "status.complete",
        "sponsor.ask_question",
        "human_response.request",
        "handoff.request",
        "consult.request",
        "queue.propose_item",
        "document.propose_update",
        "document.add_review_comment",
        "memory.propose_update",
        "risk.register",
        "decision.record",
        "relevance.record",
        "noop",
        "report.blocked",
        "report.incomplete",
    }
)

ROLE_TOOLS: dict[str, frozenset[str]] = {
    "product-manager": COMMON_TOOLS
    | {
        "product.mark_sponsor_ready",
        "work_item.mark_ready",
    },
    "engineering": COMMON_TOOLS
    | {
        "test_evidence.record",
        "implementation.record_change",
    },
    "qa-engineer": COMMON_TOOLS
    | {
        "test_evidence.record",
        "quality.approve",
        "quality.request_changes",
    },
    "release-manager": COMMON_TOOLS
    | {
        "release.request_approval",
        "release.record_decision",
        "release.deploy",
        "release.record_no_deployment",
        "release.rollback_plan",
        "release.close",
        "work_item.close",
        "work_item.supersede",
        "work_item.reopen",
        "work_item.override_blocker",
    },
}

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "status.reply": ("message",),
    "status.progress": ("message",),
    "status.complete": ("message",),
    "sponsor.ask_question": ("question", "reason"),
    "human_response.request": (
        "title",
        "question",
        "response_contract_id",
        "required_authority",
        "destination_ref",
    ),
    "handoff.request": ("target_role", "reason"),
    "consult.request": ("target_role", "reason"),
    "queue.propose_item": (
        "title",
        "summary",
        "source_ref",
        "rationale",
        "urgency",
        "suggested_owner",
        "work_type",
        "classification",
        "initiated_by",
    ),
    "document.propose_update": ("path", "document_type", "content"),
    "document.add_review_comment": ("path", "comment"),
    "memory.propose_update": ("summary", "provenance_ref"),
    "risk.register": ("risk", "impact"),
    "decision.record": ("decision", "rationale"),
    "relevance.record": ("conversation_event_id", "score", "threshold", "decision", "reason"),
    "noop": ("reason",),
    "report.blocked": ("reason", "owner", "next_action"),
    "report.incomplete": ("reason",),
    "product.mark_sponsor_ready": ("work_item_id", "summary"),
    "work_item.mark_ready": ("work_item_id", "reason"),
    "test_evidence.record": ("work_item_id", "summary"),
    "implementation.record_change": ("work_item_id", "summary"),
    "quality.approve": ("work_item_id", "summary"),
    "quality.request_changes": ("work_item_id", "reason"),
    "release.request_approval": ("work_item_id", "question"),
    "release.record_decision": ("work_item_id", "decision"),
    "release.deploy": (
        "work_item_id",
        "target_id",
        "reason",
        "scope",
        "rollback_plan",
        "residual_risks",
        "approval_ref",
        "commit_ref",
    ),
    "release.record_no_deployment": (
        "work_item_id",
        "reason",
        "scope",
        "rollback_plan",
        "residual_risks",
        "approval_ref",
        "commit_ref",
    ),
    "release.rollback_plan": ("work_item_id", "plan"),
    "release.close": ("work_item_id", "reason"),
    "work_item.close": ("work_item_id", "reason"),
    "work_item.supersede": ("work_item_id", "reason", "replacement_ref"),
    "work_item.reopen": ("work_item_id", "target_role", "reason"),
    "work_item.override_blocker": ("work_item_id", "reason"),
}

NUMERIC_FIELDS: dict[str, tuple[str, ...]] = {
    "relevance.record": ("score", "threshold"),
}


class SafeOutputError(ValueError):
    pass


@dataclass(frozen=True)
class ToolPolicy:
    role_tools: dict[str, frozenset[str]] = field(default_factory=lambda: ROLE_TOOLS)

    def tools_for_role(self, role_id: str) -> frozenset[str]:
        return self.role_tools.get(role_id, COMMON_TOOLS)

    def authorize(self, *, role_id: str, tool_name: str) -> None:
        if tool_name not in self.tools_for_role(role_id):
            raise SafeOutputError(f"`{role_id}` is not authorized to use `{tool_name}`")


@dataclass(frozen=True)
class SafeOutputCall:
    tool_name: str
    payload: dict[str, Any]
    role_id: str
    terminal: bool = False


class SafeOutputService:
    def __init__(
        self,
        db: V2Database,
        policy: ToolPolicy | None = None,
        release_service: ReleaseService | None = None,
        process_effects: bool = True,
    ) -> None:
        self.db = db
        self.policy = policy or ToolPolicy()
        self.release_service = release_service or ReleaseService(db)
        self.process_effects = process_effects

    def record(self, *, run_id: str, call: SafeOutputCall) -> str:
        self.policy.authorize(role_id=call.role_id, tool_name=call.tool_name)
        validate_payload(call.tool_name, call.payload)
        terminal = call.terminal or call.tool_name in TERMINAL_TOOLS
        call_id = f"call-{uuid4().hex}"
        self.db.record_safe_output(
            call_id=call_id,
            run_id=run_id,
            role_id=call.role_id,
            tool_name=call.tool_name,
            payload=call.payload,
            terminal=terminal,
        )
        if call.tool_name in {"handoff.request", "consult.request"}:
            self._record_role_assignment_from_route(
                call_id=call_id,
                run_id=run_id,
                call=call,
            )
        if self.process_effects:
            self.process_recorded_call(call_id=call_id, run_id=run_id, call=call)
        return call_id

    def process_recorded_call(self, *, call_id: str, run_id: str, call: SafeOutputCall) -> None:
        if call.tool_name == "release.record_no_deployment":
            self._record_no_deployment(call)
        if call.tool_name == "release.deploy":
            self._deploy_release(call)
        if call.tool_name == "release.close":
            self._close_released_work(call)
        return None

    def _record_no_deployment(self, call: SafeOutputCall) -> None:
        self.release_service.record_no_deployment(
            _release_evidence_from_payload(call.payload),
            reason=_required_text(call.payload, "reason"),
        )

    def _deploy_release(self, call: SafeOutputCall) -> None:
        evidence = _release_evidence_from_payload(call.payload)
        if _has_successful_deployment(self.db, release_id=evidence.release_id, work_item_id=evidence.work_item_id):
            return
        self.release_service.deploy_compose_release(
            evidence,
            target_id=_required_text(call.payload, "target_id"),
            smoke_checks=_smoke_checks(call.payload.get("smoke_checks")),
            evidence_links=_release_evidence_links(call.payload.get("evidence_links")),
            timeout_seconds=_optional_int(call.payload.get("timeout_seconds"), default=300),
        )

    def _close_released_work(self, call: SafeOutputCall) -> None:
        work_item_id = _required_text(call.payload, "work_item_id")
        work_item = self.db.get_work_item(work_item_id)
        if work_item.state == "closed":
            return
        self.release_service.close_released_work(
            work_item_id=work_item_id,
            from_state=_optional_text(call.payload.get("from_state")) or work_item.state,
            actor_role=call.role_id,
            reason=_required_text(call.payload, "reason"),
        )

    def _record_role_assignment_from_route(
        self,
        *,
        call_id: str,
        run_id: str,
        call: SafeOutputCall,
    ) -> None:
        source_run = self.db.get_agent_run(run_id)
        target_role = _required_text(call.payload, "target_role")
        reason = _required_text(call.payload, "reason")
        work_item_id = _optional_text(call.payload.get("work_item_id"))
        if work_item_id is None and source_run is not None:
            work_item_id = _optional_text(source_run.get("work_item_id"))
        visibility_scope = _optional_text(call.payload.get("context_visibility")) or "project"
        assignment_type = "role_handoff" if call.tool_name == "handoff.request" else "role_consult"
        title = _optional_text(call.payload.get("title")) or (
            f"Handoff to {target_role}"
            if call.tool_name == "handoff.request"
            else f"Consult {target_role}"
        )
        payload = {
            "safe_output_ref": call_id,
            "source_run_id": run_id,
            "source_role": call.role_id,
            "target_role": target_role,
            "reason": reason,
            "route_tool": call.tool_name,
            "work_item_id": work_item_id,
            "current_flow_state": call.payload.get("current_flow_state"),
            "source_documents": _string_list(call.payload.get("source_documents")),
            "target_outputs": _string_list(call.payload.get("target_outputs")),
            "allowed_tools": sorted(self.policy.tools_for_role(target_role)),
            "context_visibility": visibility_scope,
        }
        self.db.create_role_assignment(
            assignment_id=f"assignment-{call_id}",
            role_id=target_role,
            work_item_id=work_item_id,
            source_ref=call_id,
            title=title,
            summary=reason,
            assignment_type=assignment_type,
            visibility_scope=visibility_scope,
            payload=payload,
        )


def validate_payload(tool_name: str, payload: dict[str, Any]) -> None:
    if tool_name not in REQUIRED_FIELDS:
        raise SafeOutputError(f"unsupported safe-output tool `{tool_name}`")
    numeric_fields = set(NUMERIC_FIELDS.get(tool_name, ()))
    missing = []
    for field in REQUIRED_FIELDS[tool_name]:
        value = payload.get(field)
        if field in numeric_fields:
            try:
                float(value)
            except (TypeError, ValueError):
                missing.append(field)
        elif not isinstance(value, str) or not value.strip():
            missing.append(field)
    if missing:
        raise SafeOutputError(
            f"`{tool_name}` missing required fields: {', '.join(missing)}"
        )
    _reject_fake_claims(tool_name, payload)


def _reject_fake_claims(tool_name: str, payload: dict[str, Any]) -> None:
    if tool_name in {"status.reply", "status.complete"}:
        text = str(payload.get("message") or "").casefold()
        risky = [
            "created work-",
            "created queue-",
            "promoted work-",
            "deployed",
            "released",
            "closed work-",
            "approval received",
            "approved by sponsor",
            "sponsor approved",
        ]
        if any(marker in text for marker in risky):
            raise SafeOutputError(
                "status messages must not claim durable mutations; use the matching tool"
            )
        delivery_claims = [
            "delivered the teams reply successfully",
            "delivered successfully",
            "delivery succeeded",
            "human delivery succeeded",
            "teams delivery succeeded",
            "message was delivered",
            "sent successfully",
            "successfully sent",
            "sent the teams reply",
            "delivered to the human",
        ]
        if any(marker in text for marker in delivery_claims):
            raise SafeOutputError(
                "status messages must not claim connector delivery success; runtime delivery records own that fact"
            )
    for key in payload:
        if key in {"created_work_item_id", "created_queue_item_id"}:
            raise SafeOutputError("safe-output payloads must not invent created ids")
    if tool_name == "queue.propose_item":
        private_source_keys = {"body", "message", "raw_text", "raw_message"}
        if private_source_keys.intersection(payload):
            raise SafeOutputError(
                "queue proposals must store source references and rationale, not raw conversation text"
            )
    if tool_name == "release.deploy":
        if {"command", "cwd"}.intersection(payload):
            raise SafeOutputError(
                "release.deploy must use the configured deployment target command; command and cwd overrides are not allowed"
            )


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SafeOutputError(f"`{key}` must be a non-empty string")
    return value.strip()


def _release_evidence_from_payload(payload: dict[str, Any]) -> ReleaseEvidence:
    work_item_id = _required_text(payload, "work_item_id")
    return ReleaseEvidence(
        work_item_id=work_item_id,
        release_id=_optional_text(payload.get("release_id")) or f"release-{work_item_id}",
        scope=_required_text(payload, "scope"),
        commit_ref=_optional_text(payload.get("commit_ref")),
        approval_ref=_optional_text(payload.get("approval_ref")),
        rollback_plan=_required_text(payload, "rollback_plan"),
        residual_risks=_required_text(payload, "residual_risks"),
        deployment_result=_optional_text(payload.get("deployment_result")),
        smoke_result=_optional_text(payload.get("smoke_result")),
    )


def _smoke_checks(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise SafeOutputError("release.deploy requires non-empty smoke_checks")
    checks: dict[str, str] = {}
    for key, result in value.items():
        if not isinstance(key, str) or not key.strip():
            raise SafeOutputError("release.deploy smoke_checks keys must be non-empty strings")
        if not isinstance(result, str) or not result.strip():
            raise SafeOutputError("release.deploy smoke_checks results must be non-empty strings")
        checks[key.strip()] = result.strip()
    return checks


def _release_evidence_links(value: object) -> tuple[ReleaseEvidenceLink, ...]:
    if not isinstance(value, list) or not value:
        raise SafeOutputError("release.deploy requires evidence_links")
    links: list[ReleaseEvidenceLink] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SafeOutputError(f"release.deploy evidence_links item {index} must be an object")
        artifact_ref = _optional_text(item.get("artifact_ref")) or _optional_text(item.get("path"))
        artifact_type = _optional_text(item.get("artifact_type")) or _optional_text(item.get("type"))
        role_id = _optional_text(item.get("role_id"))
        if artifact_ref is None or artifact_type is None or role_id is None:
            raise SafeOutputError(
                f"release.deploy evidence_links item {index} requires artifact_ref, artifact_type, and role_id"
            )
        links.append(
            ReleaseEvidenceLink(
                artifact_ref=artifact_ref,
                artifact_type=artifact_type,
                role_id=role_id,
                status=_optional_text(item.get("status")) or "accepted",
            )
        )
    return tuple(links)


def _optional_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SafeOutputError("timeout_seconds must be a positive integer when provided")
    return value


def _has_successful_deployment(db: V2Database, *, release_id: str, work_item_id: str) -> bool:
    return any(
        run.get("release_id") == release_id
        and run.get("work_item_id") == work_item_id
        and run.get("status") == "succeeded"
        for run in db.list_deployment_runs()
    )


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
