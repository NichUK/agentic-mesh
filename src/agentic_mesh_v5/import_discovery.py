from __future__ import annotations

from bisect import bisect_left
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import subprocess
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from agentic_mesh_v5.document_store import DocumentStore


_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_CREDENTIAL_REF = re.compile(
    r"^(?:secret|mount|oauth-cache)://[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$"
)


class ImportDiscoveryError(ValueError):
    pass


class DiscoverySourceUnavailable(RuntimeError):
    """A safe, content-free signal that a source could not be inspected."""


@dataclass(frozen=True, slots=True)
class RepositorySource:
    source_id: str
    url: str
    default_branch_hint: str | None = None
    credential_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source_id"))
        object.__setattr__(self, "url", _git_url(self.url))
        if self.default_branch_hint is not None:
            object.__setattr__(
                self,
                "default_branch_hint",
                _text(self.default_branch_hint, "default_branch_hint", 200),
            )
        object.__setattr__(
            self,
            "credential_reference",
            _credential_reference(self.credential_reference),
        )

    @property
    def locator(self) -> str:
        return self.url


@dataclass(frozen=True, slots=True)
class DocumentRootSource:
    source_id: str
    adapter: str
    drive_id: str
    root: str
    credential_reference: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source_id"))
        object.__setattr__(self, "adapter", _identifier(self.adapter, "adapter"))
        object.__setattr__(self, "drive_id", _external_id(self.drive_id, "drive_id"))
        object.__setattr__(self, "root", _document_root(self.root))
        credential = _credential_reference(self.credential_reference)
        if credential is None:
            raise ImportDiscoveryError("document credential_reference is required")
        object.__setattr__(self, "credential_reference", credential)

    @property
    def locator(self) -> str:
        return f"{self.adapter}|{self.drive_id}|{self.root}"


@dataclass(frozen=True, slots=True)
class BindingSource:
    source_id: str
    kind: str
    locator: str
    credential_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source_id"))
        object.__setattr__(self, "kind", _identifier(self.kind, "kind"))
        object.__setattr__(self, "locator", _locator(self.locator, "locator"))
        object.__setattr__(
            self,
            "credential_reference",
            _credential_reference(self.credential_reference),
        )


@dataclass(frozen=True, slots=True)
class ImportDiscoveryRequest:
    project_id_hint: str | None
    display_name_hint: str | None
    repositories: tuple[RepositorySource, ...] = ()
    document_roots: tuple[DocumentRootSource, ...] = ()
    bindings: tuple[BindingSource, ...] = ()

    def __post_init__(self) -> None:
        if self.project_id_hint is not None:
            object.__setattr__(
                self,
                "project_id_hint",
                _identifier(self.project_id_hint, "project_id_hint"),
            )
        if self.display_name_hint is not None:
            object.__setattr__(
                self,
                "display_name_hint",
                _text(self.display_name_hint, "display_name_hint", 200),
            )
        for field, expected in (
            (self.repositories, RepositorySource),
            (self.document_roots, DocumentRootSource),
            (self.bindings, BindingSource),
        ):
            if not isinstance(field, tuple) or not all(
                isinstance(item, expected) for item in field
            ):
                raise ImportDiscoveryError("discovery source collections must be tuples")


@dataclass(frozen=True, slots=True)
class RepositoryInventory:
    default_branch: str | None
    head_revision: str | None
    branch_count: int
    refs_digest: str

    def __post_init__(self) -> None:
        if self.default_branch is not None:
            object.__setattr__(
                self,
                "default_branch",
                _text(self.default_branch, "default_branch", 200),
            )
        if self.head_revision is not None and (
            not isinstance(self.head_revision, str)
            or _COMMIT.fullmatch(self.head_revision) is None
        ):
            raise ImportDiscoveryError("head_revision is invalid")
        _nonnegative_integer(self.branch_count, "branch_count")
        if (
            not isinstance(self.refs_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.refs_digest) is None
        ):
            raise ImportDiscoveryError("refs_digest is invalid")


