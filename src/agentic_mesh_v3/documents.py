from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DocumentRef:
    relative_path: str
    title: str
    url: str | None = None


@dataclass(frozen=True)
class WorkItemIndex:
    work_item_id: str
    title: str
    status: str
    owner_role: str
    raci_summary: str
    governance_state: str
    artifacts: tuple[DocumentRef, ...] = ()
    decisions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    next_action: str = ""

    def render_markdown(self) -> str:
        lines = [
            f"# {self.title}",
            "",
            f"- Work item: `{self.work_item_id}`",
            f"- Status: `{self.status}`",
            f"- Owner role: `{self.owner_role}`",
            f"- RACI: {self.raci_summary}",
            f"- Governance state: {self.governance_state}",
            f"- Next action: {self.next_action or 'None recorded'}",
            "",
            "## Artifacts",
        ]
        if self.artifacts:
            lines.extend(f"- [{artifact.title}]({artifact.relative_path})" for artifact in self.artifacts)
        else:
            lines.append("- None recorded")
        lines.extend(["", "## Decisions"])
        lines.extend(f"- {decision}" for decision in self.decisions) if self.decisions else lines.append("- None recorded")
        lines.extend(["", "## Risks"])
        lines.extend(f"- {risk}" for risk in self.risks) if self.risks else lines.append("- None recorded")
        lines.append("")
        return "\n".join(lines)


class DocumentLibraryAdapter(Protocol):
    """Project document-library adapter boundary."""

    def write_text(self, relative_path: str, content: str) -> DocumentRef:
        """Write a document and return its canonical path/link."""

    def read_text(self, relative_path: str) -> str:
        """Read a document."""

    def exists(self, relative_path: str) -> bool:
        """Return whether a document exists."""


class LocalDocumentLibraryAdapter:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_text(self, relative_path: str, content: str) -> DocumentRef:
        target = self._target(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return DocumentRef(relative_path=relative_path.replace("\\", "/"), title=Path(relative_path).name)

    def read_text(self, relative_path: str) -> str:
        return self._target(relative_path).read_text(encoding="utf-8")

    def exists(self, relative_path: str) -> bool:
        return self._target(relative_path).exists()

    def _target(self, relative_path: str) -> Path:
        normalized = relative_path.replace("\\", "/").lstrip("/")
        if "\x00" in normalized or normalized.startswith("../") or "/../" in normalized:
            raise ValueError("document path escapes library root")
        target = (self.root / normalized).resolve(strict=False)
        root = self.root.resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("document path escapes library root") from exc
        return target


class OneDriveDocumentLibraryAdapter:
    """OneDrive adapter placeholder behind the document-library port."""

    def __init__(self, drive_id: str, root_path: str = "/documents") -> None:
        self.drive_id = drive_id
        self.root_path = root_path

    def write_text(self, relative_path: str, content: str) -> DocumentRef:
        raise NotImplementedError("OneDrive Graph wiring belongs in the document adapter slice")

    def read_text(self, relative_path: str) -> str:
        raise NotImplementedError("OneDrive Graph wiring belongs in the document adapter slice")

    def exists(self, relative_path: str) -> bool:
        raise NotImplementedError("OneDrive Graph wiring belongs in the document adapter slice")


def work_item_index_path(work_item_id: str) -> str:
    return f"work-items/{work_item_id}/index.md"


def write_work_item_index(adapter: DocumentLibraryAdapter, index: WorkItemIndex) -> DocumentRef:
    return adapter.write_text(work_item_index_path(index.work_item_id), index.render_markdown())


def write_root_work_item_index(adapter: DocumentLibraryAdapter, work_items: list[WorkItemIndex]) -> DocumentRef:
    lines = ["# Work Items", ""]
    if not work_items:
        lines.append("No work items recorded.")
    for item in sorted(work_items, key=lambda candidate: candidate.work_item_id):
        lines.append(f"- [{item.title}](work-items/{item.work_item_id}/index.md) - `{item.status}` - {item.owner_role}")
    lines.append("")
    return adapter.write_text("work-items/index.md", "\n".join(lines))
