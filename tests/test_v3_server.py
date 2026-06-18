from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.teams_ingress import TeamsActivityRouter
from agentic_mesh_v3.teams_ingress import TeamsRoleIdentity
from agentic_mesh_v3.server import DashboardAuthConfig
from agentic_mesh_v3.server import V3StatusHandler
from agentic_mesh_v3.server import dashboard_auth_config_from_env
from agentic_mesh_v3.server import _create_dashboard_session_cookie
from agentic_mesh_v3.server import _artifact_page
from agentic_mesh_v3.server import _verify_dashboard_session_cookie
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


def test_dashboard_auth_config_from_env_enables_bearer_and_proxy_users() -> None:
    config = dashboard_auth_config_from_env(
        {
            "AGENTIC_MESH_DASHBOARD_AUTH_TOKEN": "secret",
            "AGENTIC_MESH_DASHBOARD_ALLOWED_USERS": "nich@example.test, ops@example.test",
            "AGENTIC_MESH_DASHBOARD_USER_HEADERS": "X-Test-User",
            "AGENTIC_MESH_DASHBOARD_SESSION_SECRET": "session-secret",
            "AGENTIC_MESH_DASHBOARD_SESSION_TTL_SECONDS": "900",
        }
    )

    assert config.enabled is True
    assert config.bearer_token == "secret"
    assert config.allowed_users == ("nich@example.test", "ops@example.test")
    assert config.session_secret == "session-secret"
    assert config.session_ttl_seconds == 900
    assert config.trusted_user_headers == ("X-Test-User",)


def test_dashboard_auth_config_from_env_enables_entra_device_code_provider() -> None:
    config = dashboard_auth_config_from_env(
        {
            "AGENTIC_MESH_DASHBOARD_AUTH_ENABLED": "true",
            "AGENTIC_MESH_DASHBOARD_AUTH_MODE": "entra",
            "AGENTIC_MESH_DASHBOARD_ENTRA_CLIENT_ID": "client-id",
            "AGENTIC_MESH_DASHBOARD_ENTRA_TENANT_ID": "tenant-id",
            "AGENTIC_MESH_DASHBOARD_ENTRA_SCOPES": "openid profile email User.Read",
            "AGENTIC_MESH_DASHBOARD_ENTRA_FLOW": "device_code",
        }
    )

    assert config.enabled is True
    assert config.auth_mode == "entra"
    assert config.entra_client_id == "client-id"
    assert config.entra_tenant_id == "tenant-id"
    assert config.entra_scopes == "openid profile email User.Read"
    assert config.entra_flow == "device_code"


def test_dashboard_auth_rejects_status_without_authenticated_user() -> None:
    class Handler(V3StatusHandler):
        pass

    Handler.dashboard_auth = DashboardAuthConfig(enabled=True, allowed_users=("nich@example.test",))
    handler = object.__new__(Handler)
    handler.headers = {}
    captured: dict[str, object] = {}
    handler.send_response = lambda status: captured.__setitem__("status", status)  # type: ignore[method-assign]
    handler.send_header = lambda *args: None  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]

    class Writer:
        def write(self, body: bytes) -> None:
            captured["body"] = body

    handler.wfile = Writer()

    assert handler._authorize_dashboard_request() is False
    assert captured["status"].value == 401
    assert b"Authentication required" in captured["body"]


def test_dashboard_auth_redirects_to_login_when_local_token_is_configured() -> None:
    class Handler(V3StatusHandler):
        pass

    Handler.dashboard_auth = DashboardAuthConfig(enabled=True, auth_mode="token", bearer_token="secret")
    handler = object.__new__(Handler)
    handler.headers = {}
    handler.path = "/artifact-viewer/work-1/index.md"
    captured: dict[str, object] = {"headers": []}
    handler.send_response = lambda status: captured.__setitem__("status", status)  # type: ignore[method-assign]
    handler.send_header = lambda *args: captured["headers"].append(args)  # type: ignore[attr-defined, method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]

    assert handler._authorize_dashboard_request() is False

    assert captured["status"].value == 302
    assert ("Location", "/login?next=/artifact-viewer/work-1/index.md") in captured["headers"]


