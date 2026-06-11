from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agentic_mesh_v2.state_machine import TransitionRequest
from agentic_mesh_v2.state_machine import validate_transition


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class QueueItem:
    queue_item_id: str
    title: str
    summary: str
    status: str
    owner_role: str


@dataclass(frozen=True)
class WorkItem:
    work_item_id: str
    queue_item_id: str | None
    title: str
    description: str
    state: str
    owner_role: str


class V2Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS events (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_type TEXT NOT NULL,
                  aggregate_type TEXT NOT NULL,
                  aggregate_id TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS conversations (
                  conversation_id TEXT PRIMARY KEY,
                  connector TEXT NOT NULL,
                  external_ref TEXT NOT NULL,
                  sponsor_ref TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS queue_items (
                  queue_item_id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  status TEXT NOT NULL,
                  owner_role TEXT NOT NULL,
                  source_kind TEXT NOT NULL,
                  source_ref TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS work_items (
                  work_item_id TEXT PRIMARY KEY,
                  queue_item_id TEXT REFERENCES queue_items(queue_item_id),
                  title TEXT NOT NULL,
                  description TEXT NOT NULL,
                  state TEXT NOT NULL,
                  owner_role TEXT NOT NULL,
                  current_role TEXT,
                  current_lifecycle_state TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS work_item_attention (
                  work_item_id TEXT PRIMARY KEY REFERENCES work_items(work_item_id),
                  owner TEXT NOT NULL,
                  reason_class TEXT NOT NULL,
                  next_action TEXT NOT NULL,
                  retryable INTEGER NOT NULL,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                  run_id TEXT PRIMARY KEY,
                  role_id TEXT NOT NULL,
                  role_instance_id TEXT NOT NULL,
                  work_item_id TEXT REFERENCES work_items(work_item_id),
                  status TEXT NOT NULL,
                  heartbeat_at TEXT,
                  terminal_tool TEXT,
                  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS safe_output_calls (
                  call_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
                  tool_name TEXT NOT NULL,
                  role_id TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  terminal INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS approvals (
                  approval_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
                  requested_by_role TEXT NOT NULL,
                  question TEXT NOT NULL,
                  status TEXT NOT NULL,
                  response TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
                  path TEXT NOT NULL,
                  document_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_by_role TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(work_item_id, path)
                );

                CREATE TABLE IF NOT EXISTS releases (
                  release_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
                  status TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  commit_ref TEXT,
                  approval_ref TEXT,
                  deployment_result TEXT,
                  smoke_result TEXT,
                  rollback_plan TEXT NOT NULL,
                  residual_risks TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS role_memory (
                  memory_id TEXT PRIMARY KEY,
                  role_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  provenance_ref TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

    def append_event(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO events(event_type, aggregate_type, aggregate_id, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, aggregate_type, aggregate_id, json.dumps(payload, sort_keys=True)),
        )

    def create_queue_item(
        self,
        *,
        queue_item_id: str,
        title: str,
        summary: str,
        owner_role: str,
        source_kind: str = "conversation",
        source_ref: str | None = None,
    ) -> QueueItem:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO queue_items(queue_item_id, title, summary, status, owner_role, source_kind, source_ref)
                VALUES (?, ?, ?, 'queued', ?, ?, ?)
                """,
                (queue_item_id, title, summary, owner_role, source_kind, source_ref),
            )
            self.append_event(
                "queue_item.created",
                "queue_item",
                queue_item_id,
                {"title": title, "owner_role": owner_role, "source_kind": source_kind},
            )
        return QueueItem(queue_item_id, title, summary, "queued", owner_role)

    def promote_queue_item(
        self,
        *,
        queue_item_id: str,
        work_item_id: str,
        owner_role: str,
    ) -> WorkItem:
        row = self.connection.execute(
            "SELECT * FROM queue_items WHERE queue_item_id = ?",
            (queue_item_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown queue item `{queue_item_id}`")
        if row["status"] != "ready":
            raise ValueError("queue item must be ready before promotion")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO work_items(work_item_id, queue_item_id, title, description, state, owner_role, current_role)
                VALUES (?, ?, ?, ?, 'shaping', ?, ?)
                """,
                (
                    work_item_id,
                    queue_item_id,
                    row["title"],
                    row["summary"],
                    owner_role,
                    owner_role,
                ),
            )
            self.connection.execute(
                "UPDATE queue_items SET status = 'promoted', updated_at = CURRENT_TIMESTAMP WHERE queue_item_id = ?",
                (queue_item_id,),
            )
            self.append_event(
                "work_item.created",
                "work_item",
                work_item_id,
                {"queue_item_id": queue_item_id, "state": "shaping", "owner_role": owner_role},
            )
        return self.get_work_item(work_item_id)

    def mark_queue_ready(self, queue_item_id: str, *, actor_role: str, reason: str) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT status FROM queue_items WHERE queue_item_id = ?",
                (queue_item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown queue item `{queue_item_id}`")
            if row["status"] != "queued":
                raise ValueError("only queued items can be marked ready")
            self.connection.execute(
                "UPDATE queue_items SET status = 'ready', updated_at = CURRENT_TIMESTAMP WHERE queue_item_id = ?",
                (queue_item_id,),
            )
            self.append_event(
                "queue_item.ready",
                "queue_item",
                queue_item_id,
                {"actor_role": actor_role, "reason": reason},
            )

    def transition_work_item(self, request: TransitionRequest) -> WorkItem:
        validate_transition(request)
        current = self.get_work_item(request.work_item_id)
        if current.state != request.from_state:
            raise ValueError(
                f"work item `{request.work_item_id}` is in `{current.state}`, not `{request.from_state}`"
            )
        with self.connection:
            self.connection.execute(
                """
                UPDATE work_items
                SET state = ?, current_role = ?, updated_at = CURRENT_TIMESTAMP
                WHERE work_item_id = ?
                """,
                (request.to_state, request.owner or current.owner_role, request.work_item_id),
            )
            self.connection.execute(
                "DELETE FROM work_item_attention WHERE work_item_id = ?",
                (request.work_item_id,),
            )
            if request.owner and request.reason_class and request.next_action and request.retryable is not None:
                self.connection.execute(
                    """
                    INSERT INTO work_item_attention(work_item_id, owner, reason_class, next_action, retryable)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        request.work_item_id,
                        request.owner,
                        request.reason_class,
                        request.next_action,
                        1 if request.retryable else 0,
                    ),
                )
            self.append_event(
                "work_item.transitioned",
                "work_item",
                request.work_item_id,
                {
                    "from_state": request.from_state,
                    "to_state": request.to_state,
                    "actor_role": request.actor_role,
                    "reason": request.reason,
                },
            )
        return self.get_work_item(request.work_item_id)

    def create_run(
        self,
        *,
        run_id: str,
        role_id: str,
        role_instance_id: str,
        work_item_id: str | None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agent_runs(run_id, role_id, role_instance_id, work_item_id, status, heartbeat_at)
                VALUES (?, ?, ?, ?, 'running', CURRENT_TIMESTAMP)
                """,
                (run_id, role_id, role_instance_id, work_item_id),
            )
            self.append_event(
                "agent_run.started",
                "agent_run",
                run_id,
                {"role_id": role_id, "work_item_id": work_item_id},
            )

    def record_safe_output(
        self,
        *,
        call_id: str,
        run_id: str,
        role_id: str,
        tool_name: str,
        payload: dict[str, Any],
        terminal: bool,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO safe_output_calls(call_id, run_id, role_id, tool_name, payload_json, terminal)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (call_id, run_id, role_id, tool_name, json.dumps(payload, sort_keys=True), 1 if terminal else 0),
            )
            self.append_event(
                "safe_output.recorded",
                "agent_run",
                run_id,
                {"tool_name": tool_name, "role_id": role_id, "terminal": terminal},
            )

    def complete_run(self, run_id: str, *, status: str, terminal_tool: str | None) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, terminal_tool = ?, completed_at = CURRENT_TIMESTAMP
                WHERE run_id = ?
                """,
                (status, terminal_tool, run_id),
            )
            self.append_event(
                "agent_run.completed",
                "agent_run",
                run_id,
                {"status": status, "terminal_tool": terminal_tool},
            )

    def add_artifact(
        self,
        *,
        artifact_id: str,
        work_item_id: str,
        path: str,
        document_type: str,
        status: str,
        created_by_role: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO artifacts(artifact_id, work_item_id, path, document_type, status, created_by_role)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, work_item_id, path, document_type, status, created_by_role),
            )
            self.append_event(
                "artifact.recorded",
                "work_item",
                work_item_id,
                {"path": path, "document_type": document_type, "status": status},
            )

    def upsert_release(
        self,
        *,
        release_id: str,
        work_item_id: str,
        status: str,
        scope: str,
        rollback_plan: str,
        residual_risks: str,
        commit_ref: str | None = None,
        approval_ref: str | None = None,
        deployment_result: str | None = None,
        smoke_result: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO releases(
                  release_id, work_item_id, status, scope, commit_ref, approval_ref,
                  deployment_result, smoke_result, rollback_plan, residual_risks
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(release_id) DO UPDATE SET
                  status = excluded.status,
                  commit_ref = excluded.commit_ref,
                  approval_ref = excluded.approval_ref,
                  deployment_result = excluded.deployment_result,
                  smoke_result = excluded.smoke_result,
                  rollback_plan = excluded.rollback_plan,
                  residual_risks = excluded.residual_risks,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    release_id,
                    work_item_id,
                    status,
                    scope,
                    commit_ref,
                    approval_ref,
                    deployment_result,
                    smoke_result,
                    rollback_plan,
                    residual_risks,
                ),
            )
            self.append_event(
                "release.recorded",
                "work_item",
                work_item_id,
                {"release_id": release_id, "status": status},
            )

    def get_work_item(self, work_item_id: str) -> WorkItem:
        row = self.connection.execute(
            "SELECT * FROM work_items WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown work item `{work_item_id}`")
        return WorkItem(
            work_item_id=row["work_item_id"],
            queue_item_id=row["queue_item_id"],
            title=row["title"],
            description=row["description"],
            state=row["state"],
            owner_role=row["owner_role"],
        )

    def list_events(self, aggregate_id: str | None = None) -> list[dict[str, Any]]:
        params: Iterable[Any] = ()
        where = ""
        if aggregate_id is not None:
            where = "WHERE aggregate_id = ?"
            params = (aggregate_id,)
        rows = self.connection.execute(
            f"SELECT * FROM events {where} ORDER BY event_id",
            tuple(params),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
