import json
import sys
from pathlib import Path

from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.deployment import CommandDeploymentTarget
from agentic_mesh_v3.deployment import DeploymentResult
from agentic_mesh_v3.deployment import NoDeploymentDisposition
from agentic_mesh_v3.documents import DocumentRef
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.reporting import AgentStatus
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
            payload={
                "heartbeat_at": "2026-06-15T12:00:00Z",
                "current_work": "work-1",
                "inbox_depth": 2,
                "dead_letter_depth": 1,
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="memory.propose_update",
            payload={
                "summary": "Status page work should keep memory visible in agent reporting.",
                "source_ref": "work-1/index.md",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.record",
            payload={
                "work_item_id": "work-1",
                "scope": "Status page",
                "version_ref": "commit:abc123",
                "approval_ref": "approval-release-1",
                "deployment_result": "deployed locally",
                "smoke_evidence": "GET /status passed.",
                "rollback_plan": "restart previous image",
            },
        )
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert snapshot.backlog[0].queue_item_id == "queue-1"
    assert snapshot.work_items[0].work_item_id == "work-1"
    assert snapshot.agents[0].role_instance_id == "agentic-mesh-dev.engineering.1"
    assert snapshot.agents[0].dead_letter_depth == 1
    assert snapshot.agents[0].memory_count == 1
    assert snapshot.agents[0].last_memory_at is not None


def test_v3_tool_service_compacts_conversation_context_with_privacy_guard(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.record_conversation_message(
            message_id="msg-1",
            connector="teams",
            conversation_ref="dm:product-manager",
            source_type="dm",
            sender_ref="sponsor",
            text="Keep dashboard rows compact.",
        )
        tools = V3ToolService(db)

        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="conversation.compact_context",
                payload={
                    "conversation_ref": "dm:product-manager",
                    "visibility": "shared",
                    "summary": "Sponsor prefers compact dashboard rows.",
                    "source_message_ids": ["msg-1"],
                },
            )
        except ValueError as exc:
            assert "DM conversation summaries can only be shared or promoted with durable_refs" in str(exc)
        else:
            raise AssertionError("private DM summaries should not become shared without durable refs")

        tools.call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="conversation.compact_context",
            payload={
                "conversation_ref": "dm:product-manager",
                "visibility": "promoted",
                "summary": "Sponsor prefers compact dashboard rows.",
                "source_message_ids": ["msg-1"],
                "durable_refs": ["work-items/work-1/index.md"],
            },
        )

        summaries = db.list_conversation_summaries("dm:product-manager")
    finally:
        db.close()

    assert len(summaries) == 1
    assert summaries[0]["visibility"] == "promoted"
    assert summaries[0]["source_message_ids"] == ("msg-1",)
    assert summaries[0]["durable_refs"] == ("work-items/work-1/index.md",)


def test_v3_tool_service_rejects_missing_contract_fields_before_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)

        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="backlog.upsert",
                payload={
                    "queue_item_id": "queue-1",
                    "title": "Add status page",
                    "owner_role": "product-manager",
                },
            )
        except ValueError as exc:
            assert "summary is required for backlog.upsert" in str(exc)
        else:
            raise AssertionError("backlog.upsert should require summary")

        calls = db.list_tool_calls()
    finally:
        db.close()

    assert calls == []


def test_v3_release_record_requires_evidence_metadata_before_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)

        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.release-manager.1",
                tool_name="release.record",
                payload={
                    "work_item_id": "work-1",
                    "scope": "Status page",
                    "deployment_result": "deployed locally",
                    "smoke_evidence": "GET /status passed.",
                    "rollback_plan": "restart previous image",
                },
            )
        except ValueError as exc:
            assert "version_ref is required for release.record" in str(exc)
        else:
            raise AssertionError("release.record should require version evidence")

        calls = db.list_tool_calls()
        release_count = db.connection.execute("SELECT COUNT(*) AS count FROM releases").fetchone()["count"]
    finally:
        db.close()

    assert calls == []
    assert release_count == 0


def test_v3_tool_service_rejects_unknown_tools_before_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)

        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="unknown.tool",
                payload={},
            )
        except PermissionError as exc:
            assert "unknown.tool" in str(exc)
        else:
            raise AssertionError("unknown tools should fail authority before recording")

        calls = db.list_tool_calls()
    finally:
        db.close()

    assert calls == []


def test_v3_work_item_upsert_materializes_minimum_governance_context(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="work_item.upsert",
            payload={
                "work_item_id": "work-1",
                "title": "Add status page",
                "description": "Build the V3 status page.",
                "state": "active",
                "owner_role": "engineering",
                "current_phase": "development",
            },
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.governance["phase"] == "development"
    assert detail.governance["accountable_role"] == "engineering"
    assert detail.governance["responsible_roles"] == ["engineering"]


def test_v3_status_update_records_visible_work_item_progress_without_state_transition(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)
        tools.call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="work_item.upsert",
            payload={
                "work_item_id": "work-1",
                "title": "Add status page",
                "description": "Build the V3 status page.",
                "state": "active",
                "owner_role": "engineering",
                "current_phase": "development",
                "next_action": "Implement.",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="status.update",
            payload={
                "work_item_id": "work-1",
                "message": "Implementation is complete; preparing handoff to QA.",
                "current_phase": "development",
            },
        )

        detail = db.work_item_detail("work-1")
        event_types = [
            row["event_type"]
            for row in db.connection.execute(
                "SELECT event_type FROM events WHERE aggregate_id='work-1' ORDER BY created_at ASC"
            )
        ]
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "active"
    assert detail.next_action == "Implementation is complete; preparing handoff to QA."
    assert "work_item.progress_updated" in event_types


def test_v3_status_update_without_work_item_remains_audit_only(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        result = V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="status.update",
            payload={"message": "Sweep complete."},
        )
        calls = db.list_tool_calls()
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert result.tool_name == "status.update"
    assert calls[0]["payload"]["message"] == "Sweep complete."
    assert snapshot.work_items == ()


def test_v3_tool_service_accepts_legacy_status_progress_alias(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Alias check",
            description="Older agent prompt emitted a legacy progress tool name.",
            state="waiting_agent",
            owner_role="product-manager",
        )
        tools = V3ToolService(db)

        result = tools.call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="status.report_progress",
            payload={
                "work_item_id": "work-1",
                "summary": "Progress recorded through legacy alias.",
            },
        )
        detail = db.work_item_detail("work-1")
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert result.tool_name == "status.update"
    assert detail is not None
    assert detail.next_action == "Progress recorded through legacy alias."
    assert calls[-1]["tool_name"] == "status.update"


def test_v3_work_item_upsert_preserves_supplied_governance_context(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="work_item.upsert",
            payload={
                "work_item_id": "work-1",
                "title": "Shape feature",
                "description": "Shape the feature before implementation.",
                "state": "shaping",
                "owner_role": "product-manager",
                "governance": {
                    "phase": "requirements",
                    "accountable_role": "project-manager",
                    "responsible_roles": ["business-analyst", "product-manager"],
                    "consulted_roles": ["solution-architect"],
                },
            },
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.governance["phase"] == "requirements"
    assert detail.governance["accountable_role"] == "project-manager"
    assert detail.governance["responsible_roles"] == ["business-analyst", "product-manager"]
    assert detail.governance["consulted_roles"] == ["solution-architect"]


def test_v3_tool_service_writes_work_item_index_and_refreshes_root_index(tmp_path: Path) -> None:
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
                "consultations": ["QA reviewed acceptance coverage."],
                "approvals": ["Sponsor approved product definition."],
                "evidence": ["Focused status page tests passed."],
                "next_action": "Implement",
            },
        )
    finally:
        db.close()

    assert (tmp_path / "documents" / "work-items" / "work-1" / "index.md").exists()
    root_index = tmp_path / "documents" / "work-items" / "index.md"
    assert root_index.exists()
    work_index = tmp_path / "documents" / "work-items" / "work-1" / "index.md"
    index_content = work_index.read_text(encoding="utf-8")
    assert "QA reviewed acceptance coverage." in index_content
    assert "Sponsor approved product definition." in index_content
    assert "Focused status page tests passed." in index_content
    assert "[Add status page](work-1/index.md) - `active` - engineering" in root_index.read_text(
        encoding="utf-8"
    )


