from __future__ import annotations

import posixpath
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from typing import Protocol
from typing import TYPE_CHECKING
from urllib.parse import quote
from urllib.parse import urlencode

if TYPE_CHECKING:
    from agentic_mesh_v3.project_config import V3DocumentLibraryConfig


@dataclass(frozen=True)
class DocumentRef:
    relative_path: str
    title: str
    url: str | None = None


@dataclass(frozen=True)
class DocumentTypeRule:
    document_type: str
    title: str
    path_template: str | None = None

    def path_for(self, *, work_item_id: str) -> str:
        if self.path_template is None:
            raise DocumentLibraryError(f"document type `{self.document_type}` does not define a fixed path")
        return self.path_template.format(work_item_id=work_item_id)


@dataclass(frozen=True)
class DocumentFramework:
    framework_id: str
    document_types: tuple[DocumentTypeRule, ...]

    def rule_for(self, document_type: str) -> DocumentTypeRule | None:
        normalized = document_type.strip().casefold().replace("-", "_")
        for rule in self.document_types:
            if rule.document_type == normalized:
                return rule
        return None


TOGAF_SDLC_V1 = DocumentFramework(
    framework_id="togaf-sdlc-v1",
    document_types=(
        DocumentTypeRule(
            document_type="work_item_index",
            title="Work item index",
            path_template="work-items/{work_item_id}/index.md",
        ),
        DocumentTypeRule(
            document_type="product_definition",
            title="Product definition",
            path_template="work-items/{work_item_id}/020-product-definition.md",
        ),
        DocumentTypeRule(
            document_type="solution_design",
            title="Solution design",
            path_template="work-items/{work_item_id}/030-solution-design.md",
        ),
        DocumentTypeRule(
            document_type="security_review",
            title="Security review",
            path_template="work-items/{work_item_id}/050-security-review.md",
        ),
        DocumentTypeRule(
            document_type="prompt_contract",
            title="Prompt contract",
            path_template="work-items/{work_item_id}/060-prompt-contract.md",
        ),
        DocumentTypeRule(
            document_type="implementation_log",
            title="Implementation log",
            path_template="work-items/{work_item_id}/100-implementation-log.md",
        ),
        DocumentTypeRule(
            document_type="qa_evidence",
            title="QA evidence",
            path_template="work-items/{work_item_id}/110-quality-evidence.md",
        ),
        DocumentTypeRule(
            document_type="release_record",
            title="Release record",
            path_template="work-items/{work_item_id}/140-release-record.md",
        ),
        DocumentTypeRule(document_type="decision_register", title="Decision register", path_template="decisions/index.md"),
        DocumentTypeRule(document_type="risk_register", title="Risk register", path_template="risks/index.md"),
        DocumentTypeRule(document_type="artifact", title="Generic artifact"),
    ),
)

DOCUMENT_FRAMEWORKS = {TOGAF_SDLC_V1.framework_id: TOGAF_SDLC_V1}


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
            lines.extend(
                f"- [{artifact.title}]({_work_item_artifact_link(self.work_item_id, artifact)})"
                for artifact in self.artifacts
            )
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


@dataclass(frozen=True)
class GovernanceRegisterItem:
    record_id: str
    work_item_id: str
    summary: str
    status: str
    role_instance_id: str
    target_ref: str | None = None


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
    def __init__(self, root: Path, *, framework_id: str = "togaf-sdlc-v1") -> None:
        self.root = root
        self.framework_id = framework_for(framework_id).framework_id

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
        framework_id: str = "togaf-sdlc-v1",
        graph_base_url: str = "https://graph.microsoft.com/v1.0",
        transport: "GraphDocumentTransport | None" = None,
        token_provider: "GraphAccessTokenProvider | None" = None,
    ) -> None:
        self.drive_id = drive_id
        self.access_token = access_token
        self.root_path = root_path
        self.framework_id = framework_for(framework_id).framework_id
        self.graph_base_url = graph_base_url.rstrip("/")
        self.transport = transport or UrlLibGraphDocumentTransport(
            access_token=access_token,
            token_provider=token_provider,
        )

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


