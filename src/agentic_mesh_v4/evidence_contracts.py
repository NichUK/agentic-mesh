from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


KNOWN_PREDICATES = {
    "artifact_exists",
    "artifact_linked",
    "safe_output_call",
    "handoff_state",
    "blocker_recorded",
    "exception_recorded",
    "review_status",
}
WARN_ONLY_PREDICATES = {"blocker_recorded", "review_status"}
DISABLED_PREDICATES = {"exception_recorded"}
REJECT_REVIEW_ROLES = {
    "product-manager",
    "qa-engineer",
    "release-manager",
    "enterprise-architect",
    "platform-engineer",
}
DEFAULT_CONTRACTS_PATH = Path("config/flows/evidence-contracts.yaml")
DEFAULT_FLOW_PATH = Path("config/flows/sdlc.yaml")


class EvidenceContractError(ValueError):
    pass


@dataclass(frozen=True)
class EvidencePredicate:
    predicate: str
    path: str | None = None
    tool_name: str | None = None
    payload_match: dict[str, str] | None = None
    remediation: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class EvidenceContract:
    contract_id: str
    work_item_types: tuple[str, ...]
    lifecycle_state: str
    owner_role: str
    enforcement: str
    required: tuple[EvidencePredicate, ...]
    role_id: str | None = None


@dataclass(frozen=True)
class EvidenceEvaluation:
    contract: EvidenceContract
    work_item_id: str
    work_item_type: str
    missing_predicates: tuple[dict[str, Any], ...]
    observed_outputs: dict[str, Any]

    @property
    def missing(self) -> bool:
        return bool(self.missing_predicates)


def load_evidence_contracts(
    *,
    contracts_path: Path = DEFAULT_CONTRACTS_PATH,
    flow_path: Path = DEFAULT_FLOW_PATH,
) -> tuple[EvidenceContract, ...]:
    contracts_path = _runtime_config_path(contracts_path)
    flow_path = _runtime_config_path(flow_path)
    flow = _yaml_mapping(flow_path)
    raw = _yaml_mapping(contracts_path)
    _validate_flow_binding(raw=raw, flow=flow, flow_path=flow_path)
    states = _mapping(flow.get("states"))
    roles = {str(item.get("owner_role")) for item in states.values() if isinstance(item, dict)}
    work_item_types = {str(item) for item in _sequence(flow.get("work_item_types"))}
    contracts = []
    for item in _sequence(raw.get("evidence_contracts")):
        contract = _contract_from_mapping(_mapping_required(item))
        _validate_contract(contract=contract, states=states, roles=roles, work_item_types=work_item_types)
        contracts.append(contract)
    return tuple(contracts)


def resolve_evidence_contracts(
    *,
    db: Any,
    payload: dict[str, Any],
    target_role: str,
    contracts: tuple[EvidenceContract, ...] | None = None,
) -> tuple[EvidenceContract, ...]:
    work_item_id = _string(payload.get("work_item_id"))
    lifecycle_state = _string(payload.get("lifecycle_state") or payload.get("state"))
    owner_role = target_role
    work_item_type = _string(payload.get("work_item_type")) or "slice"
    if work_item_id is not None:
        row = db.connection.execute("SELECT state, owner_role FROM work_items WHERE work_item_id=?", (work_item_id,)).fetchone()
        if row is not None:
            lifecycle_state = lifecycle_state or str(row["state"])
            owner_role = str(row["owner_role"] or target_role)
    if work_item_id is None or lifecycle_state is None:
        return ()
    candidates = contracts if contracts is not None else load_evidence_contracts()
    return tuple(
        contract
        for contract in candidates
        if work_item_type in contract.work_item_types
        and contract.lifecycle_state == lifecycle_state
        and contract.owner_role == owner_role
        and (contract.role_id is None or contract.role_id == target_role)
    )


