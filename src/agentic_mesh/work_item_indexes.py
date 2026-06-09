from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


UNKNOWN = "unknown"
NOT_FOUND = "not found in visible artifacts"
LOCAL_INDEX_NAME = "00-index.md"
GLOBAL_INDEX_PATH = "work-items/index.md"
WORK_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class WorkItemDocumentRow:
    document_path: str
    purpose: str
    owner_role: str
    review_status: str


@dataclass(frozen=True)
class WorkItemIndex:
    work_item_id: str
    work_item_type: str = UNKNOWN
    lifecycle_state: str = NOT_FOUND
    owner_role: str = NOT_FOUND
    queue_item_id: str = NOT_FOUND
    source_message: str = NOT_FOUND
    source_anchor: str = NOT_FOUND
    summary: str = NOT_FOUND
    documents: list[WorkItemDocumentRow] = field(default_factory=list)
    review_log_markdown: str = ""


@dataclass(frozen=True)
class WorkItemsIndexRow:
    work_item_id: str
    work_item_type: str
    summary: str
    link: str | None = None


def validate_work_item_id(work_item_id: str) -> None:
    if not isinstance(work_item_id, str) or not work_item_id.strip():
        raise ValueError("Work item id must not be empty.")
    if not WORK_ITEM_ID_RE.fullmatch(work_item_id):
        raise ValueError("Work item id contains unsupported characters.")
    if work_item_id.startswith("."):
        raise ValueError("Work item id must not start with a dot.")
    if "/" in work_item_id or "\\" in work_item_id:
        raise ValueError("Work item id must not contain path separators.")


def local_index_path(work_item_id: str) -> str:
    validate_work_item_id(work_item_id)
    return f"work-items/{work_item_id}/{LOCAL_INDEX_NAME}"


