from __future__ import annotations

import concurrent.futures
from pathlib import Path

from agentic_mesh_v4.agent_config import materialize_agent_configs
from agentic_mesh_v4 import cli as v4_cli
from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import InMemoryTransport
from agentic_mesh_v4.compose import render_compose
from agentic_mesh_v4.config import DEFAULT_ROLE_IDS
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.reporting import render_agent_thread
from agentic_mesh_v4.reporting import render_status
from agentic_mesh_v4.runtime import V4Runtime
from v4_postgres import make_v4_db


PROJECT_CONFIG = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")


class FakeTeamsReplySender:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_reply(self, *, role_id: str, activity: dict[str, object], text_markdown: str) -> str:
        self.calls.append({"role_id": role_id, "activity": activity, "text_markdown": text_markdown})
        return "teams-delivery-1"


class TimeoutAfterNotificationTransport(InMemoryTransport):
    def receive(self) -> dict[str, object] | None:
        if self.responses or self.notifications:
            return super().receive()
        raise TimeoutError("Connection timed out")


def test_v4_loads_full_sdlc_team_without_broker() -> None:
    config = load_project_config(PROJECT_CONFIG)

    assert tuple(role.role_id for role in config.roles) == DEFAULT_ROLE_IDS
    assert config.role("project-manager").authority == "full"
    assert config.role("project-manager").sandbox_mode == "danger-full-access"
    assert config.role("project-manager").approval_policy == "never"
    assert config.role("release-manager").authority == "full"
    assert config.role("product-manager").authority == "scoped"
    assert config.role("product-manager").approval_policy == "never"
    assert config.role("qa-engineer").sandbox_mode == "workspace-write"
    assert config.role("qa-engineer").approval_policy == "never"


def test_v4_runtime_source_does_not_use_sqlite_only_upsert_syntax() -> None:
    runtime_source = Path("src/agentic_mesh_v4/runtime.py").read_text(encoding="utf-8")

    assert "INSERT OR IGNORE" not in runtime_source
    assert "INSERT OR REPLACE" not in runtime_source


def test_v4_postgres_queue_claims_steering_first(tmp_path: Path) -> None:
    db = make_v4_db()
    runtime = V4Runtime(db=db, project_config=load_project_config(PROJECT_CONFIG))
    runtime.register_roles()
    runtime.enqueue_conversation(target_role="project-manager", text="normal")
    steer_id = runtime.enqueue_conversation(target_role="project-manager", text="steer", steering=True)

    claimed = db.claim_next_message(role_id="project-manager", worker_id="agentic-mesh-dev.project-manager.1")

    assert claimed is not None
    assert claimed.message_id == steer_id
    assert claimed.steering is True


def test_v4_postgres_queue_prioritizes_human_teams_messages_after_steering(tmp_path: Path) -> None:
    db = make_v4_db()
    runtime = V4Runtime(db=db, project_config=load_project_config(PROJECT_CONFIG))
    runtime.register_roles()
    runtime.enqueue_conversation(target_role="release-manager", text="internal handoff", source="safe-output")
    teams_id = runtime.enqueue_conversation(target_role="release-manager", text="Are you releasing it?", source="teams")

    claimed = db.claim_next_message(role_id="release-manager", worker_id="agentic-mesh-dev.release-manager.1")

    assert claimed is not None
    assert claimed.message_id == teams_id
    assert claimed.source == "teams"


def test_v4_postgres_queue_claims_ready_messages_as_deliverable_work(tmp_path: Path) -> None:
    db = make_v4_db()
    runtime = V4Runtime(db=db, project_config=load_project_config(PROJECT_CONFIG))
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(target_role="ux-designer", text="Review UX handoff")
    db.mark_message_state(message_id, state="ready", summary="Legacy/manual handoff marked ready")

    assert db.has_queued_messages(role_id="ux-designer") is True
    claimed = db.claim_next_message(role_id="ux-designer", worker_id="agentic-mesh-dev.ux-designer.1")

    assert claimed is not None
    assert claimed.message_id == message_id


def test_v4_codex_protocol_uses_remote_control_thread_and_turn_methods() -> None:
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {"serverInfo": {"name": "fake"}}})
    transport.queue_response(None)
    transport.queue_response({"method": "thread/status", "params": {"summary": "starting"}})
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"method": "turn/status", "params": {"summary": "running"}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": "Hello"}})

    client = CodexAppServerClient(transport)
    client.initialize()
    thread_id = client.start_thread(
        model="gpt-5.5",
        sandbox_mode="danger-full-access",
        approval_policy="never",
    )
    turn_id = client.start_turn(thread_id=thread_id, text="Do work")

    assert thread_id == "thread-1"
    assert turn_id == "turn-1"
    assert [item["method"] for item in transport.sent[:4]] == [
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
    ]
    assert transport.sent[2]["params"]["sandbox"] == "danger-full-access"
    assert transport.sent[2]["params"]["approvalPolicy"] == "never"
    assert client.receive_event()["method"] == "thread/status"
    assert client.receive_event()["method"] == "turn/status"
    assert client.receive_event()["method"] == "item/agentMessage/delta"