class GraphAccessTokenProvider(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str | None:
        """Return a Graph access token, refreshing it when requested."""


class RefreshTokenGraphAccessTokenProvider:
    """Refresh Microsoft Graph access tokens from a persisted device-flow token."""

    def __init__(
        self,
        *,
        client_id: str,
        tenant_id: str,
        refresh_token: str,
        scopes: tuple[str, ...],
        token_base_url: str = "https://login.microsoftonline.com",
    ) -> None:
        if not client_id.strip():
            raise ValueError("client_id is required")
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not refresh_token.strip():
            raise ValueError("refresh_token is required")
        self.client_id = client_id.strip()
        self.tenant_id = tenant_id.strip()
        self.refresh_token = refresh_token.strip()
        self.scopes = scopes
        self.token_base_url = token_base_url.rstrip("/")
        self._access_token: str | None = None

    def access_token(self, *, force_refresh: bool = False) -> str | None:
        if self._access_token is not None and not force_refresh:
            return self._access_token
        response = self._request_token()
        token = response.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("Graph refresh response did not contain an access token")
        new_refresh_token = response.get("refresh_token")
        if isinstance(new_refresh_token, str) and new_refresh_token.strip():
            self.refresh_token = new_refresh_token.strip()
        self._access_token = token.strip()
        return self._access_token

    def _request_token(self) -> dict[str, object]:
        import urllib.request

        body = urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
                "scope": " ".join(self.scopes),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.token_base_url}/{quote(self.tenant_id, safe='')}/oauth2/v2.0/token",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = json.loads(response.read().decode("utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError("Graph refresh response was not an object")
        return raw


