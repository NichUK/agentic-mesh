from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


TERMINAL_MESSAGE_STATES = {"completed", "failed", "dead_lettered"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class QueuedMessage:
    message_id: str
    target_role: str
    state: str
    text: str
    steering: bool
    correlation_id: str
    conversation_ref: str | None
    thread_ref: str | None
    delivery_attempts: int


class V4Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS role_instances (
                  role_instance_id TEXT PRIMARY KEY,
                  role_id TEXT NOT NULL,
                  display_name TEXT NOT NULL,
                  service_name TEXT NOT NULL,
                  state TEXT NOT NULL,
                  authority TEXT NOT NULL,
                  codex_endpoint TEXT NOT NULL,
                  active_thread_id TEXT,
                  active_turn_id TEXT,
                  inbox_depth INTEGER NOT NULL DEFAULT 0,
                  memory_version INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_sessions (
                  session_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  codex_thread_id TEXT,
                  websocket_endpoint TEXT NOT NULL,
                  status TEXT NOT NULL,
                  prompt_version TEXT NOT NULL,
                  memory_version INTEGER NOT NULL DEFAULT 0,
                  last_hydrated_at TEXT,
                  last_event_cursor TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS codex_threads (
                  thread_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS codex_turns (
                  turn_id TEXT PRIMARY KEY,
                  thread_id TEXT NOT NULL,
                  message_id TEXT,
                  status TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS message_queue (
                  message_id TEXT PRIMARY KEY,
                  correlation_id TEXT NOT NULL,
                  source TEXT NOT NULL,
                  target_role TEXT NOT NULL,
                  conversation_ref TEXT,
                  thread_ref TEXT,
                  text TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  steering INTEGER NOT NULL DEFAULT 0,
                  delivery_attempts INTEGER NOT NULL DEFAULT 0,
                  locked_by TEXT,
                  locked_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_journal (
                  journal_id TEXT PRIMARY KEY,
                  message_id TEXT NOT NULL,
                  correlation_id TEXT NOT NULL,
                  role_instance_id TEXT,
                  stage TEXT NOT NULL,
                  status TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  payload_hash TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_events (
                  event_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  thread_id TEXT,
                  turn_id TEXT,
                  message_id TEXT,
                  event_type TEXT NOT NULL,
                  content TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS safe_output_calls (
                  call_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  tool_name TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  durable INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS role_memory (
                  memory_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  source_ref TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS summaries (
                  summary_id TEXT PRIMARY KEY,
                  role_instance_id TEXT,
                  source_ref TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  model TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS work_items (
                  work_item_id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  state TEXT NOT NULL,
                  owner_role TEXT NOT NULL,
                  next_action TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  path TEXT NOT NULL,
                  title TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approvals (
                  approval_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  requester_role TEXT NOT NULL,
                  status TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS handoffs (
                  handoff_id TEXT PRIMARY KEY,
                  work_item_id TEXT,
                  from_role TEXT NOT NULL,
                  to_role TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS releases (
                  release_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  deployment_result TEXT NOT NULL,
                  rollback_plan TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_message_queue_target_state
                  ON message_queue(target_role, state, created_at);
                CREATE INDEX IF NOT EXISTS idx_message_journal_message
                  ON message_journal(message_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_events_thread
                  ON agent_events(thread_id, created_at);
                """
            )

    def upsert_role_instance(
        self,
        *,
        role_instance_id: str,
        role_id: str,
        display_name: str,
        service_name: str,
        authority: str,
        codex_endpoint: str,
        state: str = "stopped",
    ) -> None:
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO role_instances(
                  role_instance_id, role_id, display_name, service_name, state,
                  authority, codex_endpoint, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(role_instance_id) DO UPDATE SET
                  role_id=excluded.role_id,
                  display_name=excluded.display_name,
                  service_name=excluded.service_name,
                  authority=excluded.authority,
                  codex_endpoint=excluded.codex_endpoint,
                  updated_at=excluded.updated_at
                """,
                (role_instance_id, role_id, display_name, service_name, state, authority, codex_endpoint, now),
            )

    def enqueue_message(
        self,
        *,
        target_role: str,
        text: str,
        source: str = "api",
        payload: dict[str, Any] | None = None,
        message_id: str | None = None,
        correlation_id: str | None = None,
        conversation_ref: str | None = None,
        thread_ref: str | None = None,
        steering: bool = False,
    ) -> str:
        now = utc_now()
        message_id = message_id or f"msg-{uuid4().hex}"
        correlation_id = correlation_id or f"corr-{message_id}"
        payload = dict(payload or {})
        payload.setdefault("text", text)
        payload.setdefault("target_role", target_role)
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO message_queue(
                  message_id, correlation_id, source, target_role, conversation_ref,
                  thread_ref, text, payload_json, state, steering, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message_id,
                    correlation_id,
                    source,
                    target_role,
                    conversation_ref,
                    thread_ref,
                    text,
                    json.dumps(payload, sort_keys=True),
                    "queued",
                    1 if steering else 0,
                    now,
                    now,
                ),
            )
        self.record_message_journal(
            message_id=message_id,
            correlation_id=correlation_id,
            stage="received",
            status="queued",
            summary=f"Queued message for {target_role}",
            payload=payload,
        )
        return message_id

    def claim_next_message(self, *, role_id: str, worker_id: str) -> QueuedMessage | None:
        now = utc_now()
        with self.connection:
            row = self.connection.execute(
                """
                SELECT * FROM message_queue
                WHERE target_role=? AND state='queued'
                ORDER BY steering DESC, created_at ASC
                LIMIT 1
                """,
                (role_id,),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                """
                UPDATE message_queue
                SET state='delivering',
                    delivery_attempts=delivery_attempts + 1,
                    locked_by=?,
                    locked_at=?,
                    updated_at=?
                WHERE message_id=? AND state='queued'
                """,
                (worker_id, now, now, row["message_id"]),
            )
        message = _queued_message(row, state="delivering", delivery_attempts=int(row["delivery_attempts"]) + 1)
        self.record_message_journal(
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            role_instance_id=worker_id,
            stage="claimed",
            status="delivering",
            summary=f"Claimed by {worker_id}",
        )
        return message

    def has_queued_messages(self, *, role_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM message_queue
            WHERE target_role=? AND state='queued'
            LIMIT 1
            """,
            (role_id,),
        ).fetchone()
        return row is not None

    def mark_message_state(self, message_id: str, *, state: str, summary: str = "") -> None:
        now = utc_now()
        with self.connection:
            row = self.connection.execute(
                "SELECT correlation_id FROM message_queue WHERE message_id=?",
                (message_id,),
            ).fetchone()
            self.connection.execute(
                "UPDATE message_queue SET state=?, updated_at=? WHERE message_id=?",
                (state, now, message_id),
            )
        self.record_message_journal(
            message_id=message_id,
            correlation_id=str(row["correlation_id"] if row else f"corr-{message_id}"),
            stage=state,
            status=state,
            summary=summary or f"Message state changed to {state}",
        )

    def record_message_journal(
        self,
        *,
        message_id: str,
        correlation_id: str,
        stage: str,
        status: str,
        summary: str,
        role_instance_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        journal_id = f"journal-{uuid4().hex}"
        payload_hash = _payload_hash(payload) if payload is not None else None
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO message_journal(
                  journal_id, message_id, correlation_id, role_instance_id,
                  stage, status, summary, payload_hash, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (journal_id, message_id, correlation_id, role_instance_id, stage, status, summary, payload_hash, utc_now()),
            )
        return journal_id

    def record_agent_event(
        self,
        *,
        role_instance_id: str,
        event_type: str,
        content: str = "",
        payload: dict[str, Any] | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> str:
        event_id = f"event-{uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agent_events(
                  event_id, role_instance_id, thread_id, turn_id, message_id,
                  event_type, content, payload_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    role_instance_id,
                    thread_id,
                    turn_id,
                    message_id,
                    event_type,
                    content,
                    json.dumps(payload or {}, sort_keys=True),
                    utc_now(),
                ),
            )
        return event_id

    def record_safe_output_call(
        self,
        *,
        role_instance_id: str,
        tool_name: str,
        payload: dict[str, Any],
        durable: bool = True,
        call_id: str | None = None,
    ) -> str:
        call_id = call_id or f"call-{uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO safe_output_calls(call_id, role_instance_id, tool_name, payload_json, durable, created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (call_id, role_instance_id, tool_name, json.dumps(payload, sort_keys=True), 1 if durable else 0, utc_now()),
            )
        return call_id

    def record_memory(self, *, role_instance_id: str, summary: str, source_ref: str) -> str:
        memory_id = f"mem-{uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO role_memory(memory_id, role_instance_id, summary, source_ref, created_at)
                VALUES(?,?,?,?,?)
                """,
                (memory_id, role_instance_id, summary, source_ref, utc_now()),
            )
        return memory_id

    def list_messages(self, *, state: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM message_queue"
        params: tuple[object, ...] = ()
        if state is not None:
            sql += " WHERE state=?"
            params = (state,)
        sql += " ORDER BY created_at ASC"
        return [_row_dict(row) for row in self.connection.execute(sql, params)]

    def snapshot(self) -> dict[str, Any]:
        roles = [_row_dict(row) for row in self.connection.execute("SELECT * FROM role_instances ORDER BY role_id")]
        messages = self.list_messages()
        events = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM agent_events ORDER BY created_at DESC LIMIT 50"
            )
        ]
        return {
            "roles": roles,
            "messages": messages,
            "events": events,
            "queue_depth": sum(1 for item in messages if item["state"] not in TERMINAL_MESSAGE_STATES),
        }


def _queued_message(row: sqlite3.Row, *, state: str, delivery_attempts: int) -> QueuedMessage:
    return QueuedMessage(
        message_id=str(row["message_id"]),
        target_role=str(row["target_role"]),
        state=state,
        text=str(row["text"]),
        steering=bool(row["steering"]),
        correlation_id=str(row["correlation_id"]),
        conversation_ref=row["conversation_ref"],
        thread_ref=row["thread_ref"],
        delivery_attempts=delivery_attempts,
    )


def _payload_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