def test_v4_codex_protocol_ignores_stale_response_ids_until_matching_response() -> None:
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 0, "result": {"stale": True}})
    transport.queue_response({"method": "turn/status", "params": {"summary": "running"}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})

    client = CodexAppServerClient(transport)
    client.initialize()
    thread_id = client.start_thread(model="gpt-5.5")
    turn_id = client.start_turn(thread_id=thread_id, text="Status report")

    assert turn_id == "turn-1"
    assert client.receive_event()["method"] == "turn/status"


def test_v4_codex_protocol_preserves_server_requests_with_ids_as_events() -> None:
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response(
        {
            "id": 99,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        }
    )
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})

    client = CodexAppServerClient(transport)
    client.initialize()
    thread_id = client.start_thread(model="gpt-5.5")
    turn_id = client.start_turn(thread_id=thread_id, text="Inspect files")

    assert turn_id == "turn-1"
    event = client.receive_event()
    assert event is not None
    assert event["id"] == 99
    assert event["method"] == "item/commandExecution/requestApproval"


def test_v4_runtime_dispatches_message_and_records_stream_events(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": "Done"}})
    transport.queue_notification({"method": "turn/completed", "params": {}})

    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(target_role="project-manager", text="Check status", source="api")

    result = runtime.dispatch_once(role_id="project-manager")

    assert result is not None
    assert result.message_id == message_id
    assert result.state == "completed"
    snapshot = db.snapshot()
    assert snapshot["messages"][0]["state"] == "completed"
    assert snapshot["events"][0]["event_type"] == "turn/completed"
    assert snapshot["events"][1]["content"] == "Done"


def test_v4_runtime_keeps_draining_after_agent_message_item_completed(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": "I will inspect."}})
    transport.queue_notification({"method": "item/completed", "params": {}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": " Actually done."}})
    transport.queue_notification({"method": "turn/completed", "params": {}})

    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(target_role="project-manager", text="Check status", source="api")

    result = runtime.dispatch_once(role_id="project-manager")

    assert result is not None
    assert result.state == "completed"
    row = db.connection.execute(
        "SELECT state, locked_by, locked_at FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    assert row["state"] == "completed"
    assert row["locked_by"] is None
    assert row["locked_at"] is None
    events = [dict(row) for row in db.connection.execute("SELECT event_type, content FROM agent_events ORDER BY created_at")]
    assert [event["event_type"] for event in events].count("item/completed") == 1
    assert any(event["event_type"] == "turn/completed" for event in events)
    assert any(event["content"] == " Actually done." for event in events)


def test_v4_runtime_keeps_started_turn_active_after_read_timeout(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = TimeoutAfterNotificationTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": "I will do this."}})

    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(target_role="engineering", text="Implement work", source="api")

    result = runtime.dispatch_once(role_id="engineering")

    assert result is not None
    assert result.state == "active_turn"
    row = db.connection.execute(
        "SELECT state, locked_by, locked_at FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    assert row["state"] == "active_turn"
    assert row["locked_by"] == "agentic-mesh-dev.engineering.1"
    assert row["locked_at"] is not None
    role = db.connection.execute(
        "SELECT state, active_turn_id FROM role_instances WHERE role_instance_id='agentic-mesh-dev.engineering.1'",
    ).fetchone()
    assert role["state"] == "active"
    assert role["active_turn_id"] == "turn-1"
    events = [dict(row) for row in db.connection.execute("SELECT event_type, content FROM agent_events ORDER BY created_at")]
    assert any(event["event_type"] == "turn/readTimeoutAfterOutput" for event in events)


def test_v4_snapshot_reports_busy_role_and_db_memory_count(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    role_instance_id = "agentic-mesh-dev.project-manager.1"
    db.record_memory(
        role_instance_id=role_instance_id,
        summary="Sponsor prefers visible processing state.",
        source_ref="conversation",
    )
    message_id = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Please do a status sweep",
        source="teams",
    )
    db.claim_next_message(role_id="project-manager", worker_id=role_instance_id)
    db.mark_message_state(message_id, state="active_turn", summary="Delivered to Project Manager")

    project_manager = next(
        item for item in db.snapshot()["roles"] if item["role_instance_id"] == role_instance_id
    )

    assert project_manager["effective_state"] == "busy"
    assert project_manager["current_message"]["message_id"] == message_id
    assert project_manager["memory_count"] == 1


def test_v4_snapshot_marks_active_turn_as_finalizing_after_handoff(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    role_instance_id = "agentic-mesh-dev.platform-engineer.1"
    work_item_id = "work-finalizing-handoff"
    db.upsert_work_item(
        work_item_id=work_item_id,
        title="Finalizing handoff",
        state="qa_ready",
        owner_role="qa-engineer",
        next_action="QA owns the next step.",
    )
    message_id = runtime.enqueue_conversation(
        target_role="platform-engineer",
        text="Repair platform and hand back.",
        source="safe-output",
        payload={"work_item_id": work_item_id},
    )
    db.claim_next_message(role_id="platform-engineer", worker_id=role_instance_id)
    db.mark_message_state(message_id, state="active_turn", summary="Delivered to Platform Engineer")

    snapshot = db.snapshot()
    message = next(item for item in snapshot["messages"] if item["message_id"] == message_id)
    platform = next(item for item in snapshot["roles"] if item["role_instance_id"] == role_instance_id)

    assert message["state"] == "active_turn"
    assert message["display_state"] == "finalizing_handoff"
    assert "has moved to qa-engineer" in message["display_reason"]
    assert platform["current_message"]["display_state"] == "finalizing_handoff"

    status_html = render_status(snapshot)
    assert "finalizing_handoff (raw: active_turn)" in status_html
    assert "has moved to qa-engineer" in status_html


def test_v4_same_conversation_message_steers_into_active_turn(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    role_instance_id = "agentic-mesh-dev.project-manager.1"
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    first_message = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Start work",
        source="teams",
        conversation_ref="conversation-1",
    )
    db.claim_next_message(role_id="project-manager", worker_id=role_instance_id)
    db.mark_message_state(first_message, state="active_turn", summary="Delivered to Project Manager")
    with db.connection:
        db.connection.execute(
            "UPDATE role_instances SET active_thread_id=? WHERE role_instance_id=?",
            ("thread-1", role_instance_id),
        )
        db.connection.execute(
            """
            INSERT INTO codex_threads(thread_id, role_instance_id, status, created_at, updated_at, sandbox_mode, approval_policy)
            VALUES(?,?,?,?,?,?,?)
            """,
            ("thread-1", role_instance_id, "active", "now", "now", "danger-full-access", "never"),
        )
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {}})
    transport.queue_response({"id": 3, "result": {}})
    steering_runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )

    steered_message = steering_runtime.enqueue_or_steer_conversation(
        target_role="project-manager",
        text="Add this to the same thought",
        source="teams",
        conversation_ref="conversation-1",
    )

    assert db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (steered_message,),
    ).fetchone()["state"] == "steered"
    assert [item["method"] for item in transport.sent if "method" in item] == [
        "initialize",
        "initialized",
        "thread/resume",
        "turn/steer",
    ]


def test_v4_failed_immediate_steering_downgrades_to_normal_queue(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    role_instance_id = "agentic-mesh-dev.project-manager.1"
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    first_message = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Start work",
        source="teams",
        conversation_ref="conversation-1",
    )
    db.claim_next_message(role_id="project-manager", worker_id=role_instance_id)
    db.mark_message_state(first_message, state="active_turn", summary="Delivered to Project Manager")
    with db.connection:
        db.connection.execute(
            "UPDATE role_instances SET active_thread_id=?, active_turn_id=? WHERE role_instance_id=?",
            ("thread-1", "turn-1", role_instance_id),
        )
        db.connection.execute(
            """
            INSERT INTO codex_threads(thread_id, role_instance_id, status, created_at, updated_at, sandbox_mode, approval_policy)
            VALUES(?,?,?,?,?,?,?)
            """,
            ("thread-1", role_instance_id, "active", "now", "now", "danger-full-access", "never"),
        )
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {}})
    transport.queue_response({"id": 3, "error": {"code": -32600, "message": "Invalid request"}})
    steering_runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )

    message_id = steering_runtime.enqueue_or_steer_conversation(
        target_role="project-manager",
        text="Can you hear me?",
        source="teams",
        conversation_ref="conversation-1",
    )

    row = db.connection.execute(
        "SELECT state, steering FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    assert row["state"] == "queued"
    assert row["steering"] == 0
    journal = [
        dict(row)
        for row in db.connection.execute(
            "SELECT stage, summary FROM message_journal WHERE message_id=? ORDER BY created_at",
            (message_id,),
        )
    ]
    assert any(item["stage"] == "steering_downgraded" and "Steering failed" in item["summary"] for item in journal)


def test_v4_orphaned_steering_message_dispatches_as_normal_turn(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Did it work?",
        source="api",
        steering=True,
    )
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": "Yes."}})
    transport.queue_notification({"method": "turn/completed", "params": {}})
    dispatch_runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )

    result = dispatch_runtime.dispatch_once(role_id="project-manager")

    assert result is not None
    assert result.state == "completed"
    row = db.connection.execute(
        "SELECT state, steering FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    assert row["state"] == "completed"
    assert row["steering"] == 0
    assert "turn/steer" not in [item["method"] for item in transport.sent if "method" in item]
    assert "turn/start" in [item["method"] for item in transport.sent if "method" in item]


def test_v4_cross_conversation_active_turn_does_not_trigger_steering(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    role_instance_id = "agentic-mesh-dev.project-manager.1"
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    # conversation-2 has an active turn in progress
    first_message = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Do something for conv-2",
        source="teams",
        conversation_ref="conversation-2",
    )
    db.claim_next_message(role_id="project-manager", worker_id=role_instance_id)
    db.mark_message_state(first_message, state="active_turn", summary="Delivered")
    transport = InMemoryTransport()
    steering_runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )

    # New message from conversation-1 should NOT be steered into conv-2's active turn
    queued_message = steering_runtime.enqueue_or_steer_conversation(
        target_role="project-manager",
        text="Hello from conversation-1",
        source="teams",
        conversation_ref="conversation-1",
    )

    row = db.connection.execute(
        "SELECT state, steering FROM message_queue WHERE message_id=?",
        (queued_message,),
    ).fetchone()
    assert row["state"] == "queued"
    assert row["steering"] == 0
    assert transport.sent == []


def test_v4_queue_directive_overrides_same_conversation_steering(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    role_instance_id = "agentic-mesh-dev.project-manager.1"
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    first_message = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Start work",
        source="teams",
        conversation_ref="conversation-1",
    )
    db.claim_next_message(role_id="project-manager", worker_id=role_instance_id)
    db.mark_message_state(first_message, state="active_turn", summary="Delivered to Project Manager")
    transport = InMemoryTransport()
    steering_runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )

    queued_message = steering_runtime.enqueue_or_steer_conversation(
        target_role="project-manager",
        text="QuEuE : please make this separate",
        source="teams",
        conversation_ref="conversation-1",
    )

    row = db.connection.execute(
        "SELECT state, steering FROM message_queue WHERE message_id=?",
        (queued_message,),
    ).fetchone()
    assert row["state"] == "queued"
    assert row["steering"] == 0
    assert transport.sent == []


