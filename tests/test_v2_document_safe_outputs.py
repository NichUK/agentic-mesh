from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.role_service import RoleAssignment
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputError
from agentic_mesh_v2.safe_outputs import SafeOutputService


def test_document_safe_output_writes_valid_slice_document_and_artifact(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    document_root = tmp_path / "docs"
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-document-direct",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id="work-document-safe-output",
        )
        call_id = SafeOutputService(db, document_library_root=document_root).record(
            run_id="run-document-direct",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="document.propose_update",
                payload={
                    "path": "work-items/work-document-safe-output/020-product-definition.md",
                    "document_type": "product_definition",
                    "content": _product_definition_content(),
                    "status": "accepted",
                },
            ),
        )
        artifacts = db.list_artifacts()
    finally:
        db.close()

    assert (document_root / "work-items" / "work-document-safe-output" / "020-product-definition.md").read_text(
        encoding="utf-8"
    ).startswith("# Product Definition")
    assert artifacts == [
        {
            "artifact_id": f"artifact-{call_id}",
            "work_item_id": "work-document-safe-output",
            "path": "work-items/work-document-safe-output/020-product-definition.md",
            "document_type": "product_definition",
            "status": "accepted",
            "created_by_role": "product-manager",
            "created_at": artifacts[0]["created_at"],
        }
    ]


def test_document_safe_output_revises_existing_document_and_artifact(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    document_root = tmp_path / "docs"
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-document-revision",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id="work-document-safe-output",
        )
        service = SafeOutputService(db, document_library_root=document_root)
        first_call_id = service.record(
            run_id="run-document-revision",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="document.propose_update",
                payload={
                    "path": "work-items/work-document-safe-output/020-product-definition.md",
                    "document_type": "product_definition",
                    "content": _product_definition_content(),
                },
            ),
        )
        second_content = _product_definition_content(
            acceptance="- Revised content updates the published artifact without creating split state."
        )
        second_call_id = service.record(
            run_id="run-document-revision",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="document.propose_update",
                payload={
                    "path": "work-items/work-document-safe-output/020-product-definition.md",
                    "document_type": "product_definition",
                    "content": second_content,
                    "status": "accepted",
                },
            ),
        )
        artifacts = db.list_artifacts()
    finally:
        db.close()

    assert first_call_id != second_call_id
    assert len(artifacts) == 1
    assert artifacts[0]["artifact_id"] == f"artifact-{second_call_id}"
    assert artifacts[0]["status"] == "accepted"
    assert (
        document_root / "work-items" / "work-document-safe-output" / "020-product-definition.md"
    ).read_text(encoding="utf-8") == second_content


def test_document_safe_output_rejects_framework_path_mismatch(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-document-path-mismatch",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id="work-document-safe-output",
        )
        with pytest.raises(SafeOutputError, match="path must match framework path"):
            SafeOutputService(db, document_library_root=tmp_path / "docs").record(
                run_id="run-document-path-mismatch",
                call=SafeOutputCall(
                    role_id="product-manager",
                    tool_name="document.propose_update",
                    payload={
                        "path": "work-items/work-document-safe-output/999-wrong.md",
                        "document_type": "product_definition",
                        "content": _product_definition_content(),
                    },
                ),
            )
        artifacts = db.list_artifacts()
    finally:
        db.close()

    assert artifacts == []


def test_document_safe_output_rejects_status_only_or_incomplete_content(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-document-bad-content",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id="work-document-safe-output",
        )
        with pytest.raises(ValueError, match="missing sections"):
            SafeOutputService(db, document_library_root=tmp_path / "docs").record(
                run_id="run-document-bad-content",
                call=SafeOutputCall(
                    role_id="product-manager",
                    tool_name="document.propose_update",
                    payload={
                        "path": "work-items/work-document-safe-output/020-product-definition.md",
                        "document_type": "product_definition",
                        "content": "Blocked. Waiting for operator retry.",
                    },
                ),
            )
        artifacts = db.list_artifacts()
    finally:
        db.close()

    assert artifacts == []


