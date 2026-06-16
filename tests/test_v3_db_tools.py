from pathlib import Path

from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
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
            payload={
                "heartbeat_at": "2026-06-15T12:00:00Z",
                "current_work": "work-1",
                "inbox_depth": 2,
                "dead_letter_depth": 1,
            },
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
    assert snapshot.agents[0].dead_letter_depth == 1


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
    assert "[Add status page](work-items/work-1/index.md) - `active` - engineering" in root_index.read_text(
        encoding="utf-8"
    )


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
        db.upsert_work_item(
            work_item_id="work-1",
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
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.close",
            payload={"work_item_id": "work-1"},
        )

        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.state == "closed"
    assert detail.owner_role == "project-manager"


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
            },
        )

        checklist = db.work_item_governance_checklist("work-1")

        assert checklist is not None
        assert checklist.missing_consultations == ("qa-engineer",)
        assert checklist.missing_informed_updates == ("project-manager",)
        assert checklist.pending_sponsor_decisions == ("product-signoff",)

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
        tools.call(
            role_instance_id="agentic-mesh-dev.engineering.1",
            tool_name="informed.update",
            payload={
                "work_item_id": "work-1",
                "target_role": "project-manager",
                "summary": "Development governance evidence is ready.",
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
    finally:
        db.close()

    assert len(pending) == 1
    assert pending[0].subject == "agent.engineering"
    assert pending[0].payload["message_type"] == "handoff.require"
    assert pending[0].payload["work_item_id"] == "work-1"
    assert pending[0].payload["summary"] == "Implement the signed-off product slice."
    assert pending[0].payload["payload"]["target_role"] == "engineering"


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
    finally:
        db.close()

    assert len(pending) == 1
    assert pending[0].subject == "agent.qa-engineer"
    assert pending[0].payload["message_type"] == "consult.request"
    assert pending[0].payload["summary"] == "Please review the acceptance criteria."


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
            assert "handoff.require requires phase" in str(exc)
        else:
            raise AssertionError("incomplete handoff requirements should fail validation")
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
            state="waiting_human",
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
            },
        )
        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert detail is not None
    assert detail.governance_records[0].record_type == "stakeholder.ask_question"
    assert detail.governance_records[0].target_ref == "dm:sponsor"
    assert bridge.deliveries[0].target_ref == "dm:sponsor"
    assert "Which sponsor-visible channel" in bridge.deliveries[0].text_markdown


def test_v3_tool_service_stakeholder_question_with_target_requires_bridge(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Question work",
            description="Needs sponsor input.",
            state="waiting_human",
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
    finally:
        db.close()

    assert detail is not None
    assert detail.governance_records == ()


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
    finally:
        db.close()

    assert result.tool_name == "messaging.send"
    assert bridge.deliveries[0].target_ref == "dm:sponsor"
    assert bridge.deliveries[0].text_markdown == "**Please review**"
    assert bridge.deliveries[0].importance == "high"


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
                "message": "Product Manager reply.",
                "thread_ref": "thread-1",
            },
        )
    finally:
        db.close()

    assert result.terminal is True
    assert bridge.deliveries[0].target_ref == "dm:sponsor"
    assert bridge.deliveries[0].text_markdown == "Product Manager reply."
    assert bridge.deliveries[0].thread_ref == "thread-1"


def test_v3_tool_service_status_reply_without_target_remains_audit_only(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    try:
        db.migrate()
        result = V3ToolService(db, stakeholder_bridge=bridge).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="status.reply",
            payload={"message": "MCP-local reply."},
        )
    finally:
        db.close()

    assert result.terminal is True
    assert bridge.deliveries == []


def test_v3_tool_service_status_reply_with_target_requires_bridge(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="status.reply",
                payload={"connector": "teams", "target_ref": "dm:sponsor", "message": "Reply."},
            )
        except ValueError as exc:
            assert "stakeholder bridge is not configured" in str(exc)
        else:
            raise AssertionError("status.reply with target should require a stakeholder bridge")
    finally:
        db.close()


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
    finally:
        db.close()

    assert detail is not None
    assert detail.approvals[0].status == "approved"
    assert detail.approvals[0].response == "Approved by sponsor."


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
    finally:
        db.close()

    assert detail is not None
    assert detail.approvals[0].approval_id == "approval-1"
    assert bridge.deliveries[0].target_ref == "dm:sponsor"
    assert "Approve product definition?" in bridge.deliveries[0].text_markdown
    assert "approval-1" in bridge.deliveries[0].text_markdown
    assert bridge.deliveries[0].importance == "high"


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
    finally:
        db.close()

    assert detail is not None
    assert detail.approvals == ()
