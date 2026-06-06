from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = "approval-decision-view-v1"
CONTEXT_SCHEMA_VERSION = "approval-decision-context-snapshot-v1"
ARTIFACT_INLINE_LIMIT = 8

FORBIDDEN_FIELDS = {
    "activity_id",
    "bot_id",
    "channel_id",
    "conversation_id",
    "credential_ref",
    "from_id",
    "graph_url",
    "mount_ref",
    "payload_ref",
    "raw_activity_path",
    "raw_payload",
    "secret_ref",
    "service_url",
    "team_id",
    "teams_activity_id",
    "teams_channel_id",
    "teams_conversation_id",
    "teams_service_url",
    "tenant_id",
    "user_id",
}


@dataclass(frozen=True)
class ApprovalDecisionViewModel:
    schema_version: str
    state: str
    response_request_id: str
    project_id: str | None
    work_item_id: str
    work_item_type: str | None
    queue_item_id: str | None
    title: str
    title_source: str
    description_summary: str
    decision_scope: str
    status_url: str | None
    status_label: str | None
    test_url: str | None
    test_label: str | None
    how_to_test: str | None
    artifacts: list[dict[str, str]]
    artifact_count: int
    artifact_inline_limit: int
    lifecycle_state: str
    gate_id: str
    response_type: str | None
    completion_criteria_summary: str | None
    response_options: list[dict[str, str]]
    decision_value: Any | None
    decision_label: str | None
    responder_display: str | None
    recorded_at: str | None
    requested_at: str | None
    context_completeness: str
    context_warnings: list[str]
    correlation_id: str | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        forbidden = FORBIDDEN_FIELDS.intersection(payload)
        if forbidden:
            raise ValueError(f"approval decision view contains forbidden fields: {sorted(forbidden)}")
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalDecisionViewModel":
        return cls(
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
            state=str(data.get("state") or "requested"),
            response_request_id=str(data.get("response_request_id") or ""),
            project_id=str(data["project_id"]) if data.get("project_id") else None,
            work_item_id=str(data.get("work_item_id") or ""),
            work_item_type=str(data["work_item_type"]) if data.get("work_item_type") else None,
            queue_item_id=str(data["queue_item_id"]) if data.get("queue_item_id") else None,
            title=str(data.get("title") or ""),
            title_source=str(data.get("title_source") or "fallback"),
            description_summary=str(data.get("description_summary") or ""),
            decision_scope=str(data.get("decision_scope") or ""),
            status_url=str(data["status_url"]) if data.get("status_url") else None,
            status_label=str(data["status_label"]) if data.get("status_label") else None,
            test_url=str(data["test_url"]) if data.get("test_url") else None,
            test_label=str(data["test_label"]) if data.get("test_label") else None,
            how_to_test=str(data["how_to_test"]) if data.get("how_to_test") else None,
            artifacts=list(data.get("artifacts") or []),
            artifact_count=int(data.get("artifact_count") or 0),
            artifact_inline_limit=int(data.get("artifact_inline_limit") or ARTIFACT_INLINE_LIMIT),
            lifecycle_state=str(data.get("lifecycle_state") or ""),
            gate_id=str(data.get("gate_id") or ""),
            response_type=str(data["response_type"]) if data.get("response_type") else None,
            completion_criteria_summary=(
                str(data["completion_criteria_summary"])
                if data.get("completion_criteria_summary")
                else None
            ),
            response_options=list(data.get("response_options") or []),
            decision_value=data.get("decision_value"),
            decision_label=str(data["decision_label"]) if data.get("decision_label") else None,
            responder_display=(
                str(data["responder_display"]) if data.get("responder_display") else None
            ),
            recorded_at=str(data["recorded_at"]) if data.get("recorded_at") else None,
            requested_at=str(data["requested_at"]) if data.get("requested_at") else None,
            context_completeness=str(data.get("context_completeness") or "complete"),
            context_warnings=[str(item) for item in data.get("context_warnings") or []],
            correlation_id=str(data["correlation_id"]) if data.get("correlation_id") else None,
        )