def test_v4_dispatch_does_not_claim_second_message_while_role_has_active_turn(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    role_instance_id = "agentic-mesh-dev.project-manager.1"
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    first_message = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Keep working on this",
        source="teams",
        conversation_ref="conversation-1",
    )
    db.claim_next_message(role_id="project-manager", worker_id=role_instance_id)
    db.mark_message_state(first_message, state="active_turn", summary="Delivered to Project Manager")
    second_message = runtime.enqueue_conversation(
        target_role="project-manager",
        text="queue: make this separate",
        source="teams",
        conversation_ref="conversation-1",
    )

    result = runtime.dispatch_once(role_id="project-manager")

    assert result is None
    row = db.connection.execute(
        "SELECT state, delivery_attempts, locked_by FROM message_queue WHERE message_id=?",
        (second_message,),
    ).fetchone()
    assert row["state"] == "queued"
    assert row["delivery_attempts"] == 0
    assert row["locked_by"] is None


def test_v4_requeues_orphaned_active_messages_for_stopped_role(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    role_instance_id = "agentic-mesh-dev.project-manager.1"
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="project-manager",
        text="This turn was interrupted by deployment",
        source="teams",
    )
    db.claim_next_message(role_id="project-manager", worker_id=role_instance_id)
    db.mark_message_state(message_id, state="active_turn", summary="Delivered to Project Manager")

    recovered = db.requeue_active_messages_for_role(
        target_role="project-manager",
        summary="Recovered because role container is stopped.",
    )

    assert recovered == 1
    message = db.connection.execute(
        "SELECT state, locked_by, locked_at FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    role = db.connection.execute(
        "SELECT state, active_turn_id FROM role_instances WHERE role_instance_id=?",
        (role_instance_id,),
    ).fetchone()
    assert message["state"] == "queued"
    assert message["locked_by"] is None
    assert message["locked_at"] is None
    assert role["state"] == "ready"
    assert role["active_turn_id"] is None