def render_local_index(index: WorkItemIndex) -> str:
    validate_work_item_id(index.work_item_id)
    rows = sorted(index.documents, key=lambda row: _document_sort_key(row.document_path))
    title = _heading_text(index.summary if index.summary != NOT_FOUND else index.work_item_id)
    lines = [
        f"# Work Item Index: {title}",
        "",
        f"Work item: `{_inline_code(index.work_item_id)}`",
        f"Work item type: `{_inline_code(index.work_item_type or UNKNOWN)}`",
        (
            "Lifecycle state at last index update: "
            f"`{_inline_code(index.lifecycle_state or NOT_FOUND)}`"
        ),
        (
            "Owner role at last index update: "
            f"`{_inline_code(index.owner_role or NOT_FOUND)}`"
        ),
        f"Queue item: `{_inline_code(index.queue_item_id or NOT_FOUND)}`",
        f"Source message: `{_inline_code(index.source_message or NOT_FOUND)}`",
        f"Source anchor: `{_inline_code(index.source_anchor or NOT_FOUND)}`",
        f"Summary: {_inline_text(index.summary or NOT_FOUND)}",
        "",
        "## Documents",
        "",
        "| Document | Purpose | Owner role | Review status |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        filename = Path(row.document_path).name
        lines.append(
            "| "
            + " | ".join(
                [
                    f"[{_markdown_table_cell(filename)}]({_relative_link(filename)})",
                    _markdown_table_cell(row.purpose or NOT_FOUND),
                    f"`{_inline_code(row.owner_role or UNKNOWN)}`",
                    f"`{_inline_code(row.review_status or UNKNOWN)}`",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Update Practice",
            "",
            (
                "Add or update one row in `## Documents` whenever Agentic Mesh "
                "publishes a lifecycle artifact into this work-item folder. Keep "
                "the purpose to one short sentence so the index remains useful "
                "for human browsing. Update this index in place; do not append "
                "a second full index body."
            ),
            "",
            "## Review Log",
            "",
            _review_log(index.review_log_markdown),
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_global_index(rows: list[WorkItemsIndexRow]) -> str:
    lines = [
        "# Work Items Index",
        "",
        (
            "This index provides a human-browsable entry point for work-item "
            "documentation. Each entry links to the work item's local `00-index.md` "
            "where available."
        ),
        "",
        "| Work item | Type | Summary |",
        "| --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda item: item.work_item_id):
        validate_work_item_id(row.work_item_id)
        link = row.link or f"{row.work_item_id}/00-index.md"
        lines.append(
            "| "
            + " | ".join(
                [
                    f"[{_markdown_table_cell(row.work_item_id)}]({_relative_link(link)})",
                    f"`{_inline_code(row.work_item_type or UNKNOWN)}`",
                    _markdown_table_cell(row.summary or NOT_FOUND),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Update Practice",
            "",
            (
                "Add or update a row whenever a work item local index is created "
                "or refreshed. This index is a browse surface, not live workflow "
                "state."
            ),
            "",
            "## Review Log",
            "",
            "No review entries recorded for maintained index updates.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def parse_local_index(content: str, *, work_item_id: str) -> WorkItemIndex:
    if count_local_headings(content) > 1:
        content = _last_local_body(content)
    metadata = _parse_metadata(content)
    documents = _parse_document_rows(content)
    return WorkItemIndex(
        work_item_id=work_item_id,
        work_item_type=metadata.get("Work item type", UNKNOWN),
        lifecycle_state=metadata.get("Lifecycle state at last index update")
        or metadata.get("Current lifecycle state", NOT_FOUND),
        owner_role=metadata.get("Owner role at last index update", NOT_FOUND),
        queue_item_id=metadata.get("Queue item", NOT_FOUND),
        source_message=metadata.get("Source message", NOT_FOUND),
        source_anchor=metadata.get("Source anchor", NOT_FOUND),
        summary=metadata.get("Summary", NOT_FOUND),
        documents=documents,
        review_log_markdown=extract_review_log_entries(content),
    )


def parse_global_index(content: str) -> list[WorkItemsIndexRow]:
    rows: list[WorkItemsIndexRow] = []
    for values in _parse_table_rows(content, ["Work item", "Type", "Summary"]):
        work_item_id, link = _parse_markdown_link(values[0])
        rows.append(
            WorkItemsIndexRow(
                work_item_id=work_item_id,
                link=link or f"{work_item_id}/00-index.md",
                work_item_type=_strip_code(values[1]) or UNKNOWN,
                summary=_strip_markdown_cell(values[2]) or NOT_FOUND,
            )
        )
    return rows


def upsert_document_row(
    index: WorkItemIndex,
    row: WorkItemDocumentRow,
    *,
    lifecycle_state: str | None = None,
    owner_role: str | None = None,
    queue_item_id: str | None = None,
    source_message: str | None = None,
    source_anchor: str | None = None,
    summary: str | None = None,
    work_item_type: str | None = None,
) -> WorkItemIndex:
    filename = Path(row.document_path).name
    rows = [existing for existing in index.documents if existing.document_path != filename]
    rows.append(
        WorkItemDocumentRow(
            document_path=filename,
            purpose=row.purpose,
            owner_role=row.owner_role,
            review_status=row.review_status,
        )
    )
    return WorkItemIndex(
        work_item_id=index.work_item_id,
        work_item_type=work_item_type or index.work_item_type,
        lifecycle_state=lifecycle_state or index.lifecycle_state,
        owner_role=owner_role or index.owner_role,
        queue_item_id=queue_item_id or index.queue_item_id,
        source_message=source_message or index.source_message,
        source_anchor=source_anchor or index.source_anchor,
        summary=summary or index.summary,
        documents=rows,
        review_log_markdown=index.review_log_markdown,
    )


def upsert_global_row(
    rows: list[WorkItemsIndexRow],
    row: WorkItemsIndexRow,
) -> list[WorkItemsIndexRow]:
    return [existing for existing in rows if existing.work_item_id != row.work_item_id] + [row]


def validation_errors(document_root: Path, work_item_id: str | None = None) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    work_items_root = document_root / "work-items"
    if not work_items_root.exists():
        return errors
    work_dirs = [
        child
        for child in sorted(work_items_root.iterdir())
        if child.is_dir() and (work_item_id is None or child.name == work_item_id)
    ]
    global_path = work_items_root / "index.md"
    if work_dirs and work_item_id is None:
        _validate_global_index(global_path, errors)
    for work_dir in work_dirs:
        if not _has_lifecycle_artifacts(work_dir):
            continue
        try:
            validate_work_item_id(work_dir.name)
        except ValueError as exc:
            errors.append(
                {
                    "path": f"work-items/{work_dir.name}",
                    "work_item_id": work_dir.name,
                    "reason": "invalid_work_item_id",
                    "detail": str(exc),
                }
            )
            continue
        _validate_local_index(work_dir / LOCAL_INDEX_NAME, work_dir.name, errors)
    return errors


def backfill_work_item_indexes(
    document_root: Path,
    *,
    execute: bool,
    artifact_writer,
    work_item_id: str | None = None,
    project_id: str | None = None,
    component_id: str = "work-item-index",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "project_id": project_id,
        "work_item_id": work_item_id,
        "dry_run": not execute,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "repaired": 0,
        "failed": 0,
        "validation_errors": 0,
        "details": [],
    }
    work_items_root = document_root / "work-items"
    if not work_items_root.exists():
        return report

    global_rows = _read_global_rows(work_items_root / "index.md")
    for work_dir in sorted(child for child in work_items_root.iterdir() if child.is_dir()):
        if work_item_id is not None and work_dir.name != work_item_id:
            continue
        rel_index_path = f"work-items/{work_dir.name}/{LOCAL_INDEX_NAME}"
        try:
            if not _has_lifecycle_artifacts(work_dir):
                report["skipped"] += 1
                report["details"].append(
                    {"path": rel_index_path, "work_item_id": work_dir.name, "result": "skipped"}
                )
                continue
            validate_work_item_id(work_dir.name)
            index, repaired = _index_from_visible_artifacts(work_dir)
            rendered = render_local_index(index)
            existed = (work_dir / LOCAL_INDEX_NAME).exists()
            changed = not existed or (work_dir / LOCAL_INDEX_NAME).read_text(
                encoding="utf-8"
            ) != rendered
            if repaired:
                report["repaired"] += 1
            elif existed and changed:
                report["updated"] += 1
            elif not existed:
                report["created"] += 1
            else:
                report["skipped"] += 1
            report["details"].append(
                {
                    "path": rel_index_path,
                    "work_item_id": work_dir.name,
                    "result": (
                        "repaired" if repaired else "updated" if changed and existed else "created" if changed else "skipped"
                    ),
                }
            )
            if execute and changed:
                artifact_writer(
                    relative_path=rel_index_path,
                    content=rendered,
                    work_item_id=work_dir.name,
                    index_kind="local",
                    operation="backfill",
                    component_id=component_id,
                    correlation_id=correlation_id,
                )
            global_rows = upsert_global_row(
                global_rows,
                WorkItemsIndexRow(
                    work_item_id=work_dir.name,
                    work_item_type=index.work_item_type,
                    summary=index.summary,
                ),
            )
        except Exception as exc:
            report["failed"] += 1
            report["details"].append(
                {
                    "path": rel_index_path,
                    "work_item_id": work_dir.name,
                    "result": "failed",
                    "reason": exc.__class__.__name__,
                }
            )
    rendered_global = render_global_index(global_rows)
    global_path = work_items_root / "index.md"
    global_changed = not global_path.exists() or global_path.read_text(
        encoding="utf-8"
    ) != rendered_global
    if execute and global_rows and global_changed:
        artifact_writer(
            relative_path=GLOBAL_INDEX_PATH,
            content=rendered_global,
            work_item_id=work_item_id or "global",
            index_kind="global",
            operation="backfill",
            component_id=component_id,
            correlation_id=correlation_id,
        )
    errors = validation_errors(document_root, work_item_id=work_item_id)
    if errors and execute:
        report["validation_errors"] = len(errors)
    return report


def count_local_headings(content: str) -> int:
    return len(re.findall(r"^# Work Item Index:", content, flags=re.MULTILINE))


def count_global_headings(content: str) -> int:
    return len(re.findall(r"^# Work Items Index\s*$", content, flags=re.MULTILINE))


def extract_review_log_entries(content: str) -> str:
    entries = re.findall(
        r"(?ms)^### REV[^\n]*\n(?:.*?)(?=^### REV|\Z|^# Work Item Index:)",
        content,
    )
    seen: set[str] = set()
    kept: list[str] = []
    for entry in entries:
        normalized = entry.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            kept.append(normalized)
    return "\n\n".join(kept)


def source_anchor_summary(source_anchor: Any) -> str:
    if not isinstance(source_anchor, dict):
        return NOT_FOUND
    ref = source_anchor.get("source_anchor_ref")
    received_at = source_anchor.get("received_at")
    parts = []
    if isinstance(ref, str) and ref:
        parts.append(ref)
    if isinstance(received_at, str) and received_at:
        parts.append(received_at)
    return " | ".join(parts) if parts else NOT_FOUND


def _safe_visible_source_anchor(value: str) -> str:
    text = _strip_code(value)
    if text == NOT_FOUND or text.startswith("source:"):
        return text
    return NOT_FOUND


def review_status_from_content(content: str, default: str = UNKNOWN) -> str:
    match = re.search(r"(?im)^Review status:\s*`?([^`\n]+)`?\s*$", content)
    return match.group(1).strip() if match else default


def _index_from_visible_artifacts(work_dir: Path) -> tuple[WorkItemIndex, bool]:
    index_path = work_dir / LOCAL_INDEX_NAME
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    repaired = count_local_headings(existing) > 1
    if existing:
        base = parse_local_index(existing, work_item_id=work_dir.name)
    else:
        base = WorkItemIndex(work_item_id=work_dir.name)
    rows: list[WorkItemDocumentRow] = []
    metadata: dict[str, str] = {}
    for artifact in sorted(work_dir.glob("*.md"), key=lambda path: _document_sort_key(path.name)):
        if artifact.name == LOCAL_INDEX_NAME:
            continue
        content = artifact.read_text(encoding="utf-8")
        artifact_metadata = _parse_metadata(content)
        metadata.update({key: value for key, value in artifact_metadata.items() if value})
        rows.append(
            WorkItemDocumentRow(
                document_path=artifact.name,
                purpose=_purpose_from_artifact(artifact.name, content),
                owner_role=artifact_metadata.get("Owner role", UNKNOWN),
                review_status=artifact_metadata.get("Review status")
                or review_status_from_content(content, UNKNOWN),
            )
        )
    work_item_type = metadata.get("Work item type") or base.work_item_type or UNKNOWN
    summary = metadata.get("Summary") or base.summary or NOT_FOUND
    lifecycle_state = (
        metadata.get("Lifecycle state")
        or metadata.get("Current lifecycle state")
        or base.lifecycle_state
        or NOT_FOUND
    )
    owner_role = metadata.get("Owner role") or base.owner_role or NOT_FOUND
    return (
        WorkItemIndex(
            work_item_id=work_dir.name,
            work_item_type=work_item_type,
            lifecycle_state=lifecycle_state,
            owner_role=owner_role,
            queue_item_id=metadata.get("Queue item") or base.queue_item_id or NOT_FOUND,
            source_message=metadata.get("Source message") or base.source_message or NOT_FOUND,
            source_anchor=_safe_visible_source_anchor(
                metadata.get("Source anchor") or base.source_anchor or NOT_FOUND
            ),
            summary=summary,
            documents=rows or base.documents,
            review_log_markdown=base.review_log_markdown,
        ),
        repaired,
    )


def _validate_local_index(
    path: Path,
    work_item_id: str,
    errors: list[dict[str, str]],
) -> None:
    rel = f"work-items/{work_item_id}/{LOCAL_INDEX_NAME}"
    if not path.exists():
        errors.append({"path": rel, "work_item_id": work_item_id, "reason": "missing_local_index"})
        return
    content = path.read_text(encoding="utf-8")
    if count_local_headings(content) > 1:
        errors.append({"path": rel, "work_item_id": work_item_id, "reason": "duplicate_index_body"})
    if count_local_headings(content) == 0:
        errors.append({"path": rel, "work_item_id": work_item_id, "reason": "missing_local_heading"})
    for label in [
        "Lifecycle state at last index update:",
        "Owner role at last index update:",
        "## Documents",
        "| Document | Purpose | Owner role | Review status |",
    ]:
        if label not in content:
            errors.append({"path": rel, "work_item_id": work_item_id, "reason": "missing_required_label", "detail": label})
    for link in re.findall(r"\]\(([^)]+)\)", content):
        if _is_external_or_absolute_link(link) or "/" in link or "\\" in link:
            errors.append({"path": rel, "work_item_id": work_item_id, "reason": "invalid_local_link", "detail": link})


def _validate_global_index(path: Path, errors: list[dict[str, str]]) -> None:
    if not path.exists():
        errors.append({"path": GLOBAL_INDEX_PATH, "work_item_id": "", "reason": "missing_global_index"})
        return
    content = path.read_text(encoding="utf-8")
    if count_global_headings(content) != 1:
        errors.append({"path": GLOBAL_INDEX_PATH, "work_item_id": "", "reason": "invalid_global_heading_count"})
    if "| Work item | Type | Summary |" not in content:
        errors.append({"path": GLOBAL_INDEX_PATH, "work_item_id": "", "reason": "missing_global_columns"})
    forbidden = ["Lifecycle state", "Owner role", "Blocker", "Queue status", "Approval status"]
    for value in forbidden:
        if f"| {value} |" in content:
            errors.append({"path": GLOBAL_INDEX_PATH, "work_item_id": "", "reason": "global_index_dashboard_column", "detail": value})


def _read_global_rows(path: Path) -> list[WorkItemsIndexRow]:
    if not path.exists():
        return []
    try:
        return parse_global_index(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _has_lifecycle_artifacts(work_dir: Path) -> bool:
    return any(path.name != LOCAL_INDEX_NAME for path in work_dir.glob("*.md"))


def _parse_metadata(content: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in content.splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z ]+):\s*(.*)$", line)
        if not match:
            continue
        metadata[match.group(1).strip()] = _strip_code(match.group(2).strip())
    return metadata


def _parse_document_rows(content: str) -> list[WorkItemDocumentRow]:
    rows = []
    for values in _parse_table_rows(
        content,
        ["Document", "Purpose", "Owner role", "Review status"],
    ):
        document_path, _ = _parse_markdown_link(values[0])
        rows.append(
            WorkItemDocumentRow(
                document_path=Path(document_path).name,
                purpose=_strip_markdown_cell(values[1]) or NOT_FOUND,
                owner_role=_strip_code(values[2]) or UNKNOWN,
                review_status=_strip_code(values[3]) or UNKNOWN,
            )
        )
    return rows


def _parse_table_rows(content: str, headers: list[str]) -> list[list[str]]:
    lines = content.splitlines()
    header = "| " + " | ".join(headers) + " |"
    rows: list[list[str]] = []
    for index, line in enumerate(lines):
        if line.strip() != header:
            continue
        for row in lines[index + 2 :]:
            if not row.startswith("|"):
                break
            values = [part.strip() for part in row.strip().strip("|").split(" | ")]
            if len(values) == len(headers):
                rows.append(values)
        break
    return rows


def _parse_markdown_link(value: str) -> tuple[str, str | None]:
    match = re.match(r"^\[([^\]]+)\]\(([^)]+)\)$", value.strip())
    if match:
        return _strip_markdown_cell(match.group(1)), match.group(2)
    return _strip_markdown_cell(value), None


def _strip_code(value: str) -> str:
    return _strip_markdown_cell(value.strip().strip("`"))


def _strip_markdown_cell(value: str) -> str:
    text = html.unescape(value)
    text = text.replace(r"\|", "|").replace(r"\[", "[").replace(r"\]", "]")
    text = text.replace(r"\(", "(").replace(r"\)", ")")
    return " ".join(text.split()).strip()


def _purpose_from_artifact(filename: str, content: str) -> str:
    metadata = _parse_metadata(content)
    if metadata.get("Objective"):
        return metadata["Objective"]
    title = next((line.lstrip("# ").strip() for line in content.splitlines() if line.startswith("# ")), "")
    if title:
        return title
    return f"Visible lifecycle artifact {filename}"


def _document_sort_key(filename: str) -> tuple[int, str]:
    match = re.match(r"^(\d+)-", Path(filename).name)
    return (int(match.group(1)) if match else 9999, filename)


def _last_local_body(content: str) -> str:
    matches = list(re.finditer(r"^# Work Item Index:", content, flags=re.MULTILINE))
    if not matches:
        return content
    return content[matches[-1].start() :]


def _heading_text(value: str) -> str:
    text = _inline_text(value)
    text = re.sub(r"[\[\]()`#]", "", text)
    collapsed = " ".join(text.split())
    if len(collapsed) <= 120:
        return collapsed or "Work Item"
    clipped = collapsed[:120].rsplit(" ", 1)[0].rstrip(".,;:")
    return clipped or collapsed[:120].rstrip(".,;:") or "Work Item"


def _inline_text(value: str) -> str:
    return html.escape(_neutralize_event_attributes(str(value)), quote=True).replace(
        "\n", " "
    )


def _inline_code(value: str) -> str:
    return _inline_text(value).replace("`", "&#96;")


def _markdown_table_cell(value: str) -> str:
    text = _inline_text(value)
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


def _relative_link(value: str) -> str:
    if _is_external_or_absolute_link(value) or ".." in Path(value).parts:
        raise ValueError("Index links must be project-relative Markdown paths.")
    return value.replace("\\", "/")


def _is_external_or_absolute_link(value: str) -> bool:
    lowered = value.lower()
    return (
        "://" in lowered
        or lowered.startswith(("mailto:", "/", "\\"))
        or bool(re.match(r"^[A-Za-z]:", value))
    )


def _review_log(value: str) -> str:
    return value.strip() or "No review entries recorded for maintained index updates."


def _neutralize_event_attributes(value: str) -> str:
    return re.sub(r"\bon[a-zA-Z]+\s*=", "event-attribute=", value)
