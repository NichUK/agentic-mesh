from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.db import utc_now


COMMENT_WARNING_STATUSES = {"unsupported", "unavailable", "unsupported_comment_metadata", "comment_metadata_unavailable"}


class DocumentWriteError(ValueError):
    pass


@dataclass(frozen=True)
class DocumentWriteRequest:
    role_instance_id: str
    work_item_id: str
    path: str
    title: str
    content: str
    document_type: str | None = None
    base_sha256: str | None = None
    base_revision_id: str | None = None
    base_etag: str | None = None
    comment_metadata_status: str | None = None
    comment_metadata_detail: str | None = None
    message_id: str | None = None
    turn_id: str | None = None
    source_ref: str | None = None


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_artifact(
    *,
    db: V4Database,
    document_root: Path,
    request: DocumentWriteRequest,
    safe_output_call_id: str,
) -> dict[str, object]:
    relative_path = _canonical_relative_path(path=request.path, work_item_id=request.work_item_id)
    target = _document_path(root=document_root, relative_path=relative_path)
    document_type = request.document_type or _document_type_from_path(relative_path)
    content_sha256 = sha256_text(request.content)
    existed = target.exists()
    previous_content = _read_text(target) if existed else None
    previous_sha256 = sha256_text(previous_content) if previous_content is not None else None
    warning_id = _record_comment_warning_if_needed(
        db=db,
        request=request,
        path=relative_path,
        safe_output_call_id=safe_output_call_id,
    )

    if existed and not request.base_sha256:
        merge_task_id = _create_merge_task(
            db=db,
            document_root=document_root,
            request=request,
            path=relative_path,
            base_content="",
            current_content=previous_content or "",
            proposed_content=request.content,
            base_sha256=None,
            current_sha256=previous_sha256,
            proposed_sha256=content_sha256,
            safe_output_call_id=safe_output_call_id,
            diagnostic={"reason": "missing_base_sha256"},
        )
        return {
            "status": "merge_required",
            "reason": "missing_base_sha256",
            "merge_task_id": merge_task_id,
            "path": relative_path,
            "current_sha256": previous_sha256,
            "proposed_sha256": content_sha256,
            "warning_id": warning_id,
        }

    if existed and previous_sha256 != request.base_sha256:
        merge_task_id = _create_merge_task(
            db=db,
            document_root=document_root,
            request=request,
            path=relative_path,
            base_content="",
            current_content=previous_content or "",
            proposed_content=request.content,
            base_sha256=request.base_sha256,
            current_sha256=previous_sha256,
            proposed_sha256=content_sha256,
            safe_output_call_id=safe_output_call_id,
            diagnostic={"reason": "changed_base_sha256"},
        )
        return {
            "status": "merge_required",
            "reason": "changed_base_sha256",
            "merge_task_id": merge_task_id,
            "path": relative_path,
            "current_sha256": previous_sha256,
            "proposed_sha256": content_sha256,
            "warning_id": warning_id,
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target, request.content)
    written = _read_text(target)
    written_sha256 = sha256_text(written)
    revision_id = f"docrev-{uuid4().hex}"
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO document_revisions(
              revision_id, work_item_id, path, document_type, role_instance_id,
              base_sha256, previous_sha256, content_sha256, base_revision_id, base_etag,
              status, safe_output_call_id, message_id, turn_id, source_ref, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                revision_id,
                request.work_item_id,
                relative_path,
                document_type,
                request.role_instance_id,
                request.base_sha256,
                previous_sha256,
                written_sha256,
                request.base_revision_id,
                request.base_etag,
                "written",
                safe_output_call_id,
                request.message_id,
                request.turn_id,
                request.source_ref,
                utc_now(),
            ),
        )
        db.record_artifact(work_item_id=request.work_item_id, path=relative_path, title=request.title)
    return {
        "status": "written",
        "revision_id": revision_id,
        "path": relative_path,
        "document_type": document_type,
        "previous_sha256": previous_sha256,
        "content_sha256": written_sha256,
        "warning_id": warning_id,
    }


def _canonical_relative_path(*, path: str, work_item_id: str) -> str:
    raw = path.strip()
    if not raw:
        raise DocumentWriteError("document path is required")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise DocumentWriteError("document path must be relative to the document root")
    parts = candidate.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise DocumentWriteError("document path must not contain empty or traversal segments")
    expected_prefix = ("work-items", work_item_id)
    if len(parts) < 3 or parts[:2] != expected_prefix:
        raise DocumentWriteError(f"document path must be under work-items/{work_item_id}/")
    if parts[2] == ".merge":
        raise DocumentWriteError("document.write_artifact cannot target merge scratch paths")
    return "/".join(parts)


