from pathlib import Path

import pytest

from agentic_mesh_v3.roles import load_role_template
from agentic_mesh_v3.roles import local_documentation_paths
from agentic_mesh_v3.roles import validate_local_documentation_paths


def test_load_role_template_validates_required_charter_fields(tmp_path: Path) -> None:
    role_path = tmp_path / "product-manager.yaml"
    role_path.write_text(_role_template("product-manager"), encoding="utf-8")

    template = load_role_template(role_path, expected_role_id="product-manager")

    assert template.role_id == "product-manager"
    assert "role_profile" in template.as_prompt_text()


def test_load_role_template_rejects_placeholder_template(tmp_path: Path) -> None:
    role_path = tmp_path / "thin-role.yaml"
    role_path.write_text("role_id: thin-role\npurpose: Too small.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required fields"):
        load_role_template(role_path)


def test_load_role_template_rejects_legacy_default_tools(tmp_path: Path) -> None:
    role_path = tmp_path / "product-manager.yaml"
    role_path.write_text(_role_template("product-manager") + "default_tools:\n  - docs.read\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must not define default_tools"):
        load_role_template(role_path)


def test_all_starter_role_templates_are_valid() -> None:
    for role_path in Path("config/roles").glob("*.yaml"):
        load_role_template(role_path, expected_role_id=role_path.stem)


def test_all_starter_role_documentation_obligations_exist() -> None:
    repo_root = Path(".")
    for role_path in Path("config/roles").glob("*.yaml"):
        template = load_role_template(role_path, expected_role_id=role_path.stem)
        validate_local_documentation_paths(template, repo_root=repo_root)


def test_local_documentation_paths_ignores_work_item_templates(tmp_path: Path) -> None:
    role_path = tmp_path / "test-role.yaml"
    role_path.write_text(_role_template("test-role"), encoding="utf-8")
    template = load_role_template(role_path)

    assert local_documentation_paths(template) == ()


def _role_template(role_id: str) -> str:
    return f"""
role_id: {role_id}
purpose: Test role.
role_profile: Act as a specialist role for tests.
accountabilities:
  - Do the role work.
decision_rights:
  owns:
    - Own role decisions.
  advises:
    - Advise related roles.
  escalates:
    - Escalate blockers.
boundaries:
  - Stay inside role authority.
collaboration_style:
  - Be concise.
quality_bar:
  - Evidence is recorded.
memory_focus:
  - Useful recurring context.
core_workflows:
  - workflow_id: test-workflow
    trigger: Test trigger.
    inputs:
      - Input
    outputs:
      - Output
    artifacts:
      - documents/work-items/{{work_item_id}}/index.md
standards_references:
  - name: Test Standard
    url: docs/test.md
    applies_to: Tests
anti_patterns:
  - Pretending work happened.
standing_instructions:
  - Use tools honestly.
"""
