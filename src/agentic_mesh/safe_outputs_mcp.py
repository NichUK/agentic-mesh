from __future__ import annotations

import json
import sys
from typing import Any

from agentic_mesh.safe_outputs import SAFE_OUTPUT_TOOLS
from agentic_mesh.safe_outputs import append_safe_output_record
from agentic_mesh.safe_outputs import safe_output_context_from_env
from agentic_mesh.safe_outputs import safe_output_file_from_env


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = _handle_request(request)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": str(exc)},
            }
        sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
        sys.stdout.flush()
    return 0


def _handle_request(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {
                    "name": "agentic-mesh-safeoutputs",
                    "version": "safe-output-v1",
                },
                "capabilities": {"tools": {}},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": tool,
                        "description": f"Record Agentic Mesh safe-output `{tool}`.",
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    }
                    for tool in SAFE_OUTPUT_TOOLS
                ]
            },
        }
    if method == "tools/call":
        params = request.get("params") or {}
        tool = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        record = append_safe_output_record(
            output_file=safe_output_file_from_env(),
            tool=tool,
            payload=dict(arguments),
            context=safe_output_context_from_env(),
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "tool": record.tool,
                                "recorded_at": record.recorded_at,
                                "validation": record.validation,
                            },
                            sort_keys=True,
                        ),
                    }
                ],
                "isError": False,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unknown method `{method}`"},
    }


if __name__ == "__main__":
    raise SystemExit(main())