def _document_path(*, root: Path, relative_path: str) -> Path:
    root = root.resolve(strict=False)
    candidate = (root / relative_path).resolve(strict=False)
    if root != candidate and root not in candidate.parents:
        raise DocumentWriteError("document path escapes document root")
    return candidate


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _atomic_write_text(path: Path, content: str) -> None:
    data = content.encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o666)
        try:
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _create_merge_task(
    *,
    db: V4Database,
    document_root: Path,
    request: DocumentWriteRequest,
    path: str,
    base_content: str,
    current_content: str,
    proposed_content: str,
    base_sha256: str | None,
    current_sha256: str | None,
    proposed_sha256: str,
    safe_output_call_id: str,
    diagnostic: dict[str, object],
) -> str:
    merge_task_id = f"docmerge-{uuid4().hex}"
    merge_dir_relative = f"work-items/{request.work_item_id}/.merge/{merge_task_id}"
    merge_dir = _document_path(root=document_root, relative_path=merge_dir_relative)
    merge_dir.mkdir(parents=True, exist_ok=True)
    base_path = merge_dir / "base.md"
    current_path = merge_dir / "current.md"
    proposed_path = merge_dir / "proposed.md"
    _atomic_write_text(base_path, base_content)
    _atomic_write_text(current_path, current_content)
    _atomic_write_text(proposed_path, proposed_content)
    work = db.connection.execute("SELECT owner_role FROM work_items WHERE work_item_id=?", (request.work_item_id,)).fetchone()
    owner_role = str(work["owner_role"]) if work is not None and work["owner_role"] else "project-manager"
    now = utc_now()
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO document_merge_tasks(
              merge_task_id, work_item_id, path, owner_role, state, base_sha256,
              current_sha256, proposed_sha256, base_content_path, current_content_path,
              proposed_content_path, role_instance_id, safe_output_call_id, message_id,
              turn_id, source_ref, diagnostic_json, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                merge_task_id,
                request.work_item_id,
                path,
                owner_role,
                "open",
                base_sha256,
                current_sha256,
                proposed_sha256,
                f"{merge_dir_relative}/base.md",
                f"{merge_dir_relative}/current.md",
                f"{merge_dir_relative}/proposed.md",
                request.role_instance_id,
                safe_output_call_id,
                request.message_id,
                request.turn_id,
                request.source_ref,
                json.dumps(diagnostic, sort_keys=True),
                now,
                now,
            ),
        )
        db.upsert_work_item(
            work_item_id=request.work_item_id,
            owner_role=owner_role,
            next_action=f"Resolve document merge task for {path}; current canonical content was preserved.",
        )
    return merge_task_id


def _record_comment_warning_if_needed(
    *,
    db: V4Database,
    request: DocumentWriteRequest,
    path: str,
    safe_output_call_id: str,
) -> str | None:
    status = (request.comment_metadata_status or "").strip().casefold()
    if status not in COMMENT_WARNING_STATUSES:
        return None
    warning_type = "unsupported_comment_metadata" if "unsupported" in status else "comment_metadata_unavailable"
    warning_id = f"docwarn-{uuid4().hex}"
    diagnostic = {
        "comment_metadata_status": request.comment_metadata_status,
        "comment_metadata_detail": request.comment_metadata_detail or "",
        "policy": "Local Markdown content is hash protected; backend comment metadata preservation is not claimed.",
    }
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO document_write_warnings(
              warning_id, work_item_id, path, warning_type, severity, role_instance_id,
              safe_output_call_id, message_id, turn_id, diagnostic_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                warning_id,
                request.work_item_id,
                path,
                warning_type,
                "medium",
                request.role_instance_id,
                safe_output_call_id,
                request.message_id,
                request.turn_id,
                json.dumps(diagnostic, sort_keys=True),
                utc_now(),
            ),
        )
    return warning_id


def _document_type_from_path(path: str) -> str:
    name = Path(path).name
    if name == "00-index.md":
        return "work_item_index"
    if "-" in name and name.endswith(".md"):
        return name.removesuffix(".md").split("-", 1)[1].replace("-", "_")
    return Path(path).suffix.lstrip(".") or "artifact"