@dataclass(frozen=True, slots=True)
class DocumentInventory:
    file_count: int
    folder_count: int
    total_bytes: int
    page_count: int
    latest_modified_at: str | None
    sample_paths: tuple[str, ...]
    complete: bool = True

    def __post_init__(self) -> None:
        for field, value in (
            ("file_count", self.file_count),
            ("folder_count", self.folder_count),
            ("total_bytes", self.total_bytes),
            ("page_count", self.page_count),
        ):
            _nonnegative_integer(value, field)
        if self.latest_modified_at is not None:
            _text(self.latest_modified_at, "latest_modified_at", 100)
        if not isinstance(self.sample_paths, tuple):
            raise ImportDiscoveryError("sample_paths must be a tuple")
        for path in self.sample_paths:
            _relative_document_path(path)
        if type(self.complete) is not bool:
            raise ImportDiscoveryError("complete must be a boolean")


@dataclass(frozen=True, slots=True)
class BindingInventory:
    status: str = "declared"

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _identifier(self.status, "status"))


class RepositoryDiscoveryAdapter(Protocol):
    def inspect(self, source: RepositorySource) -> RepositoryInventory: ...


class DocumentDiscoveryAdapter(Protocol):
    def inspect(self, source: DocumentRootSource) -> DocumentInventory: ...


class BindingDiscoveryAdapter(Protocol):
    def inspect(self, source: BindingSource) -> BindingInventory: ...


@dataclass(frozen=True, slots=True)
class DiscoveryIssue:
    code: str
    source_kind: str | None
    source_id: str | None
    message: str

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SourceDiscoveryResult:
    source_kind: str
    source_id: str
    locator: str
    status: str
    credential_configured: bool
    configuration_digest: str
    summary: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "locator": self.locator,
            "status": self.status,
            "credential_configured": self.credential_configured,
            "configuration_digest": self.configuration_digest,
            "summary": dict(self.summary),
        }


@dataclass(frozen=True, slots=True)
class ImportDiscoveryReport:
    project_id_hint: str | None
    display_name_hint: str | None
    attempted_all: bool
    complete: bool
    results: tuple[SourceDiscoveryResult, ...]
    issues: tuple[DiscoveryIssue, ...]
    digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id_hint": self.project_id_hint,
            "display_name_hint": self.display_name_hint,
            "attempted_all": self.attempted_all,
            "complete": self.complete,
            "results": [item.to_dict() for item in self.results],
            "issues": [item.to_dict() for item in self.issues],
            "digest": self.digest,
        }


