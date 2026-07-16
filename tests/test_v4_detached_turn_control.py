from __future__ import annotations

import fcntl
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import InMemoryTransport
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.detached_turn_control import CONTROL_SCHEMA
from agentic_mesh_v4.detached_turn_control import evaluate_detached_turn_control
from agentic_mesh_v4.handoff_lifecycle import cancel_handoff
from agentic_mesh_v4.handoff_lifecycle import create_handoff
from agentic_mesh_v4.runtime import AgentTurnStillRunning
from agentic_mesh_v4.runtime import V4Runtime
from agentic_mesh_v4.shared_fleet import MigrationTraffic
from agentic_mesh_v4.shared_fleet import evaluate_migration_guard
from v4_postgres import make_v4_db


PROJECT_CONFIG = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")
WORK_ITEM_ID = "work-v4-single-shared-fleet-migration-20260716"
ROLE_INSTANCE_ID = "agentic-mesh-dev.platform-engineer.1"
SOURCE_MESSAGE_ID = "msg-platform-control"
TARGET_MESSAGE_ID = "msg-project-manager-continuation"
TURN_ID = "turn-platform-control"
HANDOFF_ID = "handoff-platform-control"


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, _params=()):
        if "FROM message_queue WHERE message_id" in sql:
            return _Result([self.db.source])
        if "FROM codex_turns WHERE turn_id" in sql:
            return _Result([self.db.turn])
        if "FROM handoffs h" in sql:
            return _Result([self.db.continuation])
        if "SELECT message_id, state FROM message_queue" in sql:
            return _Result(self.db.messages)
        raise AssertionError(sql)


class _Database:
    def __init__(self, *, call, lease_handle):
        self.calls = [call]
        self.lease_handle = lease_handle
        self.source = {
            "message_id": SOURCE_MESSAGE_ID,
            "target_role": "platform-engineer",
            "state": "active_turn",
            "locked_by": ROLE_INSTANCE_ID,
        }
        self.turn = {"turn_id": TURN_ID, "message_id": SOURCE_MESSAGE_ID, "status": "active"}
        self.continuation = {
            "handoff_id": HANDOFF_ID,
            "work_item_id": WORK_ITEM_ID,
            "from_role": "platform-engineer",
            "to_role": "project-manager",
            "status": "open",
            "source_message_id": SOURCE_MESSAGE_ID,
            "target_message_id": TARGET_MESSAGE_ID,
            "target_role": "project-manager",
            "state": "queued",
            "delivery_attempts": 0,
            "locked_by": None,
            "locked_at": None,
        }
        self.messages = [
            {"message_id": SOURCE_MESSAGE_ID, "state": "active_turn"},
            {"message_id": TARGET_MESSAGE_ID, "state": "queued"},
        ]
        self.events = []
        self.connection = _Connection(self)

    def list_safe_output_calls(self, **_kwargs):
        return self.calls

    def record_agent_event(self, **kwargs):
        self.events.append(kwargs)


class _TimeoutTransport(InMemoryTransport):
    def receive(self):
        raise TimeoutError("Connection timed out")


class _FailedInterruptTransport(_TimeoutTransport):
    def send(self, message):
        if message.get("method") == "turn/interrupt":
            raise ConnectionError("app-server interrupt unavailable")
        return super().send(message)


