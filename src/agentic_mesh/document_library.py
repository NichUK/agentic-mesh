from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from agentic_mesh.models import DocumentLibraryConfig, ProjectConfig, SdlcFlow


def resolve_document_library_root(
    workspace_root: Path,
    library: DocumentLibraryConfig,
) -> Path:
    root = Path(library.root)
    if root.is_absolute():
        return root.resolve()
    return (workspace_root / root).resolve()


def resolve_role_memory_root(workspace_root: Path, project: ProjectConfig) -> Path:
    root = Path(project.role_memory.root)
    if root.is_absolute():
        return root.resolve()
    return (workspace_root / root).resolve()


def document_library_context(
    workspace_root: Path,
    project: ProjectConfig,
) -> dict[str, object]:
    library_root = resolve_document_library_root(workspace_root, project.document_library)
    memory_root = resolve_role_memory_root(workspace_root, project)
    return {
        "backend": project.document_library.backend,
        "root": project.document_library.root,
        "absolute_root": str(library_root),
        "structure_policy": project.document_library.structure_policy,
        "index_path": project.document_library.index_path,
        "review_log_standard": project.document_library.review_log_standard,
        "versioning": project.document_library.versioning,
        "role_memory": {
            "enabled": project.role_memory.enabled,
            "backend": project.role_memory.backend,
            "root": project.role_memory.root,
            "absolute_root": str(memory_root),
            "provenance_required": project.role_memory.provenance_required,
            "refresh_from_document_library": (
                project.role_memory.refresh_from_document_library
            ),
            "team_overlay_root": project.role_memory.team_overlay_root,
        },
    }


def build_document_manifest(project: ProjectConfig) -> dict[str, object]:
    documents = []
    indexed_paths: set[str] = set()
    for path, accountability in sorted(project.document_accountabilities.items()):
        states = [
            state_id
            for state_id, state in sorted(project.flow.states.items())
            if state.artifact_path == path
            or any(path in gate.required_documents for gate in state.gates)
        ]
        documents.append(
            {
                "path": path,
                "document_kind": "durable",
                "owner_role": accountability.owner_role,
                "accountability": accountability.accountability,
                "contributing_roles": accountability.contributing_roles,
                "required_sections": accountability.required_sections,
                "lifecycle_states": states,
                "review_on_contribution": accountability.review_on_contribution,
                "status": "configured",
            }
        )
        indexed_paths.add(path)

    for state_id, state in sorted(project.flow.states.items()):
        paths = {state.artifact_path}
        for gate in state.gates:
            paths.update(gate.required_documents)
        for path in sorted(paths):
            if path in indexed_paths:
                continue
            documents.append(
                {
                    "path": path,
                    "document_kind": "work_item_template",
                    "owner_role": state.owner_role,
                    "accountability": "accountable_owner",
                    "contributing_roles": [],
                    "required_sections": [
                        "objective",
                        "scope",
                        "assumptions",
                        "decisions",
                        "evidence",
                        "risks",
                        "review_log",
                        "next_step",
                    ],
                    "lifecycle_states": [state_id],
                    "review_on_contribution": True,
                    "status": "configured",
                }
            )
            indexed_paths.add(path)

    return {
        "project_id": project.project_id,
        "document_library": asdict(project.document_library),
        "role_memory": asdict(project.role_memory),
        "documents": documents,
        "meshes": {
            mesh_id: asdict(mesh)
            for mesh_id, mesh in sorted(project.meshes.items())
        },
        "flow": {
            "flow_id": project.flow.flow_id,
            "entry_state": project.flow.entry_state,
            "work_item_types": project.flow.work_item_types,
            "states": list(project.flow.states),
        },
    }


def write_document_manifest(
    workspace_root: Path,
    project: ProjectConfig,
) -> Path:
    library_root = resolve_document_library_root(workspace_root, project.document_library)
    manifest_path = (library_root / project.document_library.index_path).resolve()
    if library_root not in manifest_path.parents and manifest_path != library_root:
        raise ValueError(
            f"Document library index escapes library root: "
            f"{project.document_library.index_path}"
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(build_document_manifest(project), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def render_flow_mermaid(flow: SdlcFlow) -> str:
    lines = [
        "flowchart LR",
        f'  start(["{_label(flow.entry_state)}"])',
    ]
    for state_id, state in flow.states.items():
        lines.append(f'  {node_id(state_id)}["{_label(state_id)}<br/>{state.owner_role}"]')
        for gate in state.gates:
            gate_shape = "{" if gate.type in {"plan_review", "document_review"} else "["
            gate_end = "}" if gate_shape == "{" else "]"
            lines.append(
                f'  {node_id(state_id)}_{node_id(gate.gate_id)}'
                f'{gate_shape}"{_label(gate.gate_id)}<br/>{_label(gate.type)}"'
                f"{gate_end}"
            )
            lines.append(
                f"  {node_id(state_id)} --> "
                f"{node_id(state_id)}_{node_id(gate.gate_id)}"
            )
        for handoff in state.handoffs.values():
            edge = "-->"
            label = handoff.status
            if handoff.target_mesh:
                label = f"{label} / {handoff.target_mesh}"
            lines.append(
                f"  {node_id(state_id)} {edge}|{label}| {node_id(handoff.target_state)}"
            )
        for consult in state.consults.values():
            lines.append(
                f"  {node_id(state_id)} -. {consult.consult_id} .-> "
                f"{node_id(consult.target_state)}"
            )
    lines.append(f"  start --> {node_id(flow.entry_state)}")
    return "\n".join(lines) + "\n"


def node_id(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)


def _label(value: str) -> str:
    return value.replace("_", " ").replace("-", " ")