def test_v4_requeues_stale_active_messages_but_keeps_fresh_active_turns(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    role_instance_id = "agentic-mesh-dev.release-manager.1"
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    stale_message = runtime.enqueue_conversation(
        target_role="release-manager",
        text="Deployment turn interrupted the dispatcher",
        source="safe-output",
    )
    fresh_message = runtime.enqueue_conversation(
        target_role="release-manager",
        text="Fresh active release turn",
        source="safe-output",
    )
    db.claim_next_message(role_id="release-manager", worker_id=role_instance_id)
    db.mark_message_state(stale_message, state="active_turn", summary="Delivered to Release Manager")
    db.claim_next_message(role_id="release-manager", worker_id=role_instance_id)
    db.mark_message_state(fresh_message, state="active_turn", summary="Delivered to Release Manager")
    db.connection.execute(
        "UPDATE message_queue SET updated_at='2000-01-01T00:00:00+00:00' WHERE message_id=?",
        (stale_message,),
    )
    db.connection.commit()

    recovered = db.requeue_active_messages_for_role(
        target_role="release-manager",
        stale_after_seconds=3600,
        summary="Recovered stale active release turn.",
    )

    assert recovered == 1
    rows = {
        row["message_id"]: row["state"]
        for row in db.connection.execute(
            "SELECT message_id, state FROM message_queue WHERE message_id IN (?,?)",
            (stale_message, fresh_message),
        )
    }
    assert rows[stale_message] == "queued"
    assert rows[fresh_message] == "active_turn"


def test_v4_agent_events_refresh_active_message_heartbeat(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    role_instance_id = "agentic-mesh-dev.release-manager.1"
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="release-manager",
        text="Deployment turn is still streaming",
        source="safe-output",
    )
    db.claim_next_message(role_id="release-manager", worker_id=role_instance_id)
    db.mark_message_state(message_id, state="active_turn", summary="Delivered to Release Manager")
    db.connection.execute(
        "UPDATE message_queue SET updated_at='2000-01-01T00:00:00+00:00' WHERE message_id=?",
        (message_id,),
    )
    db.connection.commit()

    db.record_agent_event(
        role_instance_id=role_instance_id,
        event_type="item/commandExecution/outputDelta",
        content="still deploying",
        message_id=message_id,
    )
    recovered = db.requeue_active_messages_for_role(
        target_role="release-manager",
        stale_after_seconds=3600,
        summary="Recovered stale active release turn.",
    )

    assert recovered == 0
    row = db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    assert row is not None
    assert row["state"] == "active_turn"


def test_v4_dispatch_scheduler_schedules_queued_role_while_another_role_is_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    runtime = V4Runtime(db=db, project_config=config)
    runtime.register_roles()
    active_message = runtime.enqueue_conversation(
        target_role="delivery-manager",
        text="Long delivery turn",
        source="safe-output",
    )
    db.claim_next_message(
        role_id="delivery-manager",
        worker_id="agentic-mesh-dev.delivery-manager.1",
    )
    db.mark_message_state(active_message, state="active_turn", summary="Delivery Manager is still working.")
    queued_message = runtime.enqueue_conversation(
        target_role="solution-architect",
        text="Handoff that must not wait behind Delivery Manager.",
        source="safe-output",
    )
    dispatched_roles: list[str] = []

    def fake_dispatch_role_message(**kwargs) -> int:
        dispatched_roles.append(kwargs["role_id"])
        return 1

    monkeypatch.setattr(v4_cli, "_dispatch_role_message", fake_dispatch_role_message)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        active: dict[str, concurrent.futures.Future[int]] = {}
        scheduled = v4_cli._schedule_available_dispatches(  # noqa: SLF001 - regression for dispatcher scheduling.
            db=db,
            project_config=config,
            project_config_path=PROJECT_CONFIG,
            agent_config_root=tmp_path / "agents",
            lifecycle=None,
            active_turn_stale_seconds=3600,
            executor=executor,
            active=active,
        )
        processed = v4_cli._collect_completed_dispatches(active)  # noqa: SLF001

    assert scheduled == 1
    assert processed == 1
    assert dispatched_roles == ["solution-architect"]
    active_row = db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (active_message,),
    ).fetchone()
    queued_row = db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (queued_message,),
    ).fetchone()
    assert active_row["state"] == "active_turn"
    assert queued_row["state"] == "queued"


