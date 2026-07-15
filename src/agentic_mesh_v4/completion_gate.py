from __future__ import annotations

from dataclasses import dataclass
from typing import Any


P0_PREDICATES = {
    "artifact_exists",
    "artifact_linked",
    "exception_recorded",
    "handoff_recorded",
    "safe_output_call",
    "work_item_updated",
}

EXCEPTION_PREDICATE = "exception_recorded"


@dataclass(frozen=True)
class CompletionPredicate:
    predicate: str
    work_item_id: str | None = None
    tool_name: str | None = None
    path: str | None = None
    next_action: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class CompletionContract:
    required: tuple[CompletionPredicate, ...]
    source: str


@dataclass(frozen=True)
class CompletionEvaluation:
    state: str
    missing_predicates: tuple[dict[str, Any], ...] = ()
    observed_outputs: dict[str, Any] | None = None
    next_action: str = ""
    work_item_id: str | None = None


def resolve_completion_contract(payload: dict[str, Any]) -> CompletionContract | None:
    explicit = _mapping(payload.get("completion_contract") or payload.get("completion_contract_json"))
    if explicit is not None:
        predicates = tuple(
            _predicate_from_mapping(item)
            for item in _sequence(explicit.get("required"))
            if _mapping(item) is not None
        )
        predicates = tuple(item for item in predicates if item is not None)
        return CompletionContract(required=predicates, source="message_payload") if predicates else None
    inferred = _handoff_contract(payload)
    if inferred is not None:
        return inferred
    return None


def evaluate_completion_contract(
    *,
    db: Any,
    contract: CompletionContract | None,
    role_instance_id: str,
    message_id: str,
    turn_id: str | None,
) -> CompletionEvaluation:
    if contract is None:
        return CompletionEvaluation(state="completed", observed_outputs={})
    required = tuple(item for item in contract.required if item.enabled)
    if not required:
        return CompletionEvaluation(state="completed", observed_outputs={"contract_source": contract.source})
    work_item_id = _first_string(item.work_item_id for item in required)
    exact_calls = db.list_safe_output_calls(
        role_instance_id=role_instance_id,
        message_id=message_id,
        turn_id=turn_id,
        work_item_id=work_item_id,
    )
    message_calls = [
        item
        for item in db.list_safe_output_calls(
            message_id=message_id,
            work_item_id=work_item_id,
        )
        if _same_role(item.get("role_instance_id"), role_instance_id)
    ]
    fallback_calls: list[dict[str, Any]] = []
    if not exact_calls and not message_calls and work_item_id is not None:
        fallback_calls = db.list_safe_output_calls(
            role_instance_id=role_instance_id,
            work_item_id=work_item_id,
        )
    all_calls = _unique_safe_output_calls(exact_calls, message_calls, fallback_calls)
    artifacts = _artifacts_for_work_item(db, work_item_id)
    missing = tuple(
        _predicate_diagnostic(item)
        for item in required
        if not _predicate_satisfied(item, safe_output_calls=all_calls, artifacts=artifacts)
    )
    observed = {
        "contract_source": contract.source,
        "safe_output_calls": [
            {
                "call_id": item.get("call_id"),
                "tool_name": item.get("tool_name"),
                "message_id": item.get("message_id"),
                "turn_id": item.get("turn_id"),
                "work_item_id": item.get("work_item_id"),
            }
            for item in all_calls
        ],
        "artifacts": [
            {
                "artifact_id": item.get("artifact_id"),
                "work_item_id": item.get("work_item_id"),
                "path": item.get("path"),
                "title": item.get("title"),
            }
            for item in artifacts
        ],
    }
    if not missing:
        return CompletionEvaluation(state="completed", observed_outputs=observed, work_item_id=work_item_id)
    next_action = next((item.next_action for item in required if item.next_action), "")
    return CompletionEvaluation(
        state="completed_with_missing_output",
        missing_predicates=missing,
        observed_outputs=observed,
        next_action=next_action or "Record the required durable output before completing the turn.",
        work_item_id=work_item_id,
    )


def _handoff_contract(payload: dict[str, Any]) -> CompletionContract | None:
    work_item_id = _string(payload.get("work_item_id"))
    state = _string(payload.get("state"))
    if work_item_id is None or state is None:
        return None
    if _string(payload.get("handoff_id")) is not None or {"from_role", "to_role"}.issubset(payload):
        return CompletionContract(
            required=(
                CompletionPredicate(
                    predicate="handoff_recorded",
                    work_item_id=work_item_id,
                    tool_name="handoff.require",
                    next_action=str(payload.get("next_action") or ""),
                ),
            ),
            source="handoff_payload",
        )
    return None


