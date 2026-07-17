from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentic_mesh_v4.detached_turn_control as detached_turn_control
import agentic_mesh_v4.runtime as v4_runtime
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

try:
    import fcntl as _test_fcntl
except ImportError:  # pragma: no cover - collection portability for non-POSIX runners.
    _test_fcntl = None


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


class _ContinuousTransport(InMemoryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.receive_count = 0

    def receive(self):
        self.receive_count += 1
        return {
            "method": "item/agentMessage/delta",
            "params": {"delta": f"event-{self.receive_count}"},
        }


class _ContinuousFailedInterruptTransport(_ContinuousTransport):
    def send(self, message):
        if message.get("method") == "turn/interrupt":
            raise ConnectionError("app-server interrupt unavailable")
        return super().send(message)


class _EventsThenTimeoutTransport(InMemoryTransport):
    def __init__(self, events) -> None:
        super().__init__()
        self.events = list(events)

    def receive(self):
        if self.events:
            return self.events.pop(0)
        raise TimeoutError("Connection timed out")


class _BecomesAuthorizedDatabase(_Database):
    def __init__(self, *, call, lease_handle):
        super().__init__(call=call, lease_handle=lease_handle)
        self.control_checks = 0

    def list_safe_output_calls(self, **_kwargs):
        self.control_checks += 1
        return [] if self.control_checks == 1 else self.calls


@pytest.fixture
def control_db(tmp_path: Path):
    if _test_fcntl is None or not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("POSIX advisory locking and no-follow open are required for the positive lease path")
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
    _test_fcntl.flock(lease_handle.fileno(), _test_fcntl.LOCK_EX | _test_fcntl.LOCK_NB)
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


def _expire_control(db) -> None:
    payload = json.loads(db.calls[0]["payload_json"])
    expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.calls[0]["created_at"] = (expires_at - timedelta(minutes=1)).isoformat()
    payload["detached_turn_control"]["expires_at"] = expires_at.isoformat()
    db.calls[0]["payload_json"] = json.dumps(payload)


def _drift_dispatcher(db) -> None:
    lease_path = Path(json.loads(db.calls[0]["payload_json"])["detached_turn_control"]["lease_path"])
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    lease["dispatcher_hold_id"] = "different-dispatcher"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")


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


def test_continuous_events_release_when_detached_control_becomes_authorized(control_db, monkeypatch) -> None:
    db = _BecomesAuthorizedDatabase(call=control_db.calls[0], lease_handle=control_db.lease_handle)
    transport = _ContinuousTransport()
    client = CodexAppServerClient(transport)
    client.initialized = True
    clock = iter((0.0, 1.0, 2.0))
    monkeypatch.setattr(v4_runtime, "monotonic", lambda: next(clock))
    runtime = V4Runtime(db=db, project_config=load_project_config(PROJECT_CONFIG))

    reply = runtime._drain_available_events(
        client=client,
        role_instance_id=ROLE_INSTANCE_ID,
        approval_policy="never",
        thread_id="thread-platform-control",
        turn_id=TURN_ID,
        message_id=SOURCE_MESSAGE_ID,
    )

    assert reply == "event-1event-2"
    assert transport.receive_count == 2
    assert transport.sent[-1]["method"] == "turn/interrupt"
    assert db.continuation["state"] == "queued"
    assert db.continuation["delivery_attempts"] == 0
    assert [event["event_type"] for event in db.events] == [
        "item/agentMessage/delta",
        "item/agentMessage/delta",
        "turn/detachedControlReleased",
    ]


def test_continuous_events_keep_turn_active_when_interrupt_fails(control_db, monkeypatch) -> None:
    transport = _ContinuousFailedInterruptTransport()
    client = CodexAppServerClient(transport)
    client.initialized = True
    clock = iter((0.0, 1.0))
    monkeypatch.setattr(v4_runtime, "monotonic", lambda: next(clock))
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

    assert transport.receive_count == 1
    assert control_db.events[-1]["event_type"] == "turn/detachedControlRejected"
    assert control_db.events[-1]["content"] == "interrupt_failed"
    assert not any(event["event_type"] == "turn/detachedControlReleased" for event in control_db.events)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (_expire_control, "expired_control"),
        (_drift_dispatcher, "dispatcher_or_process_mismatch"),
        (lambda db: db.continuation.update(delivery_attempts=1), "continuation_mismatch"),
        (
            lambda db: db.messages.append({"message_id": "msg-extra", "state": "queued"}),
            "additional_nonterminal_traffic",
        ),
    ],
)
def test_continuous_events_keep_unsafe_control_fail_closed(control_db, monkeypatch, mutation, reason) -> None:
    mutation(control_db)
    transport = _EventsThenTimeoutTransport(
        [{"method": "item/agentMessage/delta", "params": {"delta": "still-running"}}]
    )
    clock = iter((0.0, 1.0))
    monkeypatch.setattr(v4_runtime, "monotonic", lambda: next(clock))
    runtime = V4Runtime(db=control_db, project_config=load_project_config(PROJECT_CONFIG))

    with pytest.raises(AgentTurnStillRunning):
        runtime._drain_available_events(
            client=CodexAppServerClient(transport),
            role_instance_id=ROLE_INSTANCE_ID,
            approval_policy="never",
            thread_id="thread-platform-control",
            turn_id=TURN_ID,
            message_id=SOURCE_MESSAGE_ID,
        )

    assert not any(item.get("method") == "turn/interrupt" for item in transport.sent)
    rejected = [event for event in control_db.events if event["event_type"] == "turn/detachedControlRejected"]
    assert len(rejected) == 1
    assert {event["content"] for event in rejected} == {reason}