def test_v4_runtime_auto_accepts_approvals_when_policy_is_never(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification(
        {
            "id": 42,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "command": "ls /documents"},
        }
    )
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": "Checked."}})
    transport.queue_notification({"method": "turn/completed", "params": {}})

    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(target_role="project-manager", text="Check mounts", source="api")

    result = runtime.dispatch_once(role_id="project-manager")

    assert result is not None
    assert result.state == "completed"
    assert {"id": 42, "result": {"decision": "accept"}} in transport.sent
    events = db.snapshot()["events"]
    assert any(event["event_type"] == "item/commandExecution/requestApproval/autoAccepted" for event in events)
    assert db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()["state"] == "completed"


def test_v4_runtime_retires_thread_when_sandbox_metadata_does_not_match(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    role = config.role("project-manager")
    role_instance_id = "agentic-mesh-dev.project-manager.1"
    now = "2026-01-01T00:00:00+00:00"
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO role_instances(
              role_instance_id, role_id, display_name, service_name, state,
              authority, codex_endpoint, active_thread_id, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                role_instance_id,
                "project-manager",
                "Project Manager",
                role.service_name,
                "ready",
                "full",
                f"ws://{role.service_name}:4700",
                "old-thread",
                now,
            ),
        )
        db.connection.execute(
            """
            INSERT INTO codex_threads(thread_id, role_instance_id, status, created_at, updated_at)
            VALUES(?,?,?,?,?)
            """,
            ("old-thread", role_instance_id, "active", now, now),
        )
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {"thread": {"id": "new-thread"}}})
    client = CodexAppServerClient(transport)
    client.initialized = True

    runtime = V4Runtime(db=db, project_config=config)
    thread_id = runtime._thread_for_role(  # noqa: SLF001 - regression covers thread reuse safety.
        client=client,
        role_instance_id=role_instance_id,
        role=role,
    )

    assert thread_id == "new-thread"
    old_row = db.connection.execute("SELECT status FROM codex_threads WHERE thread_id='old-thread'").fetchone()
    new_row = db.connection.execute(
        "SELECT sandbox_mode, approval_policy FROM codex_threads WHERE thread_id='new-thread'"
    ).fetchone()
    assert old_row["status"] == "retired"
    assert new_row["sandbox_mode"] == "danger-full-access"
    assert new_row["approval_policy"] == "never"


