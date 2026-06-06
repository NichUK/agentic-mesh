from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _write_project(path: Path) -> None:
    path.write_text(
        """
project_id: cli-export-project
name: CLI Export Project
workspace:
  root: .
  default_repository: cli-export-project
  repositories:
    cli-export-project:
      type: git
      path: .
document_library:
  backend: filesystem
  root: docs
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: stub
      model: stub
    instructions: []
    write_paths: []
    channels: {}
  qa-engineer:
    template: qa-engineer
    instances: 1
    worker:
      adapter: stub
      model: stub
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: cli-flow
  entry_state: define
  work_item_types:
    - slice
  states:
    define:
      owner_role: product-manager
      purpose: Define.
      artifact_path: work-items/{work_item_id}/define.md
      gates:
        - gate_id: definition_review
          type: document_owner_review
      handoffs:
        completed:
          target_state: verify
          target_role: qa-engineer
          message_type: sdlc.verify
    verify:
      owner_role: qa-engineer
      purpose: Verify.
      artifact_path: work-items/{work_item_id}/verify.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )


def _run_cli(tmp_path: Path, project_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    env["AGENTIC_MESH_OTEL_ENABLED"] = "false"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "--config-root",
            str(Path.cwd()),
            "--project-file",
            str(project_file),
            "--workspace-root",
            str(tmp_path),
            "--state-root",
            str(tmp_path / "state"),
            *args,
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_flow_export_cli_writes_project_relative_json_and_replaces(
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    _write_project(project_file)
    output = "work-items/work-cli/lifecycle-flow.md"
    args = [
        "flow-export",
        "--format",
        "markdown",
        "--work-item-id",
        "work-cli",
        "--queue-item-id",
        "queue-cli",
        "--output",
        output,
    ]

    first = _run_cli(tmp_path, project_file, *args)
    second = _run_cli(tmp_path, project_file, *args)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload == {
        "flow_id": "cli-flow",
        "format": "markdown",
        "output": output,
        "project_id": "cli-export-project",
        "queue_item_id": "queue-cli",
        "registered": True,
        "work_item_id": "work-cli",
    }
    assert str(tmp_path) not in second.stdout
    artifact = tmp_path / "docs" / output
    content = artifact.read_text(encoding="utf-8")
    assert content.count("# Lifecycle Flow") == 1
    assert "verify (terminal)" in content
    assert "secret_ref" not in content
    assert "mount_ref" not in content


def test_flow_export_cli_rejects_bad_format_and_path_before_write(
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    _write_project(project_file)

    bad_format = _run_cli(
        tmp_path,
        project_file,
        "flow-export",
        "--format",
        "html",
        "--work-item-id",
        "work-cli",
        "--output",
        "work-items/work-cli/lifecycle-flow.md",
    )
    bad_path = _run_cli(
        tmp_path,
        project_file,
        "flow-export",
        "--format",
        "markdown",
        "--work-item-id",
        "work-cli",
        "--output",
        "../escape.md",
    )
    work_item_folder_path = _run_cli(
        tmp_path,
        project_file,
        "flow-export",
        "--format",
        "markdown",
        "--work-item-id",
        "work-cli",
        "--output",
        "work-items/work-cli/",
    )

    assert bad_format.returncode == 1
    assert bad_path.returncode == 1
    assert work_item_folder_path.returncode == 1
    assert not (tmp_path / "docs").exists()
    assert "unsupported format" in bad_format.stderr
    assert "traverse" in bad_path.stderr
    assert "work-items/work-cli/" in work_item_folder_path.stderr


def test_work_item_index_cli_validate_and_backfill_repair_duplicate_body(
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    _write_project(project_file)
    work_dir = tmp_path / "docs" / "work-items" / "work-cli"
    work_dir.mkdir(parents=True)
    (work_dir / "10-business-brief.md").write_text(
        """
# Business Brief

Work item type: `slice`
Lifecycle state: `define`
Owner role: `product-manager`
Review status: `approved`
Summary: CLI backfill summary.
""".strip(),
        encoding="utf-8",
    )
    duplicate = """
# Work Item Index: Duplicate

Work item: `work-cli`
Work item type: `slice`
Lifecycle state at last index update: `define`
Owner role at last index update: `product-manager`
Queue item: `not found in visible artifacts`
Source message: `not found in visible artifacts`
Source anchor: `not found in visible artifacts`
Summary: Duplicate.

## Documents

| Document | Purpose | Owner role | Review status |
| --- | --- | --- | --- |
| [10-business-brief.md](10-business-brief.md) | Brief. | `product-manager` | `approved` |

## Update Practice

Update in place.

## Review Log

### REV-CLI-0001 | product-manager | approved

- Scope: fixture
- Required change: none
- Disposition: approved
""".strip()
    (work_dir / "00-index.md").write_text(
        duplicate + "\n" + duplicate,
        encoding="utf-8",
    )

    before = _run_cli(
        tmp_path,
        project_file,
        "work-item-index",
        "validate",
    )
    execute = _run_cli(
        tmp_path,
        project_file,
        "work-item-index",
        "backfill",
        "--execute",
    )
    after = _run_cli(
        tmp_path,
        project_file,
        "work-item-index",
        "validate",
    )

    assert before.returncode == 1
    assert "duplicate_index_body" in before.stdout
    assert execute.returncode == 0, execute.stderr
    payload = json.loads(execute.stdout)
    assert payload["repaired"] == 1
    assert payload["failed"] == 0
    assert after.returncode == 0, after.stdout
    assert (work_dir / "00-index.md").read_text(encoding="utf-8").count(
        "# Work Item Index:"
    ) == 1
    assert (tmp_path / "docs" / "work-items" / "index.md").exists()


def test_document_library_cli_validation_and_migration_reports(
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    _write_project(project_file)
    work_dir = tmp_path / "docs" / "work-items" / "work-cli"
    work_dir.mkdir(parents=True)
    (work_dir / "10-business-brief.md").write_text("# Brief\n", encoding="utf-8")

    validation = _run_cli(
        tmp_path,
        project_file,
        "document-library",
        "validate",
        "--work-item-id",
        "work-cli",
        "--format",
        "json",
    )
    validation_report = _run_cli(
        tmp_path,
        project_file,
        "document-library",
        "validate",
        "--work-item-id",
        "work-cli",
        "--write-report",
        "work-items/work-cli/document-library-validation.json",
        "--format",
        "json",
    )
    migration = _run_cli(
        tmp_path,
        project_file,
        "document-library",
        "migration-dry-run",
        "--work-item-id",
        "work-cli",
        "--format",
        "json",
        "--write-report",
        "work-items/work-cli/document-library-migration.json",
    )

    assert validation.returncode == 1
    assert "missing_metadata" in validation.stdout
    assert validation_report.returncode == 1
    assert migration.returncode == 0, migration.stderr
    assert (
        tmp_path
        / "docs"
        / "work-items"
        / "work-cli"
        / "document-library-validation.json"
    ).exists()
    migration_report = (
        tmp_path / "docs" / "work-items" / "work-cli" / "document-library-migration.json"
    ).read_text(encoding="utf-8")
    assert "100-business-brief.md" in migration_report
    assert (work_dir / "10-business-brief.md").exists()
    assert not (work_dir / "100-business-brief.md").exists()