def test_v3_tool_service_records_document_library_url_for_work_item_index(tmp_path: Path) -> None:
    class UrlReturningDocumentLibrary:
        framework_id = "togaf-sdlc-v1"

        def __init__(self) -> None:
            self.content_by_path: dict[str, str] = {}

        def write_text(self, relative_path: str, content: str) -> DocumentRef:
            self.content_by_path[relative_path] = content
            return DocumentRef(
                relative_path=relative_path,
                title=Path(relative_path).name,
                url=f"https://example.test/documents/{relative_path}",
            )

        def read_text(self, relative_path: str) -> str:
            return self.content_by_path[relative_path]

        def exists(self, relative_path: str) -> bool:
            return relative_path in self.content_by_path

    db = V3Database(tmp_path / "v3.sqlite3")
    docs = UrlReturningDocumentLibrary()
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Add status page",
            description="Build the V3 status page.",
            state="active",
            owner_role="engineering",
        )
        V3ToolService(db, docs).call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="document.write_work_item_index",
            payload={
                "work_item_id": "work-1",
                "title": "Add status page",
                "status": "active",
                "owner_role": "engineering",
                "raci_summary": "engineering A/R",
                "governance_state": "qa consulted",
                "evidence": ["Focused status page tests passed."],
            },
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.artifacts[0].url == "https://example.test/documents/work-items/work-1/index.md"


def test_v3_tool_service_links_existing_artifact(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Add status page",
            description="Build the V3 status page.",
            state="active",
            owner_role="engineering",
        )
        V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="artifact.link",
            payload={
                "work_item_id": "work-1",
                "relative_path": "work-items/work-1/100-implementation-log.md",
                "title": "Implementation log",
                "document_type": "implementation_log",
                "status": "published",
                "url": "https://example.test/documents/work-items/work-1/100-implementation-log.md",
            },
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert len(detail.artifacts) == 1
    artifact = detail.artifacts[0]
    assert artifact.filename == "100-implementation-log.md"
    assert artifact.title == "Implementation log"
    assert artifact.relative_path == "work-items/work-1/100-implementation-log.md"
    assert artifact.document_type == "implementation_log"
    assert artifact.status == "published"
    assert artifact.created_by_role == "engineering"
    assert artifact.url == "https://example.test/documents/work-items/work-1/100-implementation-log.md"


def test_v3_tool_service_writes_and_links_typed_document_artifact(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Shape product",
            description="Define the product slice.",
            state="active",
            owner_role="product-manager",
        )
        V3ToolService(db, document_library=docs).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="document.write_artifact",
            payload={
                "work_item_id": "work-1",
                "relative_path": "work-items/work-1/020-product-definition.md",
                "title": "Product definition",
                "document_type": "product_definition",
                "content_markdown": "# Product definition\n\nUseful product content.",
            },
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert docs.read_text("work-items/work-1/020-product-definition.md").startswith("# Product definition")
    assert detail is not None
    assert len(detail.artifacts) == 1
    artifact = detail.artifacts[0]
    assert artifact.filename == "020-product-definition.md"
    assert artifact.title == "Product definition"
    assert artifact.relative_path == "work-items/work-1/020-product-definition.md"
    assert artifact.document_type == "product_definition"
    assert artifact.status == "published"
    assert artifact.created_by_role == "product-manager"


def test_v3_tool_service_rejects_framework_artifact_path_mismatch(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Shape product",
            description="Define the product slice.",
            state="active",
            owner_role="product-manager",
        )
        tools = V3ToolService(db, document_library=LocalDocumentLibraryAdapter(tmp_path / "documents"))
        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="artifact.link",
                payload={
                    "work_item_id": "work-1",
                    "relative_path": "work-items/work-1/product.md",
                    "title": "Product definition",
                    "document_type": "product_definition",
                },
            )
        except ValueError as exc:
            assert "020-product-definition.md" in str(exc)
        else:
            raise AssertionError("typed artifacts should follow the configured document framework")
    finally:
        db.close()


def test_v3_tool_service_rejects_artifact_paths_outside_document_library(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)
        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.engineering.1",
                tool_name="artifact.link",
                payload={
                    "work_item_id": "work-1",
                    "relative_path": "../secrets.md",
                },
            )
        except ValueError as exc:
            assert "relative_path must stay inside the document library" in str(exc)
        else:
            raise AssertionError("artifact.link should reject paths outside the document library")
    finally:
        db.close()


def test_v3_tool_service_rejects_disallowed_or_unknown_tool(tmp_path: Path) -> None:
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
        except PermissionError as exc:
            assert "not.real" in str(exc)
        else:
            raise AssertionError("unknown tool should fail")
    finally:
        db.close()


def test_v3_db_rejects_invalid_work_item_transition(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Closed work",
            description="Already closed.",
            state="closed",
            owner_role="project-manager",
        )
        try:
            db.update_work_item_state(work_item_id="work-1", state="active")
        except ValueError as exc:
            assert "closed -> active" in str(exc)
        else:
            raise AssertionError("closed work should not reopen through normal transition")
    finally:
        db.close()


def test_v3_tool_service_allows_authorized_terminal_reopen(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_backlog_item(
            queue_item_id="queue-1",
            title="Dashboard",
            summary="Dashboard work.",
            status="superseded",
            owner_role="product-manager",
            linked_work_item_id="work-1",
        )
        db.upsert_work_item(
            work_item_id="work-1",
            queue_item_id="queue-1",
            title="Dashboard",
            description="Incorrectly superseded.",
            state="superseded",
            owner_role="product-manager",
        )

        V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="work_item.reopen",
            payload={
                "work_item_id": "work-1",
                "state": "shaping",
                "reason": "Sponsor confirmed the product work is still required.",
                "current_phase": "requirements",
                "next_action": "Rework product definition.",
            },
        )

        detail = db.work_item_detail("work-1")
        queue = db.connection.execute(
            "SELECT status FROM backlog_items WHERE queue_item_id='queue-1'"
        ).fetchone()
        event = db.connection.execute(
            "SELECT payload_json FROM events WHERE event_type='work_item.reopened'"
        ).fetchone()
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "shaping"
    assert detail.owner_role == "product-manager"
    assert detail.current_phase == "requirements"
    assert detail.next_action == "Rework product definition."
    assert queue["status"] == "shaping"
    assert "Sponsor confirmed" in event["payload_json"]


def test_v3_tool_service_reopen_requires_terminal_source(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Active work",
            description="Already active.",
            state="active",
            owner_role="engineering",
        )

        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.engineering.1",
                tool_name="work_item.reopen",
                payload={
                    "work_item_id": "work-1",
                    "state": "active",
                    "reason": "Try to reopen active work.",
                },
            )
        except ValueError as exc:
            assert "is not terminal" in str(exc)
        else:
            raise AssertionError("non-terminal work should not be reopened")

        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.release-manager.1",
                tool_name="work_item.reopen",
                payload={
                    "work_item_id": "work-1",
                    "state": "release_review",
                    "reason": "Release manager correction.",
                },
            )
        except ValueError as exc:
            assert "is not terminal" in str(exc)
        else:
            raise AssertionError("non-terminal work should not be reopened")
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
                "version_ref": "no-code-change:design-only",
                "approval_ref": "approval-design-release-1",
            },
        )
        release_count = db.connection.execute("SELECT COUNT(*) AS count FROM releases").fetchone()["count"]
        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert release_count == 1
    assert detail is not None
    assert detail.state == "released"
    assert detail.owner_role == "release-manager"
    assert detail.current_phase == "deployment"
    assert "Release disposition `no_deployment` recorded" in detail.next_action


def test_v3_release_deploy_success_moves_work_to_released(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Deploy runtime",
            description="Needs runtime deployment.",
            state="release_review",
            owner_role="release-manager",
        )
        tools = V3ToolService(
            db,
            deployment_targets={
                "runtime": CommandDeploymentTarget(
                    target_id="runtime",
                    command=(sys.executable, "-c", "print('deployment ok')"),
                    rollback_plan="Restore previous runtime image.",
                )
            },
        )

        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.deploy",
            payload={
                "work_item_id": "work-1",
                "target_id": "runtime",
                "scope": "Runtime release",
                "version_ref": "commit:abc123",
                "approval_ref": "approval-release-1",
                "smoke_evidence": "GET /healthz passed.",
            },
        )
        detail = db.work_item_detail("work-1")
        state_events = [
            json.loads(row["payload_json"])["state"]
            for row in db.connection.execute(
                """
                SELECT payload_json
                FROM events
                WHERE event_type='work_item.state_updated' AND aggregate_id='work-1'
                ORDER BY event_id
                """
            ).fetchall()
        ]
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "released"
    assert detail.owner_role == "release-manager"
    assert detail.current_phase == "deployment"
    assert detail.next_action == "Release disposition `deployed` recorded for target `runtime`; ready for closure."
    assert detail.releases[0].status == "deployed"
    assert detail.releases[0].version_ref == "commit:abc123"
    assert detail.releases[0].approval_ref == "approval-release-1"
    assert detail.releases[0].smoke_evidence == "GET /healthz passed."
    assert detail.releases[0].closure_state == "release_disposition_recorded"
    assert detail.releases[0].deployment_result == "deployment ok"
    assert state_events == ["deploying", "released"]


