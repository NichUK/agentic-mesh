from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PATHS = (
    ROOT / ".github",
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / "config",
    ROOT / "examples",
    ROOT / "scripts",
    ROOT / "src",
    ROOT / "tests",
)


def _active_files() -> list[Path]:
    files: list[Path] = []
    for root in ACTIVE_PATHS:
        candidates = (root,) if root.is_file() else root.rglob("*")
        files.extend(
            path
            for path in candidates
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    return files


def test_retired_named_project_has_no_active_runtime_or_fixture_reference() -> None:
    retired_project_marker = "quantu" + "auma"
    references = []
    for path in _active_files():
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if retired_project_marker in content.casefold():
            references.append(path.relative_to(ROOT).as_posix())

    assert references == []
