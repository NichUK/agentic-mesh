from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from agentic_mesh import controller_auth
from agentic_mesh import telemetry
from agentic_mesh.activation_evidence import ActivationEvidence
from agentic_mesh.activation_evidence import FileActivationEvidenceStore
from agentic_mesh.controller_auth import ControllerAuthHandler
from agentic_mesh.controller_auth import ControllerAuthServer
from agentic_mesh.controller_auth import ControllerAuthService
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import Message
from agentic_mesh.problem_status import ProblemStatusStore
from agentic_mesh.problem_status import malformed_route_problem_status
from agentic_mesh.route_status import CurrentRoute
from agentic_mesh.route_status import CurrentRouteStore
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import SourceAnchor


def test_controller_auth_status_json_lists_reusable_credentials(tmp_path: Path) -> None:
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=20)

        connection.request("GET", "/auth/status.json")
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert body["credentials"][0]["credential"] == "codex-example-shared-api-key"
        assert body["credentials"][0]["status"] == "missing"
        assert body["credentials"][0]["redacted"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_current_agents_routes_and_status_navigation(tmp_path: Path) -> None:
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=20)

        connection.request("GET", "/agents/current.json")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["schema_version"] == "current-agents-v0"
        assert payload["read_only"] is True
        assert payload["refresh_mode"] == "manual_browser_refresh"
        assert len(payload["agents"]) == 14

        connection.request("GET", "/agents/current")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Current Agents" in body
        assert "No agent attention needed." in body
        assert "<form" not in body
        assert "<script" not in body
        assert "setTimeout" not in body
        assert "WebSocket" not in body
        assert "EventSource" not in body
        assert "restart" not in body.lower()
        assert "requeue" not in body.lower()

        connection.request("GET", "/status")
        response = connection.getresponse()
        status_body = response.read().decode("utf-8")

        assert response.status == 200
        assert 'href="/agents/current"' in status_body
        assert 'href="/agents/current.json"' in status_body
        assert "Current Agents table" not in status_body

        connection.request("GET", "/status/agents")
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        assert response.headers["Location"] == "/agents/current"

        connection.request("GET", "/status/agents.json")
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        assert response.headers["Location"] == "/agents/current.json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_work_item_status_page_shows_claimed_slice(
    tmp_path: Path,
) -> None:
    project_id = "example-project"
    state_root = tmp_path / "state"
    artifact_path = (
        tmp_path
        / "docs"
        / "work-items"
        / "work-queue-v0"
        / "20-product-definition.md"
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("# Stories\n\nVisible artifact content.\n", encoding="utf-8")
    prompt_path = (
        tmp_path
        / "docs"
        / "work-items"
        / "work-queue-v0"
        / "debug"
        / "prompts"
        / "example-project.product-manager.1"
        / "2026-06-09T000000Z0000-msg-abc.prompt.txt"
    )
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("Exact prompt sent to Codex.\n", encoding="utf-8")
    metadata_path = prompt_path.with_name(
        "2026-06-09T000000Z0000-msg-abc.metadata.json"
    )
    metadata_path.write_text('{"prompt_capture":"exact_stdin_sent_to_worker_adapter"}\n', encoding="utf-8")
    journal = EventJournal(state_root, project_id)
    message_store = FileMessageStore(state_root, project_id, journal)
    message = Message.create(
        role_id="product-manager",
        message_type="sdlc.product_definition",
        payload={
            "title": "Work Queue V0",
            "summary": "Create the first work queue.",
            "work_item_id": "work-queue-v0",
            "work_item_type": "slice",
            "lifecycle_state": "product_definition",
        },
        source="test",
    )
    message_store.enqueue(message)
    message_store.claim_next(
        "product-manager",
        "example-project.product-manager.1",
    )
    journal.append(
        "documentation_updated",
        project_id=project_id,
        role_id="product-manager",
        role_instance_id="example-project.product-manager.1",
        work_item_id="work-queue-v0",
        work_item_type="slice",
        lifecycle_state="product_definition",
        path="work-items/work-queue-v0/20-product-definition.md",
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=state_root,
        workspace_root=tmp_path,
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/work-items/work-queue-v0.json")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["status"] == "running"
        assert payload["current"]["role_id"] == "product-manager"
        assert payload["current"]["lifecycle_state"] == "product_definition"
        assert payload["artifacts"] == [
            "work-items/work-queue-v0/20-product-definition.md",
            "work-items/work-queue-v0/debug/prompts/example-project.product-manager.1/2026-06-09T000000Z0000-msg-abc.metadata.json",
            "work-items/work-queue-v0/debug/prompts/example-project.product-manager.1/2026-06-09T000000Z0000-msg-abc.prompt.txt",
        ]
        assert payload["artifact_records"] == [
            {
                "path": "work-items/work-queue-v0/20-product-definition.md",
                "label": "Verified artifact",
                "verification": "verified",
                "exists": True,
            },
            {
                "path": "work-items/work-queue-v0/debug/prompts/example-project.product-manager.1/2026-06-09T000000Z0000-msg-abc.metadata.json",
                "label": "Debug prompt metadata",
                "verification": "debug",
                "exists": True,
            },
            {
                "path": "work-items/work-queue-v0/debug/prompts/example-project.product-manager.1/2026-06-09T000000Z0000-msg-abc.prompt.txt",
                "label": "Debug prompt audit",
                "verification": "debug",
                "exists": True,
            }
        ]
        assert payload["missing_artifacts"] == []

        connection.request("GET", "/work-items/work-queue-v0")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Work Item work-queue-v0" in body
        assert "product_definition" in body
        assert "work-items/work-queue-v0/20-product-definition.md" in body
        assert "Debug prompt audit" in body
        assert "Debug prompt metadata" in body
        assert "work-items/work-queue-v0/debug/prompts/example-project.product-manager.1/2026-06-09T000000Z0000-msg-abc.prompt.txt" in body
        assert (
            "/artifact-viewer/work-items%2Fwork-queue-v0%2F20-product-definition.md"
            in body
        )
        assert 'target="_blank"' in body
        assert (
            "/artifacts/work-items%2Fwork-queue-v0%2F20-product-definition.md"
            in body
        )

        connection.request(
            "GET",
            "/artifacts/work-items%2Fwork-queue-v0%2F20-product-definition.md",
        )
        response = connection.getresponse()
        artifact_body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Visible artifact content." in artifact_body

        connection.request(
            "GET",
            "/artifacts/work-items%2Fwork-queue-v0%2Fdebug%2Fprompts%2Fexample-project.product-manager.1%2F2026-06-09T000000Z0000-msg-abc.prompt.txt",
        )
        response = connection.getresponse()
        prompt_body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Exact prompt sent to Codex." in prompt_body

        connection.request(
            "GET",
            "/artifact-viewer/work-items%2Fwork-queue-v0%2F20-product-definition.md",
        )
        response = connection.getresponse()
        viewer_body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Content-Security-Policy" in response.headers
        assert "marked.min.js" in viewer_body
        assert "mermaid.esm.min.mjs" in viewer_body
        assert 'securityLevel: "strict"' in viewer_body
        assert "sanitizeRenderedMarkdown" in viewer_body
        assert "script, style, iframe, object, embed, link" in viewer_body
        assert "Visible artifact content." in viewer_body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_work_item_status_page_shows_debug_only_item(
    tmp_path: Path,
) -> None:
    work_item_id = "work-debug-only"
    prompt_path = (
        tmp_path
        / "docs"
        / "work-items"
        / work_item_id
        / "debug"
        / "prompts"
        / "example-project.product-manager.1"
        / "2026-06-09T000000Z0000-msg-debug.prompt.txt"
    )
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("Exact prompt sent to Codex.\n", encoding="utf-8")
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=tmp_path / "state",
        workspace_root=tmp_path,
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", f"/work-items/{work_item_id}.json")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["status"] == "observed"
        assert payload["current"]["reason_summary"].startswith(
            "No lifecycle or queue status was recorded"
        )
        assert payload["artifacts"] == [
            f"work-items/{work_item_id}/debug/prompts/example-project.product-manager.1/2026-06-09T000000Z0000-msg-debug.prompt.txt",
        ]

        connection.request("GET", f"/work-items/{work_item_id}")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert work_item_id in body
        assert "Debug prompt audit" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_work_item_status_includes_safe_activation_section(
    tmp_path: Path,
) -> None:
    project_id = "agentic-mesh-dev"
    state_root = tmp_path / "state"
    evidence = ActivationEvidence(
        project_id=project_id,
        work_item_id="work-activation-detail",
        work_item_type="slice",
        lifecycle_state="implementation",
        impact_categories=("runtime_code", "route_or_ingress"),
        live_smoke_required=True,
        target_labels=("dogfood_compose",),
        activation_paths=("rebuild_image",),
        source_status="source_ready",
        activation_status="blocked",
        smoke_status="failed",
        failure_class="source_changed_running_service_not_updated",
        action_owner="runtime/operator",
        next_action="Rebuild image and smoke /agents/current.json.",
        retryable=True,
        notification_state="blocked_unroutable",
        updated_at="2026-06-05T16:10:00+00:00",
    )
    FileActivationEvidenceStore(state_root, project_id).write_current(evidence)
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
        state_root=state_root,
    )

    payload = service.work_item_status("work-activation-detail")

    assert payload["activation_evidence"]["schema_version"] == "activation-evidence-v0"
    assert payload["activation_summary"]["attention_needed"] is True
    assert payload["activation_evidence"]["failure_class"] == "source_changed_running_service_not_updated"
    serialized_activation = json.dumps(payload["activation_evidence"])
    assert "service_url" not in serialized_activation
    assert "tenant_id" not in serialized_activation
    assert "secret_ref" not in serialized_activation
    assert "teams_messages" not in payload["activation_evidence"]
    assert payload["teams_messages"] == []


def test_lifecycle_artifact_status_label_and_viewer_hardening(
    tmp_path: Path,
) -> None:
    project_id = "example-project"
    state_root = tmp_path / "state"
    artifact_path = tmp_path / "docs" / "work-items" / "work-life" / "lifecycle-flow.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        "# Lifecycle Flow\n\n<script>alert(1)</script>\n\n```mermaid\nflowchart TD\n```\n",
        encoding="utf-8",
    )
    journal = EventJournal(state_root, project_id)
    journal.append(
        "documentation_updated",
        project_id=project_id,
        role_id="lifecycle-export",
        role_instance_id="lifecycle-export",
        component_id="lifecycle-export",
        work_item_id="work-life",
        work_item_type="slice",
        lifecycle_state="implementation",
        path="work-items/work-life/lifecycle-flow.md",
        generated_artifact=True,
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=state_root,
        workspace_root=tmp_path,
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/work-items/work-life.json")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert payload["artifact_records"] == [
            {
                "path": "work-items/work-life/lifecycle-flow.md",
                "label": "Lifecycle flow",
                "verification": "verified",
                "exists": True,
            }
        ]

        connection.request("GET", "/work-items/work-life")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert "Lifecycle flow" in body
        assert "work-items/work-life/lifecycle-flow.md" in body
        assert 'target="_blank"' in body

        connection.request(
            "GET",
            "/artifact-viewer/work-items%2Fwork-life%2Flifecycle-flow.md",
        )
        response = connection.getresponse()
        viewer_body = response.read().decode("utf-8")

        assert response.status == 200
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert "https://cdn.jsdelivr.net" in response.headers["Content-Security-Policy"]
        assert 'securityLevel: "strict"' in viewer_body
        assert "sanitizeRenderedMarkdown" in viewer_body
        assert "name.startsWith(\"on\")" in viewer_body
        assert "isSafeUrl" in viewer_body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_work_item_status_page_marks_missing_artifacts(
    tmp_path: Path,
) -> None:
    project_id = "example-project"
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, project_id)
    journal.append(
        "documentation_updated",
        project_id=project_id,
        role_id="business-analyst",
        role_instance_id="example-project.business-analyst.1",
        work_item_id="work-missing-artifact",
        work_item_type="slice",
        lifecycle_state="business_analysis",
        path="work-items/work-missing-artifact/10-business-brief.md",
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=state_root,
        workspace_root=tmp_path,
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/work-items/work-missing-artifact.json")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["artifact_records"] == [
            {
                "path": "work-items/work-missing-artifact/10-business-brief.md",
                "label": "Verified artifact",
                "verification": "verified",
                "exists": False,
            }
        ]
        assert payload["missing_artifacts"] == [
            "work-items/work-missing-artifact/10-business-brief.md"
        ]

        connection.request("GET", "/work-items/work-missing-artifact")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Missing artifact" in body
        assert (
            "/artifact-viewer/work-items%2Fwork-missing-artifact%2F10-business-brief.md"
            not in body
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_work_item_status_page_marks_stale_claim(
    tmp_path: Path,
) -> None:
    project_id = "example-project"
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, project_id)
    message_store = FileMessageStore(state_root, project_id, journal)
    message_store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type="sdlc.product_definition",
            payload={
                "title": "Stale Product Definition",
                "summary": "A claimed item got abandoned.",
                "work_item_id": "work-stale-claim",
                "work_item_type": "slice",
                "lifecycle_state": "product_definition",
            },
            source="test",
        )
    )
    claimed = message_store.claim_next(
        "product-manager",
        "example-project.product-manager.1",
    )
    assert claimed is not None
    claimed_path = message_store._find_claimed_path(claimed)
    assert claimed_path is not None
    message_store._write_message(
        claimed_path,
        replace(claimed, claimed_at="2000-01-01T00:00:00+00:00"),
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=state_root,
        workspace_root=tmp_path,
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        with patch.dict(os.environ, {"AGENTIC_MESH_CLAIM_LEASE_SECONDS": "1"}):
            connection.request("GET", "/work-items/work-stale-claim.json")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))

            assert response.status == 200
            assert payload["status"] == "stale_claim"
            assert payload["current"]["stale"] is True
            assert payload["queue_entries"][0]["claim_age_seconds"] > 1

            connection.request("GET", "/work-items/work-stale-claim")
            response = connection.getresponse()
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert "stale claimed message" in body
        assert "stale claim" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_status_dashboard_routes_are_safe_and_read_only(
    tmp_path: Path,
) -> None:
    project_id = "example-project"
    state_root = tmp_path / "state"
    artifact_path = (
        tmp_path
        / "docs"
        / "work-items"
        / "work-dashboard"
        / "20-product-definition.md"
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("# Dashboard Product\n", encoding="utf-8")
    journal = EventJournal(state_root, project_id)
    queue_store = FileWorkQueueStore(state_root, project_id, journal)
    queue_item = queue_store.capture(
        title="<script>alert(1)</script>",
        summary="Synthetic queue item with raw details.",
        owner_role="product-manager",
        source_anchor=SourceAnchor(
            connector_type="teams",
            connector_id="teams-bot-listener",
            source_scope="all-agents",
            source_message_id="activity/raw-123",
            actor="raw-user-id",
            received_at="2026-06-05T10:00:00+00:00",
            display_label="<b>Nicholas</b> in all-agents",
            external_url="https://teams.example/raw",
        ),
        recommended_work_item_type="slice",
        raw_payload={"secret": "synthetic"},
        retain_raw_payload=True,
    )
    message_store = FileMessageStore(state_root, project_id, journal)
    message_store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type="sdlc.product_definition",
            payload={
                "title": "Dashboard Product",
                "summary": "Create status dashboard.",
                "work_item_id": "work-dashboard",
                "work_item_type": "slice",
                "lifecycle_state": "product_definition",
                "queue_item_id": queue_item.queue_item_id,
                "source_anchor": queue_item.source_anchor.redacted_summary(),
            },
            source=f"work-queue:{queue_item.queue_item_id}",
        )
    )
    journal.append(
        "documentation_updated",
        project_id=project_id,
        role_id="product-manager",
        role_instance_id="example-project.product-manager.1",
        work_item_id="work-dashboard",
        work_item_type="slice",
        lifecycle_state="product_definition",
        path="work-items/work-dashboard/20-product-definition.md",
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=state_root,
        workspace_root=tmp_path,
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/status.json")
        response = connection.getresponse()
        status_payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert status_payload["schema_version"] == "status-dashboard-v0"
        assert status_payload["read_only"] is True
        assert status_payload["refresh_mode"] == "manual_browser_refresh"
        assert status_payload["counts"]["total_work_items"] == 1
        assert status_payload["counts"]["total_queue_items"] == 1
        rendered = json.dumps(status_payload)
        for forbidden in [
            "queue_entries",
            "timeline",
            "teams_messages",
            "teams_activity_id",
            "teams_conversation_id",
            "activity/raw-123",
            "raw-user-id",
            "teams.example",
            "raw_refs",
            "secret_ref",
            "mount_ref",
            str(state_root),
        ]:
            assert forbidden not in rendered

        connection.request("GET", "/work-items.json")
        response = connection.getresponse()
        work_items_payload = json.loads(response.read().decode("utf-8"))
        row = work_items_payload["items"][0]

        assert response.status == 200
        assert row["status"] == "pending"
        assert row["display_label"] == "Pending"
        assert row["status_group"] == "active"
        assert row["queue_item_id"] == queue_item.queue_item_id
        assert row["artifact_links"][0]["viewer_url"].startswith("/artifact-viewer/")

        connection.request("GET", "/work-queue.json")
        response = connection.getresponse()
        work_queue_payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert work_queue_payload["schema_version"] == "status-dashboard-v0"
        assert work_queue_payload["items"][0]["title"] == "<script>alert(1)</script>"

        connection.request("GET", "/status")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
        assert "<script>alert(1)</script>" not in body
        for forbidden in [
            "<form",
            "setTimeout",
            "WebSocket",
            "EventSource",
            "http-equiv",
            "activity/raw-123",
            "raw-user-id",
            "teams.example",
            str(state_root),
        ]:
            assert forbidden not in body

        connection.request("GET", "/queue")
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        assert response.headers["Location"] == "/work-queue"

        connection.request("GET", "/queue.json")
        response = connection.getresponse()
        queue_alias_payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert queue_alias_payload["schema_version"] == "status-dashboard-v0"
        assert queue_alias_payload["items"][0]["queue_item_id"] == queue_item.queue_item_id
        assert any(
            span.name == "agentic_mesh.status_dashboard.read"
            for span in sink.spans
        )
        assert any(
            log.get("event_type") == "status_dashboard_read"
            for log in sink.logs
        )
        telemetry_rendered = json.dumps(
            {
                "spans": [span.attributes for span in sink.spans],
                "logs": sink.logs,
            }
        )
        for forbidden in [
            "activity/raw-123",
            "raw-user-id",
            "teams.example",
            "raw_refs",
            str(state_root),
        ]:
            assert forbidden not in telemetry_rendered
    finally:
        telemetry.set_test_sink(None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_status_dashboard_reuses_journal_events_for_work_item_rows(
    tmp_path: Path,
) -> None:
    project_id = "example-project"
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, project_id)
    for index in range(3):
        journal.append(
            "work_completed",
            project_id=project_id,
            role_id="product-manager",
            role_instance_id="example-project.product-manager.1",
            work_item_id=f"work-cache-{index}",
            work_item_type="slice",
            lifecycle_state="product_definition",
            message_id=f"msg-cache-{index}",
            status="completed",
        )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=state_root,
        workspace_root=tmp_path,
    )

    original_open = Path.open
    journal_open_count = 0

    def counting_open(path: Path, *args, **kwargs):
        nonlocal journal_open_count
        if path.name == "events.jsonl":
            journal_open_count += 1
        return original_open(path, *args, **kwargs)

    with patch.object(Path, "open", counting_open):
        payload = service.status_dashboard_payload("work-items.json")

    assert len(payload["work_items"]) == 3
    assert journal_open_count == 2


