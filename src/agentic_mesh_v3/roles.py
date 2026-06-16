from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


REQUIRED_ROLE_FIELDS = (
    "role_id",
    "purpose",
    "role_profile",
    "accountabilities",
    "decision_rights",
    "boundaries",
    "collaboration_style",
    "quality_bar",
    "memory_focus",
    "core_workflows",
    "standards_references",
    "anti_patterns",
    "standing_instructions",
)


@dataclass(frozen=True)
class RoleTemplate:
    role_id: str
    path: Path
    raw: dict[str, Any]

    def as_prompt_text(self) -> str:
        return yaml.safe_dump(self.raw, sort_keys=False).strip() + "\n"


def local_documentation_paths(template: RoleTemplate) -> tuple[str, ...]:
    """Return repo-local documentation paths referenced by a role template."""

    paths: list[str] = []
    for value in template.raw.get("documentation_obligations") or ():
        _append_local_doc_path(paths, value)
    for workflow in template.raw.get("core_workflows") or ():
        if not isinstance(workflow, dict):
            continue
        for value in workflow.get("artifacts") or ():
            _append_local_doc_path(paths, value)
    return tuple(dict.fromkeys(paths))


def validate_local_documentation_paths(template: RoleTemplate, *, repo_root: Path) -> None:
    missing = [path for path in local_documentation_paths(template) if not (repo_root / path).exists()]
    if missing:
        raise ValueError(
            f"role template `{template.path}` references missing documentation paths: {', '.join(missing)}"
        )


def load_role_template(path: Path, *, expected_role_id: str | None = None) -> RoleTemplate:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"role template `{path}` must be a mapping")
    if "default_tools" in raw:
        raise ValueError(
            f"role template `{path}` must not define default_tools; use generated V3 tool catalog prompts"
        )
    missing = [field for field in REQUIRED_ROLE_FIELDS if _is_empty(raw.get(field))]
    if missing:
        raise ValueError(f"role template `{path}` is missing required fields: {', '.join(missing)}")
    role_id = str(raw["role_id"])
    if expected_role_id is not None and role_id != expected_role_id:
        raise ValueError(f"role template `{path}` has role_id `{role_id}`, expected `{expected_role_id}`")
    _validate_decision_rights(path, raw["decision_rights"])
    _validate_core_workflows(path, raw["core_workflows"])
    _validate_standards(path, raw["standards_references"])
    return RoleTemplate(role_id=role_id, path=path, raw=raw)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _validate_decision_rights(path: Path, value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"role template `{path}` decision_rights must be a mapping")
    for key in ("owns", "advises", "escalates"):
        if _is_empty(value.get(key)):
            raise ValueError(f"role template `{path}` decision_rights.{key} is required")


def _validate_core_workflows(path: Path, value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError(f"role template `{path}` core_workflows must be a non-empty list")
    for index, workflow in enumerate(value):
        if not isinstance(workflow, dict):
            raise ValueError(f"role template `{path}` core_workflows[{index}] must be a mapping")
        for key in ("workflow_id", "trigger", "inputs", "outputs", "artifacts"):
            if _is_empty(workflow.get(key)):
                raise ValueError(f"role template `{path}` core_workflows[{index}].{key} is required")


def _validate_standards(path: Path, value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError(f"role template `{path}` standards_references must be a non-empty list")
    for index, reference in enumerate(value):
        if not isinstance(reference, dict):
            raise ValueError(f"role template `{path}` standards_references[{index}] must be a mapping")
        for key in ("name", "url", "applies_to"):
            if _is_empty(reference.get(key)):
                raise ValueError(f"role template `{path}` standards_references[{index}].{key} is required")


def _append_local_doc_path(paths: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    normalized = value.replace("\\", "/").strip()
    if not normalized.startswith("docs/") or "{{" in normalized or "}}" in normalized:
        return
    paths.append(normalized)
