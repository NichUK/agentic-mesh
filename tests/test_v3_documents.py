from pathlib import Path

from agentic_mesh_v3.documents import DocumentRef
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.documents import WorkItemIndex
from agentic_mesh_v3.documents import work_item_index_path
from agentic_mesh_v3.documents import write_root_work_item_index
from agentic_mesh_v3.documents import write_work_item_index


def test_work_item_index_path_uses_documents_work_item_shape() -> None:
    assert work_item_index_path("work-123") == "work-items/work-123/index.md"


def test_write_work_item_index(tmp_path: Path) -> None:
    adapter = LocalDocumentLibraryAdapter(tmp_path)
    index = WorkItemIndex(
        work_item_id="work-123",
        title="Add status page",
        status="active",
        owner_role="engineering",
        raci_summary="engineering A/R, qa-engineer C",
        governance_state="qa consulted",
        artifacts=(DocumentRef(relative_path="020-product-definition.md", title="Product definition"),),
        decisions=("Sponsor approved scope",),
        risks=("No staging environment yet",),
        next_action="Implement",
    )

    ref = write_work_item_index(adapter, index)

    assert ref.relative_path == "work-items/work-123/index.md"
    content = (tmp_path / "work-items" / "work-123" / "index.md").read_text(encoding="utf-8")
    assert "# Add status page" in content
    assert "qa consulted" in content
    assert "[Product definition](020-product-definition.md)" in content


def test_write_root_work_item_index(tmp_path: Path) -> None:
    adapter = LocalDocumentLibraryAdapter(tmp_path)
    write_root_work_item_index(
        adapter,
        [
            WorkItemIndex(
                work_item_id="work-123",
                title="Add status page",
                status="active",
                owner_role="engineering",
                raci_summary="engineering A/R",
                governance_state="ready",
            )
        ],
    )

    content = (tmp_path / "work-items" / "index.md").read_text(encoding="utf-8")
    assert "[Add status page](work-items/work-123/index.md)" in content
