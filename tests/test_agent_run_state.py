import json
from pathlib import Path

import pytest

from agentic_mesh.agent_run_state import AgentRunState
from agentic_mesh.agent_run_state import FileAgentRunStateStore
from agentic_mesh.agent_run_state import RUN_STATE_SCHEMA_VERSION
from agentic_mesh.agent_run_state import RunStateReadError
from agentic_mesh.config import load_mesh_config
from agentic_mesh.models import Message


def _instance():
    config = load_mesh_config(Path.cwd())
    return config.instances["agentic-mesh-dev.engineering.1"]


def test_file_agent_run_state_store_writes_safe_current_record(tmp_path: Path) -> None:
    instance = _instance()
    message = Message.create(
        role_id=instance.role_id,
        message_type="sdlc.implementation",
        payload={
            "work_item_id": "work-safe",
            "work_item_type": "slice",
            "queue_item_id": "queue-safe",
            "lifecycle_state": "implementation",
        },
        source="test",
        correlation_id="corr-safe",
    )
    store = FileAgentRunStateStore(tmp_path / "state", instance.project_id)

    state = AgentRunState.from_claim(
        instance=instance,
        message=message,
        run_state="running",
        timeout_seconds=3600,
        progress_window_seconds=600,
        evidence_source="worker_run_started",
        observed_at="2026-06-05T15:00:00+00:00",
    )
    store.write_current(state)

    loaded = store.read_current(instance.instance_id)
    assert loaded == state
    payload = json.loads(store.current_path(instance.instance_id).read_text())
    assert payload["schema_version"] == RUN_STATE_SCHEMA_VERSION
    assert payload["run_state"] == "running"
    serialized = json.dumps(payload)
    for forbidden in [
        "stdout",
        "stderr",
        "prompt",
        "command",
        "provider_error",
        "secret_ref",
        "mount_ref",
        "/mesh/",
        "container_id",
        "pid",
    ]:
        assert forbidden not in serialized


def test_file_agent_run_state_store_rejects_unsafe_ids(tmp_path: Path) -> None:
    store = FileAgentRunStateStore(tmp_path / "state", "agentic-mesh-dev")

    for unsafe in ["../escape", "/abs", "role/one", "http://x", ""]:
        with pytest.raises(ValueError):
            store.current_path(unsafe)


def test_file_agent_run_state_store_reports_corrupt_record_safely(tmp_path: Path) -> None:
    instance = _instance()
    store = FileAgentRunStateStore(tmp_path / "state", instance.project_id)
    path = store.current_path(instance.instance_id)
    path.write_text("{not-json", encoding="utf-8")

    result = store.read_current_with_error(instance.instance_id)

    assert isinstance(result, RunStateReadError)
    assert result.error_class == "JSONDecodeError"
    assert "/" not in result.error_class
