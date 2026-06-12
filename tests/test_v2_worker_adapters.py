from pathlib import Path

import pytest

from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.worker_adapters import SafeOutputFileWorker
from agentic_mesh_v2.worker_adapters import SafeOutputSubprocessWorker
from agentic_mesh_v2.worker_adapters import build_worker_adapter
from agentic_mesh_v2.worker_adapters import safe_output_file_payload


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
    ],
)
def test_build_worker_adapter_rejects_invalid_config(config: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_worker_adapter(config)
