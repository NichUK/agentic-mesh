from __future__ import annotations

from pathlib import Path
import re
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "architecture" / "v5" / "v4-asset-inventory.yaml"


def _inventory_entries() -> list[dict[str, object]]:
    data = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    entries: list[dict[str, object]] = []
    for category, group in data["categories"].items():
        defaults = group.get("defaults", {})
        for item in group.get("assets", []):
            entries.append({"category": category, **defaults, **item})
        for source in group.get("sources", []):
            entries.append({"category": category, **defaults, "source": source})
    return entries


def test_inventory_entries_are_complete_and_source_linked() -> None:
    data = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    retired_revision = str(data["retired_source_revision"])
    assert re.fullmatch(r"[0-9a-f]{40}", retired_revision)
    allowed = set(data["allowed_dispositions"])
    entries = _inventory_entries()
    sources = [str(item["source"]) for item in entries]
    duplicates = sorted({source for source in sources if sources.count(source) > 1})

    assert duplicates == []
    for item in entries:
        assert item["disposition"] in allowed
        assert str(item["rationale"]).strip()
        assert isinstance(item["target_stories"], list)
        assert item["target_stories"]
        assert all(
            re.fullmatch(r"AMV5-\d{3}", story)
            for story in item["target_stories"]
        )
        source = str(item["source"])
        if not (ROOT / source).is_file():
            archived = subprocess.run(
                ["git", "cat-file", "-e", f"{retired_revision}:{source}"],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )
            assert archived.returncode == 0, source


def test_inventory_covers_every_active_v4_asset_class() -> None:
    actual = {
        path.relative_to(ROOT).as_posix()
        for pattern in (
            "src/agentic_mesh_v4/*.py",
            "tests/test_v4_*.py",
            "config/roles/*.yaml",
            "config/flows/*",
            "config/schemas/*",
            "config/prompts/**/*",
        )
        for path in ROOT.glob(pattern)
        if path.is_file()
    }
    inventoried = {str(item["source"]) for item in _inventory_entries()}

    assert actual <= inventoried, sorted(actual - inventoried)