def test_continuous_non_control_turn_processes_events_normally(monkeypatch) -> None:
    db = _Database(call={}, lease_handle=None)
    db.calls = []
    transport = _EventsThenTimeoutTransport(
        [
            {"method": "item/agentMessage/delta", "params": {"delta": "ordinary-output"}},
            {"method": "turn/completed", "params": {}},
        ]
    )
    clock = iter((0.0, 1.0))
    monkeypatch.setattr(v4_runtime, "monotonic", lambda: next(clock))
    runtime = V4Runtime(db=db, project_config=load_project_config(PROJECT_CONFIG))

    reply = runtime._drain_available_events(
        client=CodexAppServerClient(transport),
        role_instance_id="agentic-mesh-dev.engineering.1",
        approval_policy="never",
        thread_id="thread-engineering",
        turn_id="turn-engineering",
        message_id="msg-engineering",
    )

    assert reply == "ordinary-output"
    assert [event["event_type"] for event in db.events] == ["item/agentMessage/delta", "turn/completed"]
    assert not any(item.get("method") == "turn/interrupt" for item in transport.sent)


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
    _test_fcntl.flock(control_db.lease_handle.fileno(), _test_fcntl.LOCK_UN)

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


def test_detached_control_rejects_unsupported_lock_capability(control_db, monkeypatch) -> None:
    monkeypatch.setattr(detached_turn_control, "_fcntl", None)

    result = _evaluate(control_db)

    assert result.authorized is False
    assert result.reason == "unsupported_lease_security"


def test_detached_control_rejects_fcntl_without_flock(control_db, monkeypatch) -> None:
    monkeypatch.setattr(detached_turn_control, "_fcntl", SimpleNamespace())

    result = _evaluate(control_db)

    assert result.authorized is False
    assert result.reason == "unsupported_lease_security"


def test_detached_control_rejects_unsupported_no_follow_capability(control_db, monkeypatch) -> None:
    monkeypatch.delattr(detached_turn_control.os, "O_NOFOLLOW")

    result = _evaluate(control_db)

    assert result.authorized is False
    assert result.reason == "unsupported_lease_security"


def test_detached_control_rejects_group_or_other_writable_lease(control_db) -> None:
    lease_path = Path(json.loads(control_db.calls[0]["payload_json"])["detached_turn_control"]["lease_path"])
    lease_path.chmod(0o660)

    result = _evaluate(control_db)

    assert result.authorized is False
    assert result.reason == "insecure_lease_permissions"


def test_non_detached_timeout_remains_available_without_fcntl(monkeypatch) -> None:
    db = _Database(call={}, lease_handle=None)
    db.calls = []
    monkeypatch.setattr(detached_turn_control, "_fcntl", None)
    runtime = V4Runtime(db=db, project_config=load_project_config(PROJECT_CONFIG))

    with pytest.raises(AgentTurnStillRunning):
        runtime._drain_available_events(
            client=CodexAppServerClient(_TimeoutTransport()),
            role_instance_id=ROLE_INSTANCE_ID,
            approval_policy="never",
            thread_id="thread-platform-control",
            turn_id=TURN_ID,
            message_id=SOURCE_MESSAGE_ID,
        )

    assert db.events[-1]["event_type"] == "turn/readTimeoutStillRunning"


def test_runtime_import_remains_available_without_fcntl() -> None:
    code = (
        "import builtins; "
        "real_import = builtins.__import__; "
        "builtins.__import__ = lambda name, *args, **kwargs: "
        "(_ for _ in ()).throw(ImportError('blocked fcntl')) "
        "if name == 'fcntl' else real_import(name, *args, **kwargs); "
        "import agentic_mesh_v4.runtime"
    )

    result = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


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
    assert control_db.events[-1]["payload"] == {
        "reason": "continuation_mismatch",
        "handoff_id": HANDOFF_ID,
        "target_message_id": TARGET_MESSAGE_ID,
    }


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


def test_database_wait_cycle_terminates_then_exact_cancellation_reaches_strict_zero(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if _test_fcntl is None or not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("POSIX advisory locking and no-follow open are required for the positive lease path")
    db = make_v4_db()
    config = load_project_config(PROJECT_CONFIG)
    transport = _ContinuousTransport()
    runtime = V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda _role_id: CodexAppServerClient(transport),
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
        _test_fcntl.flock(lease_handle.fileno(), _test_fcntl.LOCK_EX | _test_fcntl.LOCK_NB)
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
        clock = iter((0.0, 1.0))
        monkeypatch.setattr(v4_runtime, "monotonic", lambda: next(clock))

        result = runtime.dispatch_once(project_id=config.project_id, role_id="platform-engineer")

        assert result is not None
        assert result.state == "completed"
        assert transport.receive_count == 1
        assert any(item.get("method") == "turn/interrupt" for item in transport.sent)
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
