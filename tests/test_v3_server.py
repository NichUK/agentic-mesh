from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.teams_ingress import TeamsActivityRouter
from agentic_mesh_v3.teams_ingress import TeamsRoleIdentity
from agentic_mesh_v3.server import V3StatusHandler
from agentic_mesh_v3.server import _artifact_page
from agentic_mesh_v3.server import _teams_activity_response
from agentic_mesh_v3.tools import V3ToolService


def test_artifact_page_renders_markdown_safely() -> None:
    html = _artifact_page("work-items/work-1/index.md", "# Hello\n\n<script>alert(1)</script>")

    assert "<h1>Hello</h1>" in html
    assert "<script>" not in html
    assert "cdn.jsdelivr.net/npm/mermaid" not in html


def test_artifact_page_preserves_mermaid_diagrams_with_controlled_loader() -> None:
    html = _artifact_page(
        "work-items/work-1/lifecycle.md",
        "# Flow\n\n```mermaid\ngraph TD\n  A-->B\n```\n\n<script>alert(1)</script>",
    )

    assert '<pre class="mermaid">graph TD\n  A--&gt;B</pre>' in html
    assert "cdn.jsdelivr.net/npm/mermaid" in html
    assert "mermaid.initialize" in html
    assert "alert" not in html


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


def test_teams_activity_response_routes_to_agent_inbox() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    router = TeamsActivityRouter(
        LocalTeamsBridge(broker),
        role_identities=(TeamsRoleIdentity("product-manager", "AM-Product Manager", bot_id="bot-product"),),
    )

    response = _teams_activity_response(
        {
            "id": "msg-1",
            "text": "Please respond.",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        },
        router,
    )

    assert response == {"status": "routed", "subjects": ["agent.product-manager"]}
    broker.ensure_consumer("agent-inbox", "pm", filter_subject="agent.product-manager")
    assert broker.fetch("agent-inbox", "pm")[0].payload["text"] == "Please respond."


def test_artifact_viewer_reads_local_document_library(tmp_path: Path) -> None:
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    docs.write_text("work-items/work-1/index.md", "# Work One")

    assert docs.read_text("work-items/work-1/index.md") == "# Work One"


def test_artifact_viewer_route_renders_work_item_scoped_artifact(tmp_path: Path) -> None:
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    docs.write_text("work-items/work-1/index.md", "# Work One")
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
    finally:
        db.close()

    class Handler(V3StatusHandler):
        pass

    Handler.db_path = tmp_path / "v3.sqlite3"
    Handler.project_id = "agentic-mesh-dev"
    Handler.document_library = docs
    handler = object.__new__(Handler)
    captured: dict[str, str] = {}
    handler._send_html = lambda content: captured.__setitem__("html", content)  # type: ignore[method-assign]

    handler._render_artifact_route("work-1/index.md")

    assert "<h1>Work One</h1>" in captured["html"]
    assert "work-items/work-1/index.md" in captured["html"]


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
                    "phase": "deployment",
                    "accountable_role": "release-manager",
                    "responsible_roles": ["platform-engineer", "engineering"],
                    "consulted_roles": ["qa-engineer"],
                    "informed_roles": ["project-manager"],
                    "sponsor_decision_points": ["release-approval"],
                    "required_evidence": ["smoke evidence", "rollback plan"],
                    "extra_context": {"risk": "low"},
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
                "next_action": "Await release approval.",
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
        tools.call(
            role_instance_id="agentic-mesh-dev.platform-engineer.1",
            tool_name="blocker.raise",
            payload={
                "work_item_id": "work-1",
                "summary": "Deployment target credentials are missing.",
                "next_action": "Provide staging deployment credentials.",
                "blocked_role": "platform-engineer",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.solution-architect.1",
            tool_name="decision.record",
            payload={
                "work_item_id": "work-1",
                "summary": "Keep the status page as the first sponsor visibility surface.",
                "target_ref": "decision-status-page",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.security-architect.1",
            tool_name="risk.register",
            payload={
                "work_item_id": "work-1",
                "summary": "Deployment credentials may delay release validation.",
                "target_ref": "risk-deployment-credentials",
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
    assert "Accountable" in html
    assert "Responsible" in html
    assert "platform-engineer" in html
    assert "qa-engineer" in html
    assert "Sponsor decisions" in html
    assert "Required evidence" in html
    assert "smoke evidence" in html
    assert "Additional governance data" in html
    assert "extra_context" in html
    assert "<pre>" not in html
    assert "Work item index" in html
    assert "/artifact-viewer/work-1/index.md" in html
    assert "consult.request" in html
    assert "Confirm smoke evidence remains valid." in html
    assert "Governance Checklist" in html
    assert "Informed updates" in html
    assert "project-manager" in html
    assert "release-approval" in html
    assert "Required evidence" in html
    assert "smoke evidence" in html
    assert "approval-1" in html
    assert "Approve release?" in html
    assert "Blockers" in html
    assert "Deployment target credentials are missing." in html
    assert "platform-engineer" in html
    assert "Decisions" in html
    assert "Keep the status page as the first sponsor visibility surface." in html
    assert "Risks" in html
    assert "Deployment credentials may delay release validation." in html
    assert "release-1" in html
    assert "staging smoke passed" in html
