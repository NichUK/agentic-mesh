from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from typing import Protocol
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from agentic_mesh_v3.project_config import V3DocumentLibraryConfig


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
    consultations: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
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
        lines.extend(["", "## Consultations"])
        if self.consultations:
            lines.extend(f"- {consultation}" for consultation in self.consultations)
        else:
            lines.append("- None recorded")
        lines.extend(["", "## Approvals"])
        if self.approvals:
            lines.extend(f"- {approval}" for approval in self.approvals)
        else:
            lines.append("- None recorded")
        lines.extend(["", "## Evidence"])
        if self.evidence:
            lines.extend(f"- {evidence}" for evidence in self.evidence)
        else:
            lines.append("- None recorded")
        lines.extend(["", "## Decisions"])
        lines.extend(f"- {decision}" for decision in self.decisions) if self.decisions else lines.append("- None recorded")
        lines.extend(["", "## Risks"])
        lines.extend(f"- {risk}" for risk in self.risks) if self.risks else lines.append("- None recorded")
        lines.append("")
        return "\n".join(lines)


class DocumentLibraryError(ValueError):
    """Raised when a document-library artifact violates V3 evidence rules."""


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
    """Microsoft Graph-backed OneDrive/SharePoint document adapter.

    The HTTP transport is injectable so tests and enterprise hosts can provide
    their own authentication/session layer. The adapter writes beneath the
    configured Teams Shared Files root, normally `/documents`.
    """

    def __init__(
        self,
        drive_id: str,
        *,
        access_token: str | None = None,
        root_path: str = "/documents",
        graph_base_url: str = "https://graph.microsoft.com/v1.0",
        transport: "GraphDocumentTransport | None" = None,
    ) -> None:
        self.drive_id = drive_id
        self.access_token = access_token
        self.root_path = root_path
        self.graph_base_url = graph_base_url.rstrip("/")
        self.transport = transport or UrlLibGraphDocumentTransport(access_token=access_token)

    def write_text(self, relative_path: str, content: str) -> DocumentRef:
        graph_path = self._graph_path(relative_path)
        response = self.transport.put_text(
            f"{self.graph_base_url}/drives/{self.drive_id}/root:/{graph_path}:/content",
            content,
        )
        web_url = response.get("webUrl") if isinstance(response, dict) else None
        return DocumentRef(relative_path=relative_path.replace("\\", "/"), title=Path(relative_path).name, url=web_url)

    def read_text(self, relative_path: str) -> str:
        graph_path = self._graph_path(relative_path)
        return self.transport.get_text(
            f"{self.graph_base_url}/drives/{self.drive_id}/root:/{graph_path}:/content"
        )

    def exists(self, relative_path: str) -> bool:
        graph_path = self._graph_path(relative_path)
        return self.transport.exists(
            f"{self.graph_base_url}/drives/{self.drive_id}/root:/{graph_path}"
        )

    def _graph_path(self, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/").lstrip("/")
        if "\x00" in normalized or normalized.startswith("../") or "/../" in normalized:
            raise ValueError("document path escapes library root")
        root = self.root_path.strip("/")
        full_path = f"{root}/{normalized}" if root else normalized
        return "/".join(quote(part) for part in full_path.split("/"))


class GraphDocumentTransport(Protocol):
    def put_text(self, url: str, content: str) -> dict[str, object]:
        """Upload text content."""

    def get_text(self, url: str) -> str:
        """Download text content."""

    def exists(self, url: str) -> bool:
        """Return whether a Graph drive item exists."""


class UrlLibGraphDocumentTransport:
    def __init__(self, *, access_token: str | None) -> None:
        self.access_token = access_token

    def put_text(self, url: str, content: str) -> dict[str, object]:
        import json
        import urllib.request

        request = urllib.request.Request(
            url,
            data=content.encode("utf-8"),
            method="PUT",
            headers=self._headers(content_type="text/plain; charset=utf-8"),
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_text(self, url: str) -> str:
        import urllib.request

        request = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")

    def exists(self, url: str) -> bool:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers


def build_document_library_adapter(
    config: "V3DocumentLibraryConfig",
    *,
    access_token: str | None = None,
    graph_base_url: str = "https://graph.microsoft.com/v1.0",
    transport: GraphDocumentTransport | None = None,
) -> DocumentLibraryAdapter:
    adapter = config.adapter.casefold().replace("_", "-")
    if adapter in {"local", "filesystem", "file", "git"}:
        if config.root is None:
            raise ValueError("document_library.root is required for local/filesystem/git document libraries")
        return LocalDocumentLibraryAdapter(config.root)
    if adapter in {"onedrive", "sharepoint"}:
        if not config.drive_id:
            raise ValueError("document_library.drive_id is required for OneDrive/SharePoint document libraries")
        return OneDriveDocumentLibraryAdapter(
            config.drive_id,
            access_token=access_token,
            root_path=config.root_path,
            graph_base_url=graph_base_url,
            transport=transport,
        )
    raise ValueError(f"unsupported document library adapter: {config.adapter}")


def work_item_index_path(work_item_id: str) -> str:
    return f"work-items/{work_item_id}/index.md"


def write_work_item_index(adapter: DocumentLibraryAdapter, index: WorkItemIndex) -> DocumentRef:
    validate_work_item_index(index)
    return adapter.write_text(work_item_index_path(index.work_item_id), index.render_markdown())


def write_root_work_item_index(adapter: DocumentLibraryAdapter, work_items: list[WorkItemIndex]) -> DocumentRef:
    lines = ["# Work Items", ""]
    if not work_items:
        lines.append("No work items recorded.")
    for item in sorted(work_items, key=lambda candidate: candidate.work_item_id):
        lines.append(f"- [{item.title}]({item.work_item_id}/index.md) - `{item.status}` - {item.owner_role}")
    lines.append("")
    return adapter.write_text("work-items/index.md", "\n".join(lines))


def validate_work_item_index(index: WorkItemIndex) -> None:
    if not index.work_item_id.strip():
        raise DocumentLibraryError("work item index requires work_item_id")
    if not index.title.strip():
        raise DocumentLibraryError("work item index requires title")
    if not index.status.strip():
        raise DocumentLibraryError("work item index requires status")
    if not index.owner_role.strip():
        raise DocumentLibraryError("work item index requires owner_role")
    if not index.raci_summary.strip():
        raise DocumentLibraryError("work item index requires raci_summary")
    if not index.governance_state.strip():
        raise DocumentLibraryError("work item index requires governance_state")
    has_evidence = bool(
        index.artifacts
        or index.consultations
        or index.approvals
        or index.evidence
        or index.decisions
        or index.risks
        or index.next_action.strip()
    )
    if not has_evidence:
        raise DocumentLibraryError("work item index cannot be status-only; record evidence, risks, decisions, or next action")
    _reject_duplicates(
        (artifact.relative_path.casefold() for artifact in index.artifacts),
        label="artifact path",
    )
    _reject_duplicates((consultation.strip().casefold() for consultation in index.consultations), label="consultation")
    _reject_duplicates((approval.strip().casefold() for approval in index.approvals), label="approval")
    _reject_duplicates((evidence.strip().casefold() for evidence in index.evidence), label="evidence")
    _reject_duplicates((decision.strip().casefold() for decision in index.decisions), label="decision")
    _reject_duplicates((risk.strip().casefold() for risk in index.risks), label="risk")


def _reject_duplicates(values: Iterable[str], *, label: str) -> None:
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        if text in seen:
            raise DocumentLibraryError(f"work item index duplicates {label}: {text}")
        seen.add(text)
