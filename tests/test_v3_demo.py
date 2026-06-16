from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.demo import run_demo_slice
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter


def test_run_demo_slice_closes_work_and_writes_indexes(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    try:
        db.migrate()
        run_demo_slice(db, project_id="agentic-mesh-dev", document_library=docs)
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert snapshot.recent_completions[0].work_item_id == "work-v3-demo-slice"
    assert (tmp_path / "documents" / "work-items" / "work-v3-demo-slice" / "index.md").exists()
    assert (tmp_path / "documents" / "work-items" / "index.md").exists()