def test_dashboard_auth_accepts_bearer_or_trusted_proxy_user() -> None:
    class Handler(V3StatusHandler):
        pass

    Handler.dashboard_auth = DashboardAuthConfig(
        enabled=True,
        auth_mode="token",
        bearer_token="secret",
        allowed_users=("nich@example.test",),
        trusted_user_headers=("X-Test-User",),
    )
    handler = object.__new__(Handler)
    handler.headers = {"Authorization": "Bearer secret"}

    assert handler._authorize_dashboard_request() is True

    handler.headers = {"X-Test-User": "nich@example.test"}

    assert handler._authorize_dashboard_request() is True


def test_dashboard_auth_accepts_valid_session_cookie() -> None:
    class Handler(V3StatusHandler):
        pass

    cookie_value = _create_dashboard_session_cookie("session-secret", ttl_seconds=900)
    Handler.dashboard_auth = DashboardAuthConfig(
        enabled=True,
        bearer_token="secret",
        session_secret="session-secret",
    )
    handler = object.__new__(Handler)
    handler.headers = {"Cookie": f"agentic_mesh_dashboard={cookie_value}"}
    handler.path = "/status"

    assert handler._authorize_dashboard_request() is True
    deterministic_cookie = _create_dashboard_session_cookie("session-secret", ttl_seconds=900, now=1000)
    assert _verify_dashboard_session_cookie(deterministic_cookie, "session-secret", now=1100) is True
    assert _verify_dashboard_session_cookie(deterministic_cookie, "session-secret", now=2000) is False


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


def test_focused_reporting_json_endpoints_return_agents_and_work(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db)
        tools.call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="backlog.upsert",
            payload={
                "queue_item_id": "queue-1",
                "title": "Add dashboard",
                "summary": "Add a project status dashboard.",
                "status": "queued",
                "owner_role": "product-manager",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.project-manager.1",
            tool_name="work_item.upsert",
            payload={
                "work_item_id": "work-1",
                "queue_item_id": "queue-1",
                "title": "Add status page",
                "description": "Build status page",
                "state": "active",
                "owner_role": "engineering",
            },
        )
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.engineering.1",
                container_state="running",
                heartbeat_at="2026-06-16T10:00:00+00:00",
                current_work="work-1",
                inbox_depth=2,
            )
        )
    finally:
        db.close()

    class Handler(V3StatusHandler):
        pass

    Handler.db_path = tmp_path / "v3.sqlite3"
    Handler.project_id = "agentic-mesh-dev"
    Handler.document_library = LocalDocumentLibraryAdapter(tmp_path / "documents")
    handler = object.__new__(Handler)
    captured: dict[str, object] = {}
    handler._send_json = lambda payload, **kwargs: captured.update(payload)  # type: ignore[method-assign]

    handler._send_json_agents()

    assert captured["status"] == "ok"
    assert captured["project_id"] == "agentic-mesh-dev"
    agents = captured["agents"]
    assert isinstance(agents, list)
    assert agents[0]["role_instance_id"] == "agentic-mesh-dev.engineering.1"
    assert agents[0]["current_work"] == "work-1"

    captured.clear()
    handler._send_json_work_items()

    assert captured["status"] == "ok"
    backlog = captured["backlog"]
    work_items = captured["work_items"]
    assert isinstance(backlog, list)
    assert isinstance(work_items, list)
    assert backlog[0]["queue_item_id"] == "queue-1"
    assert work_items[0]["work_item_id"] == "work-1"


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