def evaluate_evidence_contracts(
    *,
    db: Any,
    contracts: tuple[EvidenceContract, ...],
    payload: dict[str, Any],
    role_instance_id: str,
    message_id: str,
    turn_id: str | None,
) -> tuple[EvidenceEvaluation, ...]:
    work_item_id = _string(payload.get("work_item_id"))
    if work_item_id is None:
        return ()
    work_item_type = _string(payload.get("work_item_type")) or "slice"
    calls = db.list_safe_output_calls(
        role_instance_id=role_instance_id,
        message_id=message_id,
        turn_id=turn_id,
        work_item_id=work_item_id,
    )
    if not calls:
        calls = db.list_safe_output_calls(role_instance_id=role_instance_id, work_item_id=work_item_id)
    artifacts = _artifacts_for_work_item(db=db, work_item_id=work_item_id)
    evaluations = []
    for contract in contracts:
        missing = tuple(
            _missing_diagnostic(contract=contract, predicate=predicate, work_item_id=work_item_id, work_item_type=work_item_type)
            for predicate in contract.required
            if predicate.enabled and not _predicate_satisfied(predicate, work_item_id=work_item_id, calls=calls, artifacts=artifacts)
        )
        observed = {
            "contract_id": contract.contract_id,
            "enforcement": contract.enforcement,
            "lifecycle_state": contract.lifecycle_state,
            "owner_role": contract.owner_role,
            "work_item_type": work_item_type,
            "safe_output_calls": [
                {
                    "call_id": item.get("call_id"),
                    "tool_name": item.get("tool_name"),
                    "message_id": item.get("message_id"),
                    "turn_id": item.get("turn_id"),
                    "work_item_id": item.get("work_item_id"),
                }
                for item in calls
            ],
            "artifacts": [
                {
                    "artifact_id": item.get("artifact_id"),
                    "path": item.get("path"),
                    "work_item_id": item.get("work_item_id"),
                }
                for item in artifacts
            ],
        }
        evaluations.append(
            EvidenceEvaluation(
                contract=contract,
                work_item_id=work_item_id,
                work_item_type=work_item_type,
                missing_predicates=missing,
                observed_outputs=observed,
            )
        )
    return tuple(evaluations)


def _contract_from_mapping(item: dict[str, Any]) -> EvidenceContract:
    contract_id = _required_string(item, "contract_id")
    enforcement = _required_string(item, "enforcement")
    required = tuple(_predicate_from_mapping(_mapping_required(value)) for value in _sequence(item.get("required")))
    return EvidenceContract(
        contract_id=contract_id,
        work_item_types=tuple(_required_sequence_strings(item, "work_item_types")),
        lifecycle_state=_required_string(item, "lifecycle_state"),
        owner_role=_required_string(item, "owner_role"),
        role_id=_string(item.get("role_id")),
        enforcement=enforcement,
        required=required,
    )


def _predicate_from_mapping(item: dict[str, Any]) -> EvidencePredicate:
    predicate = _required_string(item, "predicate")
    enabled = bool(item.get("enabled", True)) and predicate not in DISABLED_PREDICATES
    return EvidencePredicate(
        predicate=predicate,
        path=_normalize_path(_string(item.get("path"))),
        tool_name=_string(item.get("tool_name")),
        payload_match={str(key): str(value) for key, value in _mapping(item.get("payload_match")).items()},
        remediation=str(item.get("remediation") or ""),
        enabled=enabled,
    )


def _validate_flow_binding(*, raw: dict[str, Any], flow: dict[str, Any], flow_path: Path) -> None:
    if raw.get("flow_id") != flow.get("flow_id"):
        raise EvidenceContractError("evidence contract flow_id does not match flow YAML")
    expected_hash = hashlib.sha256(flow_path.read_bytes()).hexdigest()
    if raw.get("flow_hash") != expected_hash:
        raise EvidenceContractError("evidence contract flow_hash does not match flow YAML")


def _validate_contract(
    *,
    contract: EvidenceContract,
    states: dict[str, Any],
    roles: set[str],
    work_item_types: set[str],
) -> None:
    if contract.lifecycle_state not in states:
        raise EvidenceContractError(f"unknown lifecycle_state: {contract.lifecycle_state}")
    state = _mapping_required(states[contract.lifecycle_state])
    flow_owner = _required_string(state, "owner_role")
    if contract.owner_role != flow_owner:
        raise EvidenceContractError(f"owner_role {contract.owner_role} does not match flow owner {flow_owner}")
    if contract.owner_role not in roles:
        raise EvidenceContractError(f"unknown owner_role: {contract.owner_role}")
    for work_item_type in contract.work_item_types:
        if work_item_type not in work_item_types:
            raise EvidenceContractError(f"unknown work_item_type: {work_item_type}")
    if contract.enforcement not in {"warn", "reject"}:
        raise EvidenceContractError(f"unsupported enforcement: {contract.enforcement}")
    if contract.enforcement == "reject":
        raise EvidenceContractError("reject evidence contracts require specialist review approval metadata")
    if not contract.required:
        raise EvidenceContractError(f"contract {contract.contract_id} has no required predicates")
    for predicate in contract.required:
        if predicate.predicate not in KNOWN_PREDICATES:
            raise EvidenceContractError(f"unknown predicate: {predicate.predicate}")
        if predicate.predicate in WARN_ONLY_PREDICATES and contract.enforcement == "reject":
            raise EvidenceContractError(f"predicate {predicate.predicate} is warn-only")


