from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.dogfood import run_local_e2e_dogfood_slice
from agentic_mesh_v3.documents import build_document_library_adapter
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.project_config import V3DocumentLibraryConfig


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


def test_local_e2e_dogfood_slice_can_use_onedrive_document_library_urls(tmp_path: Path) -> None:
    transport = FakeGraphDocumentTransport()
    docs = build_document_library_adapter(
        V3DocumentLibraryConfig(
            adapter="onedrive",
            drive_id="drive-123",
            root_path="/documents",
            structure_policy="togaf-sdlc-v1",
        ),
        graph_base_url="https://graph.test/v1.0",
        transport=transport,
    )
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        work_item_id = run_local_e2e_dogfood_slice(db=db, document_library=docs)
        detail = db.work_item_detail(work_item_id)
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "closed"
    assert any(
        url == "https://graph.test/documents/work-items/work-v3-local-e2e/index.md"
        for url in (artifact.url for artifact in detail.artifacts)
    )
    assert (
        "https://graph.test/v1.0/drives/drive-123/root:/documents/work-items/work-v3-local-e2e/index.md:/content"
        in transport.text_by_url
    )
    assert (
        "https://graph.test/v1.0/drives/drive-123/root:/documents/work-items/index.md:/content"
        in transport.text_by_url
    )


class FakeGraphDocumentTransport:
    def __init__(self) -> None:
        self.text_by_url: dict[str, str] = {}

    def put_text(self, url: str, content: str) -> dict[str, object]:
        self.text_by_url[url] = content
        web_url = url.replace("https://graph.test/v1.0/drives/drive-123/root:", "https://graph.test")
        web_url = web_url.removesuffix(":/content")
        return {"webUrl": web_url}

    def get_text(self, url: str) -> str:
        return self.text_by_url[url]

    def exists(self, url: str) -> bool:
        return url in self.text_by_url