def test_v3_release_deploy_can_start_from_release_waiting_agent(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Deploy runtime",
            description="Needs runtime deployment.",
            state="waiting_agent",
            owner_role="release-manager",
            current_phase="deployment",
        )
        tools = V3ToolService(
            db,
            deployment_targets={
                "runtime": CommandDeploymentTarget(
                    target_id="runtime",
                    command=(sys.executable, "-c", "print('deployment ok')"),
                    rollback_plan="Restore previous runtime image.",
                )
            },
        )

        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.deploy",
            payload={
                "work_item_id": "work-1",
                "target_id": "runtime",
                "scope": "Runtime release",
                "version_ref": "commit:abc123",
                "approval_ref": "approval-release-1",
                "smoke_evidence": "GET /healthz passed.",
            },
        )
        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "released"
    assert detail.owner_role == "release-manager"
    assert detail.releases[0].status == "deployed"


def test_v3_release_deploy_unknown_target_fails_before_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Deploy runtime",
            description="Needs runtime deployment.",
            state="release_review",
            owner_role="release-manager",
        )
        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.release-manager.1",
                tool_name="release.deploy",
                payload={
                    "work_item_id": "work-1",
                    "target_id": "missing-target",
                    "scope": "Runtime release",
                    "version_ref": "commit:abc123",
                    "approval_ref": "approval-release-1",
                },
            )
        except ValueError as exc:
            assert "deployment target is not configured: missing-target" in str(exc)
        else:
            raise AssertionError("release.deploy should require a configured deployment target")

        detail = db.work_item_detail("work-1")
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "release_review"
    assert detail.releases == ()
    assert calls == []


def test_v3_release_deploy_failure_moves_work_to_recovering(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Deploy runtime",
            description="Needs runtime deployment.",
            state="release_review",
            owner_role="release-manager",
        )
        tools = V3ToolService(
            db,
            deployment_targets={
                "failing-target": CommandDeploymentTarget(
                    target_id="failing-target",
                    command=(sys.executable, "-c", "import sys; print('deploy failed'); sys.exit(7)"),
                    rollback_plan="Keep previous runtime active.",
                )
            },
        )

        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.deploy",
            payload={
                "work_item_id": "work-1",
                "target_id": "failing-target",
                "scope": "Runtime release",
                "version_ref": "commit:abc123",
                "approval_ref": "approval-release-1",
                "residual_risks": "Deployment failed.",
            },
        )
        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "recovering"
    assert detail.owner_role == "release-manager"
    assert detail.current_phase == "deployment"
    assert "Deployment target `failing-target` failed." in detail.next_action
    assert "deploy failed" in detail.next_action
    assert detail.releases[0].status == "failed"
    assert detail.releases[0].rollback_plan == "Keep previous runtime active."


def test_v3_release_deploy_retry_updates_failed_release_record(tmp_path: Path) -> None:
    class RetryTarget:
        def __init__(self) -> None:
            self.calls = 0

        def deploy(self) -> DeploymentResult:
            self.calls += 1
            if self.calls == 1:
                return DeploymentResult(
                    target_id="retry-target",
                    status="failed",
                    output="docker socket was unavailable",
                    rollback_plan="Keep previous runtime active.",
                )
            return DeploymentResult(
                target_id="retry-target",
                status="deployed",
                output="runtime restarted and smoke passed",
                rollback_plan="Restore previous runtime image.",
            )

    db = V3Database(tmp_path / "v3.sqlite3")
    target = RetryTarget()
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Deploy runtime",
            description="Needs runtime deployment.",
            state="release_review",
            owner_role="release-manager",
        )
        tools = V3ToolService(db, deployment_targets={"retry-target": target})
        payload = {
            "release_id": "release-work-1",
            "work_item_id": "work-1",
            "target_id": "retry-target",
            "scope": "Runtime release",
            "version_ref": "commit:abc123",
            "approval_ref": "approval-release-1",
            "smoke_evidence": "GET /healthz passed.",
        }

        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.deploy",
            payload=payload,
        )
        first_detail = db.work_item_detail("work-1")
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.deploy",
            payload=payload,
        )
        detail = db.work_item_detail("work-1")
        release_count = db.connection.execute("SELECT COUNT(*) AS count FROM releases").fetchone()["count"]
    finally:
        db.close()

    assert first_detail is not None
    assert first_detail.state == "recovering"
    assert detail is not None
    assert detail.state == "released"
    assert release_count == 1
    assert detail.releases[0].status == "deployed"
    assert detail.releases[0].deployment_result == "runtime restarted and smoke passed"
    assert detail.releases[0].rollback_plan == "Restore previous runtime image."


def test_v3_release_deploy_timeout_moves_work_to_recovering(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Deploy runtime",
            description="Needs runtime deployment.",
            state="release_review",
            owner_role="release-manager",
        )
        tools = V3ToolService(
            db,
            deployment_targets={
                "slow-target": CommandDeploymentTarget(
                    target_id="slow-target",
                    command=(sys.executable, "-c", "import time; time.sleep(5)"),
                    rollback_plan="Keep previous runtime active.",
                    timeout_seconds=1,
                )
            },
        )

        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.deploy",
            payload={
                "work_item_id": "work-1",
                "target_id": "slow-target",
                "scope": "Runtime release",
                "version_ref": "commit:abc123",
                "approval_ref": "approval-release-1",
                "residual_risks": "Deployment timed out.",
            },
        )
        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "recovering"
    assert "Deployment target `slow-target` failed." in detail.next_action
    assert "timed out after 1 seconds" in detail.next_action
    assert detail.releases[0].status == "failed"
    assert detail.releases[0].deployment_result.startswith("Deployment command timed out")


def test_v3_release_close_requires_release_disposition(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Release work",
            description="Needs release.",
            state="release_review",
            owner_role="release-manager",
        )
        tools = V3ToolService(db)

        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.release-manager.1",
                tool_name="release.close",
                payload={"work_item_id": "work-1"},
            )
        except ValueError as exc:
            assert "no deployment or no-deployment release disposition" in str(exc)
        else:
            raise AssertionError("release close should require release evidence")
    finally:
        db.close()


