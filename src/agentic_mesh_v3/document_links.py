from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlparse


class LinkResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    EXTERNAL_PRESERVED = "external_preserved"
    ALREADY_RENDERED_PRESERVED = "already_rendered_preserved"
    RAW_ARTIFACT_PROMOTED = "raw_artifact_promoted"
    UNSAFE_SUPPRESSED = "unsafe_suppressed"
    UNRESOLVED_CONTEXT = "unresolved_context"


@dataclass(frozen=True)
class DocumentLinkDiagnostic:
    original_target: str
    status: LinkResolutionStatus
    resolved_target: str | None = None
    document_path: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class DocumentLinkResult:
    href: str | None
    diagnostic: DocumentLinkDiagnostic

    @property
    def available(self) -> bool:
        return self.href is not None


@dataclass(frozen=True)
class RenderedDocumentLinks:
    content: str
    diagnostics: tuple[DocumentLinkDiagnostic, ...]


_INLINE_MARKDOWN_LINK_RE = re.compile(
    r"(?P<prefix>!?\[[^\]\n]+\]\()(?P<target><[^>\n]*>|[^)\s\n]+)(?P<suffix>(?:\s+\"[^\"]*\")?\))"
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_APPROVED_EXTERNAL_SCHEMES = {"http", "https", "mailto"}
_ALREADY_RENDERED_PREFIXES = (
    "/artifact-viewer/",
    "/work-items/",
    "/work-queue",
    "/queue",
    "/agents/",
    "/auth/",
)
_DOCUMENT_ROOT_SEGMENTS = {
    "00-index",
    "architecture",
    "business",
    "decisions",
    "delivery",
    "documents",
    "docs",
    "engineering",
    "operations",
    "platform",
    "product",
    "qa",
    "releases",
    "risks",
    "roles",
    "security",
    "technical-writing",
    "ux",
    "work-items",
}
_SENSITIVE_PARTS = {
    ".azure",
    ".aws",
    ".config",
    ".docker",
    ".kube",
    ".ssh",
    "credential",
    "credentials",
    "debug",
    "docker.sock",
    "key",
    "keys",
    "mount",
    "mounts",
    "oauth",
    "private",
    "secret",
    "secrets",
    "state",
    "token",
    "tokens",
    "worker_mounts",
}
_SENSITIVE_QUERY_MARKERS = (
    "access_token",
    "auth=",
    "bearer",
    "client_secret",
    "code=",
    "credential",
    "id_token",
    "refresh_token",
    "sas=",
    "secret",
    "sig=",
    "token",
)
_CONTAINER_ABSOLUTE_PREFIXES = (
    "/dev/",
    "/etc/",
    "/home/",
    "/mesh/",
    "/mnt/",
    "/proc/",
    "/root/",
    "/run/",
    "/sys/",
    "/tmp/",
    "/var/",
)
_MARKDOWN_EXTENSIONS = (".md", ".markdown", ".txt", ".json", ".yaml", ".yml")
_UNAVAILABLE_MARKDOWN_TARGET = "#document-link-unavailable"


def artifact_viewer_route(document_path: str) -> str:
    return f"/artifact-viewer/{quote(document_path, safe='')}"


def resolve_document_href(target: str, *, source_path: str | None = None) -> DocumentLinkResult:
    original = _strip_markdown_target(target)
    suppressed = _suppressed(original)
    if suppressed:
        return _diagnostic_result(original, LinkResolutionStatus.UNSAFE_SUPPRESSED, reason=suppressed)

    parsed = urlparse(original)
    if parsed.scheme:
        if parsed.scheme.lower() in _APPROVED_EXTERNAL_SCHEMES:
            if _has_sensitive_url_material(original):
                return _diagnostic_result(
                    original,
                    LinkResolutionStatus.UNSAFE_SUPPRESSED,
                    reason="sensitive_url_material",
                )
            return _diagnostic_result(
                original,
                LinkResolutionStatus.EXTERNAL_PRESERVED,
                resolved_target=original,
                href=original,
            )
        return _diagnostic_result(
            original,
            LinkResolutionStatus.UNSAFE_SUPPRESSED,
            reason="unsupported_url_scheme",
        )

    if _is_raw_artifact_route(original):
        document_path = _document_path_from_artifact_route(original)
        if document_path is None:
            return _diagnostic_result(
                original,
                LinkResolutionStatus.UNSAFE_SUPPRESSED,
                reason="unsafe_artifact_source_path",
            )
        href = artifact_viewer_route(document_path)
        return _diagnostic_result(
            original,
            LinkResolutionStatus.RAW_ARTIFACT_PROMOTED,
            resolved_target=href,
            document_path=document_path,
            href=href,
        )

    if _is_already_rendered_route(original):
        return _diagnostic_result(
            original,
            LinkResolutionStatus.ALREADY_RENDERED_PRESERVED,
            resolved_target=original,
            href=original,
        )

    document_path = _resolve_document_path(original, source_path=source_path)
    if document_path is None:
        return _diagnostic_result(
            original,
            LinkResolutionStatus.UNRESOLVED_CONTEXT,
            reason="missing_or_unsafe_source_context",
        )
    href = artifact_viewer_route(document_path)
    return _diagnostic_result(
        original,
        LinkResolutionStatus.RESOLVED,
        resolved_target=href,
        document_path=document_path,
        href=href,
    )


def render_document_markdown_links(
    content: str,
    *,
    source_path: str | None = None,
    unavailable_target: str = _UNAVAILABLE_MARKDOWN_TARGET,
) -> RenderedDocumentLinks:
    diagnostics: list[DocumentLinkDiagnostic] = []

    def replace(match: re.Match[str]) -> str:
        result = resolve_document_href(match.group("target"), source_path=source_path)
        diagnostics.append(result.diagnostic)
        href = result.href or unavailable_target
        return f"{match.group('prefix')}{href}{match.group('suffix')}"

    return RenderedDocumentLinks(
        content=_INLINE_MARKDOWN_LINK_RE.sub(replace, str(content)),
        diagnostics=tuple(diagnostics),
    )


def _strip_markdown_target(target: str) -> str:
    value = str(target).strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return value


def _diagnostic_result(
    original: str,
    status: LinkResolutionStatus,
    *,
    resolved_target: str | None = None,
    document_path: str | None = None,
    href: str | None = None,
    reason: str | None = None,
) -> DocumentLinkResult:
    return DocumentLinkResult(
        href=href,
        diagnostic=DocumentLinkDiagnostic(
            original_target=original,
            status=status,
            resolved_target=resolved_target,
            document_path=document_path,
            reason=reason,
        ),
    )


def _suppressed(target: str) -> str | None:
    if not target:
        return "empty_target"
    lower = target.lower()
    if "\x00" in target or "\n" in target or "\r" in target:
        return "control_character"
    if _WINDOWS_DRIVE_RE.match(target):
        return "windows_drive_path"
    if target.startswith("\\\\") or target.startswith("//"):
        return "unc_or_protocol_relative_path"
    if "\\" in target:
        return "backslash_path"
    if any(lower.startswith(prefix) for prefix in _CONTAINER_ABSOLUTE_PREFIXES):
        return "absolute_filesystem_path"
    if _has_sensitive_url_material(target):
        return "sensitive_url_material"
    return None


def _has_sensitive_url_material(target: str) -> bool:
    lower = target.lower()
    parsed = urlparse(target)
    query = parsed.query.lower()
    if query and any(marker in query for marker in _SENSITIVE_QUERY_MARKERS):
        return True
    return any(f"/{part}/" in f"/{lower.strip('/')}/" for part in _SENSITIVE_PARTS)


def _is_raw_artifact_route(target: str) -> bool:
    return urlparse(target).path.startswith("/artifacts/")


def _document_path_from_artifact_route(target: str) -> str | None:
    parsed = urlparse(target)
    raw_path = parsed.path.removeprefix("/artifacts/")
    return _safe_document_path(unquote(raw_path))


def _is_already_rendered_route(target: str) -> bool:
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    path = parsed.path
    if path.startswith("/work-items/"):
        tail = path.removeprefix("/work-items/").strip("/")
        if "/" in tail or tail.endswith(_MARKDOWN_EXTENSIONS):
            return False
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _ALREADY_RENDERED_PREFIXES)