def test_artifact_viewer_route_reports_document_backend_errors(tmp_path: Path) -> None:
    class FailingDocumentLibrary:
        def exists(self, relative_path: str) -> bool:
            raise RuntimeError("HTTP Error 401: Unauthorized")

        def read_text(self, relative_path: str) -> str:
            raise AssertionError("read_text should not be called after failed exists")

    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
    finally:
        db.close()

    class Handler(V3StatusHandler):
        pass

    Handler.db_path = tmp_path / "v3.sqlite3"
    Handler.project_id = "agentic-mesh-dev"
    Handler.document_library = FailingDocumentLibrary()
    handler = object.__new__(Handler)
    captured: dict[str, object] = {}
    handler.send_error = lambda status, message=None: captured.update(  # type: ignore[method-assign]
        {"status": status, "message": message}
    )

    handler._render_artifact_route("work-1/index.md")

    assert captured["status"].value == 502
    assert captured["message"] == "document library lookup failed: HTTP Error 401: Unauthorized"


def test_work_item_page_renders_detail_evidence(tmp_path: Path) -> None:
    docs = LocalDocumentLibraryAdapter(tmp_path / "documents")
    broker = InMemoryBrokerAdapter()
    bridge = LocalTeamsBridge(broker)
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        tools = V3ToolService(db, docs, stakeholder_bridge=bridge)
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
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="artifact.link",
            payload={
                "work_item_id": "work-1",
                "relative_path": "work-items/work-1/020-product-definition.md",
                "title": "Product definition",
                "document_type": "product_definition",
                "status": "published",
            },
        )
        tools.call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="approval.request",
            payload={
                "approval_id": "approval-1",
                "work_item_id": "work-1",
                "question": "Approve release?",
                "connector": "teams",
                "target_ref": "dm:sponsor",
                "thread_ref": "thread-1",
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
                "version_ref": "commit:abc123",
                "approval_ref": "approval-release-1",
                "deployment_result": "staging smoke passed",
                "smoke_evidence": "GET /status passed in staging.",
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
    assert 'target="_blank" rel="noopener noreferrer">Work item index</a>' in html
    assert "Document framework" in html
    assert "togaf-sdlc-v1" in html
    assert "Framework Path" in html
    assert "work-items/work-1/020-product-definition.md" in html
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
    assert "Outbound Deliveries" in html
    assert "approval.request" in html
    assert "dm:sponsor" in html
    assert "Thread: thread-1" in html


def test_work_item_json_endpoint_returns_detail_evidence(tmp_path: Path) -> None:
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
                "state": "active",
                "owner_role": "engineering",
                "current_phase": "development",
                "next_action": "Implement the status page.",
                "governance": {
                    "phase": "development",
                    "accountable_role": "engineering",
                    "responsible_roles": ["engineering"],
                    "consulted_roles": ["qa-engineer"],
                    "informed_roles": ["project-manager"],
                    "required_evidence": ["implementation log"],
                },
            },
        )
        tools.call(
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
    finally:
        db.close()

    class Handler(V3StatusHandler):
        pass

    Handler.db_path = tmp_path / "v3.sqlite3"
    Handler.project_id = "agentic-mesh-dev"
    Handler.document_library = docs
    handler = object.__new__(Handler)
    captured: dict[str, object] = {}
    handler._send_json = lambda payload, **kwargs: captured.update(payload)  # type: ignore[method-assign]

    handler._send_json_work_item("work-1")

    assert captured["status"] == "ok"
    assert captured["document_framework_id"] == "togaf-sdlc-v1"
    work_item = captured["work_item"]
    assert isinstance(work_item, dict)
    assert work_item["work_item_id"] == "work-1"
    assert work_item["title"] == "Add status page"
    assert work_item["governance"]["accountable_role"] == "engineering"
    assert work_item["governance_checklist"]["missing_consultations"] == ["qa-engineer"]
    assert work_item["artifacts"][0]["relative_path"] == "work-items/work-1/100-implementation-log.md"
    assert "url" in work_item["artifacts"][0]
