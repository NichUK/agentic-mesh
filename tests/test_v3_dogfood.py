from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.dogfood import run_local_e2e_dogfood_slice
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter


def test_local_e2e_dogfood_slice_records_release_and_closure(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    try:
        db.migrate()
        work_item_id = run_local_e2e_dogfood_slice(db=db, document_library=docs)
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
        release = db.connection.execute(
            "SELECT status, deployment_result FROM releases WHERE work_item_id=?",
            (work_item_id,),
        ).fetchone()
        approval = db.approval_detail("approval-v3-local-product")
        detail = db.work_item_detail(work_item_id)
    finally:
        db.close()

    assert work_item_id == "work-v3-local-e2e"
    assert snapshot.backlog == ()
    assert snapshot.work_items == ()
    assert snapshot.recent_completions[0].work_item_id == work_item_id
    assert release["status"] == "deployed"
    assert "v3 local smoke deployed" in release["deployment_result"]
    assert approval is not None
    assert approval["status"] == "approved"
    assert detail is not None
    record_types = [record.record_type for record in detail.governance_records]
    assert "handoff.require" in record_types
    assert "consult.request" in record_types
    assert "informed.update" in record_types
    assert "decision.record" in record_types
    assert detail.governance_checklist is not None
    assert detail.governance_checklist.is_satisfied is True
    assert (tmp_path / "documents" / "work-items" / work_item_id / "index.md").exists()
    assert (tmp_path / "documents" / "work-items" / "index.md").exists()
