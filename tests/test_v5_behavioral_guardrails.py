from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh_v5 import config_activation
from agentic_mesh_v5.behavioral_guardrails import BehavioralGuardrailError
from agentic_mesh_v5.behavioral_guardrails import decide_scenario_action
from agentic_mesh_v5.behavioral_guardrails import evaluate_behavioral_guardrails
from agentic_mesh_v5.config_activation import ConfigActivationError
from agentic_mesh_v5.config_activation import ConfigActivationStore


PROMPT = """Do not over-engineer. Prefer the smallest reliable solution, mature existing
tools, and the fewest necessary handoffs. Ask the sponsor when material intent
is ambiguous.
"""
REFERENCES = [
    "role/engineering@0.1.0",
    "flow/sdlc@0.1.0",
    "policy/behavioral-guardrails@0.1.0",
]


def _scenario(
    scenario_id: str,
    expected_action: str,
    *,
    material_ambiguity: bool = False,
    mature_reuse_available: bool = True,
    avoidable_custom_components: int = 0,
    necessary_handoffs: int = 1,
    proposed_handoffs: int = 1,
) -> dict[str, object]:
    return {
        "id": scenario_id,
        "description": f"{scenario_id} scenario",
        "facts": {
            "material_ambiguity": material_ambiguity,
            "mature_reuse_available": mature_reuse_available,
            "avoidable_custom_components": avoidable_custom_components,
            "necessary_handoffs": necessary_handoffs,
            "proposed_handoffs": proposed_handoffs,
        },
        "expected_action": expected_action,
    }


def _policy() -> dict[str, object]:
    return {
        "behavioral_guardrails": {
            "schema_version": 1,
            "policy_id": "simplicity-and-ambiguity",
            "prompt_requirements": [
                "do not over-engineer",
                "prefer the smallest reliable solution",
                "mature existing tools",
                "fewest necessary handoffs",
                "ask the sponsor when material intent is ambiguous",
            ],
            "scenarios": [
                _scenario("simple", "proceed"),
                _scenario("ambiguous", "ask_sponsor", material_ambiguity=True),
                _scenario(
                    "over-engineered",
                    "simplify",
                    avoidable_custom_components=3,
                    proposed_handoffs=4,
                ),
            ],
        }
    }


def _package(
    root: Path,
    reference: str,
    content_name: str,
    content: str | dict[str, object],
    *,
    dependencies: list[str] | None = None,
) -> Path:
    package, version = reference.split("@", 1)
    kind, package_id = package.split("/", 1)
    package_root = root / "packages" / kind / package_id / version
    package_root.mkdir(parents=True)
    if isinstance(content, str):
        rendered = content
    else:
        rendered = json.dumps(content, sort_keys=True) + "\n"
    content_path = package_root / content_name
    content_path.write_text(rendered, encoding="utf-8")
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": package_id,
                "kind": kind,
                "version": version,
                "content": [content_name],
                "dependencies": dependencies or [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return content_path


def _repository(tmp_path: Path) -> tuple[Path, Path, Path]:
    schema = tmp_path / "schemas" / "package.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    prompt_path = _package(
        tmp_path,
        "fragment/simple-delivery@0.1.0",
        "fragment.md",
        PROMPT,
    )
    _package(
        tmp_path,
        "role/engineering@0.1.0",
        "role.json",
        {"role": {"role_id": "engineering"}},
        dependencies=["fragment/simple-delivery@0.1.0"],
    )
    _package(
        tmp_path,
        "flow/sdlc@0.1.0",
        "flow.json",
        {"flow": {"flow_id": "sdlc"}},
    )
    policy_path = _package(
        tmp_path,
        "policy/behavioral-guardrails@0.1.0",
        "guardrails.json",
        _policy(),
        dependencies=["fragment/simple-delivery@0.1.0"],
    )
    return tmp_path, prompt_path, policy_path


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (_scenario("simple", "proceed")["facts"], "proceed"),
        (
            _scenario("ambiguous", "ask_sponsor", material_ambiguity=True)["facts"],
            "ask_sponsor",
        ),
        (
            _scenario(
                "over-engineered",
                "simplify",
                avoidable_custom_components=3,
                proposed_handoffs=4,
            )["facts"],
            "simplify",
        ),
    ],
)
def test_scenario_decisions(facts: object, expected: str) -> None:
    assert isinstance(facts, dict)
    assert decide_scenario_action(facts) == expected


