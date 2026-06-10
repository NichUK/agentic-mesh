from pathlib import Path

from agentic_mesh.config import load_mesh_config
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import Message
from agentic_mesh.role_context import build_role_context
from agentic_mesh.storage import FileMessageStore


def test_role_context_reads_memory_and_role_config() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    instance = mesh_config.instances["agentic-mesh-dev.product-manager.1"]
    message = Message.create(
        role_id="product-manager",
        message_type="conversation.direct",
        payload={
            "text": "Give me a status update.",
            "source_channel": "dm",
            "source_connector": "teams",
            "source_connector_id": "teams-bot-listener",
            "teams_conversation_id": "conversation-1",
        },
        source="teams:teams-bot-listener:dm",
    )

    context = build_role_context(
        project=mesh_config.project,
        instance=instance,
        message=message,
        workspace_root=Path("examples/projects/agentic-mesh-dev"),
        state_root=Path("unused-state-root"),
    )

    assert context["memory"]["exists"] is True
    assert "Product Manager Memory" in context["memory"]["content"]
    assert context["memory"]["path"] == "agentic-mesh/roles/product-manager/MEMORY.md"
    assert context["role_config"]["root"] == "agentic-mesh/roles/product-manager"
    assert context["role_config"]["files"][0]["path"].endswith("role.yaml")


def test_role_context_includes_recent_same_conversation_messages(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    instance = mesh_config.instances["agentic-mesh-dev.product-manager.1"]
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    previous = store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type="conversation.direct",
            payload={
                "text": "I disagree with the product definition. Rework the dashboard scope.",
                "summary": "I disagree with the product definition.",
                "source_channel": "dm",
                "source_connector": "teams",
                "source_connector_id": "teams-bot-listener",
                "teams_conversation_id": "conversation-1",
            },
            source="teams:teams-bot-listener:dm",
        )
    )
    claimed = store.claim_next("product-manager", instance.instance_id)
    assert claimed is not None
    store.complete(claimed, "completed")

    current = Message.create(
        role_id="product-manager",
        message_type="conversation.direct",
        payload={
            "text": "Yes. Rework it please.",
            "source_channel": "dm",
            "source_connector": "teams",
            "source_connector_id": "teams-bot-listener",
            "teams_conversation_id": "conversation-1",
        },
        source="teams:teams-bot-listener:dm",
    )

    context = build_role_context(
        project=mesh_config.project,
        instance=instance,
        message=current,
        workspace_root=Path("examples/projects/agentic-mesh-dev"),
        state_root=tmp_path / "state",
    )

    rows = context["recent_direct_conversation"]
    assert len(rows) == 1
    assert rows[0]["message_id"] == previous.message_id
    assert "Rework the dashboard scope" in rows[0]["text"]
