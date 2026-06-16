from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from agentic_mesh_v3.governance import GovernanceChecklist
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import evaluate_governance_checklist
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.reporting import ApprovalStatus
from agentic_mesh_v3.reporting import ArtifactStatus
from agentic_mesh_v3.reporting import BacklogItemStatus
from agentic_mesh_v3.reporting import GovernanceRecordStatus
from agentic_mesh_v3.reporting import ReleaseStatus
from agentic_mesh_v3.reporting import ReportingSnapshot
from agentic_mesh_v3.reporting import WorkItemDetail
from agentic_mesh_v3.reporting import WorkItemStatus
from agentic_mesh_v3.state_machine import StateTransition
from agentic_mesh_v3.state_machine import TERMINAL_STATES
from agentic_mesh_v3.state_machine import validate_state
from agentic_mesh_v3.state_machine import validate_transition


SCHEMA_VERSION = 1
ATTENTION_STATES = {"blocked", "waiting_human", "waiting_agent", "waiting_external", "recovering"}
STATUS_STALE_AFTER_SECONDS = 3600


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
                  dead_letter_depth INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS role_memory (
                  memory_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  source_ref TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(role_instance_id, summary, source_ref)
                );

                CREATE TABLE IF NOT EXISTS conversations (
                  message_id TEXT PRIMARY KEY,
                  connector TEXT NOT NULL,
                  conversation_ref TEXT NOT NULL,
                  thread_ref TEXT,
                  source_type TEXT NOT NULL,
                  sender_ref TEXT NOT NULL,
                  text TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS governance_records (
                  record_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  record_type TEXT NOT NULL,
                  role_instance_id TEXT NOT NULL,
                  target_ref TEXT,
                  summary TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                INSERT OR IGNORE INTO schema_migrations(version) VALUES (1);
                """
            )
            _ensure_column(self.connection, "agents", "dead_letter_depth", "INTEGER NOT NULL DEFAULT 0")

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
        existing = self.connection.execute(
            "SELECT state FROM work_items WHERE work_item_id=?",
            (work_item_id,),
        ).fetchone()
        if existing is None:
            validate_state(state)
        else:
            validate_transition(
                StateTransition(
                    work_item_id=work_item_id,
                    from_state=existing["state"],
                    to_state=state,
                    reason="upsert",
                )
            )
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
            self._sync_linked_backlog_item_state(work_item_id=work_item_id, state=state)
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
        existing = self.connection.execute(
            "SELECT state FROM work_items WHERE work_item_id=?",
            (work_item_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"work item `{work_item_id}` was not found")
        validate_transition(
            StateTransition(
                work_item_id=work_item_id,
                from_state=existing["state"],
                to_state=state,
                reason=next_action,
            )
        )
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
            self._sync_linked_backlog_item_state(work_item_id=work_item_id, state=state)
            self.record_event(
                "work_item.state_updated",
                "work_item",
                work_item_id,
                {"state": state, "owner_role": owner_role, "current_phase": current_phase, "next_action": next_action},
            )

    def update_work_item_progress(
        self,
        *,
        work_item_id: str,
        next_action: str,
        owner_role: str | None = None,
        current_phase: str | None = None,
    ) -> None:
        existing = self.connection.execute(
            "SELECT state FROM work_items WHERE work_item_id=?",
            (work_item_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"work item `{work_item_id}` was not found")
        with self.connection:
            self.connection.execute(
                """
                UPDATE work_items SET
                  owner_role=COALESCE(?, owner_role),
                  current_phase=COALESCE(?, current_phase),
                  next_action=?,
                  updated_at=CURRENT_TIMESTAMP
                WHERE work_item_id=?
                """,
                (owner_role, current_phase, next_action, work_item_id),
            )
            self.record_event(
                "work_item.progress_updated",
                "work_item",
                work_item_id,
                {"owner_role": owner_role, "current_phase": current_phase, "next_action": next_action},
            )

    def _sync_linked_backlog_item_state(self, *, work_item_id: str, state: str) -> None:
        if state not in TERMINAL_STATES:
            return
        row = self.connection.execute(
            """
            SELECT queue_item_id
            FROM backlog_items
            WHERE linked_work_item_id=?
            """,
            (work_item_id,),
        ).fetchone()
        if row is None:
            return
        self.connection.execute(
            """
            UPDATE backlog_items SET status=?, updated_at=CURRENT_TIMESTAMP
            WHERE queue_item_id=?
            """,
            (state, row["queue_item_id"]),
        )
        self.record_event(
            "backlog_item.state_synced",
            "backlog_item",
            row["queue_item_id"],
            {"linked_work_item_id": work_item_id, "status": state},
        )

    def upsert_agent_status(self, status: AgentStatus) -> None:
        role_id = status.role_instance_id.split(".")[-2] if "." in status.role_instance_id else status.role_instance_id
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agents(
                  role_instance_id, role_id, container_state, heartbeat_at, current_work,
                  inbox_depth, dead_letter_depth, governance_waits_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(role_instance_id) DO UPDATE SET
                  role_id=excluded.role_id,
                  container_state=excluded.container_state,
                  heartbeat_at=excluded.heartbeat_at,
                  current_work=excluded.current_work,
                  inbox_depth=excluded.inbox_depth,
                  dead_letter_depth=excluded.dead_letter_depth,
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
                    status.dead_letter_depth,
                    json.dumps(list(status.governance_waits)),
                ),
            )
            self.record_event("agent.status_updated", "agent", status.role_instance_id, asdict(status))

    def record_agent_lifecycle_result(
        self,
        *,
        role_instance_id: str,
        action: str,
        reason: str,
        service_name: str,
        command: tuple[str, ...],
        working_directory: str | None,
        exit_code: int | None,
        stdout: str = "",
        stderr: str = "",
        executed: bool = False,
    ) -> None:
        payload = {
            "action": action,
            "reason": reason,
            "service_name": service_name,
            "command": list(command),
            "working_directory": working_directory,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "executed": executed,
        }
        with self.connection:
            self.record_event(
                "agent.lifecycle_action_recorded",
                "agent",
                role_instance_id,
                payload,
            )
        if not executed:
            return

        status_state = _container_state_for_lifecycle_result(action=action, exit_code=exit_code)
        existing = self.connection.execute(
            """
            SELECT inbox_depth, dead_letter_depth, governance_waits_json
            FROM agents
            WHERE role_instance_id=?
            """,
            (role_instance_id,),
        ).fetchone()
        inbox_depth = int(existing["inbox_depth"]) if existing else 0
        dead_letter_depth = int(existing["dead_letter_depth"]) if existing else 0
        governance_waits: tuple[str, ...] = ()
        if status_state == "lifecycle_failed":
            failure_detail = stderr.strip() or stdout.strip() or f"{action} exited with {exit_code}"
            governance_waits = (f"Lifecycle {action} failed for {service_name}: {failure_detail}",)
        elif existing:
            governance_waits = tuple(json.loads(existing["governance_waits_json"] or "[]"))

        self.upsert_agent_status(
            AgentStatus(
                role_instance_id=role_instance_id,
                container_state=status_state,
                heartbeat_at=datetime.now(timezone.utc).isoformat(),
                current_work=None,
                inbox_depth=inbox_depth,
                dead_letter_depth=dead_letter_depth,
                governance_waits=governance_waits,
            )
        )

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

    def record_approval_response(
        self,
        *,
        approval_id: str,
        response: str,
        status: str,
        responder_ref: str,
    ) -> None:
        existing = self.connection.execute(
            """
            SELECT a.work_item_id, a.requested_by_role, wi.state, wi.current_phase
            FROM approvals a
            LEFT JOIN work_items wi ON wi.work_item_id = a.work_item_id
            WHERE a.approval_id=?
            """,
            (approval_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"approval `{approval_id}` was not found")
        with self.connection:
            self.connection.execute(
                """
                UPDATE approvals SET
                  status=?,
                  response=?,
                  updated_at=CURRENT_TIMESTAMP
                WHERE approval_id=?
                """,
                (status, response, approval_id),
            )
            self.record_event(
                "approval.response_recorded",
                "work_item",
                existing["work_item_id"],
                {
                    "approval_id": approval_id,
                    "status": status,
                    "response": response,
                    "responder_ref": responder_ref,
                },
            )
            if existing["state"] == "waiting_human":
                self.update_work_item_state(
                    work_item_id=existing["work_item_id"],
                    state="waiting_agent",
                    owner_role=existing["requested_by_role"],
                    current_phase=existing["current_phase"],
                    next_action=(
                        f"Approval `{approval_id}` recorded as `{status}`; "
                        f"awaiting {existing['requested_by_role']} to continue."
                    ),
                )

    def approval_detail(self, approval_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT approval_id, work_item_id, requested_by_role, question, status, response
            FROM approvals
            WHERE approval_id=?
            """,
            (approval_id,),
        ).fetchone()
        return dict(row) if row else None

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

    def has_release_disposition(self, work_item_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM releases
            WHERE work_item_id=? AND status IN ('deployed', 'no_deployment', 'released')
            """,
            (work_item_id,),
        ).fetchone()
        return bool(row and row["count"] > 0)

    def record_role_memory(
        self,
        *,
        memory_id: str,
        role_instance_id: str,
        summary: str,
        source_ref: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO role_memory(memory_id, role_instance_id, summary, source_ref)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, role_instance_id, summary, source_ref),
            )
            self.record_event(
                "role_memory.recorded",
                "role_memory",
                role_instance_id,
                {"memory_id": memory_id, "summary": summary, "source_ref": source_ref},
            )

    def record_governance_record(
        self,
        *,
        record_id: str,
        work_item_id: str,
        record_type: str,
        role_instance_id: str,
        summary: str,
        status: str = "recorded",
        target_ref: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO governance_records(
                  record_id, work_item_id, record_type, role_instance_id, target_ref,
                  summary, status, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    work_item_id,
                    record_type,
                    role_instance_id,
                    target_ref,
                    summary,
                    status,
                    json.dumps(payload or {}, sort_keys=True),
                ),
            )
            self.record_event(
                "governance.recorded",
                "work_item",
                work_item_id,
                {
                    "record_id": record_id,
                    "record_type": record_type,
                    "role_instance_id": role_instance_id,
                    "target_ref": target_ref,
                    "status": status,
                },
            )

    def list_role_memory(self, role_instance_id: str | None = None) -> list[dict[str, Any]]:
        if role_instance_id is None:
            rows = self.connection.execute(
                """
                SELECT memory_id, role_instance_id, summary, source_ref, created_at
                FROM role_memory
                ORDER BY created_at ASC, memory_id ASC
                """
            )
        else:
            rows = self.connection.execute(
                """
                SELECT memory_id, role_instance_id, summary, source_ref, created_at
                FROM role_memory
                WHERE role_instance_id=?
                ORDER BY created_at ASC, memory_id ASC
                """,
                (role_instance_id,),
            )
        return [dict(row) for row in rows]

    def record_conversation_message(
        self,
        *,
        message_id: str,
        connector: str,
        conversation_ref: str,
        source_type: str,
        sender_ref: str,
        text: str,
        thread_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO conversations(
                  message_id, connector, conversation_ref, thread_ref, source_type, sender_ref, text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, connector, conversation_ref, thread_ref, source_type, sender_ref, text),
            )
            self.record_event(
                "conversation.message_recorded",
                "conversation",
                conversation_ref,
                {
                    "message_id": message_id,
                    "connector": connector,
                    "source_type": source_type,
                    "sender_ref": sender_ref,
                    "thread_ref": thread_ref,
                },
            )

    def list_conversation_messages(self, conversation_ref: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self.connection.execute(
            """
            SELECT message_id, connector, conversation_ref, thread_ref, source_type, sender_ref, text, created_at
            FROM conversations
            WHERE conversation_ref=?
            ORDER BY created_at DESC, message_id DESC
            LIMIT ?
            """,
            (conversation_ref, limit),
        )
        return list(reversed([dict(row) for row in rows]))

    def status_snapshot(self, *, project_id: str) -> ReportingSnapshot:
        now = datetime.now(timezone.utc)
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
                updated_at=row["updated_at"],
                attention_reason=_work_item_attention_reason(row=dict(row), now=now),
            )
            for row in self.connection.execute(
                """
                SELECT wi.work_item_id, wi.title, wi.state, wi.owner_role, wi.next_action, wi.updated_at,
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
                updated_at=row["updated_at"],
            )
            for row in self.connection.execute(
                """
                SELECT wi.work_item_id, wi.title, wi.state, wi.owner_role, wi.next_action, wi.updated_at,
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
                dead_letter_depth=row["dead_letter_depth"],
                governance_waits=tuple(json.loads(row["governance_waits_json"] or "[]")),
                memory_count=row["memory_count"],
                last_memory_at=row["last_memory_at"],
            )
            for row in self.connection.execute(
                """
                SELECT
                  a.role_instance_id,
                  a.container_state,
                  a.heartbeat_at,
                  a.current_work,
                  a.inbox_depth,
                  a.dead_letter_depth,
                  a.governance_waits_json,
                  COUNT(rm.memory_id) AS memory_count,
                  MAX(rm.created_at) AS last_memory_at
                FROM agents a
                LEFT JOIN role_memory rm ON rm.role_instance_id = a.role_instance_id
                GROUP BY a.role_instance_id
                ORDER BY a.role_instance_id ASC
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

    def work_item_detail(self, work_item_id: str) -> WorkItemDetail | None:
        work = self.connection.execute(
            """
            SELECT work_item_id, title, description, state, owner_role, current_phase, next_action, governance_json
            FROM work_items
            WHERE work_item_id=?
            """,
            (work_item_id,),
        ).fetchone()
        if work is None:
            return None
        artifacts = tuple(
            ArtifactStatus(
                filename=row["filename"],
                title=row["title"],
                relative_path=row["relative_path"],
                document_type=row["document_type"],
                status=row["status"],
                created_by_role=row["created_by_role"],
            )
            for row in self.connection.execute(
                """
                SELECT filename, title, relative_path, document_type, status, created_by_role
                FROM artifacts
                WHERE work_item_id=?
                ORDER BY created_at ASC, filename ASC
                """,
                (work_item_id,),
            )
        )
        approvals = tuple(
            ApprovalStatus(
                approval_id=row["approval_id"],
                requested_by_role=row["requested_by_role"],
                question=row["question"],
                status=row["status"],
                response=row["response"],
            )
            for row in self.connection.execute(
                """
                SELECT approval_id, requested_by_role, question, status, response
                FROM approvals
                WHERE work_item_id=?
                ORDER BY created_at ASC, approval_id ASC
                """,
                (work_item_id,),
            )
        )
        releases = tuple(
            ReleaseStatus(
                release_id=row["release_id"],
                status=row["status"],
                scope=row["scope"],
                deployment_result=row["deployment_result"],
                rollback_plan=row["rollback_plan"],
                residual_risks=row["residual_risks"],
            )
            for row in self.connection.execute(
                """
                SELECT release_id, status, scope, deployment_result, rollback_plan, residual_risks
                FROM releases
                WHERE work_item_id=?
                ORDER BY created_at ASC, release_id ASC
                """,
                (work_item_id,),
            )
        )
        governance_records = tuple(
            GovernanceRecordStatus(
                record_id=row["record_id"],
                record_type=row["record_type"],
                role_instance_id=row["role_instance_id"],
                target_ref=row["target_ref"],
                summary=row["summary"],
                status=row["status"],
            )
            for row in self.connection.execute(
                """
                SELECT record_id, record_type, role_instance_id, target_ref, summary, status
                FROM governance_records
                WHERE work_item_id=?
                ORDER BY created_at ASC, record_id ASC
                """,
                (work_item_id,),
            )
        )
        governance = json.loads(work["governance_json"] or "{}")
        context = _governance_context_from_values(
            work_item_id=work["work_item_id"],
            governance=governance,
            current_phase=work["current_phase"],
            owner_role=work["owner_role"],
        )
        governance_checklist = evaluate_governance_checklist(
            context,
            governance_records=governance_records,
            approvals=approvals,
        )
        return WorkItemDetail(
            work_item_id=work["work_item_id"],
            title=work["title"],
            description=work["description"],
            state=work["state"],
            owner_role=work["owner_role"],
            current_phase=work["current_phase"],
            next_action=work["next_action"],
            governance=governance,
            artifacts=artifacts,
            approvals=approvals,
            releases=releases,
            governance_records=governance_records,
            governance_checklist=governance_checklist,
        )

    def work_item_governance_context(self, work_item_id: str) -> GovernanceContext | None:
        detail = self.work_item_detail(work_item_id)
        if detail is None:
            return None
        return _governance_context_from_values(
            work_item_id=detail.work_item_id,
            governance=detail.governance,
            current_phase=detail.current_phase,
            owner_role=detail.owner_role,
        )

    def work_item_governance_checklist(self, work_item_id: str) -> GovernanceChecklist | None:
        detail = self.work_item_detail(work_item_id)
        if detail is None:
            return None
        return detail.governance_checklist

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


def _tuple_strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item))
    return (str(value),)