def _resolve_document_path(target: str, *, source_path: str | None) -> str | None:
    parsed = urlparse(target)
    if parsed.netloc or parsed.params or parsed.query:
        return None
    raw_path = unquote(parsed.path)
    if raw_path.startswith("/"):
        return _safe_document_path(raw_path.lstrip("/"))
    root_relative = _safe_document_path(raw_path)
    if root_relative is not None:
        return root_relative
    if not source_path:
        return None
    safe_source = _safe_document_path(source_path)
    if safe_source is None:
        return None
    base = posixpath.dirname(safe_source)
    return _safe_document_path(posixpath.join(base, raw_path))


def _safe_document_path(path: str | None) -> str | None:
    if path is None:
        return None
    raw = str(path).strip().replace("\\", "/")
    if not raw:
        return None
    if _suppressed(raw):
        return None
    normalized = posixpath.normpath(raw.lstrip("/"))
    if normalized in {"", "."} or normalized.startswith("../") or normalized == "..":
        return None
    parts = [part for part in normalized.split("/") if part]
    if not parts or parts[0] not in _DOCUMENT_ROOT_SEGMENTS:
        return None
    if parts[0] == "documents" and parts[:2] != ["documents", "analysis"]:
        return None
    if {part.lower() for part in parts} & _SENSITIVE_PARTS:
        return None
    return normalized