def test_v4_runtime_retires_thread_when_agent_config_changes(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    agent_config_root = tmp_path / "agents"
    materialize_agent_configs(
        project_config=config,
        output_root=agent_config_root,
        role_templates_dir=Path("config/roles"),
    )
    role = config.role("engineering")
    role_instance_id = "agentic-mesh-dev.engineering.1"
    runtime = V4Runtime(db=db, project_config=config, agent_config_root=agent_config_root)
    original_hash = runtime._agent_config_hash(role_id="engineering")  # noqa: SLF001 - regression covers staleness.
    now = "2026-01-01T00:00:00+00:00"
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO role_instances(
              role_instance_id, role_id, display_name, service_name, state,
              authority, codex_endpoint, active_thread_id, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                role_instance_id,
                "engineering",
                "Engineering",
                role.service_name,
                "ready",
                "full",
                f"ws://{role.service_name}:4709",
                "old-thread",
                now,
            ),
        )
        db.connection.execute(
            """
            INSERT INTO codex_threads(
              thread_id, role_instance_id, agent_config_hash, sandbox_mode,
              approval_policy, status, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            ("old-thread", role_instance_id, original_hash, role.sandbox_mode, role.approval_policy, "active", now, now),
        )

    agents_path = agent_config_root / "engineering" / "1" / "AGENTS.md"
    agents_path.write_text(agents_path.read_text(encoding="utf-8") + "\nNew instruction.\n", encoding="utf-8")
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {"thread": {"id": "new-thread"}}})
    client = CodexAppServerClient(transport)
    client.initialized = True

    thread_id = runtime._thread_for_role(  # noqa: SLF001 - regression covers config freshness.
        client=client,
        role_instance_id=role_instance_id,
        role=role,
    )

    assert thread_id == "new-thread"
    old_row = db.connection.execute("SELECT status FROM codex_threads WHERE thread_id='old-thread'").fetchone()
    new_row = db.connection.execute(
        "SELECT agent_config_hash, status FROM codex_threads WHERE thread_id='new-thread'"
    ).fetchone()
    assert old_row["status"] == "retired"
    assert new_row["status"] == "active"
    assert new_row["agent_config_hash"] != original_hash


def test_v4_runtime_delivers_completed_teams_reply(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": "Yes"}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": ". Done."}})
    transport.queue_notification({"method": "turn/completed", "params": {}})
    teams_sender = FakeTeamsReplySender()
    activity = {
        "serviceUrl": "https://smba.test/tenant/",
        "conversation": {"id": "conversation-1"},
        "id": "activity-1",
    }

    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
        teams_reply_sender=teams_sender,  # type: ignore[arg-type]
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="project-manager",
        text="Can you hear me?",
        source="teams",
        payload=activity,
    )

    result = runtime.dispatch_once(role_id="project-manager")

    assert result is not None
    assert result.state == "completed"
    expected_activity = dict(activity)
    expected_activity.update({"target_role": "project-manager", "text": "Can you hear me?"})
    assert teams_sender.calls == [
        {"role_id": "project-manager", "activity": expected_activity, "text_markdown": "Yes. Done."}
    ]
    journal = [
        dict(row)
        for row in db.connection.execute(
            "SELECT stage, status, summary FROM message_journal WHERE message_id=? ORDER BY created_at",
            (message_id,),
        )
    ]
    assert any(item["stage"] == "reply_delivered" and item["status"] == "delivered" for item in journal)


def test_v4_runtime_ignores_foreign_turn_events_for_current_message(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-current"}}})
    transport.queue_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-old",
                "item": {
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "This belongs to the previous turn.",
                },
            },
        }
    )
    transport.queue_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-current",
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "This answers the current message.",
                },
            },
        }
    )
    transport.queue_notification(
        {"method": "turn/completed", "params": {"threadId": "thread-1", "turnId": "turn-current"}}
    )
    teams_sender = FakeTeamsReplySender()
    activity = {
        "serviceUrl": "https://smba.test/tenant/",
        "conversation": {"id": "conversation-1"},
        "id": "activity-1",
    }
    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
        teams_reply_sender=teams_sender,  # type: ignore[arg-type]
    )
    runtime.register_roles()
    old_message_id = runtime.enqueue_conversation(target_role="release-manager", text="old", source="teams")
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO codex_turns(turn_id, thread_id, message_id, status, started_at, completed_at)
            VALUES(?,?,?,?,?,NULL)
            """,
            ("turn-old", "thread-1", old_message_id, "active", "2026-01-01T00:00:00+00:00"),
        )
    db.mark_message_state(old_message_id, state="failed", summary="Old turn failed before retry.")
    message_id = runtime.enqueue_conversation(
        target_role="release-manager",
        text="Did it work?",
        source="teams",
        payload=activity,
    )

    result = runtime.dispatch_once(role_id="release-manager")

    assert result is not None
    assert result.state == "completed"
    assert [call["text_markdown"] for call in teams_sender.calls] == ["This answers the current message."]
    events = [
        dict(row)
        for row in db.connection.execute(
            "SELECT event_type, message_id, turn_id FROM agent_events ORDER BY created_at"
        )
    ]
    assert any(
        event["event_type"] == "item/completed/foreignTurnIgnored"
        and event["message_id"] == old_message_id
        and event["turn_id"] == "turn-old"
        for event in events
    )
    assert db.connection.execute(
        "SELECT status FROM codex_turns WHERE turn_id='turn-old'"
    ).fetchone()["status"] == "stale_closed"
    assert db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()["state"] == "completed"


