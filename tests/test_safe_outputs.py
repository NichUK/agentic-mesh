import json
import subprocess
import sys
from pathlib import Path

from agentic_mesh.safe_outputs import load_safe_output_records
from agentic_mesh.safe_outputs import safe_output_tools_prompt


def test_safe_output_tools_prompt_preserves_mandatory_finish_contract() -> None:
    prompt = safe_output_tools_prompt()

    assert "You MUST call at least one safe-output tool during every agent run." in prompt
    assert "You MUST call at least one terminal safe-output tool before finishing." in prompt
    assert "The terminal safe-output call is the only valid way to finish a run." in prompt
    assert "status.report_progress` is non-terminal" in prompt
    assert "Do NOT use placeholder, speculative, or fake safe-output calls." in prompt
    assert prompt.index("Terminal tools:") < prompt.index("Available tools:")


def test_safe_output_cli_records_valid_payload(tmp_path: Path) -> None:
    output_file = tmp_path / "safe-outputs.jsonl"
    payload = {"message": "No durable work needed."}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "safe-output",
            "noop",
            ".",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={
            **__import__("os").environ,
            "AGENTIC_MESH_SAFE_OUTPUT_FILE": str(output_file),
            "AGENTIC_MESH_WORK_ITEM_ID": "work-cli",
        },
    )

    assert completed.returncode == 0, completed.stderr
    records = load_safe_output_records(output_file)
    assert records[0].tool == "noop"
    assert records[0].payload == payload


def test_safe_outputs_mcp_records_tool_call(tmp_path: Path) -> None:
    output_file = tmp_path / "safe-outputs.jsonl"
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "status.report_completion",
            "arguments": {"message": "Completed through MCP."},
        },
    }
    completed = subprocess.run(
        [sys.executable, "-m", "agentic_mesh.safe_outputs_mcp"],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env={
            **__import__("os").environ,
            "AGENTIC_MESH_SAFE_OUTPUT_FILE": str(output_file),
            "AGENTIC_MESH_WORK_ITEM_ID": "work-mcp",
        },
    )

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout.splitlines()[0])
    assert response["result"]["isError"] is False
    records = load_safe_output_records(output_file)
    assert records[0].tool == "status.report_completion"
    assert records[0].payload["message"] == "Completed through MCP."
