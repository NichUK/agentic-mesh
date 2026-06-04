from __future__ import annotations

from pathlib import Path

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import DocumentUpdate


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

    def _root_for_update(self, update: DocumentUpdate) -> Path:
        if update.path.startswith("documents/analysis/"):
            return self.workspace_root
        return self.document_library_root
