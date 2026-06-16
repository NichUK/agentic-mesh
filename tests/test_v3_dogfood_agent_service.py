from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.dogfood import DogfoodSponsorContact
from agentic_mesh_v3.dogfood_agent_service import WORK_ITEM_ID
from agentic_mesh_v3.dogfood_agent_service import _ProductManagerWorker
from agentic_mesh_v3.dogfood_agent_service import run_agent_service_e2e_dogfood_slice
from agentic_mesh_v3.dogfood_audit import audit_v3_dogfood_completion
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.agent import AgentMessage
from agentic_mesh_v3.tools import V3ToolService


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


def test_v3_agent_service_product_manager_ignores_wrong_approval_response(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)
        worker = _ProductManagerWorker(tools, project_id="agentic-mesh-dev", sponsor_contact=None)

        calls = worker.run(
            "",
            AgentMessage(
                message_id="msg-wrong-approval",
                subject="agent.product-manager",
                payload={
                    "message_type": "approval.response_recorded",
                    "approval_id": "approval-unrelated",
                    "status": "approved",
                },
            ),
        )
        tool_calls = db.list_tool_calls()
    finally:
        db.close()

    assert calls
    assert {call["tool_name"] for call in tool_calls} == {"noop", "status.reply"}


def test_v3_agent_service_without_sponsor_contact_does_not_claim_notification(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    try:
        db.migrate()
        run_agent_service_e2e_dogfood_slice(
            db=db,
            document_library=docs,
            runtime_state_dir=tmp_path / "runtime",
            sponsor_contact=None,
        )
        project_manager_replies = [
            call["payload"]["text_markdown"]
            for call in db.list_tool_calls()
            if call["role_instance_id"] == "agentic-mesh-dev.project-manager.1"
            and call["tool_name"] == "status.reply"
        ]
    finally:
        db.close()

    assert project_manager_replies == ["Project closure recorded; no sponsor notification was configured."]
