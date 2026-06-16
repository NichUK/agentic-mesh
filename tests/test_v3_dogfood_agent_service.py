from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.dogfood import DogfoodSponsorContact
from agentic_mesh_v3.dogfood_agent_service import WORK_ITEM_ID
from agentic_mesh_v3.dogfood_agent_service import run_agent_service_e2e_dogfood_slice
from agentic_mesh_v3.dogfood_audit import audit_v3_dogfood_completion
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter


def test_v3_agent_service_dogfood_passes_completion_audit(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    try:
        db.migrate()
        work_item_id = run_agent_service_e2e_dogfood_slice(
            db=db,
            document_library=docs,
            runtime_state_dir=tmp_path / "runtime",
            sponsor_contact=DogfoodSponsorContact(connector="teams", target_ref="dm:sponsor"),
        )
        result = audit_v3_dogfood_completion(db=db, document_library=docs, work_item_id=work_item_id)
        detail = db.work_item_detail(WORK_ITEM_ID)
        runs = db.list_agent_runs()
    finally:
        db.close()

    assert work_item_id == WORK_ITEM_ID
    assert result.passed is True
    assert detail is not None
    assert detail.state == "closed"
    assert {run["role_instance_id"] for run in runs} >= {
        "agentic-mesh-dev.product-manager.1",
        "agentic-mesh-dev.engineering.1",
        "agentic-mesh-dev.solution-architect.1",
        "agentic-mesh-dev.qa-engineer.1",
        "agentic-mesh-dev.release-manager.1",
        "agentic-mesh-dev.project-manager.1",
    }
    assert all(run["status"] == "completed" for run in runs)
