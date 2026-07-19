from __future__ import annotations

from dataclasses import dataclass
import subprocess

import pytest

from agentic_mesh_v5.document_store import DocumentMetadata, DocumentPage
from agentic_mesh_v5.import_discovery import BindingInventory
from agentic_mesh_v5.import_discovery import BindingSource
from agentic_mesh_v5.import_discovery import DiscoverySourceUnavailable
from agentic_mesh_v5.import_discovery import DocumentRootSource
from agentic_mesh_v5.import_discovery import DocumentTreeDiscovery
from agentic_mesh_v5.import_discovery import GitCliRepositoryDiscovery
from agentic_mesh_v5.import_discovery import ImportDiscoveryError
from agentic_mesh_v5.import_discovery import ImportDiscoveryRequest
from agentic_mesh_v5.import_discovery import ProjectImportDiscovery
from agentic_mesh_v5.import_discovery import RepositoryInventory
from agentic_mesh_v5.import_discovery import RepositorySource


def _repository(source_id: str = "runtime") -> RepositorySource:
    return RepositorySource(
        source_id,
        f"https://github.com/example/{source_id}.git",
        "develop",
        "oauth-cache://github/current",
    )


def _documents(source_id: str = "library") -> DocumentRootSource:
    return DocumentRootSource(
        source_id,
        "onedrive",
        f"drive-{source_id}",
        f"/Projects/{source_id}",
        "oauth-cache://graph/current",
    )


def _binding(source_id: str = "ado") -> BindingSource:
    return BindingSource(
        source_id,
        "ado",
        f"https://dev.azure.com/example/{source_id}",
        "oauth-cache://ado/current",
    )


@dataclass
class _RepositoryAdapter:
    unavailable: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        self.calls: list[str] = []

    def inspect(self, source: RepositorySource) -> RepositoryInventory:
        self.calls.append(source.source_id)
        if source.source_id in self.unavailable:
            raise DiscoverySourceUnavailable("private transport detail")
        revision = ("a" if source.source_id == "runtime" else "b") * 40
        return RepositoryInventory("develop", revision, 4, "c" * 64)


@dataclass
class _BindingAdapter:
    unavailable: bool = False

    def __post_init__(self) -> None:
        self.calls: list[str] = []

    def inspect(self, source: BindingSource) -> BindingInventory:
        self.calls.append(source.source_id)
        if self.unavailable:
            raise DiscoverySourceUnavailable("connector token leaked here")
        return BindingInventory("reachable")


class _PagedDocumentStore:
    def __init__(
        self, items: dict[str, tuple[DocumentMetadata, ...]], *, unavailable: bool = False
    ) -> None:
        self.items = items
        self.unavailable = unavailable
        self.calls: list[tuple[str, str | None, int]] = []

    def list(
        self, path: str = "", *, cursor: str | None = None, page_size: int = 200
    ) -> DocumentPage:
        self.calls.append((path, cursor, page_size))
        if self.unavailable:
            raise RuntimeError("Graph token must not escape")
        start = 0 if cursor is None else int(cursor)
        entries = self.items.get(path, ())
        end = min(start + page_size, len(entries))
        next_cursor = str(end) if end < len(entries) else None
        return DocumentPage(entries[start:end], next_cursor)

    def stat(self, path: str):
        raise AssertionError("discovery must not call stat")

    def read(self, path: str):
        raise AssertionError("discovery must not read content")

    def create(self, path: str, content: bytes, *, content_type: str):
        raise AssertionError("discovery must not create documents")

    def update(
        self,
        path: str,
        content: bytes,
        *,
        content_type: str,
        expected_etag: str,
    ):
        raise AssertionError("discovery must not update documents")


def _metadata(
    path: str, *, folder: bool = False, size: int = 10, modified: str = "2026-01-01"
) -> DocumentMetadata:
    return DocumentMetadata(
        item_id=path,
        name=path.rsplit("/", 1)[-1],
        path=path,
        size=0 if folder else size,
        etag=f'etag-{path}',
        is_folder=folder,
        mime_type=None if folder else "text/plain",
        created_at="2025-01-01",
        modified_at=modified,
    )


def test_discovers_multiple_sources_and_produces_a_deterministic_report() -> None:
    store = _PagedDocumentStore(
        {
            "": (
                _metadata("architecture", folder=True),
                _metadata("README.md", size=20),
            ),
            "architecture": (_metadata("architecture/design.md", size=30),),
        }
    )
    repositories = _RepositoryAdapter()
    bindings = _BindingAdapter()
    discovery = ProjectImportDiscovery(
        repositories=repositories,
        documents={"onedrive": DocumentTreeDiscovery(lambda _: store, page_size=1)},
        bindings={"ado": bindings},
    )
    request = ImportDiscoveryRequest(
        "sample-project",
        "Sample Project",
        repositories=(_repository("portal"), _repository()),
        document_roots=(_documents(),),
        bindings=(_binding(), BindingSource("teams", "teams", "tenant|team")),
    )

    first = discovery.discover(request)
    second = discovery.discover(request)

    assert first == second
    assert first.attempted_all is True
    assert first.complete is True
    assert first.digest == second.digest
    assert len(first.results) == 5
    assert [item.source_id for item in first.results] == [
        "ado",
        "teams",
        "library",
        "portal",
        "runtime",
    ]
    document = next(item for item in first.results if item.source_id == "library")
    assert document.summary["file_count"] == 2
    assert document.summary["folder_count"] == 1
    assert document.summary["total_bytes"] == 50
    assert document.summary["page_count"] == 3
    assert document.summary["sample_paths"] == (
        "README.md",
        "architecture",
        "architecture/design.md",
    )
    assert document.credential_configured is True
    assert len(document.configuration_digest) == 64
    ado = next(item for item in first.results if item.source_id == "ado")
    assert ado.summary["binding_kind"] == "ado"
    assert bindings.calls == ["ado", "ado"]
    assert all(method[0] in {"", "architecture"} for method in store.calls)


