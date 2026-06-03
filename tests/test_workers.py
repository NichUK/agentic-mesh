from pathlib import Path

from agentic_mesh.cli import build_runtime
from agentic_mesh.config import load_mesh_config
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED
from agentic_mesh.models import Message
from agentic_mesh.storage import FileMessageStore
from agentic_mesh import workers
from agentic_mesh.workers import ConfiguredWorkerAdapter
from agentic_mesh.workers import parse_agent_result
from agentic_mesh.workers import result_from_payload


def test_parse_agent_result_accepts_structured_worker_json() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    flow_state = mesh_config.project.flow.states["business_analysis"]

    result = result_from_payload(
        parse_agent_result(
            """
            {
              "status": "completed",
              "message": "Analysed the project.",
              "document_updates": [
                {
                  "path": "documents/requirements/business-analyst.md",
                  "content": "# Business Analyst Worklist\\n\\nActual analysis."
                }
              ],
              "handoffs": []
            }
            """
        ),
        flow_state,
    )

    assert result.status == "completed"
    assert result.document_updates[0].path == "documents/requirements/business-analyst.md"


def test_configured_worker_blocks_when_codex_secret_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(workers.shutil, "which", lambda command: "codex")
    mesh_config = load_mesh_config(Path.cwd())
    worker = ConfiguredWorkerAdapter(
        project=mesh_config.project,
        auth_methods=mesh_config.auth_methods,
        workspace_root=tmp_path / "workspace",
        state_root=tmp_path / "state",
    )
    flow_state = mesh_config.project.flow.states["product_definition"]
    message = Message.create(
        role_id="product-manager",
        message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
        payload={
            "title": "Adopt this project",
            "summary": "Analyse the repo.",
            "work_item_id": "work-adoption",
            "work_item_type": "directive",
        },
        source="test",
    )

    result = worker.run(
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
        message,
        flow_state,
    )

    assert result.status == "blocked"
    assert "codex-agentic-mesh-dev-product-manager-token" in result.message
    assert result.document_updates[0].path == flow_state.artifact_path


def test_build_runtime_uses_configured_worker_adapter(tmp_path: Path) -> None:
    _, _, message_store, _, _, runtime = build_runtime(
        Path.cwd(),
        "examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
        tmp_path / "workspace",
        tmp_path / "state",
    )

    assert isinstance(runtime.worker, ConfiguredWorkerAdapter)
    assert isinstance(message_store, FileMessageStore)
