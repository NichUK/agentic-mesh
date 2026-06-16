from pathlib import Path

from agentic_mesh_v3.documents import DocumentRef
from agentic_mesh_v3.documents import DocumentLibraryError
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.documents import OneDriveDocumentLibraryAdapter
from agentic_mesh_v3.documents import WorkItemIndex
from agentic_mesh_v3.documents import build_document_library_adapter
from agentic_mesh_v3.documents import work_item_index_path
from agentic_mesh_v3.documents import write_root_work_item_index
from agentic_mesh_v3.documents import write_work_item_index
from agentic_mesh_v3.project_config import V3DocumentLibraryConfig


def test_work_item_index_path_uses_documents_work_item_shape() -> None:
    assert work_item_index_path("work-123") == "work-items/work-123/index.md"


def test_write_work_item_index(tmp_path: Path) -> None:
    adapter = LocalDocumentLibraryAdapter(tmp_path)
    index = WorkItemIndex(
        work_item_id="work-123",
        title="Add status page",
        status="active",
        owner_role="engineering",
        raci_summary="engineering A/R, qa-engineer C",
        governance_state="qa consulted",
        artifacts=(DocumentRef(relative_path="020-product-definition.md", title="Product definition"),),
        consultations=("QA consulted on acceptance evidence",),
        approvals=("Sponsor approved product sign-off",),
        evidence=("Focused status-page tests passed",),
        decisions=("Sponsor approved scope",),
        risks=("No staging environment yet",),
        next_action="Implement",
    )

    ref = write_work_item_index(adapter, index)

    assert ref.relative_path == "work-items/work-123/index.md"
    content = (tmp_path / "work-items" / "work-123" / "index.md").read_text(encoding="utf-8")
    assert "# Add status page" in content
    assert "qa consulted" in content
    assert "[Product definition](020-product-definition.md)" in content
    assert "## Consultations" in content
    assert "QA consulted on acceptance evidence" in content
    assert "## Approvals" in content
    assert "Sponsor approved product sign-off" in content
    assert "## Evidence" in content
    assert "Focused status-page tests passed" in content


def test_work_item_index_links_artifacts_relative_to_work_item_folder(tmp_path: Path) -> None:
    adapter = LocalDocumentLibraryAdapter(tmp_path)
    index = WorkItemIndex(
        work_item_id="work-123",
        title="Link artifacts",
        status="active",
        owner_role="engineering",
        raci_summary="engineering A/R",
        governance_state="ready",
        artifacts=(
            DocumentRef(relative_path="work-items/work-123/020-product-definition.md", title="Product definition"),
            DocumentRef(relative_path="work-items/work-456/index.md", title="Related work"),
            DocumentRef(relative_path="architecture/decisions.md", title="Decision register"),
            DocumentRef(
                relative_path="work-items/work-123/030-design.md",
                title="Design web link",
                url="https://example.test/design",
            ),
        ),
    )

    write_work_item_index(adapter, index)

    content = (tmp_path / "work-items" / "work-123" / "index.md").read_text(encoding="utf-8")
    assert "[Product definition](020-product-definition.md)" in content
    assert "[Related work](../work-456/index.md)" in content
    assert "[Decision register](../../architecture/decisions.md)" in content
    assert "[Design web link](https://example.test/design)" in content


def test_work_item_index_rejects_escaping_artifact_links(tmp_path: Path) -> None:
    adapter = LocalDocumentLibraryAdapter(tmp_path)
    index = WorkItemIndex(
        work_item_id="work-123",
        title="Bad artifact",
        status="active",
        owner_role="engineering",
        raci_summary="engineering A/R",
        governance_state="ready",
        artifacts=(DocumentRef(relative_path="../secrets.md", title="Bad link"),),
    )

    try:
        write_work_item_index(adapter, index)
    except DocumentLibraryError as exc:
        assert "escapes document library root" in str(exc)
    else:
        raise AssertionError("escaping artifact links should be rejected")


def test_write_root_work_item_index(tmp_path: Path) -> None:
    adapter = LocalDocumentLibraryAdapter(tmp_path)
    write_root_work_item_index(
        adapter,
        [
            WorkItemIndex(
                work_item_id="work-123",
                title="Add status page",
                status="active",
                owner_role="engineering",
                raci_summary="engineering A/R",
                governance_state="ready",
                next_action="Implement the status page.",
            )
        ],
    )

    content = (tmp_path / "work-items" / "index.md").read_text(encoding="utf-8")
    assert "[Add status page](work-123/index.md)" in content
    assert "Governance: ready" in content
    assert "Next action: Implement the status page." in content