def test_large_document_tree_is_fully_counted_with_a_bounded_sample() -> None:
    root_items = tuple(_metadata(f"file-{index:04}.md", size=index) for index in range(450))
    nested_items = tuple(
        _metadata(f"archive/file-{index:04}.pdf", size=100) for index in range(80)
    )
    store = _PagedDocumentStore(
        {"": (_metadata("archive", folder=True), *root_items), "archive": nested_items}
    )
    adapter = DocumentTreeDiscovery(
        lambda _: store, maximum_items=1_000, sample_size=7, page_size=37
    )

    inventory = adapter.inspect(_documents())

    assert inventory.complete is True
    assert inventory.file_count == 530
    assert inventory.folder_count == 1
    assert inventory.total_bytes == sum(range(450)) + 8_000
    assert inventory.page_count == 16
    assert len(inventory.sample_paths) == 7
    assert inventory.sample_paths == tuple(sorted(inventory.sample_paths))
    assert all(len(page.items) <= 37 for page in _pages_seen(store))


def _pages_seen(store: _PagedDocumentStore) -> tuple[DocumentPage, ...]:
    pages: list[DocumentPage] = []
    for path, cursor, size in store.calls:
        start = 0 if cursor is None else int(cursor)
        entries = store.items[path]
        end = min(start + size, len(entries))
        pages.append(DocumentPage(entries[start:end], None))
    return tuple(pages)


def test_inaccessible_sources_do_not_stop_remaining_inventory_or_leak_errors() -> None:
    repositories = _RepositoryAdapter(frozenset({"private"}))
    document_store = _PagedDocumentStore({}, unavailable=True)
    binding = _BindingAdapter(unavailable=True)
    discovery = ProjectImportDiscovery(
        repositories=repositories,
        documents={"onedrive": DocumentTreeDiscovery(lambda _: document_store)},
        bindings={"ado": binding},
    )
    request = ImportDiscoveryRequest(
        "sample-project",
        None,
        repositories=(_repository("private"), _repository("public")),
        document_roots=(_documents(),),
        bindings=(_binding(),),
    )

    report = discovery.discover(request)
    encoded = str(report.to_dict())

    assert report.attempted_all is True
    assert report.complete is False
    assert repositories.calls == ["private", "public"]
    assert binding.calls == ["ado"]
    assert {item.source_id for item in report.results} == {
        "private",
        "public",
        "library",
        "ado",
    }
    assert next(item for item in report.results if item.source_id == "public").status == (
        "discovered"
    )
    assert sum(issue.code == "source-unavailable" for issue in report.issues) == 3
    assert "token" not in encoded.casefold()
    assert "private transport detail" not in encoded


def test_conflicting_ids_and_locators_are_reported_after_all_sources_are_tried() -> None:
    repositories = _RepositoryAdapter()
    same_locator = "https://github.com/example/shared.git"
    request = ImportDiscoveryRequest(
        None,
        None,
        repositories=(
            RepositorySource("repo", same_locator),
            RepositorySource("repo", "https://github.com/example/other.git"),
            RepositorySource("another", same_locator),
        ),
    )
    report = ProjectImportDiscovery(
        repositories=repositories,
        documents={},
    ).discover(request)

    assert report.attempted_all is True
    assert report.complete is False
    assert repositories.calls == ["repo", "repo", "another"]
    assert {issue.code for issue in report.issues} == {
        "source-id-conflict",
        "locator-conflict",
    }
    assert len(report.results) == 3


def test_source_identifier_conflicts_across_source_kinds() -> None:
    store = _PagedDocumentStore({"": ()})
    report = ProjectImportDiscovery(
        repositories=_RepositoryAdapter(),
        documents={"onedrive": DocumentTreeDiscovery(lambda _: store)},
    ).discover(
        ImportDiscoveryRequest(
            None,
            None,
            repositories=(_repository("shared"),),
            document_roots=(_documents("shared"),),
            bindings=(BindingSource("shared", "teams", "tenant|team"),),
        )
    )

    assert report.attempted_all is True
    assert report.complete is False
    conflicts = [issue for issue in report.issues if issue.code == "source-id-conflict"]
    assert len(conflicts) == 3
    assert {issue.source_kind for issue in conflicts} == {
        "repository",
        "document-root",
        "binding",
    }


