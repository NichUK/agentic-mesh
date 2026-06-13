from pathlib import Path
import sys

import pytest

from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.worker_adapters import CodexCliWorker
from agentic_mesh_v2.worker_adapters import SafeOutputFileWorker
from agentic_mesh_v2.worker_adapters import SafeOutputSubprocessWorker
from agentic_mesh_v2.worker_adapters import build_worker_adapter
from agentic_mesh_v2.worker_adapters import safe_output_file_payload
from agentic_mesh_v2.worker_adapters import _codex_command_with_options


def _assignment() -> RoleAssignment:
    return RoleAssignment(
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        work_item_id=None,
        title="Adapter assignment",
        summary="Exercise worker adapter config.",
    )


def test_build_worker_adapter_creates_safe_output_file_worker(tmp_path: Path) -> None:
    calls_path = tmp_path / "calls.json"
    calls_path.write_text(
        safe_output_file_payload(
            [
                {
                    "tool_name": "status.complete",
                    "payload": {"message": "Configured file worker complete."},
                    "terminal": True,
                }
            ]
        ),
        encoding="utf-8",
    )

    worker = build_worker_adapter({"adapter": "safe-output-file", "path": str(calls_path)})
    calls = worker.run(_assignment())

    assert isinstance(worker, SafeOutputFileWorker)
    assert calls[0].tool_name == "status.complete"
    assert calls[0].payload["message"] == "Configured file worker complete."


def test_build_worker_adapter_creates_safe_output_subprocess_worker() -> None:
    worker = build_worker_adapter(
        {
            "adapter": "safe-output-subprocess",
            "command": ["python", "-c", "print('{}')"],
            "timeout_seconds": 5,
        }
    )

    assert isinstance(worker, SafeOutputSubprocessWorker)
    assert worker.command == ("python", "-c", "print('{}')")
    assert worker.timeout_seconds == 5


def test_build_worker_adapter_creates_codex_cli_worker() -> None:
    worker = build_worker_adapter(
        {
            "adapter": "codex-cli",
            "command": [
                sys.executable,
                "-c",
                (
                    "import json, sys; "
                    "payload=json.load(sys.stdin); "
                    "assert payload['worker']['adapter'] == 'codex-cli'; "
                    "assert payload['worker']['model'] == 'gpt-test'; "
                    "assert payload['assignment']['title'] == 'Adapter assignment'; "
                    "print(json.dumps({'calls':[{'tool_name':'status.complete',"
                    "'payload':{'message':'Codex adapter complete.'},'terminal':True}]}))"
                ),
            ],
            "timeout_seconds": 5,
            "model": "gpt-test",
            "reasoning_effort": "high",
            "sandbox_mode": "workspace-write",
            "auth": {"credential": "codex-test"},
        }
    )

    calls = worker.run(_assignment())

    assert isinstance(worker, CodexCliWorker)
    assert worker.timeout_seconds == 5
    assert worker.model == "gpt-test"
    assert worker.reasoning_effort == "high"
    assert worker.sandbox_mode == "workspace-write"
    assert worker.auth == {"credential": "codex-test"}
    assert calls[0].tool_name == "status.complete"
    assert calls[0].payload["message"] == "Codex adapter complete."


def test_codex_cli_worker_allows_non_json_stdout_when_safe_output_transport_is_available() -> None:
    worker = build_worker_adapter(
        {
            "adapter": "codex-cli",
            "command": [sys.executable, "-c", "print('human summary, not JSON')"],
            "timeout_seconds": 5,
        }
    )
    assignment = RoleAssignment(
        role_id="product-manager",
        role_instance_id="agentic-mesh-dev.product-manager.1",
        work_item_id=None,
        title="Adapter assignment",
        summary="Exercise tool transport stdout handling.",
        safe_output_transport={"transport": "cli"},
    )

    calls = worker.run(assignment)

    assert calls == []


def test_build_worker_adapter_creates_default_codex_cli_worker() -> None:
    worker = build_worker_adapter({"adapter": "codex-cli"})

    assert isinstance(worker, CodexCliWorker)
    assert worker.command == ("codex", "exec")
    assert worker.timeout_seconds == 14400


def test_codex_cli_worker_applies_runtime_options_to_real_codex_exec_command() -> None:
    command = _codex_command_with_options(
        ("codex", "exec"),
        model="gpt-5.5",
        reasoning_effort="high",
        sandbox_mode="danger-full-access",
    )

    assert command == (
        "codex",
        "exec",
        "--model",
        "gpt-5.5",
        "--sandbox",
        "danger-full-access",
        "--config",
        'model_reasoning_effort="high"',
    )


def test_codex_cli_worker_accepts_schema_reasoning_effort_values() -> None:
    for effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
        worker = build_worker_adapter({"adapter": "codex-cli", "reasoning_effort": effort})

        assert isinstance(worker, CodexCliWorker)
        assert worker.reasoning_effort == effort


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "unsupported worker adapter"),
        ({"adapter": "safe-output-file"}, "requires non-empty path"),
        ({"adapter": "safe-output-subprocess"}, "requires non-empty command list"),
        (
            {"adapter": "safe-output-subprocess", "command": ["python"], "timeout_seconds": "slow"},
            "timeout_seconds must be an integer",
        ),
        (
            {"adapter": "safe-output-subprocess", "command": ["python"], "timeout_seconds": True},
            "timeout_seconds must be an integer",
        ),
        (
            {"adapter": "safe-output-subprocess", "command": ["python", ""]},
            "command item 1 must be a non-empty string",
        ),
        ({"adapter": "codex-cli", "command": []}, "requires non-empty command list"),
        ({"adapter": "codex-cli", "command": ["codex", ""]}, "command item 1 must be a non-empty string"),
        ({"adapter": "codex-cli", "timeout_seconds": "slow"}, "timeout_seconds must be an integer"),
        ({"adapter": "codex-cli", "reasoning_effort": "extreme"}, "reasoning_effort must be one of"),
        ({"adapter": "codex-cli", "auth": "secret"}, "auth must be a mapping"),
    ],
)
def test_build_worker_adapter_rejects_invalid_config(config: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_worker_adapter(config)
