from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from subprocess import CompletedProcess

from agentic_mesh_v3.agent import AgentMessage
import agentic_mesh_v3.worker_adapters as worker_adapters
from agentic_mesh_v3.worker_adapters import build_worker_adapter
from agentic_mesh_v3.worker_adapters import CodexCliWorker
from agentic_mesh_v3.worker_adapters import _codex_command_with_options
from agentic_mesh_v3.worker_adapters import _codex_resume_command_with_options
from agentic_mesh_v3.worker_adapters import PersistentSessionWorker
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


def test_worker_subprocess_receives_message_reply_route_environment(tmp_path: Path) -> None:
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        "import json, os\n"
        "assert os.environ['AGENTIC_MESH_MESSAGE_ID'] == 'msg-1'\n"
        "assert os.environ['AGENTIC_MESH_CORRELATION_ID'] == 'corr-msg-1'\n"
        "assert os.environ['AGENTIC_MESH_CONNECTOR'] == 'teams'\n"
        "assert os.environ['AGENTIC_MESH_CONVERSATION_REF'] == 'dm:project-manager'\n"
        "assert os.environ['AGENTIC_MESH_REPLY_TARGET_REF'] == 'chat:conversation-1'\n"
        "assert os.environ['AGENTIC_MESH_REPLY_THREAD_REF'] == 'thread-1'\n"
        "print(json.dumps({'tool_calls':[{'tool_name':'status.reply','call_id':'call-1','terminal':True}]}))\n",
        encoding="utf-8",
    )
    worker = SafeOutputSubprocessWorker(command=(sys.executable, str(worker_script)), timeout_seconds=5)

    calls = worker.run(
        "prompt",
        AgentMessage(
            message_id="msg-1",
            subject="agent.project-manager.priority",
            payload={
                "connector": "teams",
                "conversation_ref": "dm:project-manager",
                "reply_target_ref": "chat:conversation-1",
                "reply_thread_ref": "thread-1",
            },
        ),
    )

    assert calls == ["terminal:status.reply"]


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


def test_safe_output_subprocess_worker_times_out_process_tree(tmp_path: Path) -> None:
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    worker = SafeOutputSubprocessWorker(command=(sys.executable, str(worker_script)), timeout_seconds=1)

    try:
        worker.run("prompt", AgentMessage(message_id="msg-1", subject="agent.product-manager", payload={}))
    except subprocess.TimeoutExpired as exc:
        assert "worker subprocess timed out after 1 seconds" in str(exc.stderr)
    else:
        raise AssertionError("timed-out worker should raise")


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
        "assert 'All roles share the same standing operating instructions' in prompt\n"
        "assert 'Every run must include a DO safe-output call' in prompt\n"
        "assert 'Non-terminal work MUST establish who owns the next step' in prompt\n"
        "assert 'A reply alone is not enough for non-terminal work' in prompt\n"
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


def test_codex_resume_command_builder_adds_resume_options() -> None:
    command = _codex_resume_command_with_options(
        ("codex", "exec"),
        model="gpt-5.5",
        reasoning_effort="high",
    )

    assert command == [
        "codex",
        "exec",
        "resume",
        "--last",
        "--model",
        "gpt-5.5",
        "--config",
        'model_reasoning_effort="high"',
        "-",
    ]


def test_persistent_session_worker_resumes_after_first_success(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    commands: list[list[str]] = []

    def fake_run_worker_command(command, *, input, capture_output, text, timeout, env=None):  # type: ignore[no-untyped-def]
        del capture_output, text, timeout, env
        commands.append(list(command))
        assert "SAFE-OUTPUT TOOL CONTRACT" in input
        return CompletedProcess(
            list(command),
            0,
            '{"tool_calls":[{"tool_name":"status.reply","terminal":true}]}',
            "",
        )

    monkeypatch.setattr(worker_adapters, "_run_worker_command", fake_run_worker_command)
    worker = PersistentSessionWorker(
        codex_worker=CodexCliWorker(
            command=("codex", "exec"),
            timeout_seconds=5,
        )
    )

    worker.run("prompt-one", AgentMessage(message_id="msg-1", subject="agent.project-manager", payload={}))
    worker.run("prompt-two", AgentMessage(message_id="msg-2", subject="agent.project-manager", payload={}))

    assert commands[0] == ["codex", "exec"]
    assert commands[1] == ["codex", "exec", "resume", "--last", "-"]
    assert worker.session_mode == "codex-exec-resume"
    assert worker.session_status == "active"


def test_build_persistent_session_worker_marks_custom_command_degraded() -> None:
    worker = build_worker_adapter(
        adapter="persistent-session",
        command=("python", "-c", "print('{}')"),
    )

    assert isinstance(worker, PersistentSessionWorker)
    assert worker.session_mode == "resume-backed-degraded"
    assert worker.session_status == "degraded"


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
