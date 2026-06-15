from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.reporting import BacklogItemStatus
from agentic_mesh_v3.reporting import ReportingSnapshot
from agentic_mesh_v3.reporting import WorkItemStatus


SCHEMA_VERSION = 1


class V3Database:
    """SQLite read model and operational audit store for V3.

    V3 documents remain the durable project memory. This database is the
    inspectable runtime projection used for dashboards, wake-up decisions, and
    recovery evidence.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")

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

                CREATE TABLE IF NOT EXISTS backlog_items (
                  queue_item_id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  status TEXT NOT NULL,
                  owner_role TEXT NOT NULL,
                  linked_work_item_id TEXT,
                  source_ref TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS work_items (
                  work_item_id TEXT PRIMARY KEY,
                  queue_item_id TEXT,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL,
                  state TEXT NOT NULL,
                  owner_role TEXT NOT NULL,
                  current_phase TEXT,
                  next_action TEXT NOT NULL DEFAULT '',
                  governance_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS agents (
                  role_instance_id TEXT PRIMARY KEY,
                  role_id TEXT NOT NULL,
                  container_state TEXT NOT NULL,
                  heartbeat_at TEXT,
                  current_work TEXT,
                  inbox_depth INTEGER NOT NULL DEFAULT 0,
                  governance_waits_json TEXT NOT NULL DEFAULT '[]',
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                  call_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  tool_name TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  terminal INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  filename TEXT NOT NULL,
                  title TEXT NOT NULL,
                  relative_path TEXT NOT NULL,
                  document_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_by_role TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS approvals (
                  approval_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  requested_by_role TEXT NOT NULL,
                  question TEXT NOT NULL,
                  status TEXT NOT NULL,
                  response TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS releases (
                  release_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  deployment_result TEXT NOT NULL,
                  rollback_plan TEXT NOT NULL,
                  residual_risks TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                INSERT OR IGNORE INTO schema_migrations(version) VALUES (1);
                """
            )

    def record_event(
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

    def upsert_backlog_item(
        self,
        *,
        queue_item_id: str,
        title: str,
        summary: str,
        status: str,
        owner_role: str,
        linked_work_item_id: str | None = None,
        source_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO backlog_items(queue_item_id, title, summary, status, owner_role, linked_work_item_id, source_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(queue_item_id) DO UPDATE SET
                  title=excluded.title,
                  summary=excluded.summary,
                  status=excluded.status,
                  owner_role=excluded.owner_role,
                  linked_work_item_id=excluded.linked_work_item_id,
                  source_ref=excluded.source_ref,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (queue_item_id, title, summary, status, owner_role, linked_work_item_id, source_ref),
            )
            self.record_event(
                "backlog_item.upserted",
                "backlog_item",
                queue_item_id,
                {
                    "title": title,
                    "status": status,
                    "owner_role": owner_role,
                    "linked_work_item_id": linked_work_item_id,
                },
            )

    def upsert_work_item(
        self,
        *,
        work_item_id: str,
        title: str,
        description: str,
        state: str,
        owner_role: str,
        queue_item_id: str | None = None,
        current_phase: str | None = None,
        next_action: str = "",
        governance: dict[str, Any] | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO work_items(
                  work_item_id, queue_item_id, title, description, state, owner_role,
                  current_phase, next_action, governance_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_item_id) DO UPDATE SET
                  queue_item_id=excluded.queue_item_id,
                  title=excluded.title,
                  description=excluded.description,
                  state=excluded.state,
                  owner_role=excluded.owner_role,
                  current_phase=excluded.current_phase,
                  next_action=excluded.next_action,
                  governance_json=excluded.governance_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    work_item_id,
                    queue_item_id,
                    title,
                    description,
                    state,
                    owner_role,
                    current_phase,
                    next_action,
                    json.dumps(governance or {}, sort_keys=True),
                ),
            )
            self.record_event(
                "work_item.upserted",
                "work_item",
                work_item_id,
                {"title": title, "state": state, "owner_role": owner_role, "current_phase": current_phase},
            )

    def update_work_item_state(
        self,
        *,
        work_item_id: str,
        state: str,
        owner_role: str | None = None,
        current_phase: str | None = None,
        next_action: str = "",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE work_items SET
                  state=?,
                  owner_role=COALESCE(?, owner_role),
                  current_phase=COALESCE(?, current_phase),
                  next_action=?,
                  updated_at=CURRENT_TIMESTAMP
                WHERE work_item_id=?
                """,
                (state, owner_role, current_phase, next_action, work_item_id),
            )
            self.record_event(
                "work_item.state_updated",
                "work_item",
                work_item_id,
                {"state": state, "owner_role": owner_role, "current_phase": current_phase, "next_action": next_action},
            )

    def upsert_agent_status(self, status: AgentStatus) -> None:
        role_id = status.role_instance_id.split(".")[-2] if "." in status.role_instance_id else status.role_instance_id
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agents(
                  role_instance_id, role_id, container_state, heartbeat_at, current_work,
                  inbox_depth, governance_waits_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(role_instance_id) DO UPDATE SET
                  role_id=excluded.role_id,
                  container_state=excluded.container_state,
                  heartbeat_at=excluded.heartbeat_at,
                  current_work=excluded.current_work,
                  inbox_depth=excluded.inbox_depth,
                  governance_waits_json=excluded.governance_waits_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    status.role_instance_id,
                    role_id,
                    status.container_state,
                    status.heartbeat_at,
                    status.current_work,
                    status.inbox_depth,
                    json.dumps(list(status.governance_waits)),
                ),
            )
            self.record_event("agent.status_updated", "agent", status.role_instance_id, asdict(status))

    def record_tool_call(
        self,
        *,
        call_id: str,
        role_instance_id: str,
        tool_name: str,
        payload: dict[str, Any],
        terminal: bool = False,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO tool_calls(call_id, role_instance_id, tool_name, payload_json, terminal)
                VALUES (?, ?, ?, ?, ?)
                """,
                (call_id, role_instance_id, tool_name, json.dumps(payload, sort_keys=True), int(terminal)),
            )
            self.record_event(
                "tool_call.recorded",
                "tool_call",
                call_id,
                {"role_instance_id": role_instance_id, "tool_name": tool_name, "terminal": terminal},
            )

    def add_artifact(
        self,
        *,
        artifact_id: str,
        work_item_id: str,
        filename: str,
        title: str,
        relative_path: str,
        document_type: str,
        status: str,
        created_by_role: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, work_item_id, filename, title, relative_path,
                  document_type, status, created_by_role
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                  filename=excluded.filename,
                  title=excluded.title,
                  relative_path=excluded.relative_path,
                  document_type=excluded.document_type,
                  status=excluded.status,
                  created_by_role=excluded.created_by_role
                """,
                (artifact_id, work_item_id, filename, title, relative_path, document_type, status, created_by_role),
            )
            self.record_event(
                "artifact.recorded",
                "work_item",
                work_item_id,
                {"artifact_id": artifact_id, "relative_path": relative_path, "status": status},
            )

    def request_approval(
        self,
        *,
        approval_id: str,
        work_item_id: str,
        requested_by_role: str,
        question: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO approvals(approval_id, work_item_id, requested_by_role, question, status)
                VALUES (?, ?, ?, ?, 'awaiting_response')
                """,
                (approval_id, work_item_id, requested_by_role, question),
            )
            self.record_event(
                "approval.requested",
                "work_item",
                work_item_id,
                {"approval_id": approval_id, "requested_by_role": requested_by_role, "question": question},
            )

    def record_release(
        self,
        *,
        release_id: str,
        work_item_id: str,
        status: str,
        scope: str,
        deployment_result: str,
        rollback_plan: str,
        residual_risks: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO releases(release_id, work_item_id, status, scope, deployment_result, rollback_plan, residual_risks)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (release_id, work_item_id, status, scope, deployment_result, rollback_plan, residual_risks),
            )
            self.record_event(
                "release.recorded",
                "work_item",
                work_item_id,
                {"release_id": release_id, "status": status, "deployment_result": deployment_result},
            )

    def status_snapshot(self, *, project_id: str) -> ReportingSnapshot:
        backlog = tuple(
            BacklogItemStatus(
                queue_item_id=row["queue_item_id"],
                title=row["title"],
                status=row["status"],
                owner_role=row["owner_role"],
                linked_work_item_id=row["linked_work_item_id"],
            )
            for row in self.connection.execute(
                """
                SELECT queue_item_id, title, status, owner_role, linked_work_item_id
                FROM backlog_items
                WHERE status NOT IN ('closed', 'canceled', 'superseded')
                ORDER BY updated_at ASC, queue_item_id ASC
                """
            )
        )
        active_work = tuple(
            WorkItemStatus(
                work_item_id=row["work_item_id"],
                title=row["title"],
                state=row["state"],
                owner_role=row["owner_role"],
                next_action=row["next_action"],
                artifact_count=row["artifact_count"],
            )
            for row in self.connection.execute(
                """
                SELECT wi.work_item_id, wi.title, wi.state, wi.owner_role, wi.next_action,
                       COUNT(a.artifact_id) AS artifact_count
                FROM work_items wi
                LEFT JOIN artifacts a ON a.work_item_id = wi.work_item_id
                WHERE wi.state NOT IN ('closed', 'canceled', 'superseded', 'failed_terminal')
                GROUP BY wi.work_item_id
                ORDER BY wi.updated_at ASC, wi.work_item_id ASC
                """
            )
        )
        recent_completions = tuple(
            WorkItemStatus(
                work_item_id=row["work_item_id"],
                title=row["title"],
                state=row["state"],
                owner_role=row["owner_role"],
                next_action=row["next_action"],
                artifact_count=row["artifact_count"],
            )
            for row in self.connection.execute(
                """
                SELECT wi.work_item_id, wi.title, wi.state, wi.owner_role, wi.next_action,
                       COUNT(a.artifact_id) AS artifact_count
                FROM work_items wi
                LEFT JOIN artifacts a ON a.work_item_id = wi.work_item_id
                WHERE wi.state IN ('closed', 'canceled', 'superseded', 'failed_terminal')
                GROUP BY wi.work_item_id
                ORDER BY wi.updated_at DESC, wi.work_item_id ASC
                LIMIT 20
                """
            )
        )
        agents = tuple(
            AgentStatus(
                role_instance_id=row["role_instance_id"],
                container_state=row["container_state"],
                heartbeat_at=row["heartbeat_at"],
                current_work=row["current_work"],
                inbox_depth=row["inbox_depth"],
                governance_waits=tuple(json.loads(row["governance_waits_json"] or "[]")),
            )
            for row in self.connection.execute(
                """
                SELECT role_instance_id, container_state, heartbeat_at, current_work, inbox_depth, governance_waits_json
                FROM agents
                ORDER BY role_instance_id ASC
                """
            )
        )
        return ReportingSnapshot(
            project_id=project_id,
            backlog=backlog,
            work_items=active_work,
            agents=agents,
            recent_completions=recent_completions,
        )

    def artifact_for(self, work_item_id: str, filename: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT artifact_id, work_item_id, filename, title, relative_path, document_type, status, created_by_role
            FROM artifacts
            WHERE work_item_id=? AND filename=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (work_item_id, filename),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_tool_calls(self) -> list[dict[str, Any]]:
        return [
            {
                **dict(row),
                "payload": json.loads(row["payload_json"]),
                "terminal": bool(row["terminal"]),
            }
            for row in self.connection.execute(
                """
                SELECT call_id, role_instance_id, tool_name, payload_json, terminal, created_at
                FROM tool_calls
                ORDER BY created_at ASC, call_id ASC
                """
            )
        ]