def _predicate_from_mapping(value: object) -> CompletionPredicate | None:
    item = _mapping(value)
    if item is None:
        return None
    predicate = _string(item.get("predicate"))
    if predicate not in P0_PREDICATES:
        return None
    enabled = False if predicate == EXCEPTION_PREDICATE else bool(item.get("enabled", True))
    return CompletionPredicate(
        predicate=predicate,
        work_item_id=_string(item.get("work_item_id")),
        tool_name=_string(item.get("tool_name")),
        path=_canonical_document_path(_string(item.get("path"))),
        next_action=str(item.get("next_action") or ""),
        enabled=enabled,
    )


def _canonical_document_path(path: str | None) -> str | None:
    if path is None:
        return None
    replacements = {
        "/10-business-brief.md": "/010-business-brief.md",
        "/20-product-definition.md": "/020-product-definition.md",
        "/30-experience-design.md": "/030-experience-design.md",
        "/30-solution-design.md": "/050-solution-design.md",
        "/030-solution-design.md": "/050-solution-design.md",
        "/40-enterprise-alignment.md": "/040-enterprise-alignment.md",
        "/50-solution-design.md": "/050-solution-design.md",
        "/50-security-review.md": "/060-security-review.md",
        "/60-security-review.md": "/060-security-review.md",
        "/60-prompt-contract.md": "/060-prompt-contract.md",
        "/70-platform-readiness.md": "/070-platform-readiness.md",
        "/80-implementation-plan.md": "/080-implementation-plan.md",
        "/90-quality-plan.md": "/090-quality-plan.md",
        "/100-implementation-log.md": "/100-implementation-log.md",
        "/110-quality-evidence.md": "/110-quality-evidence.md",
        "/140-release-record.md": "/140-release-record.md",
    }
    for suffix, canonical in replacements.items():
        if path.endswith(suffix):
            return path[: -len(suffix)] + canonical
    return path


def _mapping(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _sequence(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _first_string(values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _same_role(candidate: object, expected: str) -> bool:
    if not isinstance(candidate, str):
        return False
    candidate_role, candidate_separator, _candidate_instance = candidate.rpartition(".")
    expected_role, expected_separator, _expected_instance = expected.rpartition(".")
    return bool(candidate_separator and expected_separator and candidate_role == expected_role)


def _unique_safe_output_calls(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            call_id = str(item.get("call_id") or "")
            if call_id and call_id in seen:
                continue
            if call_id:
                seen.add(call_id)
            calls.append(item)
    return calls


def _artifacts_for_work_item(db: Any, work_item_id: str | None) -> list[dict[str, Any]]:
    if work_item_id is None:
        return []
    return [
        dict(row)
        for row in db.connection.execute(
            "SELECT * FROM artifacts WHERE work_item_id=? ORDER BY created_at ASC",
            (work_item_id,),
        )
    ]


def _predicate_satisfied(
    predicate: CompletionPredicate,
    *,
    safe_output_calls: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> bool:
    if predicate.predicate == "artifact_linked":
        return any(
            item.get("tool_name") == (predicate.tool_name or "document.link_artifact")
            and _payload_matches(item, work_item_id=predicate.work_item_id, path=predicate.path)
            for item in safe_output_calls
        ) or any(
            (predicate.path is None or item.get("path") == predicate.path)
            and (predicate.work_item_id is None or item.get("work_item_id") == predicate.work_item_id)
            for item in artifacts
        )
    if predicate.predicate == "artifact_exists":
        return any(
            (predicate.path is None or item.get("path") == predicate.path)
            and (predicate.work_item_id is None or item.get("work_item_id") == predicate.work_item_id)
            for item in artifacts
        )
    if predicate.predicate == "work_item_updated":
        return any(
            item.get("tool_name") == (predicate.tool_name or "work_item.update")
            and _payload_matches(item, work_item_id=predicate.work_item_id)
            for item in safe_output_calls
        )
    if predicate.predicate == "handoff_recorded":
        return any(
            item.get("tool_name") == (predicate.tool_name or "handoff.require")
            and _payload_matches(item, work_item_id=predicate.work_item_id)
            for item in safe_output_calls
        )
    if predicate.predicate == "safe_output_call":
        return any(
            predicate.tool_name is None or item.get("tool_name") == predicate.tool_name
            for item in safe_output_calls
        )
    return False


def _payload_matches(item: dict[str, Any], *, work_item_id: str | None = None, path: str | None = None) -> bool:
    payload = _mapping_from_json(item.get("payload_json"))
    if work_item_id is not None and item.get("work_item_id") != work_item_id and payload.get("work_item_id") != work_item_id:
        return False
    if path is not None and payload.get("path") != path:
        return False
    return True


def _predicate_diagnostic(predicate: CompletionPredicate) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "predicate": predicate.predicate,
            "work_item_id": predicate.work_item_id,
            "tool_name": predicate.tool_name,
            "path": predicate.path,
            "next_action": predicate.next_action,
        }.items()
        if value is not None and value != ""
    }


def _mapping_from_json(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        import json

        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
