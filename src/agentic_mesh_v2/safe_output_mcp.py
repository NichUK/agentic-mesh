from __future__ import annotations

import json
import sys
from typing import Any
from typing import TextIO

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputError
from agentic_mesh_v2.safe_outputs import SafeOutputService
from agentic_mesh_v2.safe_output_transport import record_safe_output_for_run


SAFE_OUTPUT_MCP_TOOL = "safe_output.record"


def safe_output_mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": SAFE_OUTPUT_MCP_TOOL,
            "description": "Record one validated Agentic Mesh safe-output tool call for a running role-agent run.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["run_id", "role_id", "tool_name", "payload"],
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "Running agent run id that should own this safe-output call.",
                    },
                    "role_id": {
                        "type": "string",
                        "description": "Role id expected to own the run and safe-output authority.",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Agentic Mesh safe-output tool name, such as status.reply.",
                    },
                    "payload": {
                        "type": "object",
                        "description": "Safe-output payload object validated by the runtime policy.",
                    },
                    "terminal": {
                        "type": "boolean",
                        "description": "Optional terminal marker in addition to terminal-tool defaults.",
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


def handle_safe_output_mcp_request(
    db: V2Database,
    request: dict[str, Any],
    *,
    service: SafeOutputService | None = None,
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
                    "serverInfo": {"name": "agentic-mesh-safe-output", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _success_response(request_id, {"tools": safe_output_mcp_tools()})
        if method == "tools/call":
            return _handle_tool_call(db, request_id=request_id, params=request.get("params"), service=service)
    except (ValueError, SafeOutputError) as exc:
        return _error_response(request_id, -32602, str(exc))

    return _error_response(request_id, -32601, f"unsupported MCP method `{method}`")


def run_safe_output_mcp_stdio(
    db: V2Database,
    *,
    service: SafeOutputService | None = None,
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
            response = handle_safe_output_mcp_request(db, request, service=service)
        if response is not None:
            output_stream.write(json.dumps(response, sort_keys=True))
            output_stream.write("\n")
            output_stream.flush()


def _handle_tool_call(
    db: V2Database,
    *,
    request_id: object,
    params: object,
    service: SafeOutputService | None = None,
) -> dict[str, Any]:
    if request_id is None:
        raise ValueError("tools/call requires a JSON-RPC request id")
    if not isinstance(params, dict):
        raise ValueError("tools/call params must be an object")
    if params.get("name") != SAFE_OUTPUT_MCP_TOOL:
        raise ValueError(f"unsupported MCP tool `{params.get('name')}`")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("tools/call arguments must be an object")

    payload = arguments.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("safe-output payload must be a JSON object")
    terminal = arguments.get("terminal", False)
    if not isinstance(terminal, bool):
        raise ValueError("`terminal` must be a boolean when provided")
    run_id = arguments.get("run_id")
    role_id = arguments.get("role_id")
    tool_name = arguments.get("tool_name")
    receipt = record_safe_output_for_run(
        db,
        run_id=run_id,
        role_id=role_id,
        tool_name=tool_name,
        payload=payload,
        terminal=terminal,
        service=service,
    )
    return _success_response(
        request_id,
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(receipt, sort_keys=True),
                }
            ],
            "structuredContent": receipt,
            "isError": False,
        },
    )

def _success_response(request_id: object, result: dict[str, Any]) -> dict[str, Any] | None:
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: object, code: int, message: str) -> dict[str, Any] | None:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }
