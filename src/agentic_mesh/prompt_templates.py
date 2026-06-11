from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


DEFAULT_PROMPT_CONFIG_DIR = Path("config") / "prompts"


def load_prompt_template(name: str, *, base_dir: Path | None = None) -> str:
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("prompt template name must be a safe filename")
    for root in _candidate_roots(base_dir):
        path = root / name
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8").strip()
    roots = ", ".join(str(root) for root in _candidate_roots(base_dir))
    raise FileNotFoundError(f"Prompt template `{name}` not found in: {roots}")


def render_prompt_template(
    name: str,
    values: Mapping[str, object] | None = None,
    *,
    base_dir: Path | None = None,
) -> str:
    text = load_prompt_template(name, base_dir=base_dir)
    for key, value in (values or {}).items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def _candidate_roots(base_dir: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    if base_dir is not None:
        roots.append(base_dir)
    env_root = os.environ.get("AGENTIC_MESH_PROMPT_CONFIG_ROOT")
    if env_root:
        roots.append(Path(env_root))
    config_root = os.environ.get("AGENTIC_MESH_CONFIG_ROOT")
    if config_root:
        roots.append(Path(config_root) / DEFAULT_PROMPT_CONFIG_DIR)
    cwd = Path.cwd()
    roots.extend(
        [
            cwd / DEFAULT_PROMPT_CONFIG_DIR,
            Path(__file__).resolve().parents[2] / DEFAULT_PROMPT_CONFIG_DIR,
            Path("/workspace") / DEFAULT_PROMPT_CONFIG_DIR,
        ]
    )
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve() if root.exists() else root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped
