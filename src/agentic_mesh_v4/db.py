from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4


TERMINAL_MESSAGE_STATES = {"completed", "failed", "dead_lettered", "steered"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class QueuedMessage:
    message_id: str
    source: str
    target_role: str
    state: str
    text: str
    payload: dict[str, Any]
    steering: bool
    correlation_id: str
    conversation_ref: str | None
    thread_ref: str | None
    delivery_attempts: int


class V4Database:
    def __init__(self, database_url: str | Path | None = None) -> None:
        self.database_url = _resolve_database_url(database_url)
        self.path = self.database_url
        self.connection = _PostgresConnection(self.database_url)

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
                  agent_config_hash TEXT,
                  sandbox_mode TEXT,
                  approval_policy TEXT,
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
            _ensure_column(self.connection, "codex_threads", "sandbox_mode", "TEXT")
            _ensure_column(self.connection, "codex_threads", "approval_policy", "TEXT")
            _ensure_column(self.connection, "codex_threads", "agent_config_hash", "TEXT")
            self.connection.execute("ALTER TABLE role_instances ALTER COLUMN inbox_depth SET DEFAULT 0")
            self.connection.execute("ALTER TABLE role_instances ALTER COLUMN memory_version SET DEFAULT 0")
            self.connection.execute("ALTER TABLE message_queue ALTER COLUMN steering SET DEFAULT 0")
            self.connection.execute("ALTER TABLE message_queue ALTER COLUMN delivery_attempts SET DEFAULT 0")

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
                  authority, codex_endpoint, inbox_depth, memory_version, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(role_instance_id) DO UPDATE SET
                  role_id=excluded.role_id,
                  display_name=excluded.display_name,
                  service_name=excluded.service_name,
                  authority=excluded.authority,
                  codex_endpoint=excluded.codex_endpoint,
                  updated_at=excluded.updated_at
                """,
                (role_instance_id, role_id, display_name, service_name, state, authority, codex_endpoint, 0, 0, now),
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
                INSERT INTO message_queue(
                  message_id, correlation_id, source, target_role, conversation_ref,
                  thread_ref, text, payload_json, state, steering, delivery_attempts, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(message_id) DO NOTHING
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
                    0,
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
                WHERE target_role=? AND state IN ('queued', 'ready')
                ORDER BY
                    steering DESC,
                    CASE WHEN source='teams' THEN 0 ELSE 1 END,
                    created_at ASC
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
                WHERE message_id=? AND state IN ('queued', 'ready')
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
            WHERE target_role=? AND state IN ('queued', 'ready')
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
            if state == "queued" or state in TERMINAL_MESSAGE_STATES:
                self.connection.execute(
                    """
                    UPDATE message_queue
                    SET state=?,
                        locked_by=NULL,
                        locked_at=NULL,
                        updated_at=?
                    WHERE message_id=?
                    """,
                    (state, now, message_id),
                )
            else:
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

    def downgrade_message_to_normal_delivery(self, message_id: str, *, summary: str) -> None:
        now = utc_now()
        with self.connection:
            row = self.connection.execute(
                "SELECT correlation_id, locked_by FROM message_queue WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row and row["locked_by"]:
                # Message is currently claimed by a dispatcher; only clear the steering
                # flag so the in-flight work is not exposed to duplicate claiming.
                self.connection.execute(
                    "UPDATE message_queue SET steering=0, updated_at=? WHERE message_id=?",
                    (now, message_id),
                )
            else:
                # Message is not yet claimed; safe to reset fully to queued state.
                self.connection.execute(
                    """
                    UPDATE message_queue
                    SET state='queued',
                        steering=0,
                        locked_by=NULL,
                        locked_at=NULL,
                        updated_at=?
                    WHERE message_id=?
                    """,
                    (now, message_id),
                )
        self.record_message_journal(
            message_id=message_id,
            correlation_id=str(row["correlation_id"] if row else f"corr-{message_id}"),
            stage="steering_downgraded",
            status="queued",
            summary=summary,
        )

    def active_message_for_role(
        self,
        *,
        target_role: str,
        conversation_ref: str | None = None,
        unscoped_only: bool = False,
    ) -> dict[str, Any] | None:
        params: list[object] = [target_role]
        where = "target_role=? AND state IN ('delivering', 'active_turn')"
        if conversation_ref:
            where += " AND conversation_ref=?"
            params.append(conversation_ref)
        elif unscoped_only:
            where += " AND conversation_ref IS NULL"
        row = self.connection.execute(
            f"""
            SELECT * FROM message_queue
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return _row_dict(row) if row is not None else None

    def requeue_active_messages_for_role(
        self,
        *,
        target_role: str,
        summary: str,
        stale_after_seconds: float | None = None,
    ) -> int:
        rows = [
            row
            for row in list(
                self.connection.execute(
                    """
                    SELECT message_id, correlation_id, locked_by, updated_at
                    FROM message_queue
                    WHERE target_role=? AND state IN ('delivering', 'active_turn')
                    ORDER BY updated_at ASC
                    """,
                    (target_role,),
                )
            )
            if stale_after_seconds is None
            or _is_stale_timestamp(str(row["updated_at"]), stale_after_seconds=stale_after_seconds)
        ]
        if not rows:
            return 0
        now = utc_now()
        with self.connection:
            for row in rows:
                self.connection.execute(
                    """
                    UPDATE message_queue
                    SET state='queued',
                        locked_by=NULL,
                        locked_at=NULL,
                        updated_at=?
                    WHERE message_id=?
                    """,
                    (now, row["message_id"]),
                )
                if row["locked_by"]:
                    self.connection.execute(
                        """
                        UPDATE role_instances
                        SET active_turn_id=NULL,
                            state='ready',
                            updated_at=?
                        WHERE role_instance_id=?
                        """,
                        (now, row["locked_by"]),
                    )
        for row in rows:
            self.record_message_journal(
                message_id=str(row["message_id"]),
                correlation_id=str(row["correlation_id"]),
                role_instance_id=str(row["locked_by"]) if row["locked_by"] else None,
                stage="recovered",
                status="queued",
                summary=summary,
            )
        return len(rows)

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
        now = utc_now()
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
                    now,
                ),
            )
            if message_id is not None:
                self.connection.execute(
                    """
                    UPDATE message_queue
                    SET updated_at=?
                    WHERE message_id=? AND state IN ('delivering', 'active_turn')
                    """,
                    (now, message_id),
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

    def upsert_work_item(
        self,
        *,
        work_item_id: str,
        title: str | None = None,
        state: str | None = None,
        owner_role: str | None = None,
        next_action: str | None = None,
    ) -> None:
        existing = self.connection.execute(
            "SELECT * FROM work_items WHERE work_item_id=?",
            (work_item_id,),
        ).fetchone()
        if existing is None and (title is None or state is None or owner_role is None):
            raise ValueError("new work items require title, state, and owner_role")
        now = utc_now()
        with self.connection:
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO work_items(
                      work_item_id, title, state, owner_role, next_action, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (work_item_id, title, state, owner_role, next_action or "", now, now),
                )
                return
            self.connection.execute(
                """
                UPDATE work_items
                SET title=?,
                    state=?,
                    owner_role=?,
                    next_action=?,
                    updated_at=?
                WHERE work_item_id=?
                """,
                (
                    title if title is not None else existing["title"],
                    state if state is not None else existing["state"],
                    owner_role if owner_role is not None else existing["owner_role"],
                    next_action if next_action is not None else existing["next_action"],
                    now,
                    work_item_id,
                ),
            )

    def record_artifact(
        self,
        *,
        work_item_id: str,
        path: str,
        title: str,
        artifact_id: str | None = None,
    ) -> str:
        artifact_id = artifact_id or f"artifact-{uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO artifacts(artifact_id, work_item_id, path, title, created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(artifact_id) DO NOTHING
                """,
                (artifact_id, work_item_id, path, title, utc_now()),
            )
        return artifact_id

    def record_handoff(
        self,
        *,
        from_role: str,
        to_role: str,
        reason: str,
        work_item_id: str | None = None,
        handoff_id: str | None = None,
        status: str = "open",
    ) -> str:
        handoff_id = handoff_id or f"handoff-{uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO handoffs(handoff_id, work_item_id, from_role, to_role, reason, status, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (handoff_id, work_item_id, from_role, to_role, reason, status, utc_now()),
            )
        return handoff_id

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
            self.connection.execute(
                """
                UPDATE role_instances
                SET memory_version=memory_version + 1,
                    updated_at=?
                WHERE role_instance_id=?
                """,
                (utc_now(), role_instance_id),
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

    def list_agent_events_for_role(self, *, role_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM agent_events
            WHERE role_instance_id LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (f"%.{role_id}.%", limit),
        )
        return [_row_dict(row) for row in rows]

    def list_messages_for_role(self, *, role_id: str, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM message_queue
            WHERE target_role=?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (role_id, limit),
        )
        return [_row_dict(row) for row in rows]

    def snapshot(self) -> dict[str, Any]:
        roles = [_row_dict(row) for row in self.connection.execute("SELECT * FROM role_instances ORDER BY role_id")]
        messages = self.list_messages()
        work_items_by_id = {
            str(row["work_item_id"]): _row_dict(row)
            for row in self.connection.execute("SELECT * FROM work_items")
        }
        for item in messages:
            _annotate_message_display_state(item, work_items_by_id=work_items_by_id)
        memory_counts = {
            str(row["role_instance_id"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT role_instance_id, COUNT(*) AS count FROM role_memory GROUP BY role_instance_id"
            )
        }
        active_messages: dict[str, dict[str, Any]] = {}
        for item in messages:
            if item["state"] not in {"delivering", "active_turn"}:
                continue
            locked_by = item.get("locked_by")
            if not locked_by:
                continue
            active_messages[str(locked_by)] = {
                "message_id": item["message_id"],
                "state": item["state"],
                "display_state": item.get("display_state") or item["state"],
                "display_reason": item.get("display_reason") or "",
                "text": item["text"],
                "updated_at": item["updated_at"],
            }
        queued_counts: dict[str, int] = {}
        for item in messages:
            if item["state"] in {"queued", "ready"}:
                role_id = str(item["target_role"])
                queued_counts[role_id] = queued_counts.get(role_id, 0) + 1
        for role in roles:
            role_instance_id = str(role["role_instance_id"])
            role["memory_count"] = memory_counts.get(role_instance_id, 0)
            role["queued_messages"] = queued_counts.get(str(role["role_id"]), 0)
            active_message = active_messages.get(role_instance_id)
            if active_message is not None:
                role["current_message"] = active_message
                role["effective_state"] = "busy"
            else:
                role["current_message"] = None
                role["effective_state"] = role["state"]
        events = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM agent_events ORDER BY created_at DESC LIMIT 50"
            )
        ]
        work_items = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM work_items ORDER BY updated_at DESC"
            )
        ]
        artifacts = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM artifacts ORDER BY created_at DESC"
            )
        ]
        return {
            "roles": roles,
            "messages": messages,
            "work_items": work_items,
            "artifacts": artifacts,
            "events": events,
            "queue_depth": sum(1 for item in messages if item["state"] not in TERMINAL_MESSAGE_STATES),
        }


def _annotate_message_display_state(
    item: dict[str, Any],
    *,
    work_items_by_id: dict[str, dict[str, Any]],
) -> None:
    item["display_state"] = item.get("state")
    item["display_reason"] = ""
    if item.get("state") not in {"delivering", "active_turn"}:
        return
    payload = _payload_dict(item.get("payload_json"))
    work_item_id = str(payload.get("work_item_id") or "")
    if not work_item_id:
        return
    work_item = work_items_by_id.get(work_item_id)
    if not work_item:
        return
    owner_role = str(work_item.get("owner_role") or "")
    target_role = str(item.get("target_role") or "")
    if owner_role and target_role and owner_role != target_role:
        item["display_state"] = "finalizing_handoff"
        item["display_reason"] = (
            f"Work item {work_item_id} has moved to {owner_role}; "
            f"{target_role} is finishing its prior turn."
        )


def _payload_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _queued_message(row: dict[str, Any], *, state: str, delivery_attempts: int) -> QueuedMessage:
    return QueuedMessage(
        message_id=str(row["message_id"]),
        source=str(row["source"]),
        target_role=str(row["target_role"]),
        state=state,
        text=str(row["text"]),
        payload=json.loads(str(row["payload_json"])),
        steering=bool(row["steering"]),
        correlation_id=str(row["correlation_id"]),
        conversation_ref=row["conversation_ref"],
        thread_ref=row["thread_ref"],
        delivery_attempts=delivery_attempts,
    )


def _payload_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _row_dict(row: dict[str, Any]) -> dict[str, Any]:
    return dict(row)


def _is_stale_timestamp(value: str, *, stale_after_seconds: float) -> bool:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return datetime.now(UTC) - timestamp >= timedelta(seconds=stale_after_seconds)


def _ensure_column(connection: _PostgresConnection, table: str, column: str, declaration: str) -> None:
    columns = {
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=? 
            """,
            (table,),
        )
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _resolve_database_url(value: str | Path | None) -> str:
    raw = str(value) if value is not None else ""
    if raw:
        if raw.endswith(".sqlite") or raw.endswith(".sqlite3") or raw.startswith("sqlite:"):
            raise ValueError("V4 no longer supports SQLite. Configure Postgres via AGENTIC_MESH_DATABASE_URL or AGENTIC_MESH_DATABASE_*.")
        if raw.startswith(("postgresql://", "postgres://")):
            return raw
    env_url = os.environ.get("AGENTIC_MESH_DATABASE_URL", "")
    if env_url:
        return env_url
    host = os.environ.get("AGENTIC_MESH_DATABASE_HOST", "postgres")
    port = os.environ.get("AGENTIC_MESH_DATABASE_PORT", "5432")
    name = os.environ.get("AGENTIC_MESH_DATABASE_NAME", "agentic_mesh_v4")
    user = os.environ.get("AGENTIC_MESH_DATABASE_USER", "agentic_mesh")
    password = os.environ.get("AGENTIC_MESH_DATABASE_PASSWORD", "")
    password_file = os.environ.get("AGENTIC_MESH_DATABASE_PASSWORD_FILE", "")
    if not password and password_file:
        password = Path(password_file).read_text(encoding="utf-8").strip()
    if not password:
        raise ValueError("AGENTIC_MESH_DATABASE_PASSWORD or AGENTIC_MESH_DATABASE_PASSWORD_FILE is required for V4 Postgres.")
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(name, safe='')}"
    )


class _PostgresConnection:
    _qmark_pattern = re.compile(r"\?")

    def __init__(self, database_url: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self._connection = psycopg.connect(database_url, row_factory=dict_row, connect_timeout=30)
        self._connection.autocommit = True

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "_PostgresConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._connection.autocommit:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        return False

    def execute(self, sql: str, params: tuple[object, ...] | list[object] = ()) -> Any:
        cursor = self._connection.execute(self._translate_sql(sql), tuple(params))
        return cursor

    def executescript(self, script: str) -> None:
        for statement in _split_sql_script(script):
            stripped = statement.strip()
            if stripped:
                self.execute(stripped)

    @classmethod
    def _translate_sql(cls, sql: str) -> str:
        return cls._qmark_pattern.sub("%s", sql)


def _split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    previous = ""
    for char in script:
        if char == "'" and not in_double and previous != "\\":
            in_single = not in_single
        elif char == '"' and not in_single and previous != "\\":
            in_double = not in_double
        if char == ";" and not in_single and not in_double:
            statements.append("".join(current))
            current = []
        else:
            current.append(char)
        previous = char
    if current:
        statements.append("".join(current))
    return statements