def test_write_work_item_index_rejects_status_only_document(tmp_path: Path) -> None:
    adapter = LocalDocumentLibraryAdapter(tmp_path)
    index = WorkItemIndex(
        work_item_id="work-123",
        title="Blocked work",
        status="blocked",
        owner_role="delivery-manager",
        raci_summary="delivery-manager A/R",
        governance_state="blocked",
    )

    try:
        write_work_item_index(adapter, index)
    except DocumentLibraryError as exc:
        assert "status-only" in str(exc)
    else:
        raise AssertionError("status-only work-item indexes should be rejected")


def test_write_work_item_index_rejects_duplicate_evidence(tmp_path: Path) -> None:
    adapter = LocalDocumentLibraryAdapter(tmp_path)
    index = WorkItemIndex(
        work_item_id="work-123",
        title="Duplicate work",
        status="active",
        owner_role="engineering",
        raci_summary="engineering A/R",
        governance_state="ready",
        artifacts=(
            DocumentRef(relative_path="100-implementation.md", title="Implementation"),
            DocumentRef(relative_path="100-implementation.md", title="Implementation duplicate"),
        ),
    )

    try:
        write_work_item_index(adapter, index)
    except DocumentLibraryError as exc:
        assert "duplicates artifact path" in str(exc)
    else:
        raise AssertionError("duplicate work-item evidence should be rejected")


def test_write_work_item_index_rejects_duplicate_named_sections(tmp_path: Path) -> None:
    adapter = LocalDocumentLibraryAdapter(tmp_path)
    index = WorkItemIndex(
        work_item_id="work-123",
        title="Duplicate consultations",
        status="active",
        owner_role="engineering",
        raci_summary="engineering A/R",
        governance_state="ready",
        consultations=("QA consulted", "QA consulted"),
    )

    try:
        write_work_item_index(adapter, index)
    except DocumentLibraryError as exc:
        assert "duplicates consultation" in str(exc)
    else:
        raise AssertionError("duplicate named index sections should be rejected")


class FakeGraphDocumentTransport:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []
        self.text_by_url: dict[str, str] = {}

    def put_text(self, url: str, content: str) -> dict[str, object]:
        self.puts.append((url, content))
        self.text_by_url[url] = content
        return {"webUrl": "https://example.test/doc"}

    def get_text(self, url: str) -> str:
        return self.text_by_url[url]

    def exists(self, url: str) -> bool:
        return url in self.text_by_url


def test_onedrive_document_library_uses_documents_root_and_graph_paths() -> None:
    transport = FakeGraphDocumentTransport()
    adapter = OneDriveDocumentLibraryAdapter(
        "drive-123",
        root_path="/documents",
        graph_base_url="https://graph.test/v1.0",
        transport=transport,
    )

    ref = adapter.write_text("work-items/work-1/index.md", "# Work One")

    assert ref.url == "https://example.test/doc"
    assert transport.puts[0][0] == (
        "https://graph.test/v1.0/drives/drive-123/root:/documents/work-items/work-1/index.md:/content"
    )
    content_url = "https://graph.test/v1.0/drives/drive-123/root:/documents/work-items/work-1/index.md:/content"
    assert adapter.read_text("work-items/work-1/index.md") == "# Work One"
    assert transport.text_by_url[content_url] == "# Work One"


def test_document_library_factory_builds_local_adapter(tmp_path: Path) -> None:
    adapter = build_document_library_adapter(
        V3DocumentLibraryConfig(adapter="filesystem", root=tmp_path / "documents")
    )

    ref = adapter.write_text("work-items/work-1/index.md", "# Work One")

    assert ref.relative_path == "work-items/work-1/index.md"
    assert (tmp_path / "documents" / "work-items" / "work-1" / "index.md").exists()


def test_document_library_factory_builds_onedrive_adapter() -> None:
    transport = FakeGraphDocumentTransport()
    adapter = build_document_library_adapter(
        V3DocumentLibraryConfig(adapter="onedrive", drive_id="drive-123", root_path="/documents"),
        transport=transport,
    )

    adapter.write_text("work-items/work-1/index.md", "# Work One")

    assert transport.puts[0][0] == (
        "https://graph.microsoft.com/v1.0/drives/drive-123/root:/documents/work-items/work-1/index.md:/content"
    )


def test_document_library_factory_requires_onedrive_token_without_custom_transport() -> None:
    try:
        build_document_library_adapter(
            V3DocumentLibraryConfig(adapter="onedrive", drive_id="drive-123", root_path="/documents")
        )
    except ValueError as exc:
        assert "AGENTIC_MESH_ONEDRIVE_TOKEN" in str(exc)
    else:
        raise AssertionError("Graph-backed OneDrive libraries should require an access token")


def test_document_library_factory_accepts_onedrive_token_without_custom_transport() -> None:
    adapter = build_document_library_adapter(
        V3DocumentLibraryConfig(adapter="onedrive", drive_id="drive-123", root_path="/documents"),
        access_token="token-123",
    )

    assert isinstance(adapter, OneDriveDocumentLibraryAdapter)
