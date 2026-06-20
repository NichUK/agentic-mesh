from __future__ import annotations

from pathlib import Path

from agentic_mesh_v4.agent_config import materialize_agent_configs
from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import InMemoryTransport
from agentic_mesh_v4.compose import render_compose
from agentic_mesh_v4.config import DEFAULT_ROLE_IDS
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.runtime import V4Runtime


PROJECT_CONFIG = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")


class FakeTeamsReplySender:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_reply(self, *, role_id: str, activity: dict[str, object], text_markdown: str) -> str:
        self.calls.append({"role_id": role_id, "activity": activity, "text_markdown": text_markdown})
        return "teams-delivery-1"


def test_v4_loads_full_sdlc_team_without_broker() -> None:
    config = load_project_config(PROJECT_CONFIG)

    assert tuple(role.role_id for role in config.roles) == DEFAULT_ROLE_IDS
    assert config.role("project-manager").authority == "full"
    assert config.role("project-manager").sandbox_mode == "danger-full-access"
    assert config.role("project-manager").approval_policy == "never"
    assert config.role("release-manager").authority == "full"
    assert config.role("product-manager").authority == "scoped"


def test_v4_sqlite_queue_claims_steering_first(tmp_path: Path) -> None:
    db = V4Database(tmp_path / "v4.sqlite3")
    db.migrate()
    runtime = V4Runtime(db=db, project_config=load_project_config(PROJECT_CONFIG))
    runtime.register_roles()
    runtime.enqueue_conversation(target_role="project-manager", text="normal")
    steer_id = runtime.enqueue_conversation(target_role="project-manager", text="steer", steering=True)

    claimed = db.claim_next_message(role_id="project-manager", worker_id="agentic-mesh-dev.project-manager.1")

    assert claimed is not None
    assert claimed.message_id == steer_id
    assert claimed.steering is True


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
    db = V4Database(tmp_path / "v4.sqlite3")
    db.migrate()
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


def test_v4_runtime_auto_accepts_approvals_when_policy_is_never(tmp_path: Path) -> None:
    db = V4Database(tmp_path / "v4.sqlite3")
    db.migrate()
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
    db = V4Database(tmp_path / "v4.sqlite3")
    db.migrate()
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


def test_v4_runtime_delivers_completed_teams_reply(tmp_path: Path) -> None:
    db = V4Database(tmp_path / "v4.sqlite3")
    db.migrate()
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
    assert "Authority level: `full`" in text
    assert "SSH credentials are expected at `/mesh/home/.ssh`" in text


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
    assert "HOME: /mesh/home" in rendered
    assert "dispatcher:" in rendered
    assert "dispatch-loop" in rendered
    assert "working_dir: /mesh/agent" in rendered
    assert "v3-nats" not in rendered
    assert "v3-supervisor" not in rendered
    assert "run-agent-service" not in rendered
    assert "nats://" not in rendered
