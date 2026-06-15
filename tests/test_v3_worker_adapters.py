from __future__ import annotations

from pathlib import Path
import sys

from agentic_mesh_v3.agent import AgentMessage
from agentic_mesh_v3.worker_adapters import build_worker_adapter
from agentic_mesh_v3.worker_adapters import SafeOutputSubprocessWorker


def test_safe_output_subprocess_worker_returns_tool_call_status(tmp_path: Path) -> None:
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        "import json, sys\n"
        "payload=json.load(sys.stdin)\n"
        "assert '<agentic-mesh-v3-agent>' in payload['prompt']\n"
        "assert payload['message']['message_id'] == 'msg-1'\n"
        "print(json.dumps({'tool_calls':['call-1', {'tool_name':'status.reply'}]}))\n",
        encoding="utf-8",
    )
    worker = SafeOutputSubprocessWorker(command=(sys.executable, str(worker_script)), timeout_seconds=5)

    calls = worker.run(
        "<agentic-mesh-v3-agent>prompt</agentic-mesh-v3-agent>",
        AgentMessage(message_id="msg-1", subject="agent.product-manager", payload={"text": "Hello"}),
    )

    assert calls == ["call-1", "status.reply"]


def test_safe_output_subprocess_worker_reports_failure(tmp_path: Path) -> None:
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("import sys\nprint('boom', file=sys.stderr)\nsys.exit(2)\n", encoding="utf-8")
    worker = SafeOutputSubprocessWorker(command=(sys.executable, str(worker_script)), timeout_seconds=5)

    try:
        worker.run("prompt", AgentMessage(message_id="msg-1", subject="agent.product-manager", payload={}))
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("failing subprocess should raise")


def test_safe_output_subprocess_worker_rejects_malformed_stdout(tmp_path: Path) -> None:
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("print('not json')\n", encoding="utf-8")
    worker = SafeOutputSubprocessWorker(command=(sys.executable, str(worker_script)), timeout_seconds=5)

    try:
        worker.run("prompt", AgentMessage(message_id="msg-1", subject="agent.product-manager", payload={}))
    except ValueError as exc:
        assert "stdout must be JSON" in str(exc)
    else:
        raise AssertionError("malformed worker stdout should raise")


def test_build_worker_adapter_creates_subprocess_worker() -> None:
    worker = build_worker_adapter(
        adapter="safe-output-subprocess",
        command=("python", "-c", "print('{}')"),
        timeout_seconds=9,
    )

    assert isinstance(worker, SafeOutputSubprocessWorker)
    assert worker.command == ("python", "-c", "print('{}')")
    assert worker.timeout_seconds == 9
