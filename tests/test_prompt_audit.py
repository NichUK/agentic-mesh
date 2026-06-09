from __future__ import annotations

import json
from pathlib import Path

from agentic_mesh.config import load_mesh_config
from agentic_mesh.prompt_audit import write_startup_prompt_audit


def test_startup_prompt_audit_writes_debug_prompt_and_metadata(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    instance = mesh_config.instances["agentic-mesh-dev.product-manager.1"]
    document_root = tmp_path / "docs"

    result = write_startup_prompt_audit(
        document_library_root=document_root,
        project=mesh_config.project,
        instance=instance,
        workspace_root=tmp_path / "workspace",
        reason="test-startup",
        argv=["python", "-m", "agentic_mesh.cli", "agent-loop"],
    )

    prompt_path = document_root / result["prompt_path"]
    metadata_path = document_root / result["metadata_path"]
    assert prompt_path.exists()
    assert metadata_path.exists()
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "Agentic Mesh agent startup prompt/context" in prompt
    assert "test-startup" in prompt
    assert instance.instance_id in prompt
    assert "Start the agent loop for this concrete role instance" in prompt
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["audit_kind"] == "agent_startup_prompt"
    assert metadata["role_instance_id"] == instance.instance_id
    assert metadata["prompt_capture"] == "agent_loop_startup_context"