def test_document_review_comment_appends_to_existing_review_log(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    document_root = tmp_path / "docs"
    target = document_root / "work-items" / "work-document-safe-output" / "020-product-definition.md"
    target.parent.mkdir(parents=True)
    target.write_text(_product_definition_content(), encoding="utf-8")
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-document-review-comment",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-document-safe-output",
        )
        service = SafeOutputService(db, document_library_root=document_root)
        call = SafeOutputCall(
            role_id="qa-engineer",
            tool_name="document.add_review_comment",
            payload={
                "path": "work-items/work-document-safe-output/020-product-definition.md",
                "comment": "Acceptance criteria are testable and ready for QA.",
            },
        )
        call_id = service.record(run_id="run-document-review-comment", call=call)
        service.process_recorded_call(call_id=call_id, run_id="run-document-review-comment", call=call)
    finally:
        db.close()

    text = target.read_text(encoding="utf-8")
    assert text.count(f"- {call_id} | qa-engineer | review-comment | Acceptance criteria are testable and ready for QA.") == 1


def test_document_review_comment_idempotency_is_scoped_to_review_log(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    document_root = tmp_path / "docs"
    target = document_root / "work-items" / "work-document-safe-output" / "020-product-definition.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        _product_definition_content().replace(
            "## Objective",
            "## Objective\n\n- call-preexisting | qa-engineer | review-comment | Not actually review-log evidence.\n\n## Objective",
        ),
        encoding="utf-8",
    )
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-document-review-comment-scope",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-document-safe-output",
        )
        call = SafeOutputCall(
            role_id="qa-engineer",
            tool_name="document.add_review_comment",
            payload={
                "path": "work-items/work-document-safe-output/020-product-definition.md",
                "comment": "Actual review-log evidence.",
            },
        )
        service = SafeOutputService(db, document_library_root=document_root)
        service.process_recorded_call(call_id="call-preexisting", run_id="run-document-review-comment-scope", call=call)
        service.process_recorded_call(call_id="call-preexisting", run_id="run-document-review-comment-scope", call=call)
    finally:
        db.close()

    text = target.read_text(encoding="utf-8")
    assert text.count("- call-preexisting | qa-engineer | review-comment | Actual review-log evidence.") == 1


def test_document_review_comment_rejects_missing_review_log(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    document_root = tmp_path / "docs"
    target = document_root / "work-items" / "work-document-safe-output" / "020-product-definition.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Product Definition\n\n## Objective\n\nMissing review log.\n", encoding="utf-8")
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-document-review-comment-missing-log",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-document-safe-output",
        )
        with pytest.raises(SafeOutputError, match="Review Log"):
            SafeOutputService(db, document_library_root=document_root).record(
                run_id="run-document-review-comment-missing-log",
                call=SafeOutputCall(
                    role_id="qa-engineer",
                    tool_name="document.add_review_comment",
                    payload={
                        "path": "work-items/work-document-safe-output/020-product-definition.md",
                        "comment": "This cannot be attached without a review log.",
                    },
                ),
            )
        calls = db.list_safe_output_calls_for_run("run-document-review-comment-missing-log")
    finally:
        db.close()

    assert calls == []


def test_document_review_comment_rejects_missing_target_document(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-document-review-comment-missing-target",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id="work-document-safe-output",
        )
        with pytest.raises(SafeOutputError, match="was not found"):
            SafeOutputService(db, document_library_root=tmp_path / "docs").record(
                run_id="run-document-review-comment-missing-target",
                call=SafeOutputCall(
                    role_id="qa-engineer",
                    tool_name="document.add_review_comment",
                    payload={
                        "path": "work-items/work-document-safe-output/020-product-definition.md",
                        "comment": "This cannot be attached without a target document.",
                    },
                ),
            )
        calls = db.list_safe_output_calls_for_run("run-document-review-comment-missing-target")
    finally:
        db.close()

    assert calls == []


