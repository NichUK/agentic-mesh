from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh_v4.cli import main
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.documents import DocumentWriteRequest
from agentic_mesh_v4.documents import sha256_text
from agentic_mesh_v4.documents import write_artifact
from v4_postgres import make_v4_db_url


PROJECT_CONFIG = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")
WORK_ITEM_ID = "work-document-recovery-test"
DOCUMENT_PATH = f"work-items/{WORK_ITEM_ID}/050-solution-design.md"


@pytest.fixture
def database() -> tuple[V4Database, str]:
    db_url = make_v4_db_url()
    db = V4Database(db_url)
    db.migrate()
    db.upsert_work_item(
        work_item_id=WORK_ITEM_ID,
        title="Synthetic document recovery",
        state="solution_design",
        owner_role="solution-architect",
        next_action="Exercise synthetic document recovery.",
    )
    try:
        yield db, db_url
    finally:
        db.close()


def _write(
    *,
    db: V4Database,
    document_root: Path,
    content: str,
    call_id: str,
    base_sha256: str | None = None,
    base_revision_id: str | None = None,
    source_ref: str = "synthetic:test",
) -> dict[str, object]:
    return write_artifact(
        db=db,
        document_root=document_root,
        request=DocumentWriteRequest(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            work_item_id=WORK_ITEM_ID,
            path=DOCUMENT_PATH,
            title="Synthetic solution design",
            content=content,
            base_sha256=base_sha256,
            base_revision_id=base_revision_id,
            message_id="msg-synthetic",
            turn_id="turn-synthetic",
            source_ref=source_ref,
        ),
        safe_output_call_id=call_id,
    )


def test_normal_write_persists_restorable_content_and_actor_diagnostics(
    tmp_path: Path,
    database: tuple[V4Database, str],
) -> None:
    db, _ = database
    content = "# Synthetic design\n\nNormal non-sensitive fixture.\n"

    result = _write(db=db, document_root=tmp_path, content=content, call_id="call-normal")

    assert result["status"] == "written"
    assert (tmp_path / DOCUMENT_PATH).read_text(encoding="utf-8") == content
    stored = db.connection.execute(
        "SELECT * FROM document_revision_contents WHERE revision_id=?",
        (result["revision_id"],),
    ).fetchone()
    assert stored["content"] == content
    assert stored["content_sha256"] == sha256_text(content)
    assert stored["byte_length"] == len(content.encode("utf-8"))
    attempt = db.connection.execute(
        "SELECT * FROM document_write_attempts WHERE attempt_id=?",
        (result["attempt_id"],),
    ).fetchone()
    assert attempt["actor_role"] == "release-manager"
    assert attempt["role_instance_id"] == "agentic-mesh-dev.release-manager.1"
    assert attempt["source_ref"] == "synthetic:test"
    assert attempt["message_id"] == "msg-synthetic"
    assert attempt["turn_id"] == "turn-synthetic"
    assert attempt["status"] == "written"