def test_v3_release_close_closes_after_no_deployment_disposition(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_backlog_item(
            queue_item_id="queue-1",
            title="Planning release",
            summary="Planning-only.",
            status="promoted",
            owner_role="product-manager",
            linked_work_item_id="work-1",
        )
        db.upsert_work_item(
            work_item_id="work-1",
            queue_item_id="queue-1",
            title="Planning release",
            description="Planning-only.",
            state="release_review",
            owner_role="release-manager",
        )
        tools = V3ToolService(
            db,
            deployment_targets={
                "planning-only": NoDeploymentDisposition(
                    target_id="planning-only",
                    reason="Planning-only slice.",
                )
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.deploy",
            payload={
                "work_item_id": "work-1",
                "target_id": "planning-only",
                "scope": "Planning-only release",
                "version_ref": "no-code-change:planning-only",
                "approval_ref": "approval-planning-release-1",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.close",
            payload={"work_item_id": "work-1"},
        )

        detail = db.work_item_detail("work-1")
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "closed"
    assert detail.owner_role == "project-manager"
    assert detail.releases[0].closure_state == "closed"
    assert snapshot.backlog == ()


def test_v3_release_close_closes_already_released_work(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Close deployed work",
            description="Deployment is already recorded.",
            state="released",
            owner_role="release-manager",
        )
        db.record_release(
            release_id="release-1",
            work_item_id="work-1",
            status="deployed",
            scope="Runtime release",
            deployment_result="ok",
            rollback_plan="Restore previous image.",
            residual_risks="None.",
            version_ref="commit:abc123",
            approval_ref="approval-release-1",
            smoke_evidence="GET /healthz passed.",
            closure_state="release_disposition_recorded",
        )
        tools = V3ToolService(db)

        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.close",
            payload={
                "work_item_id": "work-1",
                "closure_note": "Release deployed and closed.",
            },
        )
        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "closed"
    assert detail.owner_role == "project-manager"
    assert detail.current_phase == "project-closure"
    assert detail.next_action == "Release deployed and closed."
    assert detail.releases[0].closure_state == "closed"


def test_v3_terminal_work_item_state_syncs_linked_backlog_item(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_backlog_item(
            queue_item_id="queue-1",
            title="Build a status row",
            summary="Add a small status row.",
            status="promoted",
            owner_role="product-manager",
            linked_work_item_id="work-1",
        )
        db.upsert_work_item(
            work_item_id="work-1",
            queue_item_id="queue-1",
            title="Build a status row",
            description="Add a small status row.",
            state="active",
            owner_role="engineering",
        )

        db.update_work_item_state(work_item_id="work-1", state="superseded", next_action="Superseded by work-2.")

        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert snapshot.backlog == ()


def test_v3_status_snapshot_marks_attention_and_stale_work(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-blocked",
            title="Blocked release",
            description="Needs release fix.",
            state="blocked",
            owner_role="release-manager",
            next_action="Fix deployment target.",
        )
        db.upsert_work_item(
            work_item_id="work-stale",
            title="Stale implementation",
            description="No movement.",
            state="active",
            owner_role="engineering",
            next_action="Chase owner.",
        )
        db.connection.execute(
            "UPDATE work_items SET updated_at='2000-01-01 00:00:00' WHERE work_item_id='work-stale'"
        )
        db.connection.commit()

        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    attention = {item.work_item_id: item.attention_reason for item in snapshot.work_items}
    timestamps = {item.work_item_id: item.updated_at for item in snapshot.work_items}
    assert attention["work-blocked"] == "work item is in blocked"
    assert attention["work-stale"].startswith("work item has not changed for ")
    assert timestamps["work-stale"] == "2000-01-01 00:00:00"


def test_v3_status_snapshot_marks_unresolved_governance_attention(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-governance",
            title="Governed work",
            description="Needs governance evidence.",
            state="active",
            owner_role="engineering",
            current_phase="development",
            governance={
                "phase": "development",
                "accountable_role": "engineering",
                "responsible_roles": ["engineering"],
                "consulted_roles": ["qa-engineer"],
                "informed_roles": ["project-manager"],
                "sponsor_decision_points": ["product-signoff"],
                "required_evidence": ["100-implementation-log.md"],
            },
        )

        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    attention = {item.work_item_id: item.attention_reason for item in snapshot.work_items}
    assert attention["work-governance"] == (
        "governance checklist has unresolved items: "
        "consultations=qa-engineer; informed_updates=project-manager; "
        "sponsor_decisions=product-signoff; required_evidence=100-implementation-log.md"
    )


def test_v3_status_snapshot_prioritizes_waiting_state_over_governance(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-waiting",
            title="Waiting work",
            description="Waiting for another role.",
            state="waiting_agent",
            owner_role="engineering",
            current_phase="development",
            governance={
                "phase": "development",
                "accountable_role": "engineering",
                "responsible_roles": ["engineering"],
                "consulted_roles": ["qa-engineer"],
            },
        )

        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    attention = {item.work_item_id: item.attention_reason for item in snapshot.work_items}
    assert attention["work-waiting"] == "work item is in waiting_agent"


def test_v3_tool_service_records_governance_safe_outputs(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Governed work",
            description="Needs consultation.",
            state="active",
            owner_role="engineering",
        )
        tools = V3ToolService(db)
        tools.call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="consult.request",
            payload={
                "work_item_id": "work-1",
                "target_role": "qa-engineer",
                "question": "Please review the acceptance criteria.",
            },
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert len(detail.governance_records) == 1
    record = detail.governance_records[0]
    assert record.record_type == "consult.request"
    assert record.target_ref == "qa-engineer"
    assert record.status == "requested"
    assert record.summary == "Please review the acceptance criteria."


def test_v3_tool_service_records_decisions_and_risks_as_governance_evidence(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Governed delivery",
            description="Needs durable decision and risk evidence.",
            state="active",
            owner_role="project-manager",
        )
        tools = V3ToolService(db, docs)
        tools.call(
            role_instance_id="agentic-mesh-dev.solution-architect.1",
            tool_name="decision.record",
            payload={
                "work_item_id": "work-1",
                "summary": "Use NATS JetStream as the first V3 broker adapter.",
                "target_ref": "ADR-v3-broker",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="decision.record",
            payload={
                "work_item_id": "work-1",
                "decision": "Product scope remains unchanged after release validation feedback.",
                "target_ref": "product-release-check",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.security-architect.1",
            tool_name="risk.register",
            payload={
                "work_item_id": "work-1",
                "summary": "Graph token scopes may block Teams installation automation.",
                "target_ref": "risk-identity-consent",
                "impact": "Project onboarding may require manual tenant admin consent.",
                "mitigation": "Document required scopes and preflight tenant permissions.",
            },
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    decision_records = [record for record in detail.governance_records if record.record_type == "decision.record"]
    risk_records = {record.record_type: record for record in detail.governance_records if record.record_type != "decision.record"}
    assert any(
        record.summary == "Use NATS JetStream as the first V3 broker adapter."
        and record.status == "decision_recorded"
        and record.target_ref == "ADR-v3-broker"
        for record in decision_records
    )
    assert any(
        record.summary == "Product scope remains unchanged after release validation feedback."
        and record.status == "decision_recorded"
        and record.target_ref == "product-release-check"
        for record in decision_records
    )
    assert risk_records["risk.register"].summary == "Graph token scopes may block Teams installation automation."
    assert risk_records["risk.register"].status == "risk_open"
    assert risk_records["risk.register"].target_ref == "risk-identity-consent"
    decision_register = (tmp_path / "documents" / "decisions" / "index.md").read_text(encoding="utf-8")
    risk_register = (tmp_path / "documents" / "risks" / "index.md").read_text(encoding="utf-8")
    assert "# Decision Register" in decision_register
    assert "[work-1](../work-items/work-1/index.md)" in decision_register
    assert "Use NATS JetStream as the first V3 broker adapter." in decision_register
    assert "# Risk Register" in risk_register
    assert "[work-1](../work-items/work-1/index.md)" in risk_register
    assert "Graph token scopes may block Teams installation automation." in risk_register


def test_v3_tool_service_records_project_channel_relevance_without_work_item(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)

        result = tools.call(
            role_instance_id="agentic-mesh-dev.qa-engineer.1",
            tool_name="relevance.record",
            payload={
                "source_message_id": "msg-project-1",
                "conversation_ref": "team:team-1/channel:project",
                "relevance_score": 15,
                "rationale": "No QA action yet; monitor until acceptance criteria or test evidence changes.",
                "route_type": "project_channel_relevance_check",
            },
        )

        record = db.connection.execute(
            """
            SELECT work_item_id, record_type, role_instance_id, target_ref, summary, status, payload_json
            FROM governance_records
            WHERE record_id=?
            """,
            (f"governance-{result.call_id}",),
        ).fetchone()
        event = db.connection.execute(
            """
            SELECT aggregate_type, aggregate_id
            FROM events
            WHERE event_type='governance.recorded' AND aggregate_id='message:msg-project-1'
            """
        ).fetchone()
    finally:
        db.close()

    assert record is not None
    assert record["work_item_id"] == "message:msg-project-1"
    assert record["record_type"] == "relevance.record"
    assert record["role_instance_id"] == "agentic-mesh-dev.qa-engineer.1"
    assert record["target_ref"] == "msg-project-1"
    assert record["summary"] == "No QA action yet; monitor until acceptance criteria or test evidence changes."
    assert record["status"] == "relevance_recorded"
    assert event is not None
    assert event["aggregate_type"] == "conversation_message"


def test_v3_tool_service_relevance_record_rejects_invalid_score(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)

        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.qa-engineer.1",
                tool_name="relevance.record",
                payload={
                    "source_message_id": "msg-project-1",
                    "relevance_score": 101,
                    "rationale": "Too high.",
                },
            )
        except ValueError as exc:
            assert "relevance_score must be between 0 and 100" in str(exc)
        else:
            raise AssertionError("relevance.record should reject invalid scores")
    finally:
        db.close()


def test_v3_tool_service_raises_blocker_with_visible_next_action(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Deploy Teams connector",
            description="Needs tenant permission before deployment.",
            state="active",
            owner_role="platform-engineer",
            current_phase="deployment",
        )
        tools = V3ToolService(db)
        tools.call(
            role_instance_id="agentic-mesh-dev.platform-engineer.1",
            tool_name="blocker.raise",
            payload={
                "work_item_id": "work-1",
                "summary": "Tenant admin consent is missing for Graph application permissions.",
                "next_action": "Sponsor must grant admin consent or provide a tenant admin contact.",
                "current_phase": "deployment",
                "target_ref": "entra-admin-consent",
            },
        )

        detail = db.work_item_detail("work-1")
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "blocked"
    assert detail.owner_role == "platform-engineer"
    assert detail.next_action == "Sponsor must grant admin consent or provide a tenant admin contact."
    record = detail.governance_records[0]
    assert record.record_type == "blocker.raise"
    assert record.status == "blocked"
    assert record.target_ref == "entra-admin-consent"
    assert record.summary == "Tenant admin consent is missing for Graph application permissions."
    assert snapshot.work_items[0].attention_reason == "work item is in blocked"


def test_v3_database_builds_work_item_governance_checklist(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Governed work",
            description="Needs consultation and approval.",
            state="active",
            owner_role="engineering",
            current_phase="development",
            governance={
                "phase": "development",
                "accountable_role": "engineering",
                "responsible_roles": ["engineering"],
                "consulted_roles": ["qa-engineer"],
                "informed_roles": ["project-manager"],
                "sponsor_decision_points": ["product-signoff"],
                "required_evidence": ["100-implementation-log.md"],
            },
        )

        checklist = db.work_item_governance_checklist("work-1")

        assert checklist is not None
        assert checklist.missing_consultations == ("qa-engineer",)
        assert checklist.missing_informed_updates == ("project-manager",)
        assert checklist.pending_sponsor_decisions == ("product-signoff",)
        assert checklist.missing_required_evidence == ("100-implementation-log.md",)

        tools = V3ToolService(db)
        tools.call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="artifact.link",
            payload={
                "work_item_id": "work-1",
                "relative_path": "work-items/work-1/100-implementation-log.md",
                "filename": "100-implementation-log.md",
                "title": "Implementation log",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="consult.request",
            payload={
                "work_item_id": "work-1",
                "target_role": "qa-engineer",
                "question": "Please review the acceptance criteria.",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="informed.update",
            payload={
                "work_item_id": "work-1",
                "target_role": "project-manager",
                "message": "Development governance evidence is ready.",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="approval.request",
            payload={
                "approval_id": "product-signoff",
                "work_item_id": "work-1",
                "question": "Approve product-signoff?",
            },
        )
        db.record_approval_response(
            approval_id="product-signoff",
            status="approved",
            response="Approved",
            responder_ref="sponsor",
        )

        checklist = db.work_item_governance_checklist("work-1")
    finally:
        db.close()

    assert checklist is not None
    assert checklist.is_satisfied is True


def test_v3_tool_service_records_handoff_requirements_payload(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Governed handoff",
            description="Needs engineering implementation.",
            state="ready",
            owner_role="product-manager",
        )
        V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="handoff.require",
            payload={
                "work_item_id": "work-1",
                "target_role": "engineering",
                "phase": "development",
                "accountable_role": "engineering",
                "required_next_action": "Implement the signed-off product slice.",
                "acceptance_criteria": ["Feature behavior matches the signed-off product definition."],
                "evidence_requirements": ["Implementation log and focused tests are linked."],
                "artifact_links": ["work-items/work-1/020-product-definition.md"],
                "open_decisions": [],
                "open_risks": ["No staging environment is configured."],
                "consulted_roles": ["qa-engineer", "solution-architect"],
                "informed_roles": ["project-manager", "delivery-manager"],
                "stakeholder_follow_up": ["Ask sponsor if acceptance criteria change."],
            },
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "waiting_agent"
    assert detail.owner_role == "engineering"
    assert detail.current_phase == "development"
    assert detail.next_action == "Implement the signed-off product slice."
    assert detail.governance["phase"] == "development"
    assert detail.governance["accountable_role"] == "engineering"
    assert detail.governance["responsible_roles"] == ["engineering"]
    assert detail.governance["consulted_roles"] == ["qa-engineer", "solution-architect"]
    assert detail.governance["informed_roles"] == ["project-manager", "delivery-manager"]
    assert detail.governance["required_evidence"] == ["Implementation log and focused tests are linked."]
    assert len(detail.governance_records) == 1
    record = detail.governance_records[0]
    assert record.record_type == "handoff.require"
    assert record.target_ref == "engineering"
    assert record.status == "required"
    assert record.summary == "Implement the signed-off product slice."


def test_v3_tool_service_publishes_handoff_to_target_role_inbox(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.engineering"])
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Governed handoff",
            description="Needs engineering implementation.",
            state="ready",
            owner_role="product-manager",
        )
        V3ToolService(db, broker=broker, broker_stream="agent-inbox").call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="handoff.require",
            payload={
                "work_item_id": "work-1",
                "correlation_id": "corr-handoff-1",
                "target_role": "engineering",
                "phase": "development",
                "accountable_role": "engineering",
                "required_next_action": "Implement the signed-off product slice.",
                "acceptance_criteria": ["Feature behavior matches the signed-off product definition."],
                "evidence_requirements": ["Implementation log and focused tests are linked."],
                "artifact_links": ["work-items/work-1/020-product-definition.md"],
                "open_decisions": [],
                "open_risks": [],
                "consulted_roles": ["qa-engineer"],
                "informed_roles": ["project-manager"],
                "stakeholder_follow_up": [],
            },
        )

        pending = broker.pending("agent-inbox")
        detail = db.work_item_detail("work-1")
        journal_rows = db.connection.execute(
            """
            SELECT correlation_id, direction, stage, status, source_ref, target_role,
                   role_instance_id, work_item_id, broker_subject, summary
            FROM message_journal
            WHERE stage='published'
            """
        ).fetchall()
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "waiting_agent"
    assert detail.owner_role == "engineering"
    assert len(pending) == 1
    assert pending[0].subject == "agent.engineering"
    assert pending[0].payload["message_type"] == "handoff.require"
    assert pending[0].payload["work_item_id"] == "work-1"
    assert pending[0].payload["summary"] == "Implement the signed-off product slice."
    assert pending[0].payload["payload"]["target_role"] == "engineering"
    assert len(journal_rows) == 1
    assert journal_rows[0]["correlation_id"] == "corr-handoff-1"
    assert journal_rows[0]["direction"] == "broker"
    assert journal_rows[0]["status"] == "published"
    assert journal_rows[0]["source_ref"].startswith("call-")
    assert journal_rows[0]["target_role"] == "engineering"
    assert journal_rows[0]["role_instance_id"] == "agentic-mesh-dev.product-manager.1"
    assert journal_rows[0]["work_item_id"] == "work-1"
    assert journal_rows[0]["broker_subject"] == "agent.engineering"
    assert "Published handoff.require to engineering" in journal_rows[0]["summary"]


def test_v3_tool_service_delegates_lightweight_task_to_target_role_inbox(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    try:
        db.migrate()
        result = V3ToolService(db, broker=broker, broker_stream="agent-inbox").call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="agent.delegate",
            payload={
                "target_role": "platform-engineer",
                "task": "Inspect why the deployment source checkout is not writable.",
                "reason": "Project Manager is coordinating a runtime debugging request from the sponsor.",
                "expected_output": "Reply with a short diagnosis and either a fix, a blocker, or the next role to involve.",
                "work_item_id": "work-runtime-debug",
                "correlation_id": "corr-delegate-1",
                "context": "Runtime status inspection showed deployment recovery waiting on platform.",
            },
        )

        pending = broker.pending("agent-inbox")
        event = db.connection.execute(
            """
            SELECT payload_json
            FROM events
            WHERE event_type='agent.delegated'
            """
        ).fetchone()
        journal_rows = db.connection.execute(
            """
            SELECT correlation_id, direction, stage, status, target_role, role_instance_id,
                   work_item_id, broker_subject, broker_consumer, summary
            FROM message_journal
            WHERE stage='published'
            """
        ).fetchall()
    finally:
        db.close()

    assert result.tool_name == "agent.delegate"
    assert result.output is not None
    assert result.output["target_role"] == "platform-engineer"
    assert len(pending) == 1
    assert pending[0].subject == "agent.platform-engineer"
    assert pending[0].payload["message_type"] == "agent.delegate"
    assert pending[0].payload["task"] == "Inspect why the deployment source checkout is not writable."
    assert pending[0].payload["expected_output"].startswith("Reply with a short diagnosis")
    assert pending[0].payload["work_item_id"] == "work-runtime-debug"
    assert event is not None
    event_payload = json.loads(event["payload_json"])
    assert event_payload["target_role"] == "platform-engineer"
    assert event_payload["work_item_id"] == "work-runtime-debug"
    assert len(journal_rows) == 1
    assert journal_rows[0]["correlation_id"] == "corr-delegate-1"
    assert journal_rows[0]["direction"] == "broker"
    assert journal_rows[0]["status"] == "published"
    assert journal_rows[0]["target_role"] == "platform-engineer"
    assert journal_rows[0]["role_instance_id"] == "agentic-mesh-dev.project-manager.1"
    assert journal_rows[0]["work_item_id"] == "work-runtime-debug"
    assert journal_rows[0]["broker_subject"] == "agent.platform-engineer"
    assert journal_rows[0]["broker_consumer"] == "platform-engineer.1"
    assert "Delegated task to platform-engineer" in journal_rows[0]["summary"]


def test_v3_tool_service_publishes_consult_to_target_role_inbox(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.qa-engineer"])
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Governed work",
            description="Needs QA consultation.",
            state="active",
            owner_role="engineering",
        )
        V3ToolService(db, broker=broker, broker_stream="agent-inbox").call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="consult.request",
            payload={
                "work_item_id": "work-1",
                "target_role": "qa-engineer",
                "question": "Please review the acceptance criteria.",
            },
        )

        pending = broker.pending("agent-inbox")
        journal_rows = db.connection.execute(
            """
            SELECT direction, stage, status, target_role, role_instance_id,
                   work_item_id, broker_subject, summary
            FROM message_journal
            WHERE stage='published'
            """
        ).fetchall()
    finally:
        db.close()

    assert len(pending) == 1
    assert pending[0].subject == "agent.qa-engineer"
    assert pending[0].payload["message_type"] == "consult.request"
    assert pending[0].payload["summary"] == "Please review the acceptance criteria."
    assert len(journal_rows) == 1
    assert journal_rows[0]["direction"] == "broker"
    assert journal_rows[0]["status"] == "published"
    assert journal_rows[0]["target_role"] == "qa-engineer"
    assert journal_rows[0]["role_instance_id"] == "agentic-mesh-dev.engineering.1"
    assert journal_rows[0]["work_item_id"] == "work-1"
    assert journal_rows[0]["broker_subject"] == "agent.qa-engineer"
    assert "Published consult.request to qa-engineer" in journal_rows[0]["summary"]


def test_v3_tool_service_runtime_sweep_request_publishes_findings(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.project-manager"])
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-blocked",
            title="Blocked work",
            description="Needs project management follow-up.",
            state="blocked",
            owner_role="engineering",
            next_action="Resolve the blocker.",
        )

        result = V3ToolService(db, broker=broker, broker_stream="agent-inbox").call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="runtime.sweep.request",
            payload={"reason": "Sponsor asked Project Manager to check the mesh health."},
        )

        pending = broker.pending("agent-inbox")
        event = db.connection.execute(
            """
            SELECT payload_json
            FROM events
            WHERE event_type='runtime.sweep_requested'
            """
        ).fetchone()
    finally:
        db.close()

    assert result.tool_name == "runtime.sweep.request"
    assert len(pending) == 1
    assert pending[0].subject == "agent.project-manager"
    assert pending[0].payload["message_type"] == "project_sweep.finding"
    assert pending[0].payload["work_item_id"] == "work-blocked"
    assert event is not None
    payload = json.loads(event["payload_json"])
    assert payload["finding_count"] == 1
    assert payload["published_message_count"] == 1


def test_v3_tool_service_runtime_broker_inspect_returns_role_depths(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.project-manager", "agent.release-manager"])
    broker.publish("agent-inbox", "agent.release-manager", {"work_item_id": "work-release"})
    broker.publish("agent-inbox", "agent.release-manager", {"work_item_id": "work-release-2"})
    broker.publish("agent-inbox", "agent.project-manager", {"request": "status"})
    try:
        db.migrate()
        result = V3ToolService(db, broker=broker, broker_stream="agent-inbox").call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="runtime.broker.inspect",
            payload={
                "reason": "Project Manager is checking stuck work routing.",
                "role_ids": ["project-manager", "release-manager"],
                "limit": 1,
            },
        )
        event = db.connection.execute(
            """
            SELECT payload_json
            FROM events
            WHERE event_type='runtime.broker_inspected'
            """
        ).fetchone()
        journal = db.connection.execute(
            """
            SELECT message_id
            FROM message_journal
            WHERE stage='tool_call_recorded'
            """
        ).fetchall()
    finally:
        db.close()

    assert result.tool_name == "runtime.broker.inspect"
    assert result.output is not None
    inspection = result.output["inspection"]
    assert inspection["stream"] == "agent-inbox"
    role_counts = {
        item["role_id"]: item["pending_count"]
        for item in inspection["role_consumers"]
    }
    assert role_counts == {"project-manager": 1, "release-manager": 2}
    assert len(inspection["pending"]) == 1
    assert event is not None
    event_payload = json.loads(event["payload_json"])
    assert event_payload["reason"] == "Project Manager is checking stuck work routing."
    assert any(row["message_id"].startswith("runtime-broker-inspect-") for row in journal)


def test_v3_tool_service_runtime_status_inspect_returns_mesh_status(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_backlog_item(
            queue_item_id="queue-status",
            title="Inspect runtime",
            summary="Project Manager needs a read-only status view.",
            status="queued",
            owner_role="project-manager",
        )
        db.upsert_work_item(
            work_item_id="work-status",
            title="Runtime status inspection",
            description="Expose runtime status to agents.",
            state="waiting_agent",
            owner_role="delivery-manager",
            next_action="Delivery Manager needs to coordinate the next step.",
        )
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.project-manager.1",
                container_state="running",
                heartbeat_at=None,
                inbox_depth=2,
                current_work="work-status",
            )
        )

        result = V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="runtime.status.inspect",
            payload={"reason": "Sponsor asked Project Manager what is happening."},
        )
        event = db.connection.execute(
            """
            SELECT payload_json
            FROM events
            WHERE event_type='runtime.status_inspected'
            """
        ).fetchone()
        journal = db.connection.execute(
            """
            SELECT message_id, summary
            FROM message_journal
            WHERE stage='tool_call_recorded'
            """
        ).fetchall()
    finally:
        db.close()

    assert result.tool_name == "runtime.status.inspect"
    assert result.output is not None
    inspection = result.output["inspection"]
    assert inspection["counts"]["backlog"] == 1
    assert inspection["counts"]["current_work"] == 1
    assert inspection["counts"]["agents"] == 1
    assert inspection["work_items"][0]["work_item_id"] == "work-status"
    assert inspection["agents"][0]["role_instance_id"] == "agentic-mesh-dev.project-manager.1"
    assert inspection["agents"][0]["inbox_depth"] == 2
    assert event is not None
    event_payload = json.loads(event["payload_json"])
    assert event_payload["reason"] == "Sponsor asked Project Manager what is happening."
    assert any(row["message_id"].startswith("runtime-status-inspect-") for row in journal)
    assert any("Runtime status inspected" in row["summary"] for row in journal)


def test_v3_tool_service_rejects_incomplete_handoff_requirements(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)
        try:
            tools.call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="handoff.require",
                payload={
                    "work_item_id": "work-1",
                    "target_role": "engineering",
                    "required_next_action": "Implement this.",
                },
            )
        except ValueError as exc:
            assert "phase is required for handoff.require" in str(exc)
        else:
            raise AssertionError("incomplete handoff requirements should fail validation")
    finally:
        db.close()


def test_v3_tool_service_rejects_incomplete_governance_communication_payloads(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)
        cases = [
            (
                "consult.request",
                {"work_item_id": "work-1", "question": "Review this?"},
                "target_role is required",
            ),
            (
                "informed.update",
                {"work_item_id": "work-1", "target_role": "project-manager"},
                "message is required",
            ),
            (
                "stakeholder.ask_question",
                {"work_item_id": "work-1"},
                "question is required",
            ),
            (
                "governance.record_exception",
                {"work_item_id": "work-1"},
                "reason is required",
            ),
        ]
        for tool_name, payload, expected_message in cases:
            try:
                tools.call(
                    role_instance_id="agentic-mesh-dev.engineering.1",
                    tool_name=tool_name,
                    payload=payload,
                )
            except ValueError as exc:
                assert expected_message in str(exc)
            else:
                raise AssertionError(f"{tool_name} should reject incomplete governance payload")
    finally:
        db.close()


def test_v3_tool_service_stakeholder_question_delivers_when_target_is_present(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Question work",
            description="Needs sponsor input.",
            state="active",
            owner_role="product-manager",
        )
        V3ToolService(db, stakeholder_bridge=bridge).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="stakeholder.ask_question",
            payload={
                "work_item_id": "work-1",
                "question": "Which sponsor-visible channel should be used?",
                "connector": "teams",
                "stakeholder_ref": "dm:sponsor",
                "thread_ref": "thread-1",
                "waiting_owner_role": "sponsor",
                "current_phase": "product-shaping",
            },
        )
        detail = db.work_item_detail("work-1")
        deliveries = db.list_outbound_deliveries("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "waiting_human"
    assert detail.owner_role == "sponsor"
    assert detail.current_phase == "product-shaping"
    assert detail.next_action == "Awaiting stakeholder answer: Which sponsor-visible channel should be used?"
    assert detail.governance_records[0].record_type == "stakeholder.ask_question"
    assert detail.governance_records[0].record_id.startswith("question-")
    assert detail.governance_records[0].target_ref == "dm:sponsor"
    assert bridge.deliveries[0].target_ref == "dm:sponsor"
    assert "Which sponsor-visible channel" in bridge.deliveries[0].text_markdown
    assert "Question ID: `question-" in bridge.deliveries[0].text_markdown
    assert len(deliveries) == 1
    assert deliveries[0]["purpose"] == "stakeholder.ask_question"
    assert deliveries[0]["work_item_id"] == "work-1"
    assert deliveries[0]["target_ref"] == "dm:sponsor"
    assert deliveries[0]["thread_ref"] == "thread-1"
    assert detail.deliveries[0].purpose == "stakeholder.ask_question"


def test_v3_tool_service_stakeholder_question_appends_question_id_to_custom_markdown(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Question work",
            description="Needs sponsor input.",
            state="active",
            owner_role="product-manager",
        )
        V3ToolService(db, stakeholder_bridge=bridge).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="stakeholder.ask_question",
            payload={
                "work_item_id": "work-1",
                "question": "Which sponsor-visible channel should be used?",
                "connector": "teams",
                "stakeholder_ref": "dm:sponsor",
                "text_markdown": "**Please review**",
            },
        )
        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    question_id = detail.governance_records[0].record_id
    assert bridge.deliveries[0].text_markdown == f"**Please review**\n\nQuestion ID: `{question_id}`"


