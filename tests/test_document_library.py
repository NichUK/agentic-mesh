from pathlib import Path

from agentic_mesh.config import load_mesh_config
from agentic_mesh.document_library import build_document_manifest
from agentic_mesh.document_library import render_flow_mermaid
from agentic_mesh.document_library import resolve_document_library_root
from agentic_mesh.document_library import write_document_manifest


def test_document_library_root_resolves_independently_from_workspace() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    workspace_root = Path.cwd() / "examples" / "projects" / "agentic-mesh-dev"

    assert resolve_document_library_root(
        workspace_root,
        mesh_config.project.document_library,
    ) == Path.cwd()


def test_build_document_manifest_includes_plans_meshes_and_memory() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    manifest = build_document_manifest(mesh_config.project)
    documents = {doc["path"]: doc for doc in manifest["documents"]}

    assert manifest["document_library"]["structure_policy"] == "togaf-sdlc-v1"
    assert manifest["role_memory"]["provenance_required"] is True
    assert sorted(manifest["meshes"]) == ["governance", "sdlc"]
    implementation_plan = documents[
        "work-items/{work_item_id}/80-implementation-plan.md"
    ]
    assert implementation_plan["document_kind"] == "work_item_template"
    assert "implementation_planning" in implementation_plan["lifecycle_states"]
    assert "evidence" in implementation_plan["required_sections"]

    quality_plan = documents["work-items/{work_item_id}/90-quality-plan.md"]
    assert quality_plan["document_kind"] == "work_item_template"
    assert "quality_planning" in quality_plan["lifecycle_states"]


def test_write_document_manifest_uses_configured_index_path(tmp_path) -> None:
    mesh_config = load_mesh_config(
        Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
    )

    manifest_path = write_document_manifest(tmp_path, mesh_config.project)

    assert manifest_path == tmp_path / "docs" / "00-index" / (
        "document-library-manifest.json"
    )
    assert manifest_path.exists()


def test_render_flow_mermaid_includes_planning_and_review_loops() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    mermaid = render_flow_mermaid(mesh_config.project.flow)

    assert "flowchart LR" in mermaid
    assert "implementation planning" in mermaid
    assert "quality planning" in mermaid
    assert "plan review" in mermaid