def build_requested_approval_decision(payload: dict[str, Any]) -> ApprovalDecisionViewModel:
    approval_context = _dict(payload.get("approval_context"))
    warnings: list[str] = []
    title, title_source = _title(payload, approval_context, warnings)
    status_url = _safe_url(approval_context.get("status_url"), warnings, "status_url")
    test_url = _safe_url(approval_context.get("test_url"), warnings, "test_url")
    if not test_url:
        test_url = status_url
    artifacts = _safe_artifacts(approval_context.get("artifact_paths"), warnings)
    description = _bounded(
        str(
            approval_context.get("work_performed_summary")
            or approval_context.get("description_summary")
            or payload.get("summary")
            or "Approval summary unavailable. Open Status and review for current work item context."
        ),
        900,
    )
    return ApprovalDecisionViewModel(
        schema_version=SCHEMA_VERSION,
        state="requested",
        response_request_id=str(payload.get("response_request_id") or ""),
        project_id=str(payload["project_id"]) if payload.get("project_id") else None,
        work_item_id=str(payload.get("work_item_id") or ""),
        work_item_type=str(payload["work_item_type"]) if payload.get("work_item_type") else None,
        queue_item_id=str(payload["queue_item_id"]) if payload.get("queue_item_id") else None,
        title=title,
        title_source=title_source,
        description_summary=description,
        decision_scope=_decision_scope_text(payload),
        status_url=status_url,
        status_label="Status and review" if status_url else None,
        test_url=test_url,
        test_label=str(approval_context.get("test_url_label") or "Review and test") if test_url else None,
        how_to_test=str(approval_context["how_to_test"])[:900] if approval_context.get("how_to_test") else None,
        artifacts=artifacts,
        artifact_count=len(artifacts),
        artifact_inline_limit=ARTIFACT_INLINE_LIMIT,
        lifecycle_state=str(payload.get("lifecycle_state") or ""),
        gate_id=str(payload.get("gate_id") or ""),
        response_type=str(payload["response_type"]) if payload.get("response_type") else None,
        completion_criteria_summary=_completion_criteria_summary(payload),
        response_options=_response_options(payload),
        decision_value=None,
        decision_label=None,
        responder_display=None,
        recorded_at=None,
        requested_at=str(payload["requested_at"]) if payload.get("requested_at") else None,
        context_completeness="partial" if warnings else "complete",
        context_warnings=warnings,
        correlation_id=str(payload["correlation_id"]) if payload.get("correlation_id") else None,
    )


def build_recorded_approval_decision(
    context: dict[str, Any] | ApprovalDecisionViewModel | None,
    *,
    request_metadata: dict[str, Any],
    decision_value: Any,
    responder_display: str | None,
    recorded_at: str | None,
) -> ApprovalDecisionViewModel:
    if isinstance(context, ApprovalDecisionViewModel):
        requested = context
    elif isinstance(context, dict) and context:
        requested = ApprovalDecisionViewModel.from_dict(context)
    else:
        requested = _fallback_requested_model(request_metadata)
    warnings = list(requested.context_warnings)
    completeness = requested.context_completeness
    if not context:
        completeness = "unavailable"
        warnings.append("missing_request_context")
    decision_label = response_value_label(
        {"response_template": {"options": requested.response_options}},
        response_value=decision_value,
    )
    return ApprovalDecisionViewModel(
        **{
            **requested.to_dict(),
            "state": "recorded",
            "response_options": [],
            "decision_value": _safe_decision_value(decision_value),
            "decision_label": decision_label,
            "responder_display": _bounded(responder_display or "teams-user", 120),
            "recorded_at": recorded_at,
            "context_completeness": completeness,
            "context_warnings": _unique(warnings),
        }
    )


def response_value_label(payload: dict[str, Any], *, response_value: Any) -> str:
    template = _dict(payload.get("response_template"))
    for option in template.get("options") or []:
        if isinstance(option, dict) and option.get("value") == response_value:
            return _bounded(str(option.get("label") or response_value), 80)
    if response_value == "approved":
        return "Approve"
    if response_value == "not_approved":
        return "Not Approve"
    return _bounded(str(response_value or "Submitted"), 80)