class GitCliRepositoryDiscovery:
    """Inspect a remote with `git ls-remote`; no clone or remote write occurs."""

    def __init__(
        self,
        *,
        git_executable: str = "git",
        timeout_seconds: float = 30.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._git_executable = _text(git_executable, "git_executable", 260)
        if timeout_seconds <= 0:
            raise ImportDiscoveryError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def inspect(self, source: RepositorySource) -> RepositoryInventory:
        try:
            completed = self._runner(
                [
                    self._git_executable,
                    "ls-remote",
                    "--symref",
                    source.url,
                    "HEAD",
                    "refs/heads/*",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise DiscoverySourceUnavailable("repository inspection failed") from None
        if completed.returncode != 0:
            raise DiscoverySourceUnavailable("repository inspection failed")
        default_branch: str | None = None
        head_revision: str | None = None
        branches: dict[str, str] = {}
        for raw_line in completed.stdout.splitlines():
            fields = raw_line.split("\t", 1)
            if len(fields) != 2:
                raise DiscoverySourceUnavailable("repository response was invalid")
            value, ref = fields
            if value.startswith("ref: ") and ref == "HEAD":
                target = value[5:]
                if not target.startswith("refs/heads/"):
                    raise DiscoverySourceUnavailable("repository response was invalid")
                default_branch = target.removeprefix("refs/heads/")
                continue
            revision = value.lower()
            if _COMMIT.fullmatch(revision) is None:
                raise DiscoverySourceUnavailable("repository response was invalid")
            if ref == "HEAD":
                head_revision = revision
            elif ref.startswith("refs/heads/"):
                branches[ref.removeprefix("refs/heads/")] = revision
        canonical_refs = json.dumps(branches, sort_keys=True, separators=(",", ":"))
        return RepositoryInventory(
            default_branch=default_branch,
            head_revision=head_revision,
            branch_count=len(branches),
            refs_digest=hashlib.sha256(canonical_refs.encode("utf-8")).hexdigest(),
        )


class DocumentTreeDiscovery:
    """Summarise one scoped document root using only DocumentStore.list."""

    def __init__(
        self,
        store_factory: Callable[[DocumentRootSource], DocumentStore],
        *,
        maximum_items: int = 100_000,
        sample_size: int = 25,
        page_size: int = 200,
    ) -> None:
        if maximum_items < 1:
            raise ImportDiscoveryError("maximum_items must be positive")
        if sample_size < 0 or sample_size > 1_000:
            raise ImportDiscoveryError("sample_size must be between 0 and 1000")
        if page_size < 1 or page_size > 200:
            raise ImportDiscoveryError("page_size must be between 1 and 200")
        self._store_factory = store_factory
        self._maximum_items = maximum_items
        self._sample_size = sample_size
        self._page_size = page_size

    def inspect(self, source: DocumentRootSource) -> DocumentInventory:
        try:
            store = self._store_factory(source)
            pending = deque([""])
            scheduled = {""}
            observed_paths: set[str] = set()
            files = folders = total_bytes = pages = 0
            latest: str | None = None
            samples: list[str] = []
            complete = True
            while pending and complete:
                folder = pending.popleft()
                cursor: str | None = None
                seen_cursors: set[str] = set()
                while True:
                    page = store.list(
                        folder, cursor=cursor, page_size=self._page_size
                    )
                    pages += 1
                    for item in page.items:
                        if files + folders >= self._maximum_items:
                            complete = False
                            break
                        if item.path in observed_paths:
                            raise DiscoverySourceUnavailable(
                                "document inventory contained a duplicate path"
                            )
                        observed_paths.add(item.path)
                        if type(item.size) is not int or item.size < 0:
                            raise DiscoverySourceUnavailable(
                                "document inventory contained an invalid size"
                            )
                        if item.is_folder:
                            folders += 1
                            if item.path in scheduled:
                                raise DiscoverySourceUnavailable(
                                    "document inventory contained a duplicate path"
                                )
                            scheduled.add(item.path)
                            pending.append(item.path)
                        else:
                            files += 1
                            total_bytes += item.size
                        if item.modified_at is not None and (
                            latest is None or item.modified_at > latest
                        ):
                            latest = item.modified_at
                        if self._sample_size:
                            position = bisect_left(samples, item.path)
                            if len(samples) < self._sample_size:
                                samples.insert(position, item.path)
                            elif position < self._sample_size:
                                samples.insert(position, item.path)
                                samples.pop()
                    if not complete or page.next_cursor is None:
                        break
                    if page.next_cursor in seen_cursors:
                        raise DiscoverySourceUnavailable(
                            "document inventory cursor repeated"
                        )
                    seen_cursors.add(page.next_cursor)
                    cursor = page.next_cursor
            return DocumentInventory(
                file_count=files,
                folder_count=folders,
                total_bytes=total_bytes,
                page_count=pages,
                latest_modified_at=latest,
                sample_paths=tuple(samples),
                complete=complete,
            )
        except DiscoverySourceUnavailable:
            raise
        except Exception:
            raise DiscoverySourceUnavailable("document inspection failed") from None


class DeclaredBindingDiscovery:
    def inspect(self, source: BindingSource) -> BindingInventory:
        return BindingInventory()


class ProjectImportDiscovery:
    def __init__(
        self,
        *,
        repositories: RepositoryDiscoveryAdapter,
        documents: Mapping[str, DocumentDiscoveryAdapter],
        bindings: Mapping[str, BindingDiscoveryAdapter] | None = None,
    ) -> None:
        self._repositories = repositories
        self._documents = dict(documents)
        self._bindings = dict(bindings or {})
        self._declared_binding = DeclaredBindingDiscovery()

    def discover(self, request: ImportDiscoveryRequest) -> ImportDiscoveryReport:
        if not isinstance(request, ImportDiscoveryRequest):
            raise ImportDiscoveryError("request must be an ImportDiscoveryRequest")
        results: list[SourceDiscoveryResult] = []
        issues = self._conflicts(request)
        declared_count = (
            len(request.repositories)
            + len(request.document_roots)
            + len(request.bindings)
        )
        if declared_count == 0:
            issues.append(
                DiscoveryIssue(
                    "no-sources",
                    None,
                    None,
                    "no project sources were declared",
                )
            )

        for source in request.repositories:
            try:
                inventory = self._repositories.inspect(source)
                summary = asdict(inventory)
                if source.default_branch_hint is not None:
                    summary["default_branch_matches_hint"] = (
                        inventory.default_branch == source.default_branch_hint
                    )
                results.append(self._success("repository", source, summary))
                if (
                    source.default_branch_hint is not None
                    and inventory.default_branch != source.default_branch_hint
                ):
                    issues.append(
                        DiscoveryIssue(
                            "default-branch-conflict",
                            "repository",
                            source.source_id,
                            "observed default branch differs from its declaration",
                        )
                    )
            except Exception:
                results.append(self._unavailable("repository", source))
                issues.append(self._unavailable_issue("repository", source.source_id))

        for source in request.document_roots:
            adapter = self._documents.get(source.adapter)
            if adapter is None:
                results.append(self._unavailable("document-root", source))
                issues.append(
                    DiscoveryIssue(
                        "adapter-unavailable",
                        "document-root",
                        source.source_id,
                        "document discovery adapter is unavailable",
                    )
                )
                continue
            try:
                inventory = adapter.inspect(source)
                results.append(
                    self._success("document-root", source, asdict(inventory))
                )
                if not inventory.complete:
                    issues.append(
                        DiscoveryIssue(
                            "safety-limit",
                            "document-root",
                            source.source_id,
                            "document inventory reached its configured safety limit",
                        )
                    )
            except Exception:
                results.append(self._unavailable("document-root", source))
                issues.append(
                    self._unavailable_issue("document-root", source.source_id)
                )

        for source in request.bindings:
            adapter = self._bindings.get(source.kind, self._declared_binding)
            try:
                inventory = adapter.inspect(source)
                results.append(self._success("binding", source, asdict(inventory)))
            except Exception:
                results.append(self._unavailable("binding", source))
                issues.append(self._unavailable_issue("binding", source.source_id))

        ordered_results = tuple(
            sorted(results, key=lambda item: (item.source_kind, item.source_id, item.locator))
        )
        ordered_issues = tuple(
            sorted(
                issues,
                key=lambda item: (
                    item.source_kind or "",
                    item.source_id or "",
                    item.code,
                    item.message,
                ),
            )
        )
        body: dict[str, object] = {
            "project_id_hint": request.project_id_hint,
            "display_name_hint": request.display_name_hint,
            "attempted_all": len(results) == declared_count,
            "complete": declared_count > 0 and not ordered_issues,
            "results": [item.to_dict() for item in ordered_results],
            "issues": [item.to_dict() for item in ordered_issues],
        }
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ImportDiscoveryReport(
            request.project_id_hint,
            request.display_name_hint,
            bool(body["attempted_all"]),
            bool(body["complete"]),
            ordered_results,
            ordered_issues,
            digest,
        )

    @staticmethod
    def _conflicts(request: ImportDiscoveryRequest) -> list[DiscoveryIssue]:
        declarations = [
            *(
                ("repository", item.source_id, item.locator)
                for item in request.repositories
            ),
            *(
                ("document-root", item.source_id, item.locator)
                for item in request.document_roots
            ),
            *(
                ("binding", item.source_id, f"{item.kind}|{item.locator}")
                for item in request.bindings
            ),
        ]
        issues: list[DiscoveryIssue] = []
        identifiers: dict[str, list[tuple[str, str]]] = {}
        locators: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for kind, source_id, locator in declarations:
            identifiers.setdefault(source_id, []).append((kind, source_id))
            locators.setdefault((kind, locator), []).append((kind, source_id))
        for declarations_with_id in identifiers.values():
            if len(declarations_with_id) > 1:
                issues.extend(
                    DiscoveryIssue(
                        "source-id-conflict",
                        kind,
                        source_id,
                        "source declaration conflicts with another declaration",
                    )
                    for kind, source_id in declarations_with_id
                )
        for declarations_with_locator in locators.values():
            if len(declarations_with_locator) > 1:
                issues.extend(
                    DiscoveryIssue(
                        "locator-conflict",
                        kind,
                        source_id,
                        "source declaration conflicts with another declaration",
                    )
                    for kind, source_id in declarations_with_locator
                )
        return issues

    @staticmethod
    def _success(
        kind: str,
        source: RepositorySource | DocumentRootSource | BindingSource,
        summary: Mapping[str, object],
    ) -> SourceDiscoveryResult:
        return SourceDiscoveryResult(
            kind,
            source.source_id,
            source.locator,
            "discovered",
            source.credential_reference is not None,
            _source_configuration_digest(source),
            dict(summary),
        )

    @staticmethod
    def _unavailable(
        kind: str, source: RepositorySource | DocumentRootSource | BindingSource
    ) -> SourceDiscoveryResult:
        return SourceDiscoveryResult(
            kind,
            source.source_id,
            source.locator,
            "unavailable",
            source.credential_reference is not None,
            _source_configuration_digest(source),
            {},
        )

    @staticmethod
    def _unavailable_issue(kind: str, source_id: str) -> DiscoveryIssue:
        return DiscoveryIssue(
            "source-unavailable",
            kind,
            source_id,
            "source could not be inspected",
        )


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ImportDiscoveryError(f"{field} is invalid")
    return value


def _text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(item) < 32 for item in value)
    ):
        raise ImportDiscoveryError(f"{field} is invalid")
    return value.strip()


def _external_id(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(item) < 33 for item in value)
        or any(item in value for item in ("/", "\\", "?", "#", "@", "|"))
    ):
        raise ImportDiscoveryError(f"{field} is invalid")
    return value


