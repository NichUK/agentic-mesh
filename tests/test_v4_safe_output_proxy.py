from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from agentic_mesh_v4.cli import main as cli_main
from agentic_mesh_v4.safe_output_proxy import SafeOutputProxyServer
from agentic_mesh_v4.safe_output_proxy import validated_cli_argv


def test_role_proxy_rebuilds_safe_output_command_with_fixed_project_config(tmp_path: Path) -> None:
    project_config = tmp_path / "project-v4.yaml"

    result = validated_cli_argv(
        request_argv=[
            "--project-config",
            "/untrusted/project.yaml",
            "safe-output",
            "architecture-impact",
            "--role-id",
            "enterprise-architect",
            "--work-item-id",
            "work-1",
            "--classification",
            "uncertain",
            "--rationale",
            "Requires review",
        ],
        role_id="enterprise-architect",
        project_config=project_config,
    )

    assert result[:3] == ["--project-config", str(project_config), "safe-output"]
    assert "/untrusted/project.yaml" not in result


def test_role_proxy_rejects_cross_role_authority(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="cannot act as product-manager"):
        validated_cli_argv(
            request_argv=[
                "safe-output",
                "work-item-update",
                "--role-id",
                "product-manager",
                "--work-item-id",
                "work-1",
            ],
            role_id="enterprise-architect",
            project_config=tmp_path / "project-v4.yaml",
        )


def test_cli_forwards_safe_output_without_opening_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix domain sockets are exercised by the Linux deployment tests")
    socket_path = tmp_path / "safe-output.sock"
    recorded: list[list[str]] = []

    def executor(argv: list[str]) -> tuple[int, str, str]:
        recorded.append(argv)
        return 0, '{"status": "recorded"}\n', ""

    server = SafeOutputProxyServer(
        socket_path=socket_path,
        role_id="enterprise-architect",
        project_config=tmp_path / "canonical-project-v4.yaml",
        executor=executor,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGENTIC_MESH_SAFE_OUTPUT_SOCKET", str(socket_path))
    monkeypatch.delenv("AGENTIC_MESH_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_DATABASE_PASSWORD", raising=False)
    monkeypatch.delenv("AGENTIC_MESH_DATABASE_PASSWORD_FILE", raising=False)
    try:
        cli_main(
            [
                "--project-config",
                str(tmp_path / "does-not-need-to-exist.yaml"),
                "safe-output",
                "memory-record",
                "--role-id",
                "enterprise-architect",
                "--summary",
                "Portfolio stewardship",
                "--source-ref",
                "documents/020-architecture/enterprise/000-index.md",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert recorded
    assert recorded[0][:3] == [
        "--project-config",
        str(tmp_path / "canonical-project-v4.yaml"),
        "safe-output",
    ]
    assert capsys.readouterr().out == '{"status": "recorded"}\n'
