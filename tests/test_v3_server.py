from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.server import V3StatusHandler
from agentic_mesh_v3.server import _artifact_page
from agentic_mesh_v3.tools import V3ToolService


def test_artifact_page_renders_markdown_safely() -> None:
    html = _artifact_page("work-items/work-1/index.md", "# Hello\n\n<script>alert(1)</script>")

    assert "<h1>Hello</h1>" in html
    assert "<script>" not in html


def test_status_handler_snapshot_uses_v3_db(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="work_item.upsert",
            payload={
                "work_item_id": "work-1",
                "title": "Add status page",
                "description": "Build status page",
                "state": "active",
                "owner_role": "engineering",
            },
        )
    finally:
        db.close()

    class Handler(V3StatusHandler):
        pass

    Handler.db_path = tmp_path / "v3.sqlite3"
    Handler.project_id = "agentic-mesh-dev"
    Handler.document_library = LocalDocumentLibraryAdapter(tmp_path / "documents")

    # Bypass BaseHTTPRequestHandler construction; _snapshot only uses class attrs.
    handler = object.__new__(Handler)
    snapshot = handler._snapshot()

    assert snapshot.work_items[0].work_item_id == "work-1"


def test_artifact_viewer_reads_local_document_library(tmp_path: Path) -> None:
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    docs.write_text("work-items/work-1/index.md", "# Work One")

    assert docs.read_text("work-items/work-1/index.md") == "# Work One"


def test_work_item_page_renders_detail_evidence(tmp_path: Path) -> None:
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db, docs)
        tools.call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="work_item.upsert",
            payload={
                "work_item_id": "work-1",
                "title": "Add status page",
                "description": "Build the V3 status page.",
                "state": "release_review",
                "owner_role": "release-manager",
                "current_phase": "deployment",
                "next_action": "Awaiting release approval.",
                "governance": {
                    "accountable_role": "release-manager",
                    "consulted_roles": ["qa-engineer"],
                },
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="document.write_work_item_index",
            payload={
                "work_item_id": "work-1",
                "title": "Add status page",
                "status": "release_review",
                "owner_role": "release-manager",
                "raci_summary": "release-manager A, qa-engineer C",
                "governance_state": "QA consulted",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="approval.request",
            payload={
                "approval_id": "approval-1",
                "work_item_id": "work-1",
                "question": "Approve release?",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="consult.request",
            payload={
                "work_item_id": "work-1",
                "target_role": "qa-engineer",
                "question": "Confirm smoke evidence remains valid.",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.record",
            payload={
                "release_id": "release-1",
                "work_item_id": "work-1",
                "status": "ready",
                "scope": "Status page",
                "deployment_result": "staging smoke passed",
                "rollback_plan": "restart previous image",
            },
        )
    finally:
        db.close()

    class Handler(V3StatusHandler):
        pass

    Handler.db_path = tmp_path / "v3.sqlite3"
    Handler.project_id = "agentic-mesh-dev"
    Handler.document_library = docs
    handler = object.__new__(Handler)

    html = handler._render_work_item("work-1")

    assert "Build the V3 status page." in html
    assert "release-manager" in html
    assert "qa-engineer" in html
    assert "Work item index" in html
    assert "/artifact-viewer/work-1/index.md" in html
    assert "consult.request" in html
    assert "Confirm smoke evidence remains valid." in html
    assert "approval-1" in html
    assert "Approve release?" in html
    assert "release-1" in html
    assert "staging smoke passed" in html
