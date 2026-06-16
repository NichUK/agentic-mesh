import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import yaml


def _schema() -> dict[str, object]:
    return json.loads(Path("config/schemas/role-template.schema.json").read_text(encoding="utf-8"))


def test_role_template_schema_allows_starter_roles() -> None:
    validator = jsonschema.Draft202012Validator(_schema())

    for role_path in Path("config/roles").glob("*.yaml"):
        role = yaml.safe_load(role_path.read_text(encoding="utf-8"))
        validator.validate(role)


def test_role_template_schema_rejects_legacy_default_tools() -> None:
    role = yaml.safe_load(Path("config/roles/product-manager.yaml").read_text(encoding="utf-8"))
    role = deepcopy(role)
    role["default_tools"] = ["docs.read"]

    validator = jsonschema.Draft202012Validator(_schema())

    errors = sorted(validator.iter_errors(role), key=lambda error: tuple(error.path))

    assert any("Additional properties are not allowed" in error.message for error in errors)


def test_role_template_schema_requires_expanded_charter() -> None:
    validator = jsonschema.Draft202012Validator(_schema())

    errors = sorted(
        validator.iter_errors(
            {
                "role_id": "thin-role",
                "version": 1,
                "purpose": "Too small.",
                "standing_instructions": ["Do work."],
                "documentation_obligations": [],
                "handoff_targets": [],
            }
        ),
        key=lambda error: tuple(error.path),
    )

    messages = "\n".join(error.message for error in errors)
    assert "'role_profile' is a required property" in messages
    assert "'core_workflows' is a required property" in messages
    assert "'standards_references' is a required property" in messages