def test_v4_runtime_delivers_commentary_progress_to_teams(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "I am rebuilding the runtime and will report back.",
                }
            },
        }
    )
    transport.queue_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Runtime rebuild completed.",
                }
            },
        }
    )
    transport.queue_notification({"method": "turn/completed", "params": {}})
    teams_sender = FakeTeamsReplySender()
    activity = {
        "serviceUrl": "https://smba.test/tenant/",
        "conversation": {"id": "conversation-1"},
        "id": "activity-1",
    }
    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
        teams_reply_sender=teams_sender,  # type: ignore[arg-type]
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="release-manager",
        text="try again",
        source="teams",
        payload=activity,
    )

    result = runtime.dispatch_once(role_id="release-manager")

    assert result is not None
    assert result.state == "completed"
    assert [call["text_markdown"] for call in teams_sender.calls] == [
        "I am rebuilding the runtime and will report back.",
        "Runtime rebuild completed.",
    ]
    journal = [
        dict(row)
        for row in db.connection.execute(
            "SELECT stage, status, summary FROM message_journal WHERE message_id=? ORDER BY created_at",
            (message_id,),
        )
    ]
    assert any(item["stage"] == "progress_delivered" and item["status"] == "delivered" for item in journal)
    assert any(item["stage"] == "reply_delivered" and item["status"] == "delivered" for item in journal)


def test_v4_agent_thread_page_uses_push_stream_without_auto_refresh() -> None:
    html = render_agent_thread(
        role_id="release-manager",
        messages=[
            {
                "updated_at": "2026-06-24T10:00:00+00:00",
                "state": "active_turn",
                "message_id": "msg-1",
                "text": "try again",
            }
        ],
        events=[
            {
                "created_at": "2026-06-24T10:00:01+00:00",
                "event_type": "item/agentMessage/delta",
                "turn_id": "turn-1",
                "message_id": "msg-1",
                "content": "No-cache rebuild started.",
                "payload_json": '{"method":"item/agentMessage/delta"}',
            },
            {
                "created_at": "2026-06-24T10:00:02+00:00",
                "event_type": "item/agentMessage/delta",
                "turn_id": "turn-1",
                "message_id": "msg-1",
                "content": " Still running.",
                "payload_json": '{"method":"item/agentMessage/delta"}',
            }
        ],
    )

    assert "new EventSource(\"thread/events\")" in html
    assert "http-equiv=\"refresh\"" not in html
    assert 'id="agent-output"' in html
    assert 'class="agent-console"' in html
    assert "No-cache rebuild started. Still running." in html
    assert "Live push stream connected." in html
    assert "<th>Event</th>" not in html
    assert "output.textContent += fragment" in html