def test_v3_db_records_stakeholder_question_response_and_wakes_asking_role(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Question work",
            description="Needs sponsor input.",
            state="waiting_human",
            owner_role="sponsor",
            current_phase="product-shaping",
        )
        db.record_governance_record(
            record_id="question-1",
            work_item_id="work-1",
            record_type="stakeholder.ask_question",
            role_instance_id="agentic-mesh-dev.product-manager.1",
            target_ref="dm:sponsor",
            summary="Which sponsor-visible channel should be used?",
            status="requested",
            payload={"question": "Which sponsor-visible channel should be used?"},
        )

        response = db.record_governance_response(
            record_id="question-1",
            response="question-1: use direct messages for approvals.",
            status="answered",
            responder_ref="sponsor",
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert response["work_item_id"] == "work-1"
    assert response["role_instance_id"] == "agentic-mesh-dev.product-manager.1"
    assert detail is not None
    assert detail.state == "waiting_agent"
    assert detail.owner_role == "product-manager"
    assert detail.current_phase == "product-shaping"
    assert detail.governance_records[0].status == "answered"
    assert detail.next_action == "Stakeholder response recorded for `question-1`; awaiting product-manager to continue."


def test_v3_tool_service_stakeholder_question_with_target_requires_bridge(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Question work",
            description="Needs sponsor input.",
            state="active",
            owner_role="product-manager",
        )
        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="stakeholder.ask_question",
                payload={
                    "work_item_id": "work-1",
                    "question": "Which sponsor-visible channel should be used?",
                    "connector": "teams",
                    "stakeholder_ref": "dm:sponsor",
                },
            )
        except ValueError as exc:
            assert "stakeholder bridge is not configured" in str(exc)
        else:
            raise AssertionError("targeted stakeholder question should require a bridge")
        detail = db.work_item_detail("work-1")
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "active"
    assert detail.governance_records == ()
    assert calls == []


