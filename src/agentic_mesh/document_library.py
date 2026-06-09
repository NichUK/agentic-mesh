from __future__ import annotations

import hashlib
import json
import html
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from agentic_mesh.models import (
    DocumentLibraryConfig,
    ProjectConfig,
    SdlcFlow,
    utc_now_iso,
)


REVIEW_STATUS_UNKNOWN = "unknown"
REVIEW_STATUS_CONFIGURED_ONLY = "configured_only"
REVIEW_STATUS_NAVIGATION_ONLY = "navigation_only"
REVIEW_AUTHORITY_NONE = "not_gate_authoritative"
REVIEW_AUTHORITY_GATE = "gate_authoritative"

VALIDATION_REASON_CODES = {
    "missing_metadata",
    "broken_link",
    "stale_generated_summary",
    "review_status_ambiguous",
    "unresolved_rollup",
    "migration_conflict",
    "redaction_violation",
    "path_escape",
    "duplicate_index_body",
    "unknown",
}

SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"secret[_-]?ref\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"credential[_-]?ref\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"oauth[_-]?token\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"file://", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[/\\]"),
    re.compile(r"(^|[\s`])/(mesh|home|Users|tmp|var)/"),
    re.compile(r"tenant[_-]?id", re.IGNORECASE),
    re.compile(r"channel[_-]?id", re.IGNORECASE),
    re.compile(r"user[_-]?id", re.IGNORECASE),
    re.compile(r"provider diagnostic", re.IGNORECASE),
    re.compile(r"sig=", re.IGNORECASE),
]

SOURCE_PROVENANCE_ALLOWLIST = {
    "work_item_id",
    "work_item_type",
    "queue_item_id",
    "source_anchor_ref",
    "connector_type",
    "connector_instance_id",
    "source_scope",
    "display_label",
    "received_at",
    "correlation_id",
}


@dataclass(frozen=True)
class ReviewStatusSource:
    status: str = REVIEW_STATUS_UNKNOWN
    source_type: str = REVIEW_STATUS_UNKNOWN
    authority: str = REVIEW_AUTHORITY_NONE
    evidence_ref: str | None = None
    review_id: str | None = None


@dataclass(frozen=True)
class ReviewEvidence:
    review_id: str
    status: str
    source_path: str
    reviewer_role: str | None = None
    authority: str = REVIEW_AUTHORITY_NONE


@dataclass(frozen=True)
class Backlink:
    source_path: str
    relation: str
    evidence_ref: str | None = None


@dataclass(frozen=True)
class RollupObligation:
    target_path: str
    status: str = REVIEW_STATUS_UNKNOWN
    evidence_ref: str | None = None


@dataclass(frozen=True)
class MigrationCompatibility:
    current_path: str
    target_path: str | None = None
    compatibility_status: str = "compatible"
    alias_required: bool = False
    link_check_status: str = REVIEW_STATUS_UNKNOWN
    conflict_status: str = "none"
    rollback_plan_ref: str | None = None
    migration_slice_id: str | None = None


@dataclass(frozen=True)
class WorkItemArtifactMetadata:
    work_item_id: str
    work_item_type: str = REVIEW_STATUS_UNKNOWN
    lifecycle_state: str = REVIEW_STATUS_UNKNOWN
    owner_role: str = REVIEW_STATUS_UNKNOWN
    queue_item_id: str | None = None
    source_anchor_ref: str | None = None


@dataclass(frozen=True)
class DocumentRecord:
    path: str
    document_kind: str
    owner_role: str
    accountability: str
    contributing_roles: list[str] = field(default_factory=list)
    required_sections: list[str] = field(default_factory=list)
    lifecycle_states: list[str] = field(default_factory=list)
    review_on_contribution: bool = False
    review_status: str = REVIEW_STATUS_CONFIGURED_ONLY
    review_status_source: ReviewStatusSource = field(
        default_factory=lambda: ReviewStatusSource(
            status=REVIEW_STATUS_CONFIGURED_ONLY,
            source_type=REVIEW_STATUS_CONFIGURED_ONLY,
        )
    )
    backend_url: str | None = None
    migration: MigrationCompatibility | None = None
    backlinks: list[Backlink] = field(default_factory=list)
    status: str = "configured"