def _work_item_attention_reason(*, row: dict[str, Any], now: datetime) -> str:
    state = str(row["state"])
    if state in ATTENTION_STATES:
        return f"work item is in {state}"
    updated_at = _parse_sqlite_timestamp(str(row["updated_at"]))
    if updated_at is None:
        return "work item updated_at timestamp is unreadable"
    age = now - updated_at
    if age >= timedelta(seconds=STATUS_STALE_AFTER_SECONDS):
        return f"work item has not changed for {int(age.total_seconds())} seconds"
    return ""


def _container_state_for_lifecycle_result(*, action: str, exit_code: int | None) -> str:
    if exit_code != 0:
        return "lifecycle_failed"
    if action in {"start", "wake"}:
        return "running"
    if action == "hibernate":
        return "hibernated"
    return "unknown"


def _parse_sqlite_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _governance_context_from_values(
    *,
    work_item_id: str,
    governance: dict[str, object],
    current_phase: str | None,
    owner_role: str,
) -> GovernanceContext:
    return GovernanceContext(
        work_item_id=work_item_id,
        phase=str(governance.get("phase") or current_phase or ""),
        accountable_role=str(governance.get("accountable_role") or owner_role),
        responsible_roles=_tuple_strings(governance.get("responsible_roles")) or (owner_role,),
        consulted_roles=_tuple_strings(governance.get("consulted_roles")),
        informed_roles=_tuple_strings(governance.get("informed_roles")),
        sponsor_decision_points=_tuple_strings(governance.get("sponsor_decision_points")),
        required_evidence=_tuple_strings(governance.get("required_evidence")),
        consultation_exceptions=_tuple_strings(governance.get("consultation_exceptions")),
    )