def _credential_reference(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CREDENTIAL_REF.fullmatch(value) is None:
        raise ImportDiscoveryError("credential_reference is invalid")
    return value


def _git_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2_000:
        raise ImportDiscoveryError("repository url is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(".git")
    ):
        raise ImportDiscoveryError(
            "repository url must be a credential-free HTTPS URL"
        )
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        raise ImportDiscoveryError("repository url is invalid") from None
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit(("https", netloc, parsed.path, "", ""))


def _document_root(value: object) -> str:
    root = _text(value, "root", 1_024).replace("\\", "/")
    if not root.startswith("/") or "//" in root or any(
        part in {".", ".."} for part in root.split("/")
    ):
        raise ImportDiscoveryError("root is invalid")
    return root.rstrip("/") or "/"


def _locator(value: object, field: str) -> str:
    text = _text(value, field, 2_000)
    if any(term in text.casefold() for term in ("password=", "token=", "secret=")):
        raise ImportDiscoveryError(f"{field} must not contain credentials")
    return text


def _relative_document_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2_048
        or value.startswith("/")
        or "\\" in value
        or any(ord(item) < 32 for item in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ImportDiscoveryError("sample path is invalid")
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ImportDiscoveryError(f"{field} must be a non-negative integer")
    return value


def _source_configuration_digest(
    source: RepositorySource | DocumentRootSource | BindingSource,
) -> str:
    if isinstance(source, RepositorySource):
        value = {
            "kind": "repository",
            "source_id": source.source_id,
            "url": source.url,
            "default_branch_hint": source.default_branch_hint,
            "credential_reference": source.credential_reference,
        }
    elif isinstance(source, DocumentRootSource):
        value = {
            "kind": "document-root",
            "source_id": source.source_id,
            "adapter": source.adapter,
            "drive_id": source.drive_id,
            "root": source.root,
            "credential_reference": source.credential_reference,
        }
    else:
        value = {
            "kind": "binding",
            "source_id": source.source_id,
            "binding_kind": source.kind,
            "locator": source.locator,
            "credential_reference": source.credential_reference,
        }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
