from __future__ import annotations

import json
import sys
from typing import Any
from typing import TextIO

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.tool_catalog import tool_catalog_for_role
from agentic_mesh_v3.tools import V3ToolService


V3_TOOL_MCP_TOOL = "agentic_mesh_v3.tool_call"
V3_TOOL_CATALOG_MCP_TOOL = "agentic_mesh_v3.tool_catalog"


def v3_mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": V3_TOOL_CATALOG_MCP_TOOL,
            "description": "List Agentic Mesh V3 safe-output tools allowed for a role.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["role_id"],
                "properties": {
                    "role_id": {
                        "type": "string",
                        "description": "Role id, for example product-manager or release-manager.",
                    },
                },
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
        {
            "name": V3_TOOL_MCP_TOOL,
            "description": "Record one Agentic Mesh V3 tool call for a role instance.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["role_instance_id", "tool_name", "payload"],
                "properties": {
                    "role_instance_id": {
                        "type": "string",
                        "description": "Concrete role instance id, for example agentic-mesh-dev.product-manager.1.",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "V3 tool name, for example status.reply or work_item.upsert.",
                    },
                    "payload": {
                        "type": "object",
                        "description": "V3 tool payload object.",
                    },
                    "terminal": {
                        "type": "boolean",
                        "description": "Optional terminal marker.",
                        "default": False,
                    },
                },
            },
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
        }
    ]


def handle_v3_mcp_request(
    db: V3Database,
    request: dict[str, Any],
    *,
    service: V3ToolService | None = None,
) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return _error_response(None, -32600, "JSON-RPC request must be an object")
    request_id = request.get("id")
    method = request.get("method")
    if not isinstance(method, str) or not method:
        return _error_response(request_id, -32600, "JSON-RPC request requires a method")

    try:
        if method == "initialize":
            return _success_response(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "agentic-mesh-v3-tools", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _success_response(request_id, {"tools": v3_mcp_tools()})
        if method == "tools/call":
            return _handle_tool_call(db, request_id=request_id, params=request.get("params"), service=service)
    except ValueError as exc:
        return _error_response(request_id, -32602, str(exc))

    return _error_response(request_id, -32601, f"unsupported MCP method `{method}`")


def run_v3_mcp_stdio(
    db: V3Database,
    *,
    service: V3ToolService | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> None:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    for line in input_stream:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response: dict[str, Any] | None = _error_response(None, -32700, f"parse error: {exc.msg}")
        else:
            response = handle_v3_mcp_request(db, request, service=service)
        if response is not None:
            output_stream.write(json.dumps(response, sort_keys=True))
            output_stream.write("\n")
            output_stream.flush()


def _handle_tool_call(
    db: V3Database,
    *,
    request_id: object,
    params: object,
    service: V3ToolService | None = None,
) -> dict[str, Any]:
    if request_id is None:
        raise ValueError("tools/call requires a JSON-RPC request id")
    if not isinstance(params, dict):
        raise ValueError("tools/call params must be an object")
    if params.get("name") != V3_TOOL_MCP_TOOL:
        if params.get("name") == V3_TOOL_CATALOG_MCP_TOOL:
            return _handle_tool_catalog(request_id=request_id, params=params)
        raise ValueError(f"unsupported MCP tool `{params.get('name')}`")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("tools/call arguments must be an object")
    payload = arguments.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("V3 tool payload must be a JSON object")
    role_instance_id = _required_text(arguments.get("role_instance_id"), "role_instance_id")
    tool_name = _required_text(arguments.get("tool_name"), "tool_name")
    terminal = arguments.get("terminal", False)
    if not isinstance(terminal, bool):
        raise ValueError("`terminal` must be a boolean when provided")
    tool_service = service or V3ToolService(db)
    receipt = tool_service.call(
        role_instance_id=role_instance_id,
        tool_name=tool_name,
        payload=payload,
        terminal=terminal,
    )
    structured = {
        "status": "ok",
        "role_instance_id": role_instance_id,
        "tool_name": tool_name,
        "call_id": receipt.call_id,
        "terminal": receipt.terminal,
    }
    return _success_response(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(structured, sort_keys=True)}],
            "structuredContent": structured,
            "isError": False,
        },
    )


def _handle_tool_catalog(*, request_id: object, params: object) -> dict[str, Any]:
    if request_id is None:
        raise ValueError("tools/call requires a JSON-RPC request id")
    if not isinstance(params, dict):
        raise ValueError("tools/call params must be an object")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("tools/call arguments must be an object")
    role_id = _required_text(arguments.get("role_id"), "role_id")
    structured = {
        "role_id": role_id,
        "tools": [entry.to_dict() for entry in tool_catalog_for_role(role_id)],
    }
    return _success_response(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(structured, sort_keys=True)}],
            "structuredContent": structured,
            "isError": False,
        },
    )


def _success_response(request_id: object, result: dict[str, Any]) -> dict[str, Any] | None:
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: object, code: int, message: str) -> dict[str, Any] | None:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{name}` must be a non-empty string")
    return value.strip()