def test_v3_tool_service_stakeholder_question_target_requires_connector_before_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Question work",
            description="Needs sponsor input.",
            state="active",
            owner_role="product-manager",
        )
        try:
            V3ToolService(db, stakeholder_bridge=bridge).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="stakeholder.ask_question",
                payload={
                    "work_item_id": "work-1",
                    "question": "Which sponsor-visible channel should be used?",
                    "stakeholder_ref": "dm:sponsor",
                },
            )
        except ValueError as exc:
            assert "connector is required for targeted stakeholder.ask_question" in str(exc)
        else:
            raise AssertionError("targeted stakeholder.ask_question should require connector")

        detail = db.work_item_detail("work-1")
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "active"
    assert detail.governance_records == ()
    assert calls == []


def test_v3_tool_service_messaging_send_uses_stakeholder_bridge(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        result = V3ToolService(db, stakeholder_bridge=bridge).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="messaging.send",
            payload={
                "connector": "teams",
                "target_ref": "dm:sponsor",
                "text_markdown": "**Please review**",
                "thread_ref": "thread-1",
                "importance": "high",
            },
        )
        deliveries = db.list_outbound_deliveries()
    finally:
        db.close()

    assert result.tool_name == "messaging.send"
    assert bridge.deliveries[0].target_ref == "dm:sponsor"
    assert bridge.deliveries[0].text_markdown == "**Please review**"
    assert bridge.deliveries[0].importance == "high"
    assert len(deliveries) == 1
    assert deliveries[0]["call_id"] == result.call_id
    assert deliveries[0]["purpose"] == "messaging.send"
    assert deliveries[0]["status"] == "sent"


