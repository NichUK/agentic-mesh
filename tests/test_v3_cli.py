from pathlib import Path
import subprocess
import sys

from agentic_mesh_v3.cli import main


def test_cli_validate_topology_reports_valid_paths(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    result = main(
        [
            "validate-topology",
            "--source-repo",
            str(tmp_path / "source"),
            "--deployed-runtime",
            str(tmp_path / "runtime"),
            "--runtime-state",
            str(tmp_path / "state"),
            "--organisation-config-repo",
            str(tmp_path / "org"),
            "--project-config-repo",
            str(tmp_path / "project"),
            "--document-library-root",
            str(tmp_path / "documents"),
        ]
    )

    output = capsys.readouterr().out

    assert result == 0
    assert '"valid": true' in output


def test_cli_validate_topology_fails_collapsed_paths(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    result = main(
        [
            "validate-topology",
            "--source-repo",
            str(tmp_path / "source"),
            "--deployed-runtime",
            str(tmp_path / "source"),
            "--runtime-state",
            str(tmp_path / "state"),
            "--organisation-config-repo",
            str(tmp_path / "org"),
            "--project-config-repo",
            str(tmp_path / "project"),
            "--document-library-root",
            str(tmp_path / "documents"),
        ]
    )

    output = capsys.readouterr().out

    assert result == 1
    assert '"valid": false' in output
    assert "source_repo and deployed_runtime" in output


def test_cli_module_entrypoint_runs(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh_v3.cli",
            "--db",
            str(tmp_path / "v3.sqlite3"),
            "init-db",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert '"status": "initialized"' in completed.stdout


def test_cli_tool_call_uses_project_config_document_library(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    docs_root = tmp_path / "documents"
    project_config.write_text(
        f"""
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: filesystem
  root: {docs_root.as_posix()}
roles:
  product-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )

    result = main(
        [
            "--db",
            str(tmp_path / "v3.sqlite3"),
            "--project-config",
            str(project_config),
            "tool-call",
            "--role-instance-id",
            "agentic-mesh-dev.product-manager.1",
            "--tool-name",
            "document.write_work_item_index",
            "--payload-json",
            (
                '{"work_item_id":"work-1","title":"Work One","status":"active",'
                '"owner_role":"product-manager","raci_summary":"PM A/R",'
                '"governance_state":"ready"}'
            ),
        ]
    )

    assert result == 0
    assert '"tool_name": "document.write_work_item_index"' in capsys.readouterr().out
    assert (docs_root / "work-items" / "work-1" / "index.md").exists()
