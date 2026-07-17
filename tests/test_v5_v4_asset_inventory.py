from __future__ import annotations

from pathlib import Path

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
    allowed = set(data["allowed_dispositions"])
    entries = _inventory_entries()
    sources = [str(item["source"]) for item in entries]

    assert len(sources) == len(set(sources))
    for item in entries:
        assert item["disposition"] in allowed
        assert str(item["rationale"]).strip()
        assert item["target_stories"]
        assert (ROOT / str(item["source"])).is_file(), item["source"]


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