def test_v3_tool_service_messaging_send_requires_bridge(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="messaging.send",
                payload={
                    "connector": "teams",
                    "target_ref": "dm:sponsor",
                    "text_markdown": "This should not pretend to send.",
                },
            )
        except ValueError as exc:
            assert "stakeholder bridge is not configured" in str(exc)
        else:
            raise AssertionError("messaging.send should require a stakeholder bridge")
    finally:
        db.close()


def test_v3_tool_service_status_reply_delivers_when_target_is_present(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        result = V3ToolService(db, stakeholder_bridge=bridge).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="status.reply",
            payload={
                "connector": "teams",
                "target_ref": "dm:sponsor",
                "text_markdown": "Product Manager reply.",
                "thread_ref": "thread-1",
            },
        )
        deliveries = db.list_outbound_deliveries()
    finally:
        db.close()

    assert result.terminal is True
    assert bridge.deliveries[0].target_ref == "dm:sponsor"
    assert bridge.deliveries[0].text_markdown == "Product Manager reply."
    assert bridge.deliveries[0].thread_ref == "thread-1"
    assert len(deliveries) == 1
    assert deliveries[0]["purpose"] == "status.reply"
    assert deliveries[0]["target_ref"] == "dm:sponsor"


def test_v3_tool_service_targeted_status_reply_raises_when_delivery_fails(tmp_path: Path) -> None:
    class FailingBridge:
        def send(self, message):  # type: ignore[no-untyped-def]
            raise RuntimeError("Graph DM failed")

        def route_inbound(self, message):  # type: ignore[no-untyped-def]
            return []

    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        try:
            V3ToolService(db, stakeholder_bridge=FailingBridge()).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="status.reply",
                payload={
                    "connector": "teams",
                    "target_ref": "user:sponsor-user",
                    "text_markdown": "Product Manager reply.",
                },
            )
        except RuntimeError as exc:
            assert "Graph DM failed" in str(exc)
        else:
            raise AssertionError("targeted status.reply should fail when Teams delivery fails")
        calls = db.list_tool_calls()
        deliveries = db.list_outbound_deliveries()
    finally:
        db.close()

    assert len(calls) == 1
    assert calls[0]["tool_name"] == "status.reply"
    assert len(deliveries) == 1
    assert deliveries[0]["purpose"] == "status.reply"
    assert deliveries[0]["status"] == "failed"
    assert "Graph DM failed" in deliveries[0]["target_ref"]


