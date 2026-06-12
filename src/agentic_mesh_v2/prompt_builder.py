from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.safe_outputs import ToolPolicy


@dataclass(frozen=True)
class PromptRender:
    prompt_text: str
    component_manifest: dict[str, Any]


class PromptAssembler:
    def __init__(
        self,
        *,
        project_file: Path,
        config_root: Path,
        prompt_config_root: Path | None = None,
        tool_policy: ToolPolicy | None = None,
    ) -> None:
        self.project_file = Path(project_file)
        self.config_root = Path(config_root)
        self.prompt_config_root = (
            Path(prompt_config_root)
            if prompt_config_root is not None
            else self.config_root / "config" / "prompts"
        )
        self.role_config_root = self.config_root / "config" / "roles"
        self.tool_policy = tool_policy or ToolPolicy()

    def render(self, assignment: RoleAssignment) -> PromptRender:
        project = _load_yaml_mapping(self.project_file)
        role = _load_yaml_mapping(self.role_config_root / f"{assignment.role_id}.yaml", missing_ok=True)
        components = self._load_components()
        root = ET.Element("agentic-mesh-worker-prompt", {"version": "v2"})

        _text_section(root, "system-security", components["system-security"])
        _safe_outputs_section(
            root,
            component_text=components["safe-outputs"],
            tools=sorted(self.tool_policy.tools_for_role(assignment.role_id)),
        )
        _mapping_section(root, "role", _role_payload(assignment, role, project))
        _mapping_section(root, "project", _project_payload(project))
        _mapping_section(root, "assignment", _assignment_payload(assignment))
        _mapping_section(root, "current-flow-state", _flow_state_payload(assignment, project))
        _context_section(root, "conversation-context", assignment.conversation_context)
        _context_section(root, "memory-context", assignment.memory_context)
        _text_section(root, "instructions", components["instructions"])

        ET.indent(root, space="  ")
        prompt_text = ET.tostring(root, encoding="unicode")
        manifest = {
            "project_file": str(self.project_file),
            "config_root": str(self.config_root),
            "prompt_config_root": str(self.prompt_config_root),
            "role_config_path": str(self.role_config_root / f"{assignment.role_id}.yaml"),
            "components": {
                key: str(self.prompt_config_root / "worker" / f"{key}.xml")
                for key in ("system-security", "safe-outputs", "instructions")
            },
            "safe_output_tools": sorted(self.tool_policy.tools_for_role(assignment.role_id)),
            "memory_context_count": len(assignment.memory_context),
        }
        return PromptRender(prompt_text=prompt_text, component_manifest=manifest)

    def _load_components(self) -> dict[str, str]:
        components: dict[str, str] = {}
        for name in ("system-security", "safe-outputs", "instructions"):
            path = self.prompt_config_root / "worker" / f"{name}.xml"
            if not path.exists():
                raise ValueError(f"prompt component missing: {path}")
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError(f"prompt component is empty: {path}")
            components[name] = text
        return components


def build_prompt_assembler_for_project(project_file: Path) -> PromptAssembler:
    config_root = Path(os.environ.get("AGENTIC_MESH_CONFIG_ROOT", Path.cwd()))
    prompt_root = os.environ.get("AGENTIC_MESH_PROMPT_CONFIG_ROOT")
    return PromptAssembler(
        project_file=project_file,
        config_root=config_root,
        prompt_config_root=Path(prompt_root) if prompt_root else None,
    )


def _load_yaml_mapping(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    if missing_ok and not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return raw if isinstance(raw, dict) else {}


def _text_section(parent: ET.Element, tag: str, text: str) -> None:
    element = ET.SubElement(parent, tag)
    element.text = text


def _safe_outputs_section(parent: ET.Element, *, component_text: str, tools: list[str]) -> None:
    section = ET.SubElement(parent, "safe-outputs")
    guidance = ET.SubElement(section, "guidance")
    guidance.text = component_text
    available = ET.SubElement(section, "available-tools")
    for tool in tools:
        item = ET.SubElement(available, "tool")
        item.text = tool


def _mapping_section(parent: ET.Element, tag: str, mapping: dict[str, Any]) -> None:
    section = ET.SubElement(parent, tag)
    for key in sorted(mapping):
        value = mapping[key]
        if value is None:
            continue
        child = ET.SubElement(section, _xml_tag(str(key)))
        if isinstance(value, dict):
            child.text = yaml.safe_dump(value, sort_keys=True).strip()
        elif isinstance(value, (list, tuple)):
            child.text = "\n".join(str(item) for item in value)
        else:
            child.text = str(value)


def _context_section(parent: ET.Element, tag: str, values: tuple[str, ...]) -> None:
    section = ET.SubElement(parent, tag)
    for value in values:
        item = ET.SubElement(section, "item")
        item.text = value


def _role_payload(assignment: RoleAssignment, role: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    project_role = {}
    roles = project.get("roles")
    if isinstance(roles, dict) and isinstance(roles.get(assignment.role_id), dict):
        project_role = roles[assignment.role_id]
    return {
        "role_id": assignment.role_id,
        "role_instance_id": assignment.role_instance_id,
        "purpose": role.get("purpose"),
        "role_profile": role.get("role_profile"),
        "accountabilities": role.get("accountabilities", []),
        "decision_rights": role.get("decision_rights", {}),
        "boundaries": role.get("boundaries", []),
        "collaboration_style": role.get("collaboration_style", []),
        "quality_bar": role.get("quality_bar", []),
        "memory_focus": role.get("memory_focus"),
        "standing_instructions": role.get("standing_instructions", []),
        "project_instructions": project_role.get("instructions", []),
        "write_paths": project_role.get("write_paths", []),
    }


def _project_payload(project: dict[str, Any]) -> dict[str, Any]:
    goal = project.get("goal") if isinstance(project.get("goal"), dict) else {}
    flow = project.get("flow") if isinstance(project.get("flow"), dict) else {}
    return {
        "project_id": project.get("project_id"),
        "name": project.get("name"),
        "goal": goal.get("description"),
        "success_measures": goal.get("success_measures", []),
        "constraints": goal.get("constraints", []),
        "flow_template": flow.get("template"),
        "document_library": project.get("document_library", {}),
        "role_memory": project.get("role_memory", {}),
    }


def _assignment_payload(assignment: RoleAssignment) -> dict[str, Any]:
    return {
        "assignment_id": assignment.assignment_id,
        "assignment_type": assignment.assignment_type,
        "work_item_id": assignment.work_item_id,
        "source_ref": assignment.source_ref,
        "visibility_scope": assignment.visibility_scope,
        "title": assignment.title,
        "summary": assignment.summary,
        "payload": assignment.payload or {},
    }


def _flow_state_payload(assignment: RoleAssignment, project: dict[str, Any]) -> dict[str, Any]:
    payload = assignment.payload or {}
    flow = project.get("flow") if isinstance(project.get("flow"), dict) else {}
    return {
        "assignment_type": assignment.assignment_type,
        "current_flow_state": payload.get("current_flow_state"),
        "context_visibility": payload.get("context_visibility") or assignment.visibility_scope,
        "flow_template": flow.get("template"),
        "source_documents": payload.get("source_documents", []),
        "target_outputs": payload.get("target_outputs", []),
    }


def _xml_tag(value: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in value).strip("-") or "value"
