from io import StringIO
from pathlib import Path
import json

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.tool_mcp import V3_TOOL_CATALOG_MCP_TOOL
from agentic_mesh_v3.tool_mcp import V3_TOOL_MCP_TOOL
from agentic_mesh_v3.tool_mcp import handle_v3_mcp_request
from agentic_mesh_v3.tool_mcp import run_v3_mcp_stdio


def test_v3_mcp_lists_tool(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        response = handle_v3_mcp_request(db, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        db.close()

    assert response is not None
    tools = response["result"]["tools"]
    tool_names = {tool["name"] for tool in tools}
    assert V3_TOOL_MCP_TOOL in tool_names
    assert V3_TOOL_CATALOG_MCP_TOOL in tool_names
    call_tool = next(tool for tool in tools if tool["name"] == V3_TOOL_MCP_TOOL)
    catalog_tool = next(tool for tool in tools if tool["name"] == V3_TOOL_CATALOG_MCP_TOOL)
    assert "role_instance_id" in call_tool["inputSchema"]["required"]
    assert catalog_tool["annotations"]["readOnlyHint"] is True


def test_v3_mcp_tool_catalog_returns_role_permissions(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        response = handle_v3_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "id": "catalog-1",
                "method": "tools/call",
                "params": {
                    "name": V3_TOOL_CATALOG_MCP_TOOL,
                    "arguments": {
                        "role_id": "product-manager",
                    },
                },
            },
        )
    finally:
        db.close()

    assert response is not None
    structured = response["result"]["structuredContent"]
    assert structured["role_id"] == "product-manager"
    tools = {tool["tool_name"]: tool for tool in structured["tools"]}
    assert tools["backlog.upsert"]["allowed"] is True
    assert tools["release.deploy"]["allowed"] is False


def test_v3_mcp_records_tool_call(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        response = handle_v3_mcp_request(
            db,
            {
                "jsonrpc": "2.0",
                "id": "call-1",
                "method": "tools/call",
                "params": {
                    "name": V3_TOOL_MCP_TOOL,
                    "arguments": {
                        "role_instance_id": "agentic-mesh-dev.product-manager.1",
                        "tool_name": "status.reply",
                        "payload": {"message": "Product reply through MCP."},
                    },
                },
            },
        )
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert response is not None
    assert response["result"]["structuredContent"]["status"] == "ok"
    assert response["result"]["structuredContent"]["terminal"] is True
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "status.reply"
    assert calls[0]["payload"]["message"] == "Product reply through MCP."


def test_v3_mcp_stdio_handles_jsonrpc_lines(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "call-stdio",
                "method": "tools/call",
                "params": {
                    "name": V3_TOOL_MCP_TOOL,
                    "arguments": {
                        "role_instance_id": "agentic-mesh-dev.project-manager.1",
                        "tool_name": "status.update",
                        "payload": {"message": "Sweep complete."},
                        "terminal": True,
                    },
                },
            }
        )
        + "\n"
    )
    output_stream = StringIO()
    try:
        db.migrate()
        run_v3_mcp_stdio(db, input_stream=input_stream, output_stream=output_stream)
        calls = db.list_tool_calls()
    finally:
        db.close()

    response = json.loads(output_stream.getvalue())
    assert response["result"]["structuredContent"]["terminal"] is True
    assert calls[0]["tool_name"] == "status.update"