def test_observed_default_branch_conflict_makes_report_incomplete() -> None:
    report = ProjectImportDiscovery(
        repositories=_RepositoryAdapter(),
        documents={},
    ).discover(
        ImportDiscoveryRequest(
            None,
            None,
            repositories=(
                RepositorySource(
                    "repo",
                    "https://github.com/example/repo.git",
                    default_branch_hint="main",
                ),
            ),
        )
    )

    assert report.complete is False
    assert report.results[0].summary["default_branch_matches_hint"] is False
    assert [issue.code for issue in report.issues] == ["default-branch-conflict"]


def test_document_safety_limit_is_visible_and_makes_report_incomplete() -> None:
    store = _PagedDocumentStore(
        {"": tuple(_metadata(f"file-{index}.md") for index in range(5))}
    )
    discovery = ProjectImportDiscovery(
        repositories=_RepositoryAdapter(),
        documents={
            "onedrive": DocumentTreeDiscovery(
                lambda _: store, maximum_items=3, page_size=2
            )
        },
    )

    report = discovery.discover(
        ImportDiscoveryRequest(None, None, document_roots=(_documents(),))
    )

    assert report.complete is False
    assert report.results[0].summary["file_count"] == 3
    assert report.results[0].summary["complete"] is False
    assert [issue.code for issue in report.issues] == ["safety-limit"]


def test_duplicate_document_path_fails_closed_without_exposing_adapter_detail() -> None:
    store = _PagedDocumentStore(
        {"": (_metadata("duplicate.md"), _metadata("duplicate.md"))}
    )
    report = ProjectImportDiscovery(
        repositories=_RepositoryAdapter(),
        documents={"onedrive": DocumentTreeDiscovery(lambda _: store)},
    ).discover(ImportDiscoveryRequest(None, None, document_roots=(_documents(),)))

    assert report.complete is False
    assert report.results[0].status == "unavailable"
    assert report.results[0].summary == {}
    assert [issue.code for issue in report.issues] == ["source-unavailable"]


def test_unknown_document_adapter_and_empty_request_are_incomplete() -> None:
    unknown = DocumentRootSource(
        "library", "future-store", "drive", "/root", "secret://future/reference"
    )
    discovery = ProjectImportDiscovery(
        repositories=_RepositoryAdapter(),
        documents={},
    )

    unknown_report = discovery.discover(
        ImportDiscoveryRequest(None, None, document_roots=(unknown,))
    )
    empty_report = discovery.discover(ImportDiscoveryRequest(None, None))

    assert unknown_report.attempted_all is True
    assert unknown_report.complete is False
    assert unknown_report.issues[0].code == "adapter-unavailable"
    assert empty_report.attempted_all is True
    assert empty_report.complete is False
    assert empty_report.issues[0].code == "no-sources"


def test_git_cli_discovery_uses_only_ls_remote_and_summarises_refs() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "ref: refs/heads/develop\tHEAD\n"
                f"{'a' * 40}\tHEAD\n"
                f"{'a' * 40}\trefs/heads/develop\n"
                f"{'b' * 40}\trefs/heads/main\n"
            ),
            stderr="",
        )

    inventory = GitCliRepositoryDiscovery(runner=runner).inspect(_repository())

    assert calls[0][0] == [
        "git",
        "ls-remote",
        "--symref",
        "https://github.com/example/runtime.git",
        "HEAD",
        "refs/heads/*",
    ]
    assert calls[0][1]["shell"] is False
    assert inventory.default_branch == "develop"
    assert inventory.head_revision == "a" * 40
    assert inventory.branch_count == 2
    assert len(inventory.refs_digest) == 64


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: RepositorySource("repo", "https://user:secret@example.com/repo"),
            "credential-free",
        ),
        (
            lambda: BindingSource("ado", "ado", "target?token=secret"),
            "credentials",
        ),
        (
            lambda: DocumentRootSource(
                "docs", "onedrive", "drive", "/../escape", "secret://graph/ref"
            ),
            "root",
        ),
    ],
)
def test_source_declarations_reject_embedded_credentials_and_unsafe_roots(
    factory, message: str
) -> None:
    with pytest.raises(ImportDiscoveryError, match=message):
        factory()


def test_configuration_digest_changes_without_exposing_credential_reference() -> None:
    discovery = ProjectImportDiscovery(
        repositories=_RepositoryAdapter(),
        documents={},
    )
    first = discovery.discover(
        ImportDiscoveryRequest(
            None,
            None,
            repositories=(
                RepositorySource(
                    "repo",
                    "https://github.com/example/repo.git",
                    credential_reference="secret://git/one",
                ),
            ),
        )
    )
    second = discovery.discover(
        ImportDiscoveryRequest(
            None,
            None,
            repositories=(
                RepositorySource(
                    "repo",
                    "https://github.com/example/repo.git",
                    credential_reference="secret://git/two",
                ),
            ),
        )
    )

    assert first.digest != second.digest
    assert first.results[0].configuration_digest != second.results[0].configuration_digest
    assert "secret://" not in str(first.to_dict())