def test_v3_tool_service_status_reply_uses_source_reply_context(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        result = V3ToolService(db, stakeholder_bridge=bridge).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="status.reply",
            payload={
                "connector": "teams",
                "reply_target_ref": "team:team-1/channel:channel-1",
                "reply_thread_ref": "root-message-1",
                "text_markdown": "Product Manager threaded reply.",
            },
        )
        deliveries = db.list_outbound_deliveries()
    finally:
        db.close()

    assert result.terminal is True
    assert bridge.deliveries[0].target_ref == "team:team-1/channel:channel-1"
    assert bridge.deliveries[0].thread_ref == "root-message-1"
    assert bridge.deliveries[0].text_markdown == "Product Manager threaded reply."
    assert deliveries[0]["purpose"] == "status.reply"
    assert deliveries[0]["target_ref"] == "team:team-1/channel:channel-1"
    assert deliveries[0]["thread_ref"] == "root-message-1"


def test_v3_tool_service_status_reply_without_target_remains_audit_only(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        result = V3ToolService(db, stakeholder_bridge=bridge).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="status.reply",
            payload={"text_markdown": "MCP-local reply."},
        )
        deliveries = db.list_outbound_deliveries()
    finally:
        db.close()

    assert result.terminal is True
    assert bridge.deliveries == []
    assert deliveries == []


def test_v3_tool_service_status_reply_with_target_requires_bridge(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="status.reply",
                payload={"connector": "teams", "target_ref": "dm:sponsor", "text_markdown": "Reply."},
            )
        except ValueError as exc:
            assert "stakeholder bridge is not configured" in str(exc)
        else:
            raise AssertionError("status.reply with target should require a stakeholder bridge")
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert calls == []


def test_v3_tool_service_status_reply_target_requires_connector_before_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        try:
            V3ToolService(db, stakeholder_bridge=bridge).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="status.reply",
                payload={"target_ref": "dm:sponsor", "text_markdown": "Reply."},
            )
        except ValueError as exc:
            assert "connector is required for targeted status.reply" in str(exc)
        else:
            raise AssertionError("targeted status.reply should require connector")

        calls = db.list_tool_calls()
    finally:
        db.close()

    assert calls == []


def test_v3_tool_service_status_reply_requires_markdown_before_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="status.reply",
                payload={},
            )
        except ValueError as exc:
            assert "text_markdown is required for status.reply" in str(exc)
        else:
            raise AssertionError("status.reply should require text_markdown")

        calls = db.list_tool_calls()
    finally:
        db.close()

    assert calls == []


def test_v3_tool_service_terminal_tools_require_context_before_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)

        for tool_name, expected in (
            ("status.complete", "summary is required for status.complete"),
            ("report.incomplete", "reason is required for report.incomplete"),
        ):
            try:
                tools.call(
                    role_instance_id="agentic-mesh-dev.product-manager.1",
                    tool_name=tool_name,
                    payload={},
                )
            except ValueError as exc:
                assert expected in str(exc)
            else:
                raise AssertionError(f"{tool_name} should require context")

        calls = db.list_tool_calls()
    finally:
        db.close()

    assert calls == []


def test_v3_tool_service_noop_defaults_missing_reason(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        result = V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.delivery-manager.1",
            tool_name="noop",
            payload={},
        )
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert result.tool_name == "noop"
    assert calls[0]["payload"]["reason"] == "No durable action was applicable for this assignment."


def test_v3_db_records_approval_response(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Approval work",
            description="Needs approval.",
            state="waiting_human",
            owner_role="product-manager",
        )
        db.request_approval(
            approval_id="approval-1",
            work_item_id="work-1",
            requested_by_role="product-manager",
            question="Approve product definition?",
        )

        db.record_approval_response(
            approval_id="approval-1",
            status="approved",
            response="Approved by sponsor.",
            responder_ref="nicholas",
        )

        detail = db.work_item_detail("work-1")
        approval = db.approval_detail("approval-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "waiting_agent"
    assert detail.owner_role == "product-manager"
    assert detail.next_action == "Approval `approval-1` recorded as `approved`; awaiting product-manager to continue."
    assert detail.approvals[0].status == "approved"
    assert detail.approvals[0].response == "Approved by sponsor."
    assert approval is not None
    assert approval["requested_by_role"] == "product-manager"
    assert approval["work_item_id"] == "work-1"


def test_v3_tool_service_approval_request_delivers_when_target_is_present(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Approval work",
            description="Needs approval.",
            state="waiting_human",
            owner_role="product-manager",
        )
        V3ToolService(db, stakeholder_bridge=bridge).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="approval.request",
            payload={
                "approval_id": "approval-1",
                "work_item_id": "work-1",
                "question": "Approve product definition?",
                "connector": "teams",
                "target_ref": "dm:sponsor",
                "thread_ref": "thread-1",
            },
        )
        detail = db.work_item_detail("work-1")
        deliveries = db.list_outbound_deliveries("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.approvals[0].approval_id == "approval-1"
    assert detail.state == "waiting_human"
    assert detail.owner_role == "sponsor"
    assert detail.next_action == "Approval `approval-1` requested by product-manager; awaiting sponsor response."
    assert bridge.deliveries[0].target_ref == "dm:sponsor"
    assert "Approve product definition?" in bridge.deliveries[0].text_markdown
    assert "approval-1" in bridge.deliveries[0].text_markdown
    assert bridge.deliveries[0].importance == "high"
    assert len(deliveries) == 1
    assert deliveries[0]["purpose"] == "approval.request"
    assert deliveries[0]["target_ref"] == "dm:sponsor"
    assert detail.deliveries[0].purpose == "approval.request"


def test_v3_tool_service_approval_request_moves_active_work_to_waiting_human(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Approval work",
            description="Needs approval.",
            state="active",
            owner_role="product-manager",
            current_phase="requirements",
        )
        V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="approval.request",
            payload={
                "approval_id": "approval-1",
                "work_item_id": "work-1",
                "question": "Approve product definition?",
                "waiting_owner_role": "sponsor",
                "current_phase": "requirements",
            },
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "waiting_human"
    assert detail.owner_role == "sponsor"
    assert detail.current_phase == "requirements"
    assert detail.approvals[0].status == "awaiting_response"


def test_v3_tool_service_approval_request_with_target_requires_bridge(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Approval work",
            description="Needs approval.",
            state="waiting_human",
            owner_role="product-manager",
        )
        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="approval.request",
                payload={
                    "approval_id": "approval-1",
                    "work_item_id": "work-1",
                    "question": "Approve product definition?",
                    "connector": "teams",
                    "target_ref": "dm:sponsor",
                },
            )
        except ValueError as exc:
            assert "stakeholder bridge is not configured" in str(exc)
        else:
            raise AssertionError("approval.request with target should require a stakeholder bridge")
        detail = db.work_item_detail("work-1")
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert detail is not None
    assert detail.approvals == ()
    assert calls == []


def test_v3_tool_service_approval_request_target_requires_connector_before_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Approval work",
            description="Needs approval.",
            state="waiting_human",
            owner_role="product-manager",
        )
        try:
            V3ToolService(db, stakeholder_bridge=bridge).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="approval.request",
                payload={
                    "approval_id": "approval-1",
                    "work_item_id": "work-1",
                    "question": "Approve product definition?",
                    "target_ref": "dm:sponsor",
                },
            )
        except ValueError as exc:
            assert "connector is required for targeted approval.request" in str(exc)
        else:
            raise AssertionError("targeted approval.request should require connector")

        detail = db.work_item_detail("work-1")
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert detail is not None
    assert detail.approvals == ()
    assert calls == []


def test_v3_tool_service_approval_request_rejects_missing_work_item_without_recording(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="approval.request",
                payload={
                    "approval_id": "approval-1",
                    "work_item_id": "work-missing",
                    "question": "Approve product definition?",
                },
            )
        except ValueError as exc:
            assert "work item `work-missing` was not found" in str(exc)
        else:
            raise AssertionError("approval.request should require an existing work item")

        approval = db.approval_detail("approval-1")
    finally:
        db.close()

    assert approval is None


def test_v3_status_snapshot_includes_configured_missing_role_instances(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        snapshot = db.status_snapshot(
            project_id="agentic-mesh-dev",
            configured_role_instance_ids=("agentic-mesh-dev.solution-architect.1",),
        )
    finally:
        db.close()

    assert len(snapshot.agents) == 1
    assert snapshot.agents[0].role_instance_id == "agentic-mesh-dev.solution-architect.1"
    assert snapshot.agents[0].container_state == "missing"
