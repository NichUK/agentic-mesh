from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from agentic_mesh_v2.db import V2Database


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
    "release.deploy": ("work_item_id", "target_id", "reason"),
    "release.record_no_deployment": ("work_item_id", "reason"),
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
    def __init__(self, db: V2Database, policy: ToolPolicy | None = None) -> None:
        self.db = db
        self.policy = policy or ToolPolicy()

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
        return call_id


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
