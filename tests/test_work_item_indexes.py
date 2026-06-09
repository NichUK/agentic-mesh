from __future__ import annotations

import json
from pathlib import Path

from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.journal import EventJournal
from agentic_mesh.work_item_indexes import WorkItemDocumentRow
from agentic_mesh.work_item_indexes import WorkItemIndex
from agentic_mesh.work_item_indexes import WorkItemsIndexRow
from agentic_mesh.work_item_indexes import backfill_work_item_indexes
from agentic_mesh.work_item_indexes import parse_local_index
from agentic_mesh.work_item_indexes import render_global_index
from agentic_mesh.work_item_indexes import render_local_index
from agentic_mesh.work_item_indexes import upsert_document_row
from agentic_mesh.work_item_indexes import validation_errors


def test_local_index_rendering_upserts_and_escapes_hostile_metadata() -> None:
    index = WorkItemIndex(
        work_item_id="work-123",
        work_item_type="slice",
        lifecycle_state="implementation",
        owner_role="engineering",
        queue_item_id="queue-1",
        source_message="msg-1",
        source_anchor="source:abc",
        summary='Index | summary <script onerror="x"> [bad](https://example.test)',
    )
    index = upsert_document_row(
        index,
        WorkItemDocumentRow(
            document_path="10-business-brief.md",
            purpose='Purpose | with <script onclick="x">',
            owner_role="business-analyst",
            review_status="approved",
        ),
    )
    index = upsert_document_row(
        index,
        WorkItemDocumentRow(
            document_path="10-business-brief.md",
            purpose="Updated purpose",
            owner_role="business-analyst",
            review_status="approved",
        ),
    )

    rendered = render_local_index(index)

    assert rendered.count("# Work Item Index:") == 1
    assert rendered.count("10-business-brief.md") == 2
    assert "Updated purpose" in rendered
    assert "Purpose | with" not in rendered
    assert "<script" not in rendered
    assert "onerror=" not in rendered
    assert "event-attribute=" in rendered
    assert "Lifecycle state at last index update" in rendered
    assert "Owner role at last index update" in rendered

    parsed = parse_local_index(rendered, work_item_id="work-123")
    assert parsed.documents[0].document_path == "10-business-brief.md"
    assert parsed.documents[0].purpose == "Updated purpose"


def test_global_index_remains_browse_surface_without_dashboard_columns() -> None:
    rendered = render_global_index(
        [
            WorkItemsIndexRow(
                work_item_id="work-123",
                work_item_type="slice",
                summary="Human-browsable index.",
            )
        ]
    )

    assert rendered.count("# Work Items Index") == 1
    assert "| Work item | Type | Summary |" in rendered
    assert "Lifecycle state" not in rendered
    assert "Owner role" not in rendered
    assert "[work-123](work-123/00-index.md)" in rendered


def test_validate_detects_duplicate_local_index_body(tmp_path: Path) -> None:
    work_dir = tmp_path / "work-items" / "work-123"
    work_dir.mkdir(parents=True)
    (work_dir / "10-business-brief.md").write_text("# Brief\n", encoding="utf-8")
    body = render_local_index(
        WorkItemIndex(
            work_item_id="work-123",
            documents=[
                WorkItemDocumentRow(
                    document_path="10-business-brief.md",
                    purpose="Brief.",
                    owner_role="business-analyst",
                    review_status="approved",
                )
            ],
        )
    )
    (work_dir / "00-index.md").write_text(body + body, encoding="utf-8")
    (tmp_path / "work-items" / "index.md").write_text(
        render_global_index(
            [
                WorkItemsIndexRow(
                    work_item_id="work-123",
                    work_item_type="slice",
                    summary="Brief.",
                )
            ]
        ),
        encoding="utf-8",
    )

    errors = validation_errors(tmp_path)

    assert any(error["reason"] == "duplicate_index_body" for error in errors)


def test_backfill_execute_repairs_duplicate_body_and_preserves_manifest(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "docs" / "00-index" / "document-library-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"manifest": True}), encoding="utf-8")
    document_root = tmp_path / "docs"
    work_dir = document_root / "work-items" / "work-123"
    work_dir.mkdir(parents=True)
    (work_dir / "10-business-brief.md").write_text(
        """
# Brief

Work item type: `slice`
Lifecycle state: `business_analysis`
Owner role: `business-analyst`
Review status: `approved`
Summary: Visible summary only.
""".strip(),
        encoding="utf-8",
    )
    valid_body = render_local_index(
        WorkItemIndex(
            work_item_id="work-123",
            documents=[
                WorkItemDocumentRow(
                    document_path="10-business-brief.md",
                    purpose="Brief.",
                    owner_role="business-analyst",
                    review_status="approved",
                )
            ],
        )
    )
    (work_dir / "00-index.md").write_text(valid_body + valid_body, encoding="utf-8")
    journal = EventJournal(tmp_path / "state", "project")
    store = ArtifactStore(
        tmp_path / "workspace",
        "project",
        journal,
        document_library_root=document_root,
    )

    dry_run = backfill_work_item_indexes(
        document_root,
        execute=False,
        artifact_writer=store.write_maintained_index,
    )
    execute = backfill_work_item_indexes(
        document_root,
        execute=True,
        artifact_writer=store.write_maintained_index,
    )

    repaired = (work_dir / "00-index.md").read_text(encoding="utf-8")
    assert dry_run["repaired"] == 1
    assert execute["repaired"] == 1
    assert repaired.count("# Work Item Index:") == 1
    assert (document_root / "work-items" / "index.md").exists()
    assert manifest.read_text(encoding="utf-8") == json.dumps({"manifest": True})


def test_backfill_skips_invalid_folder_without_lifecycle_artifacts(
    tmp_path: Path,
) -> None:
    document_root = tmp_path / "docs"
    malformed_dir = document_root / "work-items" / "*"
    malformed_dir.mkdir(parents=True)
    (malformed_dir / "00-index.md").write_text(
        "Maintained local work-item indexes refreshed by a prior run.\n",
        encoding="utf-8",
    )
    valid_dir = document_root / "work-items" / "work-123"
    valid_dir.mkdir()
    (valid_dir / "10-business-brief.md").write_text(
        """
# Brief

Work item type: `slice`
Lifecycle state: `business_analysis`
Owner role: `business-analyst`
Review status: `approved`
Summary: Visible summary only.
""".strip(),
        encoding="utf-8",
    )
    journal = EventJournal(tmp_path / "state", "project")
    store = ArtifactStore(
        tmp_path / "workspace",
        "project",
        journal,
        document_library_root=document_root,
    )

    report = backfill_work_item_indexes(
        document_root,
        execute=False,
        artifact_writer=store.write_maintained_index,
    )

    assert report["failed"] == 0
    assert any(
        detail == {
            "path": "work-items/*/00-index.md",
            "work_item_id": "*",
            "result": "skipped",
        }
        for detail in report["details"]
    )
