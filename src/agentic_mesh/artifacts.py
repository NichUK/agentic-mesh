from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import DocumentUpdate
from agentic_mesh.work_item_indexes import GLOBAL_INDEX_PATH
from agentic_mesh.work_item_indexes import local_index_path
from agentic_mesh.work_item_indexes import validate_work_item_id


class ArtifactStore:
    def __init__(
        self,
        workspace_root: Path,
        project_id: str,
        journal: EventJournal,
        document_library_root: Path | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.project_id = project_id
        self.journal = journal
        self.document_library_root = document_library_root or workspace_root

    def write_update(
        self,
        update: DocumentUpdate,
        role_id: str,
        role_instance_id: str,
        correlation_id: str,
        work_item_id: str | None = None,
        work_item_type: str | None = None,
        lifecycle_state: str | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> Path:
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            role_id=role_id,
            role_instance_id=role_instance_id,
            path=update.path,
            work_item_id=work_item_id,
            work_item_type=work_item_type,
            lifecycle_state=lifecycle_state,
            correlation_id=correlation_id,
        )
        with telemetry.start_span(
            "artifact.write",
            correlation_id=correlation_id,
            trace_context=trace_context,
            attributes=attrs,
        ):
            root = self._root_for_update(update)
            path = (root / update.path).resolve()
            resolved_root = root.resolve()
            if resolved_root not in path.parents and path != resolved_root:
                raise ValueError(f"Artifact path escapes artifact root: {update.path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(update.content.rstrip() + "\n")
            self.journal.append(
                "documentation_updated",
                project_id=self.project_id,
                role_id=role_id,
                role_instance_id=role_instance_id,
                path=update.path,
                work_item_id=work_item_id,
                work_item_type=work_item_type,
                lifecycle_state=lifecycle_state,
                correlation_id=correlation_id,
            )
            return path

    def write_generated_artifact(
        self,
        *,
        relative_path: str,
        content: str,
        work_item_id: str,
        queue_item_id: str | None = None,
        work_item_type: str | None = None,
        lifecycle_state: str | None = None,
        component_id: str = "lifecycle-export",
        correlation_id: str | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> Path:
        path = self.validate_generated_artifact_path(
            relative_path,
            work_item_id=work_item_id,
        )
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            component_id=component_id,
            path=relative_path,
            work_item_id=work_item_id,
            queue_item_id=queue_item_id,
            work_item_type=work_item_type,
            lifecycle_state=lifecycle_state,
            correlation_id=correlation_id,
            generated_artifact=True,
            result="started",
        )
        with telemetry.start_span(
            "artifact.generated.write",
            correlation_id=correlation_id,
            trace_context=trace_context,
            attributes=attrs,
        ):
            tmp_path: Path | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                with tmp_path.open("w", encoding="utf-8") as handle:
                    handle.write(content.rstrip() + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
                _fsync_directory(path.parent)
                self.journal.append(
                    "documentation_updated",
                    project_id=self.project_id,
                    role_id=component_id,
                    role_instance_id=component_id,
                    component_id=component_id,
                    path=relative_path,
                    work_item_id=work_item_id,
                    work_item_type=work_item_type,
                    lifecycle_state=lifecycle_state,
                    queue_item_id=queue_item_id,
                    correlation_id=correlation_id,
                    generated_artifact=True,
                )
                attrs["result"] = "success"
                return path
            except Exception as exc:
                attrs["result"] = "failed"
                attrs["reason"] = exc.__class__.__name__
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                raise

    def write_maintained_index(
        self,
        *,
        relative_path: str,
        content: str,
        work_item_id: str,
        index_kind: str,
        operation: str,
        queue_item_id: str | None = None,
        work_item_type: str | None = None,
        lifecycle_state: str | None = None,
        component_id: str = "work-item-index",
        correlation_id: str | None = None,
        trace_context: dict[str, str] | None = None,
    ) -> Path:
        path = self.validate_maintained_index_path(
            relative_path,
            work_item_id=work_item_id,
            index_kind=index_kind,
        )
        event_type = (
            "work_items_index_updated"
            if index_kind == "global"
            else "work_item_index_updated"
        )
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            component_id=component_id,
            path=relative_path,
            work_item_id=work_item_id,
            queue_item_id=queue_item_id,
            work_item_type=work_item_type,
            lifecycle_state=lifecycle_state,
            correlation_id=correlation_id,
            index_kind=index_kind,
            operation=operation,
            result="started",
        )
        with telemetry.start_span(
            "work_item_index.write",
            correlation_id=correlation_id,
            trace_context=trace_context,
            attributes=attrs,
        ):
            tmp_path: Path | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                with tmp_path.open("w", encoding="utf-8") as handle:
                    handle.write(content.rstrip() + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
                _fsync_directory(path.parent)
                self.journal.append(
                    event_type,
                    project_id=self.project_id,
                    component_id=component_id,
                    path=relative_path,
                    work_item_id=work_item_id,
                    work_item_type=work_item_type,
                    lifecycle_state=lifecycle_state,
                    queue_item_id=queue_item_id,
                    correlation_id=correlation_id,
                    index_kind=index_kind,
                    operation=operation,
                    result="success",
                )
                attrs["result"] = "success"
                return path
            except Exception as exc:
                attrs["result"] = "failed"
                attrs["reason"] = exc.__class__.__name__
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                raise

    def validate_maintained_index_path(
        self,
        relative_path: str,
        *,
        work_item_id: str,
        index_kind: str,
    ) -> Path:
        path_text = str(relative_path)
        if not path_text or not path_text.strip():
            raise ValueError("Maintained index output path must not be empty.")
        if _is_windows_absolute(path_text) or path_text.startswith(("/", "\\")):
            raise ValueError("Maintained index output path must be relative.")
        normalized_text = path_text.replace("\\", "/")
        requested = Path(normalized_text)
        if requested.is_absolute() or ".." in requested.parts:
            raise ValueError("Maintained index output path must not traverse.")
        if index_kind == "global":
            if normalized_text != GLOBAL_INDEX_PATH:
                raise ValueError("Global work-items index path must be work-items/index.md.")
        elif index_kind == "local":
            validate_work_item_id(work_item_id)
            if normalized_text != local_index_path(work_item_id):
                raise ValueError(
                    "Local work-item index path must be "
                    f"work-items/{work_item_id}/00-index.md."
                )
        else:
            raise ValueError("Maintained index kind must be local or global.")
        root = self.document_library_root.resolve()
        path = (root / requested).resolve()
        if path != root and root not in path.parents:
            raise ValueError("Maintained index output path escapes document library.")
        return path

    def validate_generated_artifact_path(
        self,
        relative_path: str,
        *,
        work_item_id: str,
    ) -> Path:
        path_text = str(relative_path)
        if not path_text or not path_text.strip():
            raise ValueError("Generated artifact output path must not be empty.")
        if _is_windows_absolute(path_text) or path_text.startswith(("/", "\\")):
            raise ValueError("Generated artifact output path must be relative.")
        normalized_text = path_text.replace("\\", "/")
        requested = Path(normalized_text)
        if requested.is_absolute() or ".." in requested.parts:
            raise ValueError("Generated artifact output path must not traverse.")
        required_prefix = ("work-items", work_item_id)
        if requested.parts[:2] != required_prefix or len(requested.parts) <= 2:
            raise ValueError(
                "Generated artifact output path must be under "
                f"work-items/{work_item_id}/."
            )
        root = self.document_library_root.resolve()
        path = (root / requested).resolve()
        if path != root and root not in path.parents:
            raise ValueError("Generated artifact output path escapes document library.")
        return path

    def _root_for_update(self, update: DocumentUpdate) -> Path:
        if update.path.startswith("documents/analysis/"):
            return self.workspace_root
        return self.document_library_root


def _is_windows_absolute(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:([/\\]|$)", value)) or value.startswith(
        ("//", "\\\\")
    )


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