@dataclass(frozen=True)
class ValidationFinding:
    severity: str
    reason_code: str
    required_action: str
    document_path: str | None = None
    work_item_id: str | None = None
    owner_role: str | None = None
    evidence_ref: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.reason_code not in VALIDATION_REASON_CODES:
            object.__setattr__(self, "reason_code", "unknown")


def resolve_document_library_root(
    workspace_root: Path,
    library: DocumentLibraryConfig,
) -> Path:
    root = Path(library.root)
    if root.is_absolute():
        return root.resolve()
    return (workspace_root / root).resolve()


def resolve_role_memory_root(workspace_root: Path, project: ProjectConfig) -> Path:
    root = Path(project.role_memory.root)
    if root.is_absolute():
        return root.resolve()
    return (workspace_root / root).resolve()


def document_library_context(
    workspace_root: Path,
    project: ProjectConfig,
) -> dict[str, object]:
    library_root = resolve_document_library_root(workspace_root, project.document_library)
    memory_root = resolve_role_memory_root(workspace_root, project)
    return {
        "backend": project.document_library.backend,
        "root": project.document_library.root,
        "absolute_root": str(library_root),
        "structure_policy": project.document_library.structure_policy,
        "index_path": project.document_library.index_path,
        "review_log_standard": project.document_library.review_log_standard,
        "versioning": project.document_library.versioning,
        "role_memory": {
            "enabled": project.role_memory.enabled,
            "backend": project.role_memory.backend,
            "root": project.role_memory.root,
            "absolute_root": str(memory_root),
            "provenance_required": project.role_memory.provenance_required,
            "refresh_from_document_library": (
                project.role_memory.refresh_from_document_library
            ),
            "team_overlay_root": project.role_memory.team_overlay_root,
        },
    }


def build_document_manifest(
    project: ProjectConfig,
    *,
    generated_at: str | None = None,
) -> dict[str, object]:
    documents = []
    indexed_paths: set[str] = set()
    for path, accountability in sorted(project.document_accountabilities.items()):
        states = [
            state_id
            for state_id, state in sorted(project.flow.states.items())
            if state.artifact_path == path
            or any(path in gate.required_documents for gate in state.gates)
        ]
        documents.append(
            _document_record_dict(
                DocumentRecord(
                    path=path,
                    document_kind="durable",
                    owner_role=accountability.owner_role,
                    accountability=accountability.accountability,
                    contributing_roles=accountability.contributing_roles,
                    required_sections=accountability.required_sections,
                    lifecycle_states=states,
                    review_on_contribution=accountability.review_on_contribution,
                )
            )
        )
        indexed_paths.add(path)

    for state_id, state in sorted(project.flow.states.items()):
        paths = {state.artifact_path}
        for gate in state.gates:
            paths.update(gate.required_documents)
        for path in sorted(paths):
            if path in indexed_paths:
                continue
            documents.append(
                _document_record_dict(
                    DocumentRecord(
                        path=path,
                        document_kind="work_item_template",
                        owner_role=state.owner_role,
                        accountability="accountable_owner",
                        contributing_roles=[],
                        required_sections=[
                            "objective",
                            "scope",
                            "assumptions",
                            "decisions",
                            "evidence",
                            "risks",
                            "review_log",
                            "next_step",
                        ],
                        lifecycle_states=[state_id],
                        review_on_contribution=True,
                        migration=_migration_for_template(path),
                    )
                )
            )
            indexed_paths.add(path)

    return {
        "schema_version": "document-library-manifest-v1",
        "generated_at": generated_at or utc_now_iso(),
        "project_id": project.project_id,
        "document_library": asdict(project.document_library),
        "role_memory": asdict(project.role_memory),
        "documents": documents,
        "meshes": {
            mesh_id: asdict(mesh)
            for mesh_id, mesh in sorted(project.meshes.items())
        },
        "flow": {
            "flow_id": project.flow.flow_id,
            "entry_state": project.flow.entry_state,
            "work_item_types": project.flow.work_item_types,
            "states": list(project.flow.states),
        },
    }


def write_document_manifest(
    workspace_root: Path,
    project: ProjectConfig,
) -> Path:
    library_root = resolve_document_library_root(workspace_root, project.document_library)
    adapter = GitFilesystemDocumentLibraryAdapter(library_root)
    return adapter.write_text(
        project.document_library.index_path,
        json.dumps(build_document_manifest(project), indent=2, sort_keys=True) + "\n",
    )