@pytest.fixture
def control_db(tmp_path: Path):
    now = datetime.now(UTC)
    expires_at = (now + timedelta(minutes=5)).isoformat()
    lease_path = tmp_path / "detached-control.lease"
    lease_path.write_text(
        json.dumps(
            {
                "schema": CONTROL_SCHEMA,
                "state": "waiting",
                "dispatcher_state": "held",
                "dispatcher_hold_id": "dispatcher-hold-1",
                "process_id": "detached-process-1",
                "message_id": SOURCE_MESSAGE_ID,
                "turn_id": TURN_ID,
                "work_item_id": WORK_ITEM_ID,
                "expires_at": expires_at,
            }
        ),
        encoding="utf-8",
    )
    lease_handle = lease_path.open("r", encoding="utf-8")
    fcntl.flock(lease_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    payload = {
        "work_item_id": WORK_ITEM_ID,
        "from_role": "platform-engineer",
        "to_role": "project-manager",
        "handoff_id": HANDOFF_ID,
        "detached_turn_control": {
            "lease_path": str(lease_path),
            "process_id": "detached-process-1",
            "dispatcher_hold_id": "dispatcher-hold-1",
            "expires_at": expires_at,
            "role_instance_id": ROLE_INSTANCE_ID,
        },
    }
    call = {
        "call_id": "call-control",
        "tool_name": "handoff.require",
        "role_instance_id": ROLE_INSTANCE_ID,
        "message_id": SOURCE_MESSAGE_ID,
        "turn_id": TURN_ID,
        "work_item_id": WORK_ITEM_ID,
        "created_at": now.isoformat(),
        "payload_json": json.dumps(payload),
    }
    db = _Database(call=call, lease_handle=lease_handle)
    try:
        yield db
    finally:
        lease_handle.close()


def _evaluate(db):
    return evaluate_detached_turn_control(
        db=db,
        role_instance_id=ROLE_INSTANCE_ID,
        message_id=SOURCE_MESSAGE_ID,
        turn_id=TURN_ID,
    )


def test_exact_detached_control_interrupts_waiting_turn(control_db) -> None:
    transport = _TimeoutTransport()
    client = CodexAppServerClient(transport)
    client.initialized = True
    runtime = V4Runtime(
        db=control_db,
        project_config=load_project_config(PROJECT_CONFIG),
    )

    reply = runtime._drain_available_events(
        client=client,
        role_instance_id=ROLE_INSTANCE_ID,
        approval_policy="never",
        thread_id="thread-platform-control",
        turn_id=TURN_ID,
        message_id=SOURCE_MESSAGE_ID,
    )

    assert reply == ""
    assert transport.sent[-1]["method"] == "turn/interrupt"
    assert transport.sent[-1]["params"] == {
        "threadId": "thread-platform-control",
        "turnId": TURN_ID,
    }
    assert control_db.continuation["state"] == "queued"
    assert control_db.continuation["delivery_attempts"] == 0
    assert control_db.continuation["locked_by"] is None
    assert control_db.events[-1]["event_type"] == "turn/detachedControlReleased"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda db: db.calls.append(dict(db.calls[0], call_id="call-duplicate")), "duplicate_control"),
        (lambda db: db.continuation.update(state="active_turn"), "continuation_mismatch"),
        (
            lambda db: db.messages.append({"message_id": "msg-extra", "state": "queued"}),
            "additional_nonterminal_traffic",
        ),
        (lambda db: db.source.update(target_role="engineering"), "source_identity_mismatch"),
    ],
)
def test_detached_control_rejects_unsafe_database_state(control_db, mutation, reason) -> None:
    mutation(control_db)

    result = _evaluate(control_db)

    assert result.requested is True
    assert result.authorized is False
    assert result.reason == reason


def test_detached_control_rejects_dispatcher_drift(control_db) -> None:
    lease_path = Path(json.loads(control_db.calls[0]["payload_json"])["detached_turn_control"]["lease_path"])
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    lease["dispatcher_hold_id"] = "different-dispatcher"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")

    result = _evaluate(control_db)

    assert result.authorized is False
    assert result.reason == "dispatcher_or_process_mismatch"


def test_detached_control_rejects_process_loss(control_db) -> None:
    fcntl.flock(control_db.lease_handle.fileno(), fcntl.LOCK_UN)

    result = _evaluate(control_db)

    assert result.authorized is False
    assert result.reason == "detached_process_lost"


def test_detached_control_rejects_expired_attestation(control_db) -> None:
    expires_at = datetime.fromisoformat(
        json.loads(control_db.calls[0]["payload_json"])["detached_turn_control"]["expires_at"]
    )

    result = evaluate_detached_turn_control(
        db=control_db,
        role_instance_id=ROLE_INSTANCE_ID,
        message_id=SOURCE_MESSAGE_ID,
        turn_id=TURN_ID,
        now=expires_at + timedelta(seconds=1),
    )

    assert result.authorized is False
    assert result.reason == "expired_control"


def test_rejected_detached_control_keeps_waiting_turn_active(control_db) -> None:
    control_db.continuation["delivery_attempts"] = 1
    runtime = V4Runtime(db=control_db, project_config=load_project_config(PROJECT_CONFIG))

    with pytest.raises(AgentTurnStillRunning):
        runtime._drain_available_events(
            client=CodexAppServerClient(_TimeoutTransport()),
            role_instance_id=ROLE_INSTANCE_ID,
            approval_policy="never",
            thread_id="thread-platform-control",
            turn_id=TURN_ID,
            message_id=SOURCE_MESSAGE_ID,
        )

    assert control_db.events[-1]["event_type"] == "turn/detachedControlRejected"
    assert control_db.events[-1]["content"] == "continuation_mismatch"


def test_failed_interrupt_keeps_waiting_turn_active(control_db) -> None:
    client = CodexAppServerClient(_FailedInterruptTransport())
    client.initialized = True
    runtime = V4Runtime(db=control_db, project_config=load_project_config(PROJECT_CONFIG))

    with pytest.raises(AgentTurnStillRunning, match="could not interrupt"):
        runtime._drain_available_events(
            client=client,
            role_instance_id=ROLE_INSTANCE_ID,
            approval_policy="never",
            thread_id="thread-platform-control",
            turn_id=TURN_ID,
            message_id=SOURCE_MESSAGE_ID,
        )

    assert control_db.events[-1]["event_type"] == "turn/detachedControlRejected"
    assert control_db.events[-1]["content"] == "interrupt_failed"
    assert not any(event["event_type"] == "turn/detachedControlReleased" for event in control_db.events)