def test_inconsistent_avoidable_component_facts_are_rejected() -> None:
    facts = _scenario(
        "over-engineered",
        "simplify",
        mature_reuse_available=False,
        avoidable_custom_components=1,
    )["facts"]
    assert isinstance(facts, dict)

    with pytest.raises(
        BehavioralGuardrailError,
        match="avoidable custom components require mature_reuse_available",
    ):
        decide_scenario_action(facts)


def test_valid_guardrails_allow_an_immutable_role_flow_release(tmp_path: Path) -> None:
    root, _, _ = _repository(tmp_path)
    store = ConfigActivationStore(root)

    resolved = store.validate_draft(REFERENCES)
    evaluation = evaluate_behavioral_guardrails(resolved)
    release = store.create_release(REFERENCES, actor="prompt-engineer")

    assert evaluation.applicable is True
    assert [result.observed_action for result in evaluation.scenarios] == [
        "proceed",
        "ask_sponsor",
        "simplify",
    ]
    assert store.get_release(release.digest) == release


@pytest.mark.parametrize(
    "regression",
    ["missing-policy", "missing-prompt", "missing-requirement", "wrong-expectation", "missing-scenario"],
)
def test_behavioral_regressions_block_release_creation(
    tmp_path: Path, regression: str
) -> None:
    root, prompt_path, policy_path = _repository(tmp_path)
    references = list(REFERENCES)
    if regression == "missing-policy":
        references.remove("policy/behavioral-guardrails@0.1.0")
    elif regression == "missing-prompt":
        prompt_path.write_text(
            PROMPT.replace("mature existing\ntools", "new custom\ntools"),
            encoding="utf-8",
        )
    else:
        content = json.loads(policy_path.read_text(encoding="utf-8"))
        policy = content["behavioral_guardrails"]
        if regression == "missing-requirement":
            policy["prompt_requirements"].pop()
        elif regression == "wrong-expectation":
            policy["scenarios"][0]["expected_action"] = "simplify"
        else:
            policy["scenarios"].pop()
        policy_path.write_text(json.dumps(content), encoding="utf-8")
    store = ConfigActivationStore(root)

    with pytest.raises(
        ConfigActivationError, match="behavioral promotion check failed"
    ) as caught:
        store.create_release(references, actor="prompt-engineer")

    if regression == "missing-policy":
        assert "policy is required for role or flow releases" in str(caught.value)
    assert not store.releases_dir.exists()


def test_existing_legacy_release_remains_idempotently_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = _repository(tmp_path)
    references = ["role/engineering@0.1.0", "flow/sdlc@0.1.0"]
    store = ConfigActivationStore(root)
    monkeypatch.setattr(
        config_activation, "evaluate_behavioral_guardrails", lambda resolved: None
    )
    legacy = store.create_release(references, actor="legacy-operator")
    monkeypatch.undo()

    repeated = store.create_release(references, actor="new-operator")

    assert repeated == legacy
    assert repeated.created_by == "legacy-operator"


def test_non_prompt_release_is_not_subject_to_prompt_guardrails(tmp_path: Path) -> None:
    schema = tmp_path / "schemas" / "package.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    _package(
        tmp_path,
        "system/core@1.0.0",
        "settings.json",
        {"workers": 1},
    )
    store = ConfigActivationStore(tmp_path)

    release = store.create_release(["system/core@1.0.0"], actor="operator")

    assert store.get_release(release.digest) == release