def _fallback_requested_model(metadata: dict[str, Any]) -> ApprovalDecisionViewModel:
    payload = {
        "response_request_id": metadata.get("response_request_id"),
        "project_id": metadata.get("project_id"),
        "work_item_id": metadata.get("work_item_id"),
        "work_item_type": metadata.get("work_item_type"),
        "lifecycle_state": metadata.get("lifecycle_state"),
        "gate_id": metadata.get("gate_id"),
        "response_type": metadata.get("response_type"),
        "correlation_id": metadata.get("correlation_id"),
        "title": metadata.get("title"),
        "summary": "Decision context partially available.",
    }
    return build_requested_approval_decision(payload)


def _title(
    payload: dict[str, Any],
    approval_context: dict[str, Any],
    warnings: list[str],
) -> tuple[str, str]:
    sources = [
        ("payload", payload.get("title")),
        ("approval_context", approval_context.get("title")),
        ("queue_item", payload.get("queue_item_title")),
    ]
    for source, value in sources:
        if value:
            return _bounded(str(value), 160), source
    warnings.append("title_fallback_used")
    return f"Work item {payload.get('work_item_id') or 'unknown'}", "fallback"


def _response_options(payload: dict[str, Any]) -> list[dict[str, str]]:
    template = _dict(payload.get("response_template"))
    rows: list[dict[str, str]] = []
    for option in template.get("options") or []:
        if not isinstance(option, dict) or not option.get("value"):
            continue
        rows.append(
            {
                "value": _bounded(str(option["value"]), 120),
                "label": _bounded(str(option.get("label") or option["value"]), 80),
            }
        )
    return rows


def _completion_criteria_summary(payload: dict[str, Any]) -> str | None:
    criteria = _dict(payload.get("completion_criteria"))
    accepted = [str(item) for item in criteria.get("accepted_values") or []]
    if not accepted:
        return None
    return f"Accepts {', '.join(accepted)} to satisfy the gate."


def _safe_artifacts(value: Any, warnings: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in value or []:
        path = str(item).strip()
        if not path:
            continue
        if _unsafe_path(path):
            warnings.append("artifact_path_redacted")
            continue
        rows.append({"label": _bounded(path.rsplit("/", 1)[-1] or path, 120), "path": path})
    return rows


def _safe_url(value: Any, warnings: list[str], warning_code: str) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        warnings.append(f"{warning_code}_redacted")
        return None
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "::1"} or hostname.startswith("127."):
        warnings.append(f"{warning_code}_redacted")
        return None
    if hostname.endswith("graph.microsoft.com") or "login.microsoftonline.com" in hostname:
        warnings.append(f"{warning_code}_redacted")
        return None
    return raw


def _unsafe_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith(("/", "../", "./../", "state/", "secrets/", "memory/")):
        return True
    if normalized.startswith("mesh/") or normalized.startswith("/mesh/"):
        return True
    return "/../" in normalized or "secret" in normalized.lower() or "credential" in normalized.lower()


def _decision_scope_text(payload: dict[str, Any]) -> str:
    gate_id = str(payload.get("gate_id") or "")
    if gate_id == "release_decision_response":
        return "Final release decision. This is separate from earlier planning or implementation progression approvals."
    if gate_id == "pre_implementation_sponsor_approval":
        return "Progression into implementation for this scoped work item. Specialist review and final release approval remain separate."
    return "Human response for the configured lifecycle gate."


def _safe_decision_value(value: Any) -> Any:
    if isinstance(value, str):
        return _bounded(value, 120)
    if isinstance(value, bool | int | float) or value is None:
        return value
    return "[redacted]"


def _bounded(value: str | None, max_length: int = 900) -> str:
    raw = str(value or "")
    if len(raw) <= max_length:
        return raw
    return f"{raw[: max_length - 3].rstrip()}..."


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