def test_database_wait_cycle_terminates_then_exact_cancellation_reaches_strict_zero(tmp_path: Path) -> None:
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(_TimeoutTransport()),
    )
    runtime.register_roles()
    db.upsert_work_item(
        work_item_id=WORK_ITEM_ID,
        title="Single shared-fleet migration",
        state="runtime_control_repair",
        owner_role="platform-engineer",
        next_action="Run the bounded detached control sequence.",
    )
    source_message_id = runtime.enqueue_conversation(
        target_role="platform-engineer",
        text="Run the bounded detached control sequence.",
        source="safe-output",
        payload={"work_item_id": WORK_ITEM_ID, "from_role": "project-manager"},
    )
    db.claim_next_message(role_id="platform-engineer", worker_id=ROLE_INSTANCE_ID)
    db.mark_message_state(source_message_id, state="active_turn", summary="Platform control is waiting.")
    with db.connection:
        db.connection.execute(
            """
            UPDATE role_instances
            SET active_thread_id='thread-platform-control', active_turn_id=?, state='active'
            WHERE role_instance_id=?
            """,
            (TURN_ID, ROLE_INSTANCE_ID),
        )
        db.connection.execute(
            """
            INSERT INTO codex_turns(turn_id, thread_id, message_id, status, started_at)
            VALUES(?,?,?,?,?)
            """,
            (TURN_ID, "thread-platform-control", source_message_id, "active", datetime.now(UTC).isoformat()),
        )

    handoff = create_handoff(
        db=db,
        from_role="platform-engineer",
        to_role="project-manager",
        work_item_id=WORK_ITEM_ID,
        state="runtime_control_review",
        next_action="Review the bounded runtime-control result.",
        reason="Queue the exact continuation before releasing the initiating turn.",
        source_message_id=source_message_id,
        safe_output_call_id="call-control",
    )
    now = datetime.now(UTC)
    expires_at = (now + timedelta(minutes=5)).isoformat()
    lease_path = tmp_path / "live-detached-control.lease"
    lease_path.write_text(
        json.dumps(
            {
                "schema": CONTROL_SCHEMA,
                "state": "waiting",
                "dispatcher_state": "held",
                "dispatcher_hold_id": "dispatcher-hold-1",
                "process_id": "detached-process-1",
                "message_id": source_message_id,
                "turn_id": TURN_ID,
                "work_item_id": WORK_ITEM_ID,
                "expires_at": expires_at,
            }
        ),
        encoding="utf-8",
    )
    with lease_path.open("r", encoding="utf-8") as lease_handle:
        fcntl.flock(lease_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        db.record_safe_output_call(
            role_instance_id=ROLE_INSTANCE_ID,
            tool_name="handoff.require",
            payload={
                "work_item_id": WORK_ITEM_ID,
                "from_role": "platform-engineer",
                "to_role": "project-manager",
                "handoff_id": handoff["handoff_id"],
                "detached_turn_control": {
                    "lease_path": str(lease_path),
                    "process_id": "detached-process-1",
                    "dispatcher_hold_id": "dispatcher-hold-1",
                    "expires_at": expires_at,
                    "role_instance_id": ROLE_INSTANCE_ID,
                },
            },
            call_id="call-control",
            message_id=source_message_id,
            turn_id=TURN_ID,
            work_item_id=WORK_ITEM_ID,
        )

        result = runtime.dispatch_once(project_id=config.project_id, role_id="platform-engineer")

        assert result is not None
        assert result.state == "completed"
        source = db.connection.execute(
            "SELECT state FROM message_queue WHERE message_id=?",
            (source_message_id,),
        ).fetchone()
        target = db.connection.execute(
            "SELECT state, delivery_attempts, locked_by, locked_at FROM message_queue WHERE message_id=?",
            (handoff["message_id"],),
        ).fetchone()
        assert source["state"] == "completed"
        assert dict(target) == {
            "state": "queued",
            "delivery_attempts": 0,
            "locked_by": None,
            "locked_at": None,
        }

    cancel_handoff(
        db=db,
        handoff_id=handoff["handoff_id"],
        actor_role="platform-engineer",
        reason="Detached process observed the initiating turn terminal.",
        owner_role="platform-engineer",
        next_action="Evaluate strict bilateral zero.",
        safe_output_call_id="call-cancel",
    )
    target_state = db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (handoff["message_id"],),
    ).fetchone()["state"]
    guard = evaluate_migration_guard(
        agentic_mesh_messages=(
            MigrationTraffic(source_message_id, "safe-output", "platform-engineer", "completed", WORK_ITEM_ID),
            MigrationTraffic(handoff["message_id"], "safe-output", "project-manager", target_state, WORK_ITEM_ID),
        ),
        quantauma_messages=(),
        expected_work_item_id=WORK_ITEM_ID,
    )

    assert target_state == "cancelled"
    assert guard.allowed is True
    assert guard.reason == "migration_exclusive"
