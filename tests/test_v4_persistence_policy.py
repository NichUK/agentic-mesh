from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from agentic_mesh_v4.cli import main
from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import InMemoryTransport
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.persistence_policy import BINARY_OMISSIONS_KEY
from agentic_mesh_v4.persistence_policy import PERSISTENCE_POLICY_ID
from agentic_mesh_v4.persistence_policy import sanitize_json_payload
from agentic_mesh_v4.runtime import V4Runtime
from v4_postgres import make_v4_db
from v4_postgres import make_v4_db_url


PROJECT_CONFIG = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")


def test_v4_persistence_policy_marks_nul_text_and_binary_bytes_with_hashes() -> None:
    nul_text = "ELF\x00payload"
    binary = b"\x00\xffbinary"

    sanitized = sanitize_json_payload(
        {"output": nul_text, "nested": [{"blob": binary}]},
        path="test.payload",
    )

    assert not _contains_nul(sanitized)
    assert PERSISTENCE_POLICY_ID in sanitized["output"]
    assert hashlib.sha256(nul_text.encode()).hexdigest() in sanitized["output"]
    assert hashlib.sha256(binary).hexdigest() in sanitized["nested"][0]["blob"]
    metadata = sanitized[BINARY_OMISSIONS_KEY]
    assert metadata["raw_storage"] == "external_file_or_hash_only"
    assert {item["kind"] for item in metadata["omissions"]} == {
        "nul_text",
        "binary_bytes",
    }


def test_v4_postgres_persistence_boundaries_handle_nul_without_silent_substitution() -> (
    None
):
    db = make_v4_db()
    runtime = V4Runtime(db=db, project_config=load_project_config(PROJECT_CONFIG))
    runtime.register_roles()
    raw = "prefix\x00suffix"
    raw_sha = hashlib.sha256(raw.encode()).hexdigest()

    message_id = db.enqueue_message(
        target_role="project-manager",
        text=raw,
        payload={"nested": {"raw": raw}},
    )
    journal_id = db.record_message_journal(
        message_id=message_id,
        correlation_id=f"corr-{message_id}",
        stage="nul_regression",
        status="test",
        summary=raw,
        payload={"raw": raw},
    )
    event_id = db.record_agent_event(
        role_instance_id="agentic-mesh-dev.project-manager.1",
        event_type="item/commandExecution/outputDelta",
        content=raw,
        payload={"params": {"delta": raw}},
        message_id=message_id,
    )
    call_id = db.record_safe_output_call(
        role_instance_id="agentic-mesh-dev.project-manager.1",
        tool_name="test.binary",
        payload={"raw": raw, "bytes": b"\x00\x01"},
        message_id=message_id,
    )

    message = db.connection.execute(
        "SELECT text, payload_json FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    journal = db.connection.execute(
        "SELECT summary, payload_hash FROM message_journal WHERE journal_id=?",
        (journal_id,),
    ).fetchone()
    event = db.connection.execute(
        "SELECT content, payload_json FROM agent_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    safe_output = db.connection.execute(
        "SELECT payload_json FROM safe_output_calls WHERE call_id=?",
        (call_id,),
    ).fetchone()

    assert raw_sha in message["text"]
    assert raw_sha in journal["summary"]
    assert journal["payload_hash"]
    assert raw_sha in event["content"]
    for encoded in (
        message["payload_json"],
        event["payload_json"],
        safe_output["payload_json"],
    ):
        decoded = json.loads(encoded)
        assert not _contains_nul(decoded)
        assert BINARY_OMISSIONS_KEY in decoded
    assert raw not in json.dumps(db.snapshot(), sort_keys=True)


