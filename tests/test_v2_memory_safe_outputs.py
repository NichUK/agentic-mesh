from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputService


def test_memory_safe_output_appends_source_linked_role_memory(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    memory_path = tmp_path / "roles" / "product-manager" / "MEMORY.md"
    try:
        db.migrate()
        db.create_run(
            run_id="run-memory-direct",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id=None,
        )
        call_id = SafeOutputService(
            db,
            project_id="test-project",
            role_memory_path_resolver=lambda role_id: memory_path if role_id == "product-manager" else None,
        ).record(
            run_id="run-memory-direct",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="memory.propose_update",
                payload={
                    "summary": "Sponsor prefers compact dashboard rows.",
                    "provenance_ref": "work-123/020-product-definition.md",
                },
            ),
        )
        role_memory = db.list_role_memory()
    finally:
        db.close()

    text = memory_path.read_text(encoding="utf-8")
    assert "# Role Memory" in text
    assert "## Safe-Output Memory Updates" in text
    assert f"- {call_id} | work-123/020-product-definition.md | Sponsor prefers compact dashboard rows." in text
    assert role_memory == [
        {
            "memory_id": f"memory-{call_id}",
            "role_id": "product-manager",
            "project_id": "test-project",
            "summary": "Sponsor prefers compact dashboard rows.",
            "provenance_ref": "work-123/020-product-definition.md",
            "created_at": role_memory[0]["created_at"],
        }
    ]


def test_memory_safe_output_deduplicates_existing_summary_and_provenance(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    memory_path = tmp_path / "roles" / "product-manager" / "MEMORY.md"
    try:
        db.migrate()
        db.create_run(
            run_id="run-memory-duplicate",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id=None,
        )
        service = SafeOutputService(
            db,
            project_id="test-project",
            role_memory_path_resolver=lambda role_id: memory_path if role_id == "product-manager" else None,
        )
        for _ in range(2):
            service.record(
                run_id="run-memory-duplicate",
                call=SafeOutputCall(
                    role_id="product-manager",
                    tool_name="memory.propose_update",
                    payload={
                        "summary": "Sponsor prefers compact dashboard rows.",
                        "provenance_ref": "work-123/020-product-definition.md",
                    },
                ),
            )
        role_memory = db.list_role_memory()
        event_types = [event["event_type"] for event in db.list_events()]
        indexes = db.connection.execute("PRAGMA index_list(role_memory)").fetchall()
    finally:
        db.close()

    text = memory_path.read_text(encoding="utf-8")
    exact_generated_lines = [
        line
        for line in text.splitlines()
        if line.startswith("- call-")
        and line.endswith("work-123/020-product-definition.md | Sponsor prefers compact dashboard rows.")
    ]
    assert len(exact_generated_lines) == 1
    assert len(role_memory) == 1
    assert event_types.count("role_memory.recorded") == 1
    assert any(row["name"] == "idx_role_memory_unique_fact" and row["unique"] for row in indexes)


def test_memory_safe_output_dedupe_uses_exact_generated_memory_lines(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    memory_path = tmp_path / "roles" / "product-manager" / "MEMORY.md"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text(
        "# Product Manager Memory\n\n"
        "- older note | work-123/020-product-definition.md | Sponsor prefers compact dashboard rows. with extra context\n",
        encoding="utf-8",
    )
    try:
        db.migrate()
        db.create_run(
            run_id="run-memory-substring",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id=None,
        )
        SafeOutputService(
            db,
            project_id="test-project",
            role_memory_path_resolver=lambda role_id: memory_path if role_id == "product-manager" else None,
        ).record(
            run_id="run-memory-substring",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="memory.propose_update",
                payload={
                    "summary": "Sponsor prefers compact dashboard rows.",
                    "provenance_ref": "work-123/020-product-definition.md",
                },
            ),
        )
        role_memory = db.list_role_memory()
    finally:
        db.close()

    text = memory_path.read_text(encoding="utf-8")
    exact_generated_lines = [
        line
        for line in text.splitlines()
        if line.startswith("- call-")
        and line.endswith("work-123/020-product-definition.md | Sponsor prefers compact dashboard rows.")
    ]
    assert len(exact_generated_lines) == 1
    assert "with extra context" in text
    assert len(role_memory) == 1


def test_cli_record_safe_output_project_file_publishes_memory_effect(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    project_file = _project_file(tmp_path)
    memory_path = tmp_path / "demo-project" / "agentic-mesh" / "roles" / "product-manager" / "MEMORY.md"
    try:
        db.migrate()
        db.create_run(
            run_id="run-memory-cli",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id=None,
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentic_mesh_v2.cli",
                "--db",
                str(db.path),
                "record-safe-output",
                "--run-id",
                "run-memory-cli",
                "--role-id",
                "product-manager",
                "--tool-name",
                "memory.propose_update",
                "--payload-json",
                json.dumps(
                    {
                        "summary": "Sponsor prefers compact dashboard rows.",
                        "provenance_ref": "work-123/020-product-definition.md",
                    }
                ),
                "--project-file",
                str(project_file),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        role_memory = db.list_role_memory()
        snapshot = db.status_snapshot()
        safe_outputs = db.list_safe_output_calls_for_run("run-memory-cli")
    finally:
        db.close()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(safe_outputs) == 1
    assert safe_outputs[0]["tool_name"] == "memory.propose_update"
    assert memory_path.exists()
    assert len(role_memory) == 1
    assert role_memory[0]["summary"] == "Sponsor prefers compact dashboard rows."
    assert snapshot["counts"]["role_memory"] == 1


def _project_file(tmp_path: Path) -> Path:
    project_root = tmp_path / "demo-project"
    project_file = project_root / "agentic-mesh" / "project.yaml"
    role_dir = project_root / "agentic-mesh" / "roles" / "product-manager"
    role_dir.mkdir(parents=True)
    (role_dir / "role.yaml").write_text(
        """
schema_version: role-runtime-config-v0
role_id: product-manager
memory:
  file: MEMORY.md
""",
        encoding="utf-8",
    )
    project_file.write_text(
        """
project_id: test-project
role_memory:
  enabled: true
  backend: filesystem
  config_root: agentic-mesh/roles
  memory_filename: MEMORY.md
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )
    return project_file