def test_status_dashboard_mixed_rows_use_truthful_compact_columns() -> None:
    handler = object.__new__(ControllerAuthHandler)

    html_body = handler._mixed_rows(
        [
            {
                "work_item_id": "work-status",
                "status": "blocked",
                "status_url": "/work-items/work-status",
                "title": "Fix status table",
                "owner_role": "engineering",
                "lifecycle_state": "implementation",
                "next_action": "Correct compact table headings",
                "updated_at": "2026-06-11T12:00:00+00:00",
            }
        ],
        [
            {
                "queue_item_id": "queue-status",
                "status": "active",
                "title": "Follow-up status table",
                "owner_role": "product-manager",
                "next_action": "Product triage",
                "created_at": None,
            }
        ],
        empty="No mixed rows.",
    )

    for heading in [
        "Kind",
        "Item",
        "Status",
        "Title",
        "Owner",
        "State / Type",
        "Reason / Next Action",
        "Updated",
    ]:
        assert f"<th>{heading}</th>" in html_body
    assert "<th>Timestamp</th>" not in html_body
    assert "<th>State</th>" not in html_body
    assert "Unknown type" in html_body
    assert "Unknown time" in html_body
    assert html_body.index("work-status") < html_body.index("<span")
    assert "Correct compact table headings" in html_body
    assert "Product triage" in html_body


