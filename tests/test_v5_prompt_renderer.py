from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh_v5.prompt_renderer import PromptRenderError
from agentic_mesh_v5.prompt_renderer import render_role_state_prompt


def _package(
    root: Path,
    reference: str,
    content_name: str,
    content: str | dict[str, object],
    *,
    dependencies: list[str] | None = None,
) -> None:
    path, version = reference.split("@", 1)
    kind, package_id = path.split("/", 1)
    package_root = root / "packages" / kind / package_id / version
    package_root.mkdir(parents=True)
    if isinstance(content, dict):
        rendered = json.dumps(content, sort_keys=True) + "\n"
    else:
        rendered = content
    (package_root / content_name).write_text(rendered, encoding="utf-8")
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


def _repository(root: Path, *, role_id: str = "engineering") -> Path:
    schema = root / "schemas" / "package.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    _package(root, "system/core@0.1.0", "system.md", "Keep work inspectable.\n")
    _package(
        root,
        "policy/secure-defaults@0.1.0",
        "policy.md",
        "Keep credentials outside prompts.\n",
    )
    _package(
        root,
        "fragment/simple-delivery@0.1.0",
        "fragment.md",
        "Do not over-engineer. Ask the sponsor when material intent is ambiguous.\n",
    )
    shared = [
        "system/core@0.1.0",
        "policy/secure-defaults@0.1.0",
        "fragment/simple-delivery@0.1.0",
    ]
    _package(
        root,
        f"role/{role_id}@0.1.0",
        "role.json",
        {
            "role": {
                "schema_version": 1,
                "role_id": role_id,
                "display_name": "Engineering",
                "role_class": "development",
                "purpose": "Implement approved work safely.",
                "accountabilities": ["Implement and test the accepted slice."],
                "decision_rights": {
                    "owns": ["Implementation approach."],
                    "must_not": ["Change product scope silently."],
                },
                "consults": ["qa-engineer"],
                "handoff_targets": ["qa-engineer"],
                "memory_scope": "project-role",
                "instructions": ["Prefer a focused, testable change."],
                "documentation": ["Implementation log"],
            }
        },
        dependencies=shared,
    )
    _package(
        root,
        "flow/sdlc@0.1.0",
        "flow.json",
        {
            "flow": {
                "schema_version": 1,
                "flow_id": "sdlc",
                "leader_role": "project-manager",
                "entry_state": "implementation",
                "terminal_states": ["quality_review"],
                "work_item_types": ["story"],
                "continuation": {
                    "stop_conditions": [
                        "sponsor_gate",
                        "terminal_completion",
                        "terminal_error",
                    ],
                    "technical_attempts": 3,
                    "pm_correction_attempts": 3,
                    "recovery_required": True,
                },
                "states": {
                    "implementation": {
                        "owner_role": "engineering",
                        "purpose": "Build the approved slice.",
                        "artifact": "work-items/{work_item_id}/implementation.md",
                        "consults": [
                            {"role": "qa-engineer", "purpose": "Clarify test evidence."}
                        ],
                        "gates": [
                            {
                                "id": "implementation_review",
                                "type": "owner_review",
                                "reviewer_role": "engineering",
                            }
                        ],
                        "routes": [
                            {
                                "id": "implementation_complete",
                                "outcome": "completed",
                                "target_state": "quality_review",
                                "target_role": "qa-engineer",
                            }
                        ],
                        "terminal": False,
                    },
                    "quality_review": {
                        "owner_role": "qa-engineer",
                        "purpose": "Verify acceptance evidence.",
                        "artifact": "work-items/{work_item_id}/quality.md",
                        "consults": [],
                        "gates": [
                            {
                                "id": "sponsor_acceptance",
                                "type": "sponsor_approval",
                                "requested_from": "sponsor",
                            }
                        ],
                        "routes": [],
                        "terminal": True,
                    },
                },
            }
        },
        dependencies=shared,
    )
    return root


def test_render_is_deterministic_and_includes_role_state_guardrails_and_provenance(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)

    first = render_role_state_prompt(
        root, "role/engineering@0.1.0", "flow/sdlc@0.1.0", "implementation"
    )
    second = render_role_state_prompt(
        root, "role/engineering@0.1.0", "flow/sdlc@0.1.0", "implementation"
    )

    assert first == second
    assert "Do not over-engineer" in first.text
    assert "material intent is ambiguous" in first.text
    assert "This role owns the state: yes" in first.text
    assert "qa-engineer: Clarify test evidence" in first.text
    assert "implementation_review" in first.text
    assert "hand off to qa-engineer in quality_review" in first.text
    assert f"Configuration digest: {first.digest}" in first.text
    assert "sha256:" in first.text
    assert first.to_dict()["text"] == first.text
    assert len(first.provenance) == 10


def test_consulted_role_can_render_a_state_it_does_not_own(tmp_path: Path) -> None:
    root = _repository(tmp_path, role_id="qa-engineer")

    rendered = render_role_state_prompt(
        root, "role/qa-engineer@0.1.0", "flow/sdlc@0.1.0", "implementation"
    )

    assert "This role owns the state: no" in rendered.text
    assert "Owner: engineering" in rendered.text


def test_unknown_state_and_wrong_package_kind_are_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    with pytest.raises(PromptRenderError, match="unknown flow state"):
        render_role_state_prompt(
            root, "role/engineering@0.1.0", "flow/sdlc@0.1.0", "missing"
        )
    with pytest.raises(PromptRenderError, match="must select role and flow"):
        render_role_state_prompt(
            root, "flow/sdlc@0.1.0", "role/engineering@0.1.0", "implementation"
        )


def test_package_and_content_role_identity_must_match(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    role_path = root / "packages" / "role" / "engineering" / "0.1.0" / "role.json"
    content = json.loads(role_path.read_text(encoding="utf-8"))
    content["role"]["role_id"] = "different-role"
    role_path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(PromptRenderError, match="identity does not match"):
        render_role_state_prompt(
            root, "role/engineering@0.1.0", "flow/sdlc@0.1.0", "implementation"
        )