def _predicate_satisfied(
    predicate: EvidencePredicate,
    *,
    work_item_id: str,
    calls: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> bool:
    path = _render_path(predicate.path, work_item_id=work_item_id)
    if predicate.predicate == "artifact_exists":
        return any(item.get("path") == path for item in artifacts)
    if predicate.predicate == "artifact_linked":
        return any(
            item.get("tool_name") == (predicate.tool_name or "document.link_artifact")
            and _payload_matches(item, predicate=predicate, work_item_id=work_item_id, path=path)
            for item in calls
        )
    if predicate.predicate == "safe_output_call":
        return any(predicate.tool_name is None or item.get("tool_name") == predicate.tool_name for item in calls)
    return False


def _payload_matches(item: dict[str, Any], *, predicate: EvidencePredicate, work_item_id: str, path: str | None) -> bool:
    payload = _json_mapping(item.get("payload_json"))
    expected = predicate.payload_match or {}
    for key, value in expected.items():
        rendered = _render_path(value, work_item_id=work_item_id)
        if key == "work_item_id" and item.get("work_item_id") == rendered:
            continue
        if payload.get(key) != rendered:
            return False
    if path is not None and payload.get("path") != path:
        return False
    return True


def _missing_diagnostic(
    *,
    contract: EvidenceContract,
    predicate: EvidencePredicate,
    work_item_id: str,
    work_item_type: str,
) -> dict[str, Any]:
    path = _render_path(predicate.path, work_item_id=work_item_id)
    if path is None and predicate.payload_match is not None:
        path = _render_path(predicate.payload_match.get("path"), work_item_id=work_item_id)
    return {
        "contract_id": contract.contract_id,
        "enforcement": contract.enforcement,
        "lifecycle_state": contract.lifecycle_state,
        "owner_role": contract.owner_role,
        "work_item_type": work_item_type,
        "predicate": predicate.predicate,
        "path": path,
        "tool_name": predicate.tool_name,
        "remediation": predicate.remediation,
        "next_action": predicate.remediation,
    }


def _artifacts_for_work_item(*, db: Any, work_item_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.connection.execute(
            "SELECT * FROM artifacts WHERE work_item_id=? ORDER BY created_at ASC",
            (work_item_id,),
        )
    ]


def _render_path(path: str | None, *, work_item_id: str) -> str | None:
    if path is None:
        return None
    return path.replace("{work_item_id}", work_item_id)


def _normalize_path(path: str | None) -> str | None:
    if path is None:
        return None
    aliases = {
        "/20-product-definition.md": "/020-product-definition.md",
        "/30-experience-design.md": "/030-experience-design.md",
        "/30-solution-design.md": "/030-solution-design.md",
        "/50-security-review.md": "/050-security-review.md",
        "/60-prompt-contract.md": "/060-prompt-contract.md",
    }
    for old, new in aliases.items():
        if path.endswith(old):
            return path[: -len(old)] + new
    return path


def _yaml_mapping(path: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _mapping_required(parsed)


def _runtime_config_path(path: Path) -> Path:
    if path.is_absolute() or path.exists():
        return path
    system_root = os.environ.get("AGENTIC_MESH_SYSTEM_ROOT")
    candidates = []
    if system_root:
        candidates.append(Path(system_root) / path)
    candidates.append(Path("/mesh/system") / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return path


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mapping_required(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceContractError("expected mapping")
    return value


def _sequence(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _required_sequence_strings(item: dict[str, Any], key: str) -> list[str]:
    values = [str(value) for value in _sequence(item.get(key)) if str(value)]
    if not values:
        raise EvidenceContractError(f"missing required list: {key}")
    return values


def _required_string(item: dict[str, Any], key: str) -> str:
    value = _string(item.get(key))
    if value is None:
        raise EvidenceContractError(f"missing required field: {key}")
    return value


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _json_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
