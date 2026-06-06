from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.journal import EventJournal


def _store(tmp_path: Path) -> tuple[ArtifactStore, EventJournal, Path]:
    document_root = tmp_path / "docs"
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    store = ArtifactStore(
        tmp_path / "workspace",
        "agentic-mesh-dev",
        journal,
        document_library_root=document_root,
    )
    return store, journal, document_root


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "/tmp/lifecycle-flow.md",
        "../lifecycle-flow.md",
        "work-items/work-1/../../escape.md",
        "C:/temp/lifecycle-flow.md",
        r"C:\temp\lifecycle-flow.md",
        "//server/share/lifecycle-flow.md",
        r"\\server\share\lifecycle-flow.md",
        "documents/analysis/lifecycle-flow.md",
        "work-items/work-1",
        "work-items/work-1/",
        "work-items/other-work/lifecycle-flow.md",
        r"work-items\work-1\..\other\lifecycle-flow.md",
    ],
)
def test_generated_artifact_path_rejection_has_no_write_side_effects(
    tmp_path: Path,
    bad_path: str,
) -> None:
    store, journal, document_root = _store(tmp_path)

    with pytest.raises(ValueError):
        store.write_generated_artifact(
            relative_path=bad_path,
            content="# Lifecycle Flow",
            work_item_id="work-1",
        )

    assert not document_root.exists()
    assert journal.read_all() == []


def test_generated_artifact_path_rejects_symlink_escape(tmp_path: Path) -> None:
    store, journal, document_root = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    document_root.mkdir()
    (document_root / "work-items").mkdir()
    os.symlink(outside, document_root / "work-items" / "work-1")

    with pytest.raises(ValueError):
        store.write_generated_artifact(
            relative_path="work-items/work-1/lifecycle-flow.md",
            content="# Lifecycle Flow",
            work_item_id="work-1",
        )

    assert not (outside / "lifecycle-flow.md").exists()
    assert journal.read_all() == []


def test_generated_artifact_replaces_content_and_registers_status(
    tmp_path: Path,
) -> None:
    store, journal, document_root = _store(tmp_path)
    path = "work-items/work-1/lifecycle-flow.md"

    first = store.write_generated_artifact(
        relative_path=path,
        content="# Lifecycle Flow\n\nfirst",
        work_item_id="work-1",
        queue_item_id="queue-1",
        lifecycle_state="implementation",
        correlation_id="corr-123",
    )
    second = store.write_generated_artifact(
        relative_path=path,
        content="# Lifecycle Flow\n\nsecond",
        work_item_id="work-1",
        queue_item_id="queue-1",
        lifecycle_state="implementation",
        correlation_id="corr-123",
    )

    assert first == second == document_root / path
    content = second.read_text(encoding="utf-8")
    assert content.count("# Lifecycle Flow") == 1
    assert "second" in content
    assert "first" not in content
    events = journal.read_all()
    assert [event["event_type"] for event in events] == [
        "documentation_updated",
        "documentation_updated",
    ]
    assert events[-1]["generated_artifact"] is True
    assert events[-1]["path"] == path
    assert events[-1]["queue_item_id"] == "queue-1"


def test_generated_artifact_replace_failure_preserves_previous_content(
    tmp_path: Path,
) -> None:
    store, journal, _ = _store(tmp_path)
    path = "work-items/work-1/lifecycle-flow.md"
    target = store.write_generated_artifact(
        relative_path=path,
        content="previous",
        work_item_id="work-1",
    )

    with patch("agentic_mesh.artifacts.os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            store.write_generated_artifact(
                relative_path=path,
                content="new",
                work_item_id="work-1",
            )

    assert target.read_text(encoding="utf-8") == "previous\n"
    assert len(journal.read_all()) == 1


@pytest.mark.parametrize(
    "bad_path,index_kind",
    [
        ("", "local"),
        ("/tmp/00-index.md", "local"),
        ("../00-index.md", "local"),
        ("work-items/work-1/other.md", "local"),
        ("work-items/other/00-index.md", "local"),
        ("work-items/work-1/00-index.md", "global"),
        ("work-items/work-1/../../index.md", "global"),
        ("C:/temp/00-index.md", "local"),
        (r"\\server\share\00-index.md", "local"),
    ],
)
def test_maintained_index_path_rejection_has_no_write_side_effects(
    tmp_path: Path,
    bad_path: str,
    index_kind: str,
) -> None:
    store, journal, document_root = _store(tmp_path)

    with pytest.raises(ValueError):
        store.write_maintained_index(
            relative_path=bad_path,
            content="# Work Item Index",
            work_item_id="work-1",
            index_kind=index_kind,
            operation="test",
        )

    assert not document_root.exists()
    assert journal.read_all() == []


def test_maintained_index_replaces_content_and_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    store, journal, document_root = _store(tmp_path)
    path = "work-items/work-1/00-index.md"

    first = store.write_maintained_index(
        relative_path=path,
        content="# Work Item Index: first",
        work_item_id="work-1",
        index_kind="local",
        operation="test",
        correlation_id="corr-123",
    )
    second = store.write_maintained_index(
        relative_path=path,
        content="# Work Item Index: second",
        work_item_id="work-1",
        index_kind="local",
        operation="test",
        correlation_id="corr-123",
    )

    assert first == second == document_root / path
    assert second.read_text(encoding="utf-8") == "# Work Item Index: second\n"
    events = journal.read_all()
    assert [event["event_type"] for event in events] == [
        "work_item_index_updated",
        "work_item_index_updated",
    ]
    assert events[-1]["operation"] == "test"

    outside = tmp_path / "outside"
    outside.mkdir()
    (document_root / "work-items" / "work-link").symlink_to(outside)
    with pytest.raises(ValueError):
        store.write_maintained_index(
            relative_path="work-items/work-link/00-index.md",
            content="# Work Item Index: escaped",
            work_item_id="work-link",
            index_kind="local",
            operation="test",
        )
    assert not (outside / "00-index.md").exists()
