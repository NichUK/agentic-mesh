from pathlib import Path

from agentic_mesh.prompt_templates import load_prompt_template
from agentic_mesh.prompt_templates import render_prompt_template


def test_shared_prompt_templates_load_from_config() -> None:
    text = load_prompt_template("worker-general-instructions.md")

    assert "CONVERSATION-TO-WORK BOUNDARY" in text
    assert "Do not create a work item yourself" in text


def test_prompt_template_override_root(monkeypatch, tmp_path: Path) -> None:
    prompt_root = tmp_path / "prompts"
    prompt_root.mkdir()
    (prompt_root / "example.md").write_text(
        "Hello {{name}}.",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTIC_MESH_PROMPT_CONFIG_ROOT", str(prompt_root))

    assert render_prompt_template("example.md", {"name": "mesh"}) == "Hello mesh."
