import pytest

from agentic_mesh_v2.documents import DocumentFrameworkError
from agentic_mesh_v2.documents import TOGAF_SDLC_V1
from agentic_mesh_v2.documents import render_path
from agentic_mesh_v2.documents import validate_document_content


def test_togaf_framework_renders_three_digit_work_item_paths() -> None:
    assert (
        render_path(
            framework=TOGAF_SDLC_V1,
            document_type="product_definition",
            work_item_id="work-123",
        )
        == "work-items/work-123/020-product-definition.md"
    )


def test_document_validation_requires_factual_sections_and_rejects_duplicates() -> None:
    content = """# Product Definition

## Objective
Build the thing.

## Scope
Dashboard table density.

## Non-Goals
No live updates.

## Acceptance Criteria
- Rows are compact and readable.

## Sponsor Questions
None.

## Review Log
Approved.
"""
    validate_document_content(
        framework=TOGAF_SDLC_V1,
        document_type="product_definition",
        content=content,
    )
    with pytest.raises(DocumentFrameworkError, match="duplicates"):
        validate_document_content(
            framework=TOGAF_SDLC_V1,
            document_type="product_definition",
            content=content,
            existing_content=content,
        )


def test_document_validation_rejects_status_only_content() -> None:
    content = """# Status

## Objective
Blocked.

## Scope
Runtime blocked waiting status retry operator.

## Non-Goals
Blocked.

## Acceptance Criteria
Blocked failed waiting status retry.

## Sponsor Questions
None.

## Review Log
Blocked failed waiting status retry.
"""
    with pytest.raises(DocumentFrameworkError, match="status-only"):
        validate_document_content(
            framework=TOGAF_SDLC_V1,
            document_type="product_definition",
            content=content,
        )
