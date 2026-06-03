from __future__ import annotations

from pathlib import Path

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import DocumentUpdate


class ArtifactStore:
    def __init__(self, workspace_root: Path, project_id: str, journal: EventJournal) -> None:
        self.workspace_root = workspace_root
        self.project_id = project_id
        self.journal = journal

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
            path = (self.workspace_root / update.path).resolve()
            workspace = self.workspace_root.resolve()
            if workspace not in path.parents and path != workspace:
                raise ValueError(f"Artifact path escapes workspace: {update.path}")
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