def test_controller_status_dashboard_isolates_corrupt_queue_item(
    tmp_path: Path,
) -> None:
    project_id = "example-project"
    state_root = tmp_path / "state"
    corrupt_path = (
        state_root
        / "projects"
        / project_id
        / "work_queue"
        / "items"
        / "queue-corrupt.json"
    )
    corrupt_path.parent.mkdir(parents=True)
    corrupt_path.write_text("{not-json", encoding="utf-8")
    journal_path = state_root / "projects" / project_id / "journal" / "events.jsonl"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text("{not-json\n", encoding="utf-8")
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=state_root,
        workspace_root=tmp_path,
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/work-queue.json")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["items"][0]["queue_item_id"] == "queue-corrupt"
        assert payload["items"][0]["status"] == "incomplete_record"
        assert payload["items"][0]["extraction_error"] == "JSONDecodeError"
        rendered = json.dumps(payload)
        assert str(corrupt_path) not in rendered
        assert "{not-json" not in rendered
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_artifact_viewer_redirects_to_configured_renderer(
    tmp_path: Path,
) -> None:
    project_id = "example-project"
    state_root = tmp_path / "state"
    artifact_path = (
        tmp_path
        / "docs"
        / "work-items"
        / "work-queue-v0"
        / "20-product-definition.md"
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("# Stories\n", encoding="utf-8")
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=state_root,
        workspace_root=tmp_path,
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        with patch.dict(
            os.environ,
            {
                "AGENTIC_MESH_ARTIFACT_RENDERER_URL_TEMPLATE": (
                    "https://renderer.example/view?url={artifact_url}&path={artifact_path}"
                )
            },
        ):
            connection.request(
                "GET",
                "/artifact-viewer/work-items%2Fwork-queue-v0%2F20-product-definition.md",
                headers={"Host": f"{host}:{port}"},
            )
            response = connection.getresponse()
            response.read()

        assert response.status == 303
        location = response.headers["Location"]
        assert location.startswith("https://renderer.example/view?url=http://")
        assert (
            "artifacts/work-items%2Fwork-queue-v0%2F20-product-definition.md"
            in location
        )
        assert "path=work-items%2Fwork-queue-v0%2F20-product-definition.md" in location
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_work_item_status_json_and_html_include_current_route_after_problem(
    tmp_path: Path,
) -> None:
    project_id = "example-project"
    state_root = tmp_path / "state"
    CurrentRouteStore(state_root, project_id).write_current(
        CurrentRoute(
            work_item_id="work-route-status",
            work_item_type="slice",
            queue_item_id="queue-route",
            source_message_id="msg-route",
            source_anchor_ref="source:route",
            source_anchor_summary="QA channel",
            correlation_id="corr-route",
            route_id="route-123",
            route_kind="configured_correction",
            route_status="correction_requested",
            source_role="qa-engineer",
            target_role="engineering",
            source_lifecycle_state="quality_review",
            target_lifecycle_state="implementation",
            message_type="sdlc.consult.implementation",
            configured_route_id="implementation_context",
            defect_id="DEF-QA-LIFE-001",
            required_change="Correct the route normalizer.",
            evidence_required="Implementation log and test output.",
        )
    )
    ProblemStatusStore(state_root, project_id).write_current(
        malformed_route_problem_status(
            failure_class="malformed_route",
            reason="Malformed route requires runtime recovery.",
            role_id="qa-engineer",
            role_instance_id="example-project.qa-engineer.1",
            message_payload={
                "work_item_id": "work-route-status",
                "work_item_type": "slice",
                "lifecycle_state": "quality_review",
            },
            source_message_id="msg-problem",
            correlation_id="corr-route",
            lifecycle_state="quality_review",
            attempted_target_role="engineering",
            attempted_lifecycle_state=None,
        )
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=state_root,
        workspace_root=tmp_path,
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/work-items/work-route-status.json")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["problem_status"]["failure_class"] == "malformed_route"
        assert payload["current_route"]["route_status"] == "correction_requested"
        assert payload["current_route"]["route_kind"] == "configured_correction"

        connection.request("GET", "/work-items/work-route-status")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert body.index("Current Problem") < body.index("Current Route")
        assert "DEF-QA-LIFE-001" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_auth_form_stores_secret_without_echoing_value(
    tmp_path: Path,
) -> None:
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        body = (
            "credential=codex-example-shared-api-key"
            "&secret_value=not-a-real-secret"
            "&overwrite=yes"
        )

        connection.request(
            "POST",
            "/auth/secret",
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()

        assert response.status == 303
        secret_path = tmp_path / "state" / "secrets" / "codex-example-shared-api-key"
        assert secret_path.read_text(encoding="utf-8") == "not-a-real-secret"

        connection.request("GET", "/auth/credentials")
        response = connection.getresponse()
        html = response.read().decode("utf-8")

        assert response.status == 200
        assert "not-a-real-secret" not in html
        assert "codex-example-shared-api-key" in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_auth_starts_codex_oauth_device_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )

    class FakeProcess:
        stdout = iter(["Open https://example/device\n", "Use code ABCD-EFGH\n"])

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(controller_auth.shutil, "which", lambda command: "codex")
    monkeypatch.setattr(
        controller_auth.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )

    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file=str(project_file),
        state_root=tmp_path / "state",
    )

    session = service.start_codex_oauth_login("codex-product-oauth")
    deadline = time.time() + 5
    while session.status == "running" and time.time() < deadline:
        time.sleep(0.01)

    assert session.status == "completed"
    assert session.login_url == "https://example/device"
    assert session.user_code == "ABCD-EFGH"
    assert session.output == ["Open https://example/device", "Use code ABCD-EFGH"]
    assert session.codex_home == tmp_path / "state" / "worker_mounts" / "codex-product-home"


def test_controller_auth_parses_ansi_colored_codex_device_output() -> None:
    assert (
        controller_auth._first_url(
            "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m"
        )
        == "https://auth.openai.com/codex/device"
    )
    assert (
        controller_auth._first_device_code(
            "   \x1b[94m2A4N-771S5\x1b[0m"
        )
        == "2A4N-771S5"
    )


def test_controller_auth_oauth_page_has_openai_button(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(controller_auth.shutil, "which", lambda command: None)
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file=str(project_file),
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/auth/credentials")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Sign in with OpenAI" in body
        assert "CODEX_HOME" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_auth_oauth_page_disables_button_when_configured(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ControllerAuthService,
        "_codex_oauth_status",
        lambda *args, **kwargs: ("configured", "Logged in using ChatGPT"),
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file=str(project_file),
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/auth/credentials")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Signed in" in body
        assert "status-button" in body
        assert "disabled" in body
        assert "Sign in with OpenAI" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_auth_oauth_status_requires_live_codex_check(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )
    mount_path = tmp_path / "state" / "worker_mounts" / "codex-product-home"
    mount_path.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="Logged in using ChatGPT",
                stderr="",
            )
        if command[1] == "exec":
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr=(
                    "401 Unauthorized: Your authentication token has been "
                    "invalidated. Your access token could not be refreshed."
                ),
            )
        raise AssertionError(command)

    monkeypatch.setattr(controller_auth.shutil, "which", lambda command: "codex")
    monkeypatch.setattr(controller_auth.subprocess, "run", fake_run)

    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file=str(project_file),
        state_root=tmp_path / "state",
        workspace_root=tmp_path,
    )

    [credential] = service.credential_statuses()

    assert credential["status"] == "auth_failed"
    assert "Re-authenticate the configured Codex credential" in credential["detail"]
    assert any(call[1] == "exec" for call in calls)


