from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class DocumentFrameworkError(ValueError):
    pass


@dataclass(frozen=True)
class DocumentType:
    document_type: str
    path_template: str
    required_sections: tuple[str, ...]
    owner_role: str


@dataclass(frozen=True)
class DocumentFramework:
    framework_id: str
    document_types: dict[str, DocumentType]

    def document_type(self, document_type: str) -> DocumentType:
        try:
            return self.document_types[document_type]
        except KeyError as exc:
            raise DocumentFrameworkError(
                f"unknown document type `{document_type}` for `{self.framework_id}`"
            ) from exc


TOGAF_SDLC_V1 = DocumentFramework(
    framework_id="togaf-sdlc-v1",
    document_types={
        "product_definition": DocumentType(
            document_type="product_definition",
            path_template="work-items/{work_item_id}/020-product-definition.md",
            owner_role="product-manager",
            required_sections=(
                "Objective",
                "Scope",
                "Non-Goals",
                "Acceptance Criteria",
                "Sponsor Questions",
                "Review Log",
            ),
        ),
        "implementation_evidence": DocumentType(
            document_type="implementation_evidence",
            path_template="work-items/{work_item_id}/100-implementation-log.md",
            owner_role="engineering",
            required_sections=("Changes", "Tests", "Evidence", "Known Limitations"),
        ),
        "quality_evidence": DocumentType(
            document_type="quality_evidence",
            path_template="work-items/{work_item_id}/110-quality-evidence.md",
            owner_role="qa-engineer",
            required_sections=("BDD Scenarios", "Test Results", "Residual Risk"),
        ),
        "release_record": DocumentType(
            document_type="release_record",
            path_template="work-items/{work_item_id}/140-release-record.md",
            owner_role="release-manager",
            required_sections=(
                "Release Scope",
                "Commit",
                "Approval",
                "Deployment",
                "Smoke Evidence",
                "Rollback Plan",
                "Residual Risks",
                "Closure",
            ),
        ),
    },
)


def validate_document_content(
    *,
    framework: DocumentFramework,
    document_type: str,
    content: str,
    existing_content: str | None = None,
) -> None:
    definition = framework.document_type(document_type)
    missing = [
        section
        for section in definition.required_sections
        if not re.search(rf"^##\s+{re.escape(section)}\s*$", content, re.MULTILINE)
    ]
    if missing:
        raise DocumentFrameworkError(
            f"`{document_type}` missing sections: {', '.join(missing)}"
        )
    if _looks_status_only(content):
        raise DocumentFrameworkError("document content is status-only, not evidence")
    if existing_content and _normalized(existing_content) == _normalized(content):
        raise DocumentFrameworkError("document update duplicates existing content")


def render_path(*, framework: DocumentFramework, document_type: str, work_item_id: str) -> str:
    definition = framework.document_type(document_type)
    return definition.path_template.format(work_item_id=work_item_id)


def read_existing(root: Path, relative_path: str) -> str | None:
    path = root / relative_path
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _looks_status_only(content: str) -> bool:
    body = "\n".join(
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    )
    words = re.findall(r"[A-Za-z]{4,}", body.casefold())
    if not words:
        return True
    status_words = {
        "blocked",
        "failed",
        "waiting",
        "status",
        "runtime",
        "operator",
        "retry",
        "complete",
        "completed",
    }
    evidence_words = {
        "scope",
        "acceptance",
        "decision",
        "test",
        "evidence",
        "risk",
        "rollback",
        "deployment",
        "change",
    }
    return len(set(words) & status_words) >= 3 and not (set(words) & evidence_words)


def _normalized(content: str) -> str:
    return " ".join(content.split()).casefold()
