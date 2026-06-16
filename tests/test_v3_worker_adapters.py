from __future__ import annotations

from pathlib import Path
import sys

from agentic_mesh_v3.agent import AgentMessage
from agentic_mesh_v3.worker_adapters import build_worker_adapter
from agentic_mesh_v3.worker_adapters import CodexCliWorker
from agentic_mesh_v3.worker_adapters import _codex_command_with_options
from agentic_mesh_v3.worker_adapters import SafeOutputSubprocessWorker


def test_safe_output_subprocess_worker_returns_tool_call_status(tmp_path: Path) -> None:
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        "import json, sys\n"
        "payload=json.load(sys.stdin)\n"
        "assert '<agentic-mesh-v3-agent>' in payload['prompt']\n"
        "assert payload['message']['message_id'] == 'msg-1'\n"
        "print(json.dumps({'tool_calls':['call-1', {'tool_name':'status.reply', 'call_id':'call-2', 'terminal': True}]}))\n",
        encoding="utf-8",
    )
    worker = SafeOutputSubprocessWorker(command=(sys.executable, str(worker_script)), timeout_seconds=5)

    calls = worker.run(
        "<agentic-mesh-v3-agent>prompt</agentic-mesh-v3-agent>",
        AgentMessage(message_id="msg-1", subject="agent.product-manager", payload={"text": "Hello"}),
    )

    assert calls == ["call-1", "terminal:status.reply"]


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


def test_codex_cli_worker_wraps_prompt_with_safe_output_contract(tmp_path: Path) -> None:
    worker_script = tmp_path / "codex_worker.py"
    worker_script.write_text(
        "import json, sys\n"
        "prompt=sys.stdin.read()\n"
        "assert '<agentic-mesh-v3-agent>' in prompt\n"
        "assert 'SAFE-OUTPUT TOOL CONTRACT' in prompt\n"
        "assert 'At least one successful call must be a terminal safe-output tool' in prompt\n"
        "assert '\"terminal\":true' in prompt\n"
        "assert 'status.reply' in prompt\n"
        "assert 'text_markdown' in prompt\n"
        "assert 'msg-1' in prompt\n"
        "print(json.dumps({'tool_calls':['status.reply']}))\n",
        encoding="utf-8",
    )
    worker = CodexCliWorker(command=(sys.executable, str(worker_script)), timeout_seconds=5)

    calls = worker.run(
        "<agentic-mesh-v3-agent>prompt</agentic-mesh-v3-agent>",
        AgentMessage(message_id="msg-1", subject="agent.product-manager", payload={"text": "Hello"}),
    )

    assert calls == ["status.reply"]


def test_codex_cli_command_builder_adds_exec_options() -> None:
    command = _codex_command_with_options(
        ("codex", "exec"),
        model="gpt-5.5",
        reasoning_effort="high",
        sandbox_mode="workspace-write",
    )

    assert command == [
        "codex",
        "exec",
        "--model",
        "gpt-5.5",
        "--sandbox",
        "workspace-write",
        "--config",
        'model_reasoning_effort="high"',
    ]


def test_build_worker_adapter_creates_codex_cli_worker() -> None:
    worker = build_worker_adapter(
        adapter="codex-cli",
        command=("codex", "exec"),
        timeout_seconds=30,
        model="gpt-5.5",
        reasoning_effort="medium",
        sandbox_mode="danger-full-access",
    )

    assert isinstance(worker, CodexCliWorker)
    assert worker.command == ("codex", "exec")
    assert worker.timeout_seconds == 30
    assert worker.model == "gpt-5.5"
    assert worker.reasoning_effort == "medium"
    assert worker.sandbox_mode == "danger-full-access"