def test_controller_auth_oauth_page_enables_button_when_token_invalid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ControllerAuthService,
        "_codex_oauth_status",
        lambda *args, **kwargs: (
            "auth_failed",
            "Codex OAuth token is invalid or expired.",
        ),
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file=str(project_file),
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/auth/credentials")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "auth_failed" in body
        assert "Sign in with OpenAI" in body
        assert "Signed in" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_auth_session_page_polls_without_meta_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )

    class FakeProcess:
        stdout = iter(
            [
                "Open \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n",
                "Use code \x1b[94m2A4N-771S5\x1b[0m\n",
            ]
        )

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(controller_auth.shutil, "which", lambda command: "codex")
    monkeypatch.setattr(
        controller_auth.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file=str(project_file),
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request(
            "POST",
            "/auth/codex/start",
            body=b"credential=codex-product-oauth",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        location = response.headers["Location"]
        session_id = location.rsplit("id=", 1)[1]

        deadline = time.time() + 5
        session = service.session(session_id)
        assert session is not None
        while session.status == "running" and time.time() < deadline:
            time.sleep(0.01)

        connection.request("GET", location)
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "http-equiv=\"refresh\"" not in body
        assert "id=\"copy-code\"" in body
        assert "<svg" in body
        assert "2A4N-771S5" in body
        assert "fallbackCopyText" in body
        assert "document.execCommand(\"copy\")" in body
        assert "Select and copy manually" in body

        connection.request("GET", f"/auth/codex/session.json?id={session_id}")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["login_url"] == "https://auth.openai.com/codex/device"
        assert payload["user_code"] == "2A4N-771S5"
        assert payload["redacted"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