def test_mandatory_empty_overwrite_is_rejected_and_withdrawal_is_unsupported(
    tmp_path: Path,
    database: tuple[V4Database, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, db_url = database
    monkeypatch.setenv("AGENTIC_MESH_SAFE_OUTPUT_PROXY_BYPASS", "1")
    original = "# Preserved design\n"
    initial = _write(db=db, document_root=tmp_path, content=original, call_id="call-initial")

    main(
        [
            "--project-config",
            str(PROJECT_CONFIG),
            "--db",
            db_url,
            "safe-output",
            "document-write-artifact",
            "--role-id",
            "release-manager",
            "--work-item-id",
            WORK_ITEM_ID,
            "--path",
            DOCUMENT_PATH,
            "--title",
            "Synthetic solution design",
            "--content",
            " \n\t",
            "--base-sha256",
            str(initial["content_sha256"]),
            "--source-ref",
            "synthetic:accidental-empty",
            "--document-root",
            str(tmp_path),
            "--message-id",
            "msg-empty",
            "--turn-id",
            "turn-empty",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "rejected"
    assert output["reason"] == "mandatory_lifecycle_empty_content"
    assert output["withdrawal_supported"] is False
    assert (tmp_path / DOCUMENT_PATH).read_text(encoding="utf-8") == original
    assert db.connection.execute(
        "SELECT count(*) AS count FROM document_revisions WHERE path=?",
        (DOCUMENT_PATH,),
    ).fetchone()["count"] == 1
    attempt = db.connection.execute(
        "SELECT * FROM document_write_attempts WHERE attempt_id=?",
        (output["attempt_id"],),
    ).fetchone()
    assert attempt["status"] == "rejected"
    assert attempt["reason"] == "mandatory_lifecycle_empty_content"
    assert attempt["actor_role"] == "release-manager"
    assert attempt["source_ref"] == "synthetic:accidental-empty"
    diagnostic = json.loads(attempt["diagnostic_json"])
    assert diagnostic["withdrawal_supported"] is False
    call = db.connection.execute(
        "SELECT * FROM safe_output_calls WHERE call_id=?",
        (output["call_id"],),
    ).fetchone()
    assert call["tool_name"] == "document.write_artifact"
    assert json.loads(call["payload_json"])["result"]["status"] == "rejected"


def test_stale_base_creates_three_way_merge_with_restorable_base(
    tmp_path: Path,
    database: tuple[V4Database, str],
) -> None:
    db, _ = database
    base_content = "# Revision A\n"
    current_content = "# Revision B\n"
    proposed_content = "# Revision C from stale base\n"
    revision_a = _write(db=db, document_root=tmp_path, content=base_content, call_id="call-a")
    revision_b = _write(
        db=db,
        document_root=tmp_path,
        content=current_content,
        call_id="call-b",
        base_sha256=str(revision_a["content_sha256"]),
        base_revision_id=str(revision_a["revision_id"]),
    )

    result = _write(
        db=db,
        document_root=tmp_path,
        content=proposed_content,
        call_id="call-stale",
        base_sha256=str(revision_a["content_sha256"]),
        base_revision_id=str(revision_a["revision_id"]),
    )

    assert result["status"] == "merge_required"
    assert result["reason"] == "changed_base_sha256"
    assert (tmp_path / DOCUMENT_PATH).read_text(encoding="utf-8") == current_content
    merge = db.connection.execute(
        "SELECT * FROM document_merge_tasks WHERE merge_task_id=?",
        (result["merge_task_id"],),
    ).fetchone()
    assert (tmp_path / merge["base_content_path"]).read_text(encoding="utf-8") == base_content
    assert (tmp_path / merge["current_content_path"]).read_text(encoding="utf-8") == current_content
    assert (tmp_path / merge["proposed_content_path"]).read_text(encoding="utf-8") == proposed_content
    assert json.loads(merge["diagnostic_json"])["base_content_available"] is True
    assert revision_b["content_sha256"] == sha256_text(current_content)


def test_revision_restore_creates_a_new_audited_revision(
    tmp_path: Path,
    database: tuple[V4Database, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, db_url = database
    monkeypatch.setenv("AGENTIC_MESH_SAFE_OUTPUT_PROXY_BYPASS", "1")
    revision_a_content = "# Restorable revision A\n"
    revision_b_content = "# Current revision B\n"
    revision_a = _write(db=db, document_root=tmp_path, content=revision_a_content, call_id="call-a")
    revision_b = _write(
        db=db,
        document_root=tmp_path,
        content=revision_b_content,
        call_id="call-b",
        base_sha256=str(revision_a["content_sha256"]),
        base_revision_id=str(revision_a["revision_id"]),
    )

    main(
        [
            "--project-config",
            str(PROJECT_CONFIG),
            "--db",
            db_url,
            "safe-output",
            "document-restore-artifact",
            "--role-id",
            "platform-engineer",
            "--work-item-id",
            WORK_ITEM_ID,
            "--path",
            DOCUMENT_PATH,
            "--title",
            "Synthetic solution design",
            "--revision-id",
            str(revision_a["revision_id"]),
            "--base-sha256",
            str(revision_b["content_sha256"]),
            "--source-ref",
            "synthetic:restore",
            "--document-root",
            str(tmp_path),
            "--message-id",
            "msg-restore",
            "--turn-id",
            "turn-restore",
        ]
    )
    restored = json.loads(capsys.readouterr().out)

    assert restored["status"] == "restored"
    assert restored["restored_from_revision_id"] == revision_a["revision_id"]
    assert restored["revision_id"] != revision_a["revision_id"]
    assert (tmp_path / DOCUMENT_PATH).read_text(encoding="utf-8") == revision_a_content
    revision = db.connection.execute(
        "SELECT * FROM document_revisions WHERE revision_id=?",
        (restored["revision_id"],),
    ).fetchone()
    assert revision["status"] == "restored"
    assert revision["restored_from_revision_id"] == revision_a["revision_id"]
    stored = db.connection.execute(
        "SELECT * FROM document_revision_contents WHERE revision_id=?",
        (restored["revision_id"],),
    ).fetchone()
    assert stored["content"] == revision_a_content
    attempt = db.connection.execute(
        "SELECT * FROM document_write_attempts WHERE attempt_id=?",
        (restored["attempt_id"],),
    ).fetchone()
    assert attempt["action"] == "restore"
    assert attempt["actor_role"] == "platform-engineer"
    call = db.connection.execute(
        "SELECT * FROM safe_output_calls WHERE call_id=?",
        (restored["call_id"],),
    ).fetchone()
    assert call["tool_name"] == "document.restore_artifact"
    assert json.loads(call["payload_json"])["result"]["status"] == "restored"