class UrlLibGraphDocumentTransport:
    def __init__(
        self,
        *,
        access_token: str | None,
        token_provider: GraphAccessTokenProvider | None = None,
    ) -> None:
        self.access_token = access_token
        self.token_provider = token_provider

    def put_text(self, url: str, content: str) -> dict[str, object]:
        import urllib.request

        request = urllib.request.Request(
            url,
            data=content.encode("utf-8"),
            method="PUT",
            headers=self._headers(content_type="text/plain; charset=utf-8"),
        )
        with self._open_with_refresh(request) as response:
            raw = json.loads(response.read().decode("utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError("Graph upload response was not an object")
        return raw

    def get_text(self, url: str) -> str:
        import urllib.request

        request = urllib.request.Request(url, headers=self._headers())
        with self._open_with_refresh(request) as response:
            return response.read().decode("utf-8")

    def exists(self, url: str) -> bool:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with self._open_with_refresh(request):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def _open_with_refresh(self, request: "urllib.request.Request"):
        import urllib.error
        import urllib.request

        try:
            return urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or self.token_provider is None:
                raise
            token = self.token_provider.access_token(force_refresh=True)
            if not token:
                raise
            request.remove_header("Authorization")
            request.add_header("Authorization", f"Bearer {token}")
            return urllib.request.urlopen(request, timeout=30)

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        token = self.token_provider.access_token() if self.token_provider is not None else self.access_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers


def build_document_library_adapter(
    config: "V3DocumentLibraryConfig",
    *,
    access_token: str | None = None,
    graph_base_url: str = "https://graph.microsoft.com/v1.0",
    transport: GraphDocumentTransport | None = None,
    token_provider: GraphAccessTokenProvider | None = None,
) -> DocumentLibraryAdapter:
    adapter = config.adapter.casefold().replace("_", "-")
    if adapter in {"local", "filesystem", "file", "git"}:
        if config.root is None:
            raise ValueError("document_library.root is required for local/filesystem/git document libraries")
        return LocalDocumentLibraryAdapter(config.root, framework_id=config.structure_policy)
    if adapter in {"onedrive", "sharepoint"}:
        if not config.drive_id:
            raise ValueError("document_library.drive_id is required for OneDrive/SharePoint document libraries")
        if transport is None and not access_token and token_provider is None:
            raise ValueError("AGENTIC_MESH_ONEDRIVE_TOKEN or refresh-token configuration is required for OneDrive/SharePoint document libraries")
        return OneDriveDocumentLibraryAdapter(
            config.drive_id,
            access_token=access_token,
            root_path=config.root_path,
            framework_id=config.structure_policy,
            graph_base_url=graph_base_url,
            transport=transport,
            token_provider=token_provider,
        )
    raise ValueError(f"unsupported document library adapter: {config.adapter}")


def work_item_index_path(work_item_id: str) -> str:
    return f"work-items/{work_item_id}/index.md"


def framework_for(framework_id: str) -> DocumentFramework:
    normalized = framework_id.strip().casefold()
    try:
        return DOCUMENT_FRAMEWORKS[normalized]
    except KeyError as exc:
        raise DocumentLibraryError(f"unsupported document framework: {framework_id}") from exc


def validate_framework_artifact_path(
    *,
    framework_id: str,
    document_type: str,
    work_item_id: str,
    relative_path: str,
) -> None:
    rule = framework_for(framework_id).rule_for(document_type)
    if rule is None:
        raise DocumentLibraryError(f"unknown document type `{document_type}` for framework `{framework_id}`")
    if rule.path_template is None:
        return
    expected = rule.path_for(work_item_id=work_item_id)
    actual = relative_path.replace("\\", "/").lstrip("/")
    if actual != expected:
        raise DocumentLibraryError(
            f"document type `{document_type}` must use framework path `{expected}`, got `{actual}`"
        )


def _work_item_artifact_link(work_item_id: str, artifact: DocumentRef) -> str:
    if artifact.url:
        return artifact.url
    target = artifact.relative_path.replace("\\", "/").lstrip("/")
    if "\x00" in target or target.startswith("../") or "/../" in target:
        raise DocumentLibraryError("artifact path escapes document library root")
    if not target:
        raise DocumentLibraryError("artifact path is required")
    if "/" not in target:
        return target
    return posixpath.relpath(target, start=f"work-items/{work_item_id}")


def write_work_item_index(adapter: DocumentLibraryAdapter, index: WorkItemIndex) -> DocumentRef:
    validate_work_item_index(index)
    return adapter.write_text(work_item_index_path(index.work_item_id), index.render_markdown())


def write_root_work_item_index(adapter: DocumentLibraryAdapter, work_items: list[WorkItemIndex]) -> DocumentRef:
    lines = ["# Work Items", ""]
    if not work_items:
        lines.append("No work items recorded.")
    for item in sorted(work_items, key=lambda candidate: candidate.work_item_id):
        lines.append(f"- [{item.title}]({item.work_item_id}/index.md) - `{item.status}` - {item.owner_role}")
        lines.append(f"  - Governance: {item.governance_state}")
        lines.append(f"  - Next action: {item.next_action or 'None recorded'}")
    lines.append("")
    return adapter.write_text("work-items/index.md", "\n".join(lines))


def write_governance_register(
    adapter: DocumentLibraryAdapter,
    *,
    relative_path: str,
    title: str,
    items: Iterable[GovernanceRegisterItem],
) -> DocumentRef:
    lines = [f"# {title}", ""]
    sorted_items = sorted(items, key=lambda item: (item.work_item_id, item.record_id))
    if not sorted_items:
        lines.append("No records captured.")
    for item in sorted_items:
        line = (
            f"- `{item.status}` [{item.work_item_id}](../work-items/{item.work_item_id}/index.md) - "
            f"{item.summary} "
            f"(role: `{item.role_instance_id}`, record: `{item.record_id}`"
        )
        if item.target_ref:
            line = f"{line}, target: `{item.target_ref}`"
        lines.append(f"{line})")
    lines.append("")
    return adapter.write_text(relative_path, "\n".join(lines))


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