def test_document_review_comment_without_document_root_records_intent_without_effect(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    try:
        db.migrate()
        db.create_run(
            run_id="run-document-review-comment-unconfigured",
            role_id="qa-engineer",
            role_instance_id="test-project.qa-engineer.1",
            work_item_id=None,
        )
        call_id = SafeOutputService(db).record(
            run_id="run-document-review-comment-unconfigured",
            call=SafeOutputCall(
                role_id="qa-engineer",
                tool_name="document.add_review_comment",
                payload={
                    "path": "work-items/work-document-safe-output/020-product-definition.md",
                    "comment": "No document library is configured in this context.",
                },
            ),
        )
        calls = db.list_safe_output_calls_for_run("run-document-review-comment-unconfigured")
    finally:
        db.close()

    assert calls[0]["call_id"] == call_id
    assert calls[0]["tool_name"] == "document.add_review_comment"


def test_cli_recorded_document_safe_output_publishes_once_in_role_service(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    document_root = tmp_path / "docs"
    project_file = _project_file(tmp_path, document_root=document_root)
    try:
        db.migrate()
        _work_item(db)
        service = RoleService(
            db=db,
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            worker=CliDocumentWorker(expected_project_file=project_file),
            safe_outputs=SafeOutputService(db, document_library_root=document_root),
            safe_output_project_file=project_file,
        )
        receipt = service.run_assignment(
            RoleAssignment(
                role_id="product-manager",
                role_instance_id="test-project.product-manager.1",
                work_item_id="work-document-safe-output",
                title="Document safe-output publication",
                summary="Publish product definition through CLI transport.",
            ),
            run_id="run-document-cli",
        )
        artifacts = db.list_artifacts()
        safe_outputs = db.list_safe_output_calls_for_run("run-document-cli")
    finally:
        db.close()

    assert receipt.status == "completed"
    assert receipt.safe_output_count == 2
    assert [call["tool_name"] for call in safe_outputs] == ["document.propose_update", "status.complete"]
    assert len(artifacts) == 1
    assert (document_root / artifacts[0]["path"]).exists()


def test_cli_record_safe_output_project_file_publishes_document_immediately(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    document_root = tmp_path / "docs"
    project_file = _project_file(tmp_path, document_root=document_root)
    try:
        db.migrate()
        _work_item(db)
        db.create_run(
            run_id="run-document-cli-direct",
            role_id="product-manager",
            role_instance_id="test-project.product-manager.1",
            work_item_id="work-document-safe-output",
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
                "run-document-cli-direct",
                "--role-id",
                "product-manager",
                "--tool-name",
                "document.propose_update",
                "--payload-json",
                json.dumps(
                    {
                        "path": "work-items/work-document-safe-output/020-product-definition.md",
                        "document_type": "product_definition",
                        "content": _product_definition_content(),
                    }
                ),
                "--project-file",
                str(project_file),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        artifacts = db.list_artifacts()
    finally:
        db.close()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(artifacts) == 1
    assert (document_root / "work-items" / "work-document-safe-output" / "020-product-definition.md").exists()


class CliDocumentWorker:
    def __init__(self, *, expected_project_file: Path | None = None) -> None:
        self.expected_project_file = expected_project_file

    def run(self, assignment: RoleAssignment) -> list[SafeOutputCall]:
        assert assignment.safe_output_transport is not None
        command = assignment.safe_output_transport["record_command"]
        if self.expected_project_file is not None:
            assert "--project-file" in command
            assert str(self.expected_project_file) in command
        _run_record_command(
            command,
            tool_name="document.propose_update",
            payload={
                "path": "work-items/work-document-safe-output/020-product-definition.md",
                "document_type": "product_definition",
                "content": _product_definition_content(),
            },
        )
        _run_record_command(
            command,
            tool_name="status.complete",
            payload={"message": "Product definition document published."},
            terminal=True,
        )
        return []


def _run_record_command(
    command: list[str],
    *,
    tool_name: str,
    payload: dict[str, object],
    terminal: bool = False,
) -> None:
    full_command = [
        *command,
        "--tool-name",
        tool_name,
        "--payload-json",
        json.dumps(payload),
    ]
    if terminal:
        full_command.append("--terminal")
    completed = subprocess.run(full_command, capture_output=True, check=False, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _work_item(db: V2Database) -> None:
    db.create_queue_item(
        queue_item_id="queue-document-safe-output",
        title="Document safe-output publication",
        summary="Publish a slice document from safe-output calls.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-document-safe-output", actor_role="product-manager", reason="Ready.")
    db.promote_queue_item(
        queue_item_id="queue-document-safe-output",
        work_item_id="work-document-safe-output",
        owner_role="product-manager",
    )


def _project_file(tmp_path: Path, *, document_root: Path) -> Path:
    project_file = tmp_path / "agentic-mesh" / "project.yaml"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text(
        f"""project_id: test-project
document_library:
  backend: filesystem
  root: {document_root.as_posix()}
  structure_policy: togaf-sdlc-v1
roles: {{}}
""",
        encoding="utf-8",
    )
    return project_file


def _product_definition_content(
    *,
    acceptance: str = "- The runtime records a matching artifact row for the work item.",
) -> str:
    return """# Product Definition

## Objective

Publish a valid product definition through the document safe-output path.

## Scope

The slice covers document validation, filesystem write, and artifact registration.

## Non-Goals

It does not implement OneDrive or SharePoint document backends.

## Acceptance Criteria

- Valid content is written under the configured document library root.
{acceptance}

## Sponsor Questions

No sponsor questions remain open for this test document.

## Review Log

- REV-0001 | product-manager | accepted | Initial product definition evidence.
""".format(acceptance=acceptance)
