from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.dogfood import DogfoodSponsorContact
from agentic_mesh_v3.dogfood import run_local_e2e_dogfood_slice
from agentic_mesh_v3.dogfood_audit import audit_v3_dogfood_completion
from agentic_mesh_v3.documents import build_document_library_adapter
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.project_config import V3DocumentLibraryConfig


def test_v3_dogfood_audit_passes_completed_local_slice(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    try:
        db.migrate()
        work_item_id = run_local_e2e_dogfood_slice(
            db=db,
            document_library=docs,
            sponsor_contact=DogfoodSponsorContact(connector="teams", target_ref="dm:sponsor"),
        )

        result = audit_v3_dogfood_completion(db=db, document_library=docs, work_item_id=work_item_id)
    finally:
        db.close()

    assert result.passed is True
    assert {check.check_id for check in result.checks} >= {
        "work_item.closed",
        "queue.closed",
        "approval.approved",
        "approval.delivered",
        "sponsor.closed_notification",
        "governance.handoff.require",
        "governance.consult.request",
        "governance.informed.update",
        "documents.work_item_index_exists",
        "release.deployed",
        "release.closed",
    }


def test_v3_dogfood_audit_requires_onedrive_artifact_urls(tmp_path: Path) -> None:
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
        work_item_id = run_local_e2e_dogfood_slice(
            db=db,
            document_library=docs,
            sponsor_contact=DogfoodSponsorContact(connector="teams", target_ref="dm:sponsor"),
        )

        result = audit_v3_dogfood_completion(
            db=db,
            document_library=docs,
            work_item_id=work_item_id,
            require_onedrive_artifacts=True,
        )
    finally:
        db.close()

    assert result.passed is True
    checks = {check.check_id: check for check in result.checks}
    assert checks["documents.onedrive_urls"].passed is True


def test_v3_dogfood_audit_fails_missing_work_item(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    try:
        db.migrate()
        result = audit_v3_dogfood_completion(db=db, document_library=docs, work_item_id="work-missing")
    finally:
        db.close()

    assert result.passed is False
    assert len(result.checks) == 1
    assert result.checks[0].check_id == "work_item.exists"
    assert result.checks[0].passed is False


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
        return url in self.text_by_url or f"{url}:/content" in self.text_by_url
