from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputService
from agentic_mesh_v2.safe_output_mcp import SAFE_OUTPUT_MCP_TOOL
from agentic_mesh_v2.safe_output_mcp import handle_safe_output_mcp_request
from agentic_mesh_v2.safe_output_mcp import run_safe_output_mcp_stdio


def test_safe_output_mcp_lists_record_tool(tmp_path: Path) -> None:
    db = _db_with_run(tmp_path, "run-mcp-list")
    try:
        response = handle_safe_output_mcp_request(
            db,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    finally:
        db.close()

    assert response is not None
    tools = response["result"]["tools"]
    assert tools[0]["name"] == SAFE_OUTPUT_MCP_TOOL
    assert "run_id" in tools[0]["inputSchema"]["required"]
    assert "payload" in tools[0]["inputSchema"]["required"]


def test_safe_output_mcp_records_tool_call_for_running_role_run(tmp_path: Path) -> None:
    db = _db_with_run(tmp_path, "run-mcp-record")
    try:
        response = handle_safe_output_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "id": "call-1",
                "method": "tools/call",
                "params": {
                    "name": SAFE_OUTPUT_MCP_TOOL,
                    "arguments": {
                        "run_id": "run-mcp-record",
                        "role_id": "product-manager",
                        "tool_name": "status.reply",
                        "payload": {"message": "Product reply through MCP."},
                    },
                },
            },
        )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert response is not None
    assert response["result"]["structuredContent"]["status"] == "ok"
    assert response["result"]["structuredContent"]["terminal"] is True
    assert len(calls) == 1
    assert calls[0]["run_id"] == "run-mcp-record"
    assert calls[0]["role_id"] == "product-manager"
    assert calls[0]["tool_name"] == "status.reply"
    assert calls[0]["payload"]["message"] == "Product reply through MCP."


def test_safe_output_mcp_with_configured_service_publishes_document(tmp_path: Path) -> None:
    db = _db_with_work_run(tmp_path, "run-mcp-document")
    document_root = tmp_path / "docs"
    try:
        response = handle_safe_output_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "id": "call-document",
                "method": "tools/call",
                "params": {
                    "name": SAFE_OUTPUT_MCP_TOOL,
                    "arguments": {
                        "run_id": "run-mcp-document",
                        "role_id": "product-manager",
                        "tool_name": "document.propose_update",
                        "payload": {
                            "path": "work-items/work-mcp-document/020-product-definition.md",
                            "document_type": "product_definition",
                            "content": _product_definition_content(),
                        },
                    },
                },
            },
            service=SafeOutputService(db, document_library_root=document_root),
        )
        artifacts = db.list_artifacts()
    finally:
        db.close()

    assert response is not None
    assert response["result"]["structuredContent"]["status"] == "ok"
    assert len(artifacts) == 1
    assert (document_root / "work-items" / "work-mcp-document" / "020-product-definition.md").exists()


def test_safe_output_mcp_accepts_initialized_notification_without_error(tmp_path: Path) -> None:
    db = _db_with_run(tmp_path, "run-mcp-notification")
    try:
        response = handle_safe_output_mcp_request(
            db,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert response is None
    assert calls == []


def test_safe_output_mcp_rejects_tools_call_without_request_id_before_recording(tmp_path: Path) -> None:
    db = _db_with_run(tmp_path, "run-mcp-no-id")
    try:
        response = handle_safe_output_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": SAFE_OUTPUT_MCP_TOOL,
                    "arguments": {
                        "run_id": "run-mcp-no-id",
                        "role_id": "product-manager",
                        "tool_name": "status.reply",
                        "payload": {"message": "Should not record."},
                    },
                },
            },
        )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert response is not None
    assert response["id"] is None
    assert response["error"]["code"] == -32602
    assert "requires a JSON-RPC request id" in response["error"]["message"]
    assert calls == []


def test_safe_output_mcp_rejects_unknown_mcp_tool_without_recording(tmp_path: Path) -> None:
    db = _db_with_run(tmp_path, "run-mcp-unknown-tool")
    try:
        response = handle_safe_output_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "unsafe.tool",
                    "arguments": {
                        "run_id": "run-mcp-unknown-tool",
                        "role_id": "product-manager",
                        "tool_name": "status.reply",
                        "payload": {"message": "Nope."},
                    },
                },
            },
        )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert response is not None
    assert response["error"]["code"] == -32602
    assert "unsupported MCP tool" in response["error"]["message"]
    assert calls == []


