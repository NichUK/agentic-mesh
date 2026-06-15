from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.deployment import NoDeploymentDisposition
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.tools import V3ToolService


def test_v3_tool_service_records_backlog_work_agent_and_release(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)
        tools.call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="backlog.upsert",
            payload={
                "queue_item_id": "queue-1",
                "title": "Add status page",
                "summary": "Build the V3 status page.",
                "owner_role": "project-manager",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="work_item.upsert",
            payload={
                "work_item_id": "work-1",
                "queue_item_id": "queue-1",
                "title": "Add status page",
                "description": "Build the V3 status page.",
                "state": "active",
                "owner_role": "engineering",
                "next_action": "Implement",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="agent.heartbeat",
            payload={"heartbeat_at": "2026-06-15T12:00:00Z", "current_work": "work-1", "inbox_depth": 2},
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.record",
            payload={
                "work_item_id": "work-1",
                "scope": "Status page",
                "deployment_result": "deployed locally",
                "rollback_plan": "restart previous image",
            },
        )
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert snapshot.backlog[0].queue_item_id == "queue-1"
    assert snapshot.work_items[0].work_item_id == "work-1"
    assert snapshot.agents[0].role_instance_id == "agentic-mesh-dev.engineering.1"


def test_v3_tool_service_writes_work_item_index_and_root_index(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
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
                "state": "active",
                "owner_role": "engineering",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="document.write_work_item_index",
            payload={
                "work_item_id": "work-1",
                "title": "Add status page",
                "status": "active",
                "owner_role": "engineering",
                "raci_summary": "engineering A/R",
                "governance_state": "qa consulted",
                "next_action": "Implement",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="document.write_root_work_item_index",
            payload={},
        )
    finally:
        db.close()

    assert (tmp_path / "documents" / "work-items" / "work-1" / "index.md").exists()
    assert (tmp_path / "documents" / "work-items" / "index.md").exists()


def test_v3_tool_service_rejects_unknown_tool(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)
        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.project-manager.1",
                tool_name="not.real",
                payload={},
            )
        except ValueError as exc:
            assert "unknown V3 tool" in str(exc)
        else:
            raise AssertionError("unknown tool should fail")
    finally:
        db.close()


def test_v3_tool_service_release_deploy_uses_configured_target(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Planning slice",
            description="No deployment required.",
            state="release_review",
            owner_role="release-manager",
        )
        tools = V3ToolService(
            db,
            deployment_targets={
                "planning-only": NoDeploymentDisposition(
                    target_id="planning-only",
                    reason="Design-only slice.",
                )
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.deploy",
            payload={
                "work_item_id": "work-1",
                "target_id": "planning-only",
                "scope": "No deployment design slice",
            },
        )
        release_count = db.connection.execute("SELECT COUNT(*) AS count FROM releases").fetchone()["count"]
    finally:
        db.close()

    assert release_count == 1