class GitFilesystemDocumentLibraryAdapter:
    """Local Git/filesystem document-library adapter with confined paths."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def read_text(self, relative_path: str) -> str:
        return self._resolve(relative_path).read_text(encoding="utf-8")

    def write_text(self, relative_path: str, content: str) -> Path:
        path = self._resolve(relative_path)
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
            return path
        except Exception:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def list(self, relative_path: str = ".") -> list[str]:
        path = self._resolve(relative_path)
        if not path.exists():
            return []
        if not path.is_dir():
            raise ValueError("Document library list path must be a directory.")
        return [
            child.relative_to(self.root).as_posix()
            for child in sorted(path.iterdir(), key=lambda item: item.name)
        ]

    def stat(self, relative_path: str) -> dict[str, Any]:
        path = self._resolve(relative_path)
        if not path.exists():
            return {"path": relative_path, "exists": False}
        stat = path.stat()
        return {
            "path": relative_path,
            "exists": True,
            "is_dir": path.is_dir(),
            "size": stat.st_size,
            "version": self.current_version(relative_path),
        }

    def current_version(self, relative_path: str) -> str | None:
        path = self._resolve(relative_path)
        if not path.exists() or not path.is_file():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return f"sha256:{digest}"

    def resolve_backend_url(self, relative_path: str) -> str | None:
        self._resolve(relative_path)
        return None

    def _resolve(self, relative_path: str) -> Path:
        text = _normalize_relative_path(relative_path)
        path = (self.root / text).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"Document-library path escapes root: {relative_path}")
        return path


def validate_document_library(
    root: Path,
    *,
    work_item_id: str | None = None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    root = root.resolve()
    work_items_root = root / "work-items"
    if work_items_root.exists():
        for work_dir in sorted(child for child in work_items_root.iterdir() if child.is_dir()):
            if work_item_id is not None and work_dir.name != work_item_id:
                continue
            index_path = work_dir / "00-index.md"
            lifecycle_docs = [
                child for child in work_dir.iterdir()
                if child.is_file() and re.match(r"^\d{2,3}-.*\.md$", child.name)
            ]
            if lifecycle_docs and not index_path.exists():
                findings.append(
                    ValidationFinding(
                        severity="error",
                        reason_code="missing_metadata",
                        document_path=_safe_relative(root, index_path),
                        work_item_id=work_dir.name,
                        required_action="Create or backfill the work-item local index.",
                    )
                )
            if index_path.exists() and _duplicate_index_heading(index_path):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        reason_code="duplicate_index_body",
                        document_path=_safe_relative(root, index_path),
                        work_item_id=work_dir.name,
                        required_action="Regenerate the maintained work-item index.",
                    )
                )
            for doc in lifecycle_docs + ([index_path] if index_path.exists() else []):
                _validate_document_text(root, doc, findings, work_dir.name)
    return findings


def render_validation_findings(
    findings: list[ValidationFinding],
    *,
    output_format: str = "text",
) -> str:
    safe_findings = [_finding_dict(finding) for finding in findings]
    if output_format == "json":
        return json.dumps({"findings": safe_findings}, indent=2, sort_keys=True) + "\n"
    if not safe_findings:
        return "No document-library validation findings.\n"
    lines = ["Document-library validation findings:", ""]
    for finding in safe_findings:
        lines.append(
            "- "
            + " | ".join(
                str(value)
                for value in [
                    finding["severity"],
                    finding["reason_code"],
                    finding.get("document_path") or "unknown",
                    finding["required_action"],
                ]
            )
        )
    return "\n".join(lines) + "\n"


def migration_dry_run(
    root: Path,
    *,
    work_item_id: str,
    migration_slice_id: str | None = None,
) -> dict[str, Any]:
    adapter = GitFilesystemDocumentLibraryAdapter(root)
    work_item_path = f"work-items/{work_item_id}"
    work_item_dir = adapter._resolve(work_item_path)
    records: list[MigrationCompatibility] = []
    findings: list[ValidationFinding] = []
    if not work_item_dir.exists():
        findings.append(
            ValidationFinding(
                severity="error",
                reason_code="missing_metadata",
                document_path=work_item_path,
                work_item_id=work_item_id,
                required_action="Create the work-item document folder before migration.",
            )
        )
    else:
        for doc in sorted(work_item_dir.glob("[0-9][0-9]-*.md")):
            if doc.name == "00-index.md":
                continue
            target = f"{doc.stem[:2]}0-{doc.name[3:]}"
            target_path = doc.with_name(target)
            conflict = target_path.exists() and target_path != doc
            rel_current = _safe_relative(root, doc)
            rel_target = _safe_relative(root, target_path)
            records.append(
                MigrationCompatibility(
                    current_path=rel_current,
                    target_path=rel_target,
                    compatibility_status="blocked" if conflict else "dry_run_only",
                    alias_required=True,
                    link_check_status="not_checked",
                    conflict_status="conflict" if conflict else "none",
                    rollback_plan_ref="future-migration-slice-required",
                    migration_slice_id=migration_slice_id,
                )
            )
            if conflict:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        reason_code="migration_conflict",
                        document_path=rel_current,
                        work_item_id=work_item_id,
                        required_action=f"Resolve target conflict before migration: {rel_target}",
                    )
                )
    return {
        "schema_version": "document-library-migration-dry-run-v1",
        "work_item_id": work_item_id,
        "dry_run": True,
        "renamed": 0,
        "records": [_migration_dict(record) for record in records],
        "findings": [_finding_dict(finding) for finding in findings],
    }


def render_migration_dry_run(report: dict[str, Any], *, output_format: str = "text") -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, sort_keys=True) + "\n"
    lines = [
        "Document-library migration dry run",
        "",
        f"Work item: {report['work_item_id']}",
        "Filesystem changes: none",
        "",
    ]
    if not report["records"]:
        lines.append("No 2-digit lifecycle artifacts found.")
    for record in report["records"]:
        lines.append(
            "- "
            + " -> ".join(
                [
                    record["current_path"],
                    record.get("target_path") or "unknown",
                ]
            )
            + f" | compatibility={record['compatibility_status']}"
            + f" | conflict={record['conflict_status']}"
        )
    if report["findings"]:
        lines.extend(["", "Findings:"])
        for finding in report["findings"]:
            lines.append(
                f"- {finding['severity']} {finding['reason_code']}: "
                f"{finding['required_action']}"
            )
    return "\n".join(lines).rstrip() + "\n"


def redact_source_provenance(source: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _safe_scalar(value)
        for key, value in sorted(source.items())
        if key in SOURCE_PROVENANCE_ALLOWLIST and _safe_scalar(value) is not None
    }


def classify_backend_url(url: str | None) -> str:
    if not url:
        return "unknown"
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        return "support_only"
    if parsed.hostname and parsed.hostname.endswith(".example.test"):
        return "safe"
    return "support_only"


def _document_record_dict(record: DocumentRecord) -> dict[str, Any]:
    data = asdict(record)
    if classify_backend_url(record.backend_url) != "safe":
        data["backend_url"] = None
    return _redact_nested(data)


def _migration_for_template(path: str) -> MigrationCompatibility | None:
    match = re.search(r"/\{work_item_id\}/(\d{2})-", path)
    if not match:
        return None
    current_prefix = match.group(1)
    target_path = path.replace(
        f"/{current_prefix}-",
        f"/{current_prefix}0-",
        1,
    )
    return MigrationCompatibility(
        current_path=path,
        target_path=target_path,
        compatibility_status="template_compatible",
        alias_required=True,
        link_check_status="not_applicable",
        conflict_status="not_applicable",
        rollback_plan_ref="future-migration-slice-required",
    )


def _normalize_relative_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Document-library path must not be empty.")
    text = relative_path.replace("\\", "/")
    if _is_windows_absolute(text) or text.startswith(("/", "\\")):
        raise ValueError("Document-library path must be relative.")
    requested = Path(text)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("Document-library path must not traverse.")
    if text in {".", "./"}:
        return "."
    if text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


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


def _safe_relative(root: Path, path: Path) -> str:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        return "redacted"
    return resolved_path.relative_to(resolved_root).as_posix()


def _duplicate_index_heading(path: Path) -> bool:
    return path.read_text(encoding="utf-8").count("# Work Item Index:") > 1


def _validate_document_text(
    root: Path,
    path: Path,
    findings: list[ValidationFinding],
    work_item_id: str,
) -> None:
    text = path.read_text(encoding="utf-8")
    rel = _safe_relative(root, path)
    if re.search(
        r"Review status:\s*`?approved`?",
        text,
        re.IGNORECASE,
    ) and not _has_approved_review_log(text):
        findings.append(
            ValidationFinding(
                severity="warning",
                reason_code="review_status_ambiguous",
                document_path=rel,
                work_item_id=work_item_id,
                required_action=(
                    "Use same-document review-log evidence rather than file "
                    "metadata alone for gate authority."
                ),
            )
        )
    if _is_generated_surface(path):
        for line in text.splitlines():
            if any(pattern.search(line) for pattern in SENSITIVE_VALUE_PATTERNS):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        reason_code="redaction_violation",
                        document_path=rel,
                        work_item_id=work_item_id,
                        required_action="Remove or redact sensitive operational metadata.",
                    )
                )
                break


def _finding_dict(finding: ValidationFinding) -> dict[str, Any]:
    data = asdict(finding)
    if finding.detail:
        data["detail"] = "redacted" if _contains_sensitive(finding.detail) else finding.detail
    return {
        key: value
        for key, value in _redact_nested(data).items()
        if value is not None
    }


def _migration_dict(record: MigrationCompatibility) -> dict[str, Any]:
    return {
        key: value
        for key, value in _redact_nested(asdict(record)).items()
        if value is not None
    }


def _redact_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_nested(item)
            for key, item in value.items()
            if _redact_nested(item) is not None
        }
    if isinstance(value, list):
        return [_redact_nested(item) for item in value if _redact_nested(item) is not None]
    return _safe_scalar(value)


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if _contains_sensitive(text):
        return None
    return text


def _contains_sensitive(text: str) -> bool:
    return any(pattern.search(text) for pattern in SENSITIVE_VALUE_PATTERNS)


def _is_generated_surface(path: Path) -> bool:
    return path.name in {
        "00-index.md",
        "document-library-validation.json",
        "document-library-migration-dry-run.json",
        "document-library-manifest.json",
    } or path.name.startswith("document-library-")


def _has_approved_review_log(text: str) -> bool:
    if "## Review Log" not in text:
        return False
    review_log = text.split("## Review Log", 1)[1]
    return bool(
        re.search(r"^###\s+\S+.*\|\s*approved\s*$", review_log, re.IGNORECASE | re.MULTILINE)
        or re.search(r"Disposition:\s*approved", review_log, re.IGNORECASE)
    )


def render_flow_mermaid(flow: SdlcFlow) -> str:
    lines = [
        "flowchart LR",
        f'  start(["{_label(flow.entry_state)}"])',
    ]
    for state_id, state in flow.states.items():
        lines.append(f'  {node_id(state_id)}["{_label(state_id)}<br/>{state.owner_role}"]')
        for gate in state.gates:
            gate_shape = "{" if gate.type in {"plan_review", "document_review"} else "["
            gate_end = "}" if gate_shape == "{" else "]"
            lines.append(
                f'  {node_id(state_id)}_{node_id(gate.gate_id)}'
                f'{gate_shape}"{_label(gate.gate_id)}<br/>{_label(gate.type)}"'
                f"{gate_end}"
            )
            lines.append(
                f"  {node_id(state_id)} --> "
                f"{node_id(state_id)}_{node_id(gate.gate_id)}"
            )
        for handoff in state.handoffs.values():
            edge = "-->"
            label = handoff.status
            if handoff.target_mesh:
                label = f"{label} / {handoff.target_mesh}"
            lines.append(
                f"  {node_id(state_id)} {edge}|{label}| {node_id(handoff.target_state)}"
            )
        for consult in state.consults.values():
            lines.append(
                f"  {node_id(state_id)} -. {consult.consult_id} .-> "
                f"{node_id(consult.target_state)}"
            )
    lines.append(f"  start --> {node_id(flow.entry_state)}")
    return "\n".join(lines) + "\n"


def render_flow_markdown_export(
    flow: SdlcFlow,
    *,
    project_id: str,
    project_file: str,
    flow_source: str,
    work_item_id: str,
    queue_item_id: str | None,
    generated_at: str,
    command: str,
) -> str:
    lines = [
        "# Lifecycle Flow",
        "",
        "## Metadata",
        "",
        f"- Project: `{_inline_code(project_id)}`",
        f"- Flow: `{_inline_code(flow.flow_id)}`",
        f"- Work item: `{_inline_code(work_item_id)}`",
    ]
    if queue_item_id:
        lines.append(f"- Queue item: `{_inline_code(queue_item_id)}`")
    lines.extend(
        [
            f"- Source project config: `{_inline_code(project_file)}`",
            f"- Source flow config: `{_inline_code(flow_source)}`",
            f"- Generated at: `{_inline_code(generated_at)}`",
            f"- Command: `{_inline_code(command)}`",
            "",
            (
                "Consult routes are omitted from the V0 default export for "
                "readability; configured gates and forward handoffs remain visible."
            ),
            "",
            "## Lifecycle Summary",
            "",
            "| State | Owner role | Gates | Forward handoff | Terminal |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for state_id, state in flow.states.items():
        terminal = not state.handoffs
        state_label = _label(state_id)
        if terminal:
            state_label = f"{state_label} (terminal)"
        gates = ", ".join(
            f"{_label(gate.gate_id)} ({_label(gate.type)})" for gate in state.gates
        ) or "None"
        handoffs = ", ".join(
            f"{_label(handoff.status)} -> {_label(handoff.target_state)}"
            for handoff in state.handoffs.values()
        ) or "None"
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_table_cell(state_label),
                    _markdown_table_cell(state.owner_role),
                    _markdown_table_cell(gates),
                    _markdown_table_cell(handoffs),
                    "Yes" if terminal else "No",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Diagram",
            "",
            "```mermaid",
            render_flow_lifecycle_mermaid(flow).rstrip(),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def render_flow_lifecycle_mermaid(flow: SdlcFlow) -> str:
    state_nodes = {
        state_id: f"state_{index}"
        for index, state_id in enumerate(flow.states)
    }
    lines = [
        "flowchart TD",
        f"  start({_mermaid_label(_label(flow.entry_state))})",
    ]
    for state_index, (state_id, state) in enumerate(flow.states.items()):
        terminal = not state.handoffs
        state_label = _label(state_id)
        if terminal:
            state_label = f"{state_label} (terminal)"
        lines.append(
            f"  {state_nodes[state_id]}["
            f"{_mermaid_multiline_label([state_label, state.owner_role])}]"
        )
        for gate_index, gate in enumerate(state.gates):
            gate_node = f"gate_{state_index}_{gate_index}"
            lines.append(
                f"  {gate_node}["
                f"{_mermaid_multiline_label([_label(gate.gate_id), _label(gate.type)])}]"
            )
            lines.append(f"  {state_nodes[state_id]} --> {gate_node}")
        for handoff in state.handoffs.values():
            lines.append(
                f"  {state_nodes[state_id]} -->|"
                f"{_mermaid_edge_label(handoff.status)}| "
                f"{state_nodes[handoff.target_state]}"
            )
    lines.append(f"  start --> {state_nodes[flow.entry_state]}")
    return "\n".join(lines) + "\n"


def node_id(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)


def _label(value: str) -> str:
    return value.replace("_", " ").replace("-", " ")


def _inline_code(value: str) -> str:
    return (
        html.escape(_neutralize_event_attributes(str(value)), quote=True)
        .replace("`", "&#96;")
        .replace("\n", " ")
    )


def _markdown_table_cell(value: str) -> str:
    text = html.escape(_neutralize_event_attributes(str(value)), quote=True)
    for old, new in {
        "\r": " ",
        "\n": " ",
        "|": r"\|",
        "`": "&#96;",
        "[": r"\[",
        "]": r"\]",
        "(": r"\(",
        ")": r"\)",
    }.items():
        text = text.replace(old, new)
    return " ".join(text.split())


def _mermaid_label(value: str) -> str:
    text = html.escape(_neutralize_event_attributes(str(value)), quote=True)
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("`", "&#96;")
    return json.dumps(text)


def _mermaid_multiline_label(values: list[str]) -> str:
    escaped = []
    for value in values:
        text = html.escape(_neutralize_event_attributes(str(value)), quote=True)
        text = text.replace("\r", " ").replace("\n", " ")
        text = text.replace("`", "&#96;")
        escaped.append(text)
    return json.dumps("<br/>".join(escaped))


def _mermaid_edge_label(value: str) -> str:
    text = html.escape(_neutralize_event_attributes(str(value)), quote=True)
    for old, new in {
        "\r": " ",
        "\n": " ",
        "|": "/",
        "`": "&#96;",
        ";": ",",
        "[": "(",
        "]": ")",
        "{": "(",
        "}": ")",
        "<": "&lt;",
        ">": "&gt;",
    }.items():
        text = text.replace(old, new)
    return text


def _neutralize_event_attributes(value: str) -> str:
    return re.sub(r"\bon[a-zA-Z]+\s*=", "event-attribute=", value)