def test_safe_output_mcp_rejects_wrong_role_through_shared_validation(tmp_path: Path) -> None:
    db = _db_with_run(tmp_path, "run-mcp-wrong-role")
    try:
        response = handle_safe_output_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": SAFE_OUTPUT_MCP_TOOL,
                    "arguments": {
                        "run_id": "run-mcp-wrong-role",
                        "role_id": "release-manager",
                        "tool_name": "release.close",
                        "payload": {"work_item_id": "work-1", "reason": "Done."},
                    },
                },
            },
        )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert response is not None
    assert response["error"]["code"] == -32602
    assert "belongs to role `product-manager`" in response["error"]["message"]
    assert calls == []


def test_safe_output_mcp_rejects_fake_durable_claim_through_shared_policy(tmp_path: Path) -> None:
    db = _db_with_run(tmp_path, "run-mcp-fake-claim")
    try:
        response = handle_safe_output_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": SAFE_OUTPUT_MCP_TOOL,
                    "arguments": {
                        "run_id": "run-mcp-fake-claim",
                        "role_id": "product-manager",
                        "tool_name": "status.reply",
                        "payload": {"message": "I created work-123."},
                    },
                },
            },
        )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert response is not None
    assert response["error"]["code"] == -32602
    assert "must not claim durable mutations" in response["error"]["message"]
    assert calls == []


def test_safe_output_mcp_rejects_non_boolean_terminal(tmp_path: Path) -> None:
    db = _db_with_run(tmp_path, "run-mcp-bad-terminal")
    try:
        response = handle_safe_output_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": SAFE_OUTPUT_MCP_TOOL,
                    "arguments": {
                        "run_id": "run-mcp-bad-terminal",
                        "role_id": "product-manager",
                        "tool_name": "status.progress",
                        "payload": {"message": "Still working."},
                        "terminal": "false",
                    },
                },
            },
        )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    assert response is not None
    assert response["error"]["code"] == -32602
    assert "`terminal` must be a boolean" in response["error"]["message"]
    assert calls == []


def test_safe_output_mcp_stdio_handles_json_lines_requests(tmp_path: Path) -> None:
    db = _db_with_run(tmp_path, "run-mcp-stdio")
    request = {
        "jsonrpc": "2.0",
        "id": "stdio-1",
        "method": "tools/call",
        "params": {
            "name": SAFE_OUTPUT_MCP_TOOL,
            "arguments": {
                "run_id": "run-mcp-stdio",
                "role_id": "product-manager",
                "tool_name": "status.reply",
                "payload": {"message": "Product reply through MCP stdio."},
            },
        },
    }
    output = StringIO()
    try:
        run_safe_output_mcp_stdio(
            db,
            input_stream=StringIO(json.dumps(request) + "\n"),
            output_stream=output,
        )
        calls = db.list_safe_output_calls()
    finally:
        db.close()

    response = json.loads(output.getvalue())
    assert response["id"] == "stdio-1"
    assert response["result"]["structuredContent"]["status"] == "ok"
    assert len(calls) == 1


def test_safe_output_mcp_stdio_returns_parse_error() -> None:
    db = V2Database(":memory:")
    db.migrate()
    output = StringIO()
    try:
        run_safe_output_mcp_stdio(
            db,
            input_stream=StringIO("{not-json}\n"),
            output_stream=output,
        )
    finally:
        db.close()

    response = json.loads(output.getvalue())
    assert response["id"] is None
    assert response["error"]["code"] == -32700
    assert "parse error" in response["error"]["message"]


def _db_with_run(tmp_path: Path, run_id: str) -> V2Database:
    db = V2Database(tmp_path / f"{run_id}.sqlite3")
    db.migrate()
    db.create_run(
        run_id=run_id,
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        work_item_id=None,
    )
    return db


def _db_with_work_run(tmp_path: Path, run_id: str) -> V2Database:
    db = V2Database(tmp_path / f"{run_id}.sqlite3")
    db.migrate()
    db.create_queue_item(
        queue_item_id="queue-mcp-document",
        title="MCP document publication",
        summary="Publish a document through MCP safe output.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-mcp-document", actor_role="product-manager", reason="Ready.")
    db.promote_queue_item(
        queue_item_id="queue-mcp-document",
        work_item_id="work-mcp-document",
        owner_role="product-manager",
    )
    db.create_run(
        run_id=run_id,
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        work_item_id="work-mcp-document",
    )
    return db


def _product_definition_content() -> str:
    return """# Product Definition

## Objective

Publish a valid product definition through MCP.

## Scope

The slice covers MCP safe-output recording and document artifact publication.

## Non-Goals

It does not add remote document backends.

## Acceptance Criteria

- MCP records a document safe-output call.
- The document is written under the configured library root.

## Sponsor Questions

No sponsor questions remain open for this test document.

## Review Log

- REV-0001 | product-manager | accepted | Initial MCP document evidence.
"""