def test_v4_runtime_syncs_documents_after_completed_turn(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": "Wrote artifact."}})
    transport.queue_notification({"method": "turn/completed", "params": {}})
    sync_calls: list[str] = []

    class SyncResult:
        uploaded = 3
        folders_created = 1
        root_path = "/documents"

    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
        document_syncer=lambda: sync_calls.append("sync") or SyncResult(),
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(target_role="project-manager", text="Write a dossier", source="api")

    result = runtime.dispatch_once(role_id="project-manager")

    assert result is not None
    assert result.state == "completed"
    assert sync_calls == ["sync"]
    journal = [
        dict(row)
        for row in db.connection.execute(
            "SELECT stage, status, summary FROM message_journal WHERE message_id=? ORDER BY created_at",
            (message_id,),
        )
    ]
    assert any(
        item["stage"] == "document_sync"
        and item["status"] == "completed"
        and "uploaded 3" in item["summary"]
        for item in journal
    )


def test_v4_runtime_records_document_sync_failure_without_failing_message(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = InMemoryTransport()
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": "thread-1"}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": "turn-1"}}})
    transport.queue_notification({"method": "item/agentMessage/delta", "params": {"delta": "Wrote artifact."}})
    transport.queue_notification({"method": "turn/completed", "params": {}})

    def failing_sync() -> object:
        raise RuntimeError("Graph 401")

    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
        document_syncer=failing_sync,
    )
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(target_role="project-manager", text="Write a dossier", source="api")

    result = runtime.dispatch_once(role_id="project-manager")

    assert result is not None
    assert result.state == "completed"
    assert db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()["state"] == "completed"
    journal = [
        dict(row)
        for row in db.connection.execute(
            "SELECT stage, status, summary FROM message_journal WHERE message_id=? ORDER BY created_at",
            (message_id,),
        )
    ]
    assert any(
        item["stage"] == "document_sync"
        and item["status"] == "failed"
        and "Graph 401" in item["summary"]
        for item in journal
    )
    events = db.snapshot()["events"]
    assert any(event["event_type"] == "document_sync/failed" for event in events)


def test_v4_materializes_role_agents_md_from_role_charter(tmp_path: Path) -> None:
    config = load_project_config(PROJECT_CONFIG)
    written = materialize_agent_configs(
        project_config=config,
        output_root=tmp_path / "agents",
        role_templates_dir=Path("config/roles"),
    )

    project_manager_agents = tmp_path / "agents" / "project-manager" / "1" / "AGENTS.md"
    assert project_manager_agents in written
    text = project_manager_agents.read_text(encoding="utf-8")
    assert "Agentic Mesh Role: Project Manager" in text
    assert "Durable project effects must be made through the configured safe-output tools" in text
    assert "missing safe-output tools do not remove your ordinary shell" in text
    assert "do the work before replying" in text
    assert "Before claiming a path, sandbox, or tool is read-only or unavailable" in text
    assert "Work-item dossiers must be written under `/documents/work-items/{work_item_id}`" in text
    assert "`/mesh/agent` is your mounted role identity/configuration folder" in text
    assert "`/mesh/agent-workspace` is your writable current working directory" in text
    assert "`/mesh/worker-auth/codex` is the mounted Codex runtime home" in text
    assert "Do not create project artifacts or source checkouts there" in text
    assert "report a platform mount-permission defect with the exact path" in text
    assert "If `pwd` is `/mesh/agent`, report a platform configuration defect" in text
    assert "Do not create canonical work-item artifacts under `/mesh/project/work-items`" in text
    assert "Authority level: `full`" in text
    assert "assume `/mesh/workspaces/agentic-mesh`, `/documents`, and `/mesh/project` are writable" in text
    assert "run a minimal write/access probe before reporting a blocker" in text
    assert "SSH credentials are expected at `/mesh/home/.ssh`" in text
    assert "copied to `/root/.ssh` at container startup for OpenSSH default lookup" in text
    assert "continue with shell, filesystem, Postgres, dashboard/API, Git, Docker, or SSH inspection" in text


def test_v4_compose_runs_codex_app_server_and_excludes_v3_broker_paths() -> None:
    rendered = render_compose(load_project_config(PROJECT_CONFIG))

    assert "codex app-server --listen ws://0.0.0.0:4700" in rendered
    assert "agentic-mesh-dev-project-manager-1" in rendered
    assert "--document-root /documents" in rendered
    assert "${AGENTIC_MESH_DOCUMENTS_HOST_PATH:-../documents}:/documents" in rendered
    assert "${AGENTIC_MESH_PROJECT_ENV_FILE_HOST_PATH:-.env}:/mesh/home/.env:ro" in rendered
    assert (
        "${AGENTIC_MESH_PROJECT_MANAGER_SSH_HOST_PATH:-../../state/worker_mounts/project-manager/.ssh}"
        ":/mesh/home/.ssh:ro"
    ) in rendered
    assert "cp -r /mesh/home/.ssh/. /root/.ssh/" in rendered
    assert 'sed -i "s#/mesh/home/.ssh#/root/.ssh#g" /root/.ssh/config' in rendered
    assert "HOME: /mesh/home" in rendered
    assert "dispatcher:" in rendered
    assert "dispatch-loop" in rendered
    assert "working_dir: /mesh/agent-workspace" in rendered
    assert "${AGENTIC_MESH_PROJECT_HOST_PATH:-../..}/state/v4/agent-workspaces/project-manager/1:/mesh/agent-workspace" in rendered
    assert "cp /mesh/agent/AGENTS.md /mesh/agent-workspace/AGENTS.md" in rendered
    assert "v3-nats" not in rendered
    assert "v3-supervisor" not in rendered
    assert "run-agent-service" not in rendered
    assert "nats://" not in rendered

