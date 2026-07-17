from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from agentic_mesh_v5.package_resolver import ResolvedConfiguration


REQUIRED_SCENARIOS = {"simple", "ambiguous", "over-engineered"}
REQUIRED_PROMPT_PHRASES = {
    "do not over-engineer",
    "prefer the smallest reliable solution",
    "mature existing tools",
    "fewest necessary handoffs",
    "ask the sponsor when material intent is ambiguous",
}
_POLICY_FIELDS = {
    "schema_version",
    "policy_id",
    "prompt_requirements",
    "scenarios",
}
_SCENARIO_FIELDS = {"id", "description", "facts", "expected_action"}
_FACT_FIELDS = {
    "material_ambiguity",
    "mature_reuse_available",
    "avoidable_custom_components",
    "necessary_handoffs",
    "proposed_handoffs",
}


class BehavioralGuardrailError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    expected_action: str
    observed_action: str


@dataclass(frozen=True, slots=True)
class GuardrailEvaluation:
    applicable: bool
    policy_id: str | None
    scenarios: tuple[ScenarioResult, ...]


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BehavioralGuardrailError(f"{label} must be an object")
    return value


def _non_negative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BehavioralGuardrailError(f"{label} must be a non-negative integer")
    return value


def decide_scenario_action(facts: Mapping[str, object]) -> str:
    if set(facts) != _FACT_FIELDS:
        raise BehavioralGuardrailError("scenario facts do not match the guardrail contract")
    material_ambiguity = facts.get("material_ambiguity")
    mature_reuse_available = facts.get("mature_reuse_available")
    if not isinstance(material_ambiguity, bool) or not isinstance(
        mature_reuse_available, bool
    ):
        raise BehavioralGuardrailError("scenario ambiguity and reuse facts must be boolean")
    custom_components = _non_negative_integer(
        facts.get("avoidable_custom_components"), "avoidable_custom_components"
    )
    necessary_handoffs = _non_negative_integer(
        facts.get("necessary_handoffs"), "necessary_handoffs"
    )
    proposed_handoffs = _non_negative_integer(
        facts.get("proposed_handoffs"), "proposed_handoffs"
    )
    if proposed_handoffs < necessary_handoffs:
        raise BehavioralGuardrailError(
            "proposed_handoffs cannot be lower than necessary_handoffs"
        )
    if custom_components > 0 and not mature_reuse_available:
        raise BehavioralGuardrailError(
            "avoidable custom components require mature_reuse_available"
        )
    if material_ambiguity:
        return "ask_sponsor"
    if custom_components > 0 or proposed_handoffs > necessary_handoffs:
        return "simplify"
    return "proceed"


def evaluate_behavioral_guardrails(
    resolved: ResolvedConfiguration,
) -> GuardrailEvaluation:
    if "role" not in resolved.settings and "flow" not in resolved.settings:
        return GuardrailEvaluation(applicable=False, policy_id=None, scenarios=())

    policy = _object(
        resolved.settings.get("behavioral_guardrails"), "behavioral_guardrails"
    )
    if set(policy) != _POLICY_FIELDS:
        raise BehavioralGuardrailError(
            "behavioral_guardrails fields do not match the promotion contract"
        )
    if (
        policy.get("schema_version") != 1
        or policy.get("policy_id") != "simplicity-and-ambiguity"
    ):
        raise BehavioralGuardrailError("invalid behavioral guardrail identity")
    requirements = policy.get("prompt_requirements")
    if (
        not isinstance(requirements, list)
        or not all(isinstance(item, str) and item.strip() for item in requirements)
        or len(requirements) != len(set(requirements))
        or set(requirements) != REQUIRED_PROMPT_PHRASES
    ):
        raise BehavioralGuardrailError("prompt requirements are incomplete")
    effective_text = " ".join(
        " ".join(section.text.casefold().split()) for section in resolved.text_sections
    )
    missing_phrases = sorted(
        phrase for phrase in REQUIRED_PROMPT_PHRASES if phrase not in effective_text
    )
    if missing_phrases:
        raise BehavioralGuardrailError(
            f"effective prompt is missing guardrails: {missing_phrases}"
        )

    scenarios = policy.get("scenarios")
    if not isinstance(scenarios, list):
        raise BehavioralGuardrailError("guardrail scenarios must be a list")
    results: list[ScenarioResult] = []
    scenario_ids: list[str] = []
    for value in scenarios:
        scenario = _object(value, "guardrail scenario")
        if set(scenario) != _SCENARIO_FIELDS:
            raise BehavioralGuardrailError(
                "guardrail scenario fields do not match the promotion contract"
            )
        scenario_id = scenario.get("id")
        description = scenario.get("description")
        expected = scenario.get("expected_action")
        if (
            not isinstance(scenario_id, str)
            or not isinstance(description, str)
            or not description.strip()
            or expected not in {"proceed", "ask_sponsor", "simplify"}
        ):
            raise BehavioralGuardrailError("guardrail scenario metadata is invalid")
        observed = decide_scenario_action(_object(scenario.get("facts"), "scenario facts"))
        if observed != expected:
            raise BehavioralGuardrailError(
                f"scenario {scenario_id} expected {expected}, invariant requires {observed}"
            )
        scenario_ids.append(scenario_id)
        results.append(ScenarioResult(scenario_id, expected, observed))
    if set(scenario_ids) != REQUIRED_SCENARIOS or len(scenario_ids) != 3:
        raise BehavioralGuardrailError(
            "guardrails must contain simple, ambiguous and over-engineered scenarios"
        )
    return GuardrailEvaluation(
        applicable=True,
        policy_id="simplicity-and-ambiguity",
        scenarios=tuple(results),
    )
