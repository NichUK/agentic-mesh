from pathlib import Path

from agentic_mesh.config import load_mesh_config
from agentic_mesh.journal import EventJournal
from agentic_mesh.lifecycle import LifecycleStore
from agentic_mesh.models import Message
from agentic_mesh.storage import FileMessageStore


def test_hibernated_instance_wakes_on_inbox_message(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path, mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path, mesh_config.project.project_id, journal)
    lifecycle = LifecycleStore(tmp_path, mesh_config.project.project_id, journal)
    lifecycle.ensure_instances(mesh_config)

    transitions = lifecycle.control_plane_tick(
        mesh_config,
        message_store,
        idle_grace_seconds=0,
    )
    assert any(event["event_type"] == "agent_hibernated" for event in transitions)
    assert (
        lifecycle.get_state("agentic-mesh-dev.engineering.1")["state"]
        == "hibernated"
    )

    message_store.enqueue(
        Message.create(
            role_id="engineering",
            message_type="sdlc.implementation",
            payload={
                "title": "Wake up",
                "work_item_id": "slice-wake",
                "work_item_type": "slice",
                "lifecycle_state": "implementation",
            },
            source="test",
        )
    )
    transitions = lifecycle.control_plane_tick(
        mesh_config,
        message_store,
        idle_grace_seconds=0,
    )

    assert any(event["event_type"] == "agent_woke" for event in transitions)
    assert lifecycle.get_state("agentic-mesh-dev.engineering.1")["state"] == "idle"