def test_v4_runtime_nul_command_output_reaches_terminal_state_with_hash_evidence() -> (
    None
):
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-nul"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-nul"}}})
    raw_output = "binary\x00command-output"
    transport.queue_notification(
        {
            "method": "item/commandExecution/outputDelta",
            "params": {"delta": raw_output, "command": ["rg", "-a", "executable"]},
        }
    )
    transport.queue_notification({"method": "turn/completed", "params": {}})
    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Persist command evidence safely.",
        source="api",
    )

    result = runtime.dispatch_once(project_id=config.project_id, role_id="project-manager")

    assert result is not None
    assert result.state == "completed"
    message = db.connection.execute(
        "SELECT state, locked_by, locked_at FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    turn = db.connection.execute(
        "SELECT status, completed_at FROM codex_turns WHERE turn_id='turn-nul'",
    ).fetchone()
    event = db.connection.execute(
        "SELECT content, payload_json FROM agent_events WHERE message_id=? AND event_type='item/commandExecution/outputDelta'",
        (message_id,),
    ).fetchone()
    payload = json.loads(event["payload_json"])
    raw_sha = hashlib.sha256(raw_output.encode()).hexdigest()

    assert dict(message) == {"state": "completed", "locked_by": None, "locked_at": None}
    assert turn["status"] == "completed"
    assert turn["completed_at"] is not None
    assert raw_sha in event["content"]
    assert raw_sha in json.dumps(payload, sort_keys=True)
    assert BINARY_OMISSIONS_KEY in payload
    assert raw_output not in event["content"]


def test_v4_runtime_persistence_failure_terminalizes_turn(monkeypatch) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-failed"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-failed"}}})
    transport.queue_notification(
        {
            "method": "item/commandExecution/outputDelta",
            "params": {"delta": "ordinary output"},
        }
    )
    original_record_agent_event = db.record_agent_event
    failure_injected = False

    def fail_output_event_once(**kwargs):
        nonlocal failure_injected
        if (
            not failure_injected
            and kwargs["event_type"] == "item/commandExecution/outputDelta"
        ):
            failure_injected = True
            raise RuntimeError("injected persistence failure")
        return original_record_agent_event(**kwargs)

    monkeypatch.setattr(db, "record_agent_event", fail_output_event_once)
    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Exercise terminal failure handling.",
        source="api",
    )

    result = runtime.dispatch_once(project_id=config.project_id, role_id="project-manager")

    message = db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    turn = db.connection.execute(
        "SELECT status, completed_at FROM codex_turns WHERE turn_id='turn-failed'",
    ).fetchone()
    role = db.connection.execute(
        "SELECT state, active_turn_id FROM role_instances WHERE role_id='project-manager'",
    ).fetchone()

    assert result is not None
    assert result.state == "failed"
    assert message["state"] == "failed"
    assert turn["status"] == "failed"
    assert turn["completed_at"] is not None
    assert dict(role) == {"state": "ready", "active_turn_id": None}


def test_v4_safe_output_rejects_nul_before_any_durable_effect(monkeypatch) -> None:
    db_url = make_v4_db_url()
    raw_next_action = "unsafe\x00next-action"
    raw_sha = hashlib.sha256(raw_next_action.encode()).hexdigest()
    monkeypatch.setenv("AGENTIC_MESH_SAFE_OUTPUT_PROXY_BYPASS", "1")

    with pytest.raises(ValueError, match=raw_sha):
        main(
            [
                "--project-config",
                str(PROJECT_CONFIG),
                "--db",
                db_url,
                "safe-output",
                "work-item-update",
                "--role-id",
                "product-manager",
                "--work-item-id",
                "work-nul-rejected",
                "--title",
                "NUL rejection fixture",
                "--state",
                "product_definition",
                "--owner-role",
                "product-manager",
                "--next-action",
                raw_next_action,
            ]
        )

    db = V4Database(db_url)
    try:
        db.migrate()
        work = db.connection.execute(
            "SELECT 1 FROM work_items WHERE work_item_id='work-nul-rejected'",
        ).fetchone()
        call = db.connection.execute(
            "SELECT 1 FROM safe_output_calls WHERE work_item_id='work-nul-rejected'",
        ).fetchone()
    finally:
        db.close()

    assert work is None
    assert call is None


def _contains_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(
            _contains_nul(key) or _contains_nul(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_nul(item) for item in value)
    return False
