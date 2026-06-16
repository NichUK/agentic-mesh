from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from agentic_mesh_v3.authority import role_from_instance
from agentic_mesh_v3.governance import GovernanceChecklist
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import evaluate_governance_checklist
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.reporting import AgentRunStatus
from agentic_mesh_v3.reporting import ApprovalStatus
from agentic_mesh_v3.reporting import ArtifactStatus
from agentic_mesh_v3.reporting import BacklogItemStatus
from agentic_mesh_v3.reporting import DeliveryStatus
from agentic_mesh_v3.reporting import GovernanceRecordStatus
from agentic_mesh_v3.reporting import ReleaseStatus
from agentic_mesh_v3.reporting import ReportingSnapshot
from agentic_mesh_v3.reporting import WorkItemDetail
from agentic_mesh_v3.reporting import WorkItemStatus
from agentic_mesh_v3.state_machine import StateTransition
from agentic_mesh_v3.state_machine import REOPEN_TARGET_STATES
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

                CREATE TABLE IF NOT EXISTS agent_runs (
                  run_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  message_id TEXT NOT NULL,
                  work_item_id TEXT,
                  subject TEXT NOT NULL,
                  status TEXT NOT NULL,
                  tool_calls_json TEXT NOT NULL DEFAULT '[]',
                  error TEXT,
                  started_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                  call_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  tool_name TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  terminal INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS outbound_deliveries (
                  delivery_id TEXT PRIMARY KEY,
                  call_id TEXT NOT NULL,
                  role_instance_id TEXT NOT NULL,
                  work_item_id TEXT,
                  purpose TEXT NOT NULL,
                  connector TEXT NOT NULL,
                  target_ref TEXT NOT NULL,
                  thread_ref TEXT,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  filename TEXT NOT NULL,
                  title TEXT NOT NULL,
                  relative_path TEXT NOT NULL,
                  url TEXT,
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
                  version_ref TEXT NOT NULL DEFAULT 'not-recorded',
                  approval_ref TEXT NOT NULL DEFAULT 'not-recorded',
                  deployment_result TEXT NOT NULL,
                  smoke_evidence TEXT NOT NULL DEFAULT 'not-recorded',
                  rollback_plan TEXT NOT NULL,
                  residual_risks TEXT NOT NULL,
                  closure_state TEXT NOT NULL DEFAULT 'open',
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
                  mentioned_roles_json TEXT NOT NULL DEFAULT '[]',
                  text TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS conversation_summaries (
                  summary_id TEXT PRIMARY KEY,
                  conversation_ref TEXT NOT NULL,
                  visibility TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  source_message_ids_json TEXT NOT NULL,
                  durable_refs_json TEXT NOT NULL DEFAULT '[]',
                  created_by_role TEXT NOT NULL,
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
            _ensure_column(
                self.connection,
                "conversations",
                "mentioned_roles_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            _ensure_column(self.connection, "conversations", "text_sha256", "TEXT")
            _ensure_column(self.connection, "conversations", "raw_expired_at", "TEXT")
            _ensure_column(self.connection, "agent_runs", "work_item_id", "TEXT")
            _ensure_column(self.connection, "releases", "version_ref", "TEXT NOT NULL DEFAULT 'not-recorded'")
            _ensure_column(self.connection, "releases", "approval_ref", "TEXT NOT NULL DEFAULT 'not-recorded'")
            _ensure_column(self.connection, "releases", "smoke_evidence", "TEXT NOT NULL DEFAULT 'not-recorded'")
            _ensure_column(self.connection, "releases", "closure_state", "TEXT NOT NULL DEFAULT 'open'")
            _ensure_column(self.connection, "artifacts", "url", "TEXT")

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
        governance: dict[str, Any] | None = None,
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
            if governance is None:
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
            else:
                self.connection.execute(
                    """
                    UPDATE work_items SET
                      state=?,
                      owner_role=COALESCE(?, owner_role),
                      current_phase=COALESCE(?, current_phase),
                      next_action=?,
                      governance_json=?,
                      updated_at=CURRENT_TIMESTAMP
                    WHERE work_item_id=?
                    """,
                    (state, owner_role, current_phase, next_action, json.dumps(governance, sort_keys=True), work_item_id),
                )
            self._sync_linked_backlog_item_state(work_item_id=work_item_id, state=state)
            self.record_event(
                "work_item.state_updated",
                "work_item",
                work_item_id,
                {
                    "state": state,
                    "owner_role": owner_role,
                    "current_phase": current_phase,
                    "next_action": next_action,
                    "governance": governance,
                },
            )

    def reopen_work_item(
        self,
        *,
        work_item_id: str,
        state: str,
        reason: str,
        owner_role: str,
        current_phase: str | None = None,
        next_action: str = "",
    ) -> None:
        if not reason.strip():
            raise ValueError("reopen reason is required")
        validate_state(state)
        if state not in REOPEN_TARGET_STATES:
            raise ValueError(f"invalid V3 work-item reopen target for {work_item_id}: {state}")
        existing = self.connection.execute(
            "SELECT state FROM work_items WHERE work_item_id=?",
            (work_item_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"work item `{work_item_id}` was not found")
        previous_state = existing["state"]
        if previous_state not in TERMINAL_STATES:
            raise ValueError(f"work item `{work_item_id}` is not terminal and cannot be reopened")
        with self.connection:
            self.connection.execute(
                """
                UPDATE work_items SET
                  state=?,
                  owner_role=?,
                  current_phase=COALESCE(?, current_phase),
                  next_action=?,
                  updated_at=CURRENT_TIMESTAMP
                WHERE work_item_id=?
                """,
                (state, owner_role, current_phase, next_action or reason, work_item_id),
            )
            self._sync_linked_backlog_item_state(work_item_id=work_item_id, state=state, include_non_terminal=True)
            self.record_event(
                "work_item.reopened",
                "work_item",
                work_item_id,
                {
                    "from_state": previous_state,
                    "state": state,
                    "owner_role": owner_role,
                    "current_phase": current_phase,
                    "reason": reason,
                    "next_action": next_action,
                },
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

    def _sync_linked_backlog_item_state(
        self,
        *,
        work_item_id: str,
        state: str,
        include_non_terminal: bool = False,
    ) -> None:
        if state not in TERMINAL_STATES and not include_non_terminal:
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

    def record_agent_run(
        self,
        *,
        run_id: str,
        role_instance_id: str,
        message_id: str,
        subject: str,
        status: str,
        work_item_id: str | None = None,
        tool_calls: tuple[str, ...] = (),
        error: str | None = None,
        started_at: str,
        completed_at: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agent_runs(
                  run_id, role_instance_id, message_id, work_item_id, subject, status,
                  tool_calls_json, error, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  role_instance_id=excluded.role_instance_id,
                  message_id=excluded.message_id,
                  work_item_id=excluded.work_item_id,
                  subject=excluded.subject,
                  status=excluded.status,
                  tool_calls_json=excluded.tool_calls_json,
                  error=excluded.error,
                  started_at=excluded.started_at,
                  completed_at=excluded.completed_at
                """,
                (
                    run_id,
                    role_instance_id,
                    message_id,
                    work_item_id,
                    subject,
                    status,
                    json.dumps(list(tool_calls), sort_keys=True),
                    error,
                    started_at,
                    completed_at,
                ),
            )
            self.record_event(
                "agent.run_recorded",
                "agent",
                role_instance_id,
                {
                    "run_id": run_id,
                    "message_id": message_id,
                    "work_item_id": work_item_id,
                    "subject": subject,
                    "status": status,
                    "tool_call_count": len(tool_calls),
                    "error": error,
                    "started_at": started_at,
                    "completed_at": completed_at,
                },
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

    def record_outbound_delivery(
        self,
        *,
        delivery_id: str,
        call_id: str,
        role_instance_id: str,
        purpose: str,
        connector: str,
        target_ref: str,
        status: str = "sent",
        work_item_id: str | None = None,
        thread_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO outbound_deliveries(
                  delivery_id, call_id, role_instance_id, work_item_id, purpose,
                  connector, target_ref, thread_ref, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_id) DO UPDATE SET
                  call_id=excluded.call_id,
                  role_instance_id=excluded.role_instance_id,
                  work_item_id=excluded.work_item_id,
                  purpose=excluded.purpose,
                  connector=excluded.connector,
                  target_ref=excluded.target_ref,
                  thread_ref=excluded.thread_ref,
                  status=excluded.status
                """,
                (
                    delivery_id,
                    call_id,
                    role_instance_id,
                    work_item_id,
                    purpose,
                    connector,
                    target_ref,
                    thread_ref,
                    status,
                ),
            )
            self.record_event(
                "outbound_delivery.recorded",
                "delivery",
                delivery_id,
                {
                    "call_id": call_id,
                    "role_instance_id": role_instance_id,
                    "work_item_id": work_item_id,
                    "purpose": purpose,
                    "connector": connector,
                    "target_ref": target_ref,
                    "thread_ref": thread_ref,
                    "status": status,
                },
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
        url: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, work_item_id, filename, title, relative_path,
                  url, document_type, status, created_by_role
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                  filename=excluded.filename,
                  title=excluded.title,
                  relative_path=excluded.relative_path,
                  url=excluded.url,
                  document_type=excluded.document_type,
                  status=excluded.status,
                  created_by_role=excluded.created_by_role
                """,
                (
                    artifact_id,
                    work_item_id,
                    filename,
                    title,
                    relative_path,
                    url,
                    document_type,
                    status,
                    created_by_role,
                ),
            )
            self.record_event(
                "artifact.recorded",
                "work_item",
                work_item_id,
                {"artifact_id": artifact_id, "relative_path": relative_path, "url": url, "status": status},
            )

    def request_approval(
        self,
        *,
        approval_id: str,
        work_item_id: str,
        requested_by_role: str,
        question: str,
        waiting_owner_role: str | None = None,
        current_phase: str | None = None,
        next_action: str | None = None,
    ) -> None:
        if waiting_owner_role is not None:
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
                    to_state="waiting_human",
                    reason=next_action or "approval requested",
                )
            )
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
            if waiting_owner_role is not None:
                self.connection.execute(
                    """
                    UPDATE work_items SET
                      state='waiting_human',
                      owner_role=?,
                      current_phase=COALESCE(?, current_phase),
                      next_action=?,
                      updated_at=CURRENT_TIMESTAMP
                    WHERE work_item_id=?
                    """,
                    (waiting_owner_role, current_phase, next_action or "", work_item_id),
                )
                self._sync_linked_backlog_item_state(work_item_id=work_item_id, state="waiting_human")
                self.record_event(
                    "work_item.state_updated",
                    "work_item",
                    work_item_id,
                    {
                        "state": "waiting_human",
                        "owner_role": waiting_owner_role,
                        "current_phase": current_phase,
                        "next_action": next_action or "",
                    },
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
        version_ref: str = "not-recorded",
        approval_ref: str = "not-recorded",
        smoke_evidence: str = "not-recorded",
        closure_state: str = "open",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO releases(
                  release_id, work_item_id, status, scope, version_ref, approval_ref,
                  deployment_result, smoke_evidence, rollback_plan, residual_risks, closure_state
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    release_id,
                    work_item_id,
                    status,
                    scope,
                    version_ref,
                    approval_ref,
                    deployment_result,
                    smoke_evidence,
                    rollback_plan,
                    residual_risks,
                    closure_state,
                ),
            )
            self.record_event(
                "release.recorded",
                "work_item",
                work_item_id,
                {"release_id": release_id, "status": status, "deployment_result": deployment_result},
            )

    def update_release_closure_state(self, *, work_item_id: str, closure_state: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE releases
                SET closure_state=?
                WHERE work_item_id=?
                """,
                (closure_state, work_item_id),
            )
            self.record_event(
                "release.closure_state_updated",
                "work_item",
                work_item_id,
                {"closure_state": closure_state},
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
                _governance_record_aggregate_type(work_item_id),
                work_item_id,
                {
                    "record_id": record_id,
                    "record_type": record_type,
                    "role_instance_id": role_instance_id,
                    "target_ref": target_ref,
                    "status": status,
                },
            )

    def record_governance_response(
        self,
        *,
        record_id: str,
        response: str,
        status: str,
        responder_ref: str,
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            """
            SELECT record_id, work_item_id, record_type, role_instance_id, target_ref,
                   summary, status, payload_json
            FROM governance_records
            WHERE record_id=?
            """,
            (record_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"governance record `{record_id}` was not found")
        payload = json.loads(existing["payload_json"] or "{}")
        payload["response"] = response
        payload["response_status"] = status
        payload["responder_ref"] = responder_ref
        payload["responded_at"] = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                UPDATE governance_records SET
                  status=?,
                  payload_json=?
                WHERE record_id=?
                """,
                (status, json.dumps(payload, sort_keys=True), record_id),
            )
            self.record_event(
                "governance.response_recorded",
                _governance_record_aggregate_type(existing["work_item_id"]),
                existing["work_item_id"],
                {
                    "record_id": record_id,
                    "record_type": existing["record_type"],
                    "status": status,
                    "response": response,
                    "responder_ref": responder_ref,
                },
            )
            work_item = self.connection.execute(
                "SELECT state, current_phase FROM work_items WHERE work_item_id=?",
                (existing["work_item_id"],),
            ).fetchone()
            if work_item is not None and work_item["state"] == "waiting_human":
                self.update_work_item_state(
                    work_item_id=existing["work_item_id"],
                    state="waiting_agent",
                    owner_role=role_from_instance_id(existing["role_instance_id"]),
                    current_phase=work_item["current_phase"],
                    next_action=(
                        f"Stakeholder response recorded for `{record_id}`; "
                        f"awaiting {role_from_instance_id(existing['role_instance_id'])} to continue."
                    ),
                )
        return {
            "record_id": existing["record_id"],
            "work_item_id": existing["work_item_id"],
            "record_type": existing["record_type"],
            "role_instance_id": existing["role_instance_id"],
            "status": status,
            "response": response,
        }

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

    def list_governance_records(self, *, record_type: str | None = None) -> list[dict[str, Any]]:
        if record_type is None:
            rows = self.connection.execute(
                """
                SELECT record_id, work_item_id, record_type, role_instance_id, target_ref,
                       summary, status, payload_json, created_at
                FROM governance_records
                ORDER BY created_at ASC, record_id ASC
                """
            )
        else:
            rows = self.connection.execute(
                """
                SELECT record_id, work_item_id, record_type, role_instance_id, target_ref,
                       summary, status, payload_json, created_at
                FROM governance_records
                WHERE record_type=?
                ORDER BY created_at ASC, record_id ASC
                """,
                (record_type,),
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
        mentioned_roles: tuple[str, ...] = (),
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO conversations(
                  message_id, connector, conversation_ref, thread_ref, source_type, sender_ref,
                  mentioned_roles_json, text, text_sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    connector,
                    conversation_ref,
                    thread_ref,
                    source_type,
                    sender_ref,
                    json.dumps(list(mentioned_roles), sort_keys=True),
                    text,
                    _sha256(text),
                ),
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
                    "mentioned_roles": list(mentioned_roles),
                },
            )

    def list_conversation_messages(self, conversation_ref: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self.connection.execute(
            """
            SELECT message_id, connector, conversation_ref, thread_ref, source_type, sender_ref,
                   mentioned_roles_json, text, text_sha256, raw_expired_at, created_at
            FROM conversations
            WHERE conversation_ref=?
            ORDER BY created_at DESC, message_id DESC
            LIMIT ?
            """,
            (conversation_ref, limit),
        )
        messages = []
        for row in reversed([dict(row) for row in rows]):
            row["mentioned_roles"] = tuple(json.loads(row.pop("mentioned_roles_json") or "[]"))
            messages.append(row)
        return messages

    def compact_conversation_context(
        self,
        *,
        summary_id: str,
        conversation_ref: str,
        visibility: str,
        summary: str,
        source_message_ids: tuple[str, ...],
        durable_refs: tuple[str, ...] = (),
        created_by_role: str,
    ) -> None:
        if visibility not in {"private", "shared", "promoted"}:
            raise ValueError("visibility must be one of private, shared, promoted")
        if not source_message_ids:
            raise ValueError("source_message_ids must not be empty")
        if not summary.strip():
            raise ValueError("summary must not be empty")
        if conversation_ref.startswith("dm:") and visibility != "private" and not durable_refs:
            raise ValueError("DM conversation summaries can only be shared or promoted with durable_refs")
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO conversation_summaries(
                  summary_id, conversation_ref, visibility, summary, source_message_ids_json,
                  durable_refs_json, created_by_role
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_id,
                    conversation_ref,
                    visibility,
                    summary,
                    json.dumps(list(source_message_ids), sort_keys=True),
                    json.dumps(list(durable_refs), sort_keys=True),
                    created_by_role,
                ),
            )
            self.record_event(
                "conversation.context_compacted",
                "conversation",
                conversation_ref,
                {
                    "summary_id": summary_id,
                    "visibility": visibility,
                    "source_message_ids": list(source_message_ids),
                    "durable_refs": list(durable_refs),
                    "created_by_role": created_by_role,
                },
            )

    def list_conversation_summaries(self, conversation_ref: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self.connection.execute(
            """
            SELECT summary_id, conversation_ref, visibility, summary, source_message_ids_json,
                   durable_refs_json, created_by_role, created_at
            FROM conversation_summaries
            WHERE conversation_ref=?
            ORDER BY created_at DESC, summary_id DESC
            LIMIT ?
            """,
            (conversation_ref, limit),
        )
        summaries = []
        for row in reversed([dict(row) for row in rows]):
            row["source_message_ids"] = tuple(json.loads(row.pop("source_message_ids_json") or "[]"))
            row["durable_refs"] = tuple(json.loads(row.pop("durable_refs_json") or "[]"))
            summaries.append(row)
        return summaries

    def expire_conversation_raw_text(
        self,
        *,
        message_id: str,
        replacement_text: str = "[expired raw conversation]",
        expired_at: str | None = None,
    ) -> None:
        existing = self.connection.execute(
            "SELECT text, text_sha256 FROM conversations WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"conversation message `{message_id}` was not found")
        digest = existing["text_sha256"] or _sha256(str(existing["text"]))
        with self.connection:
            self.connection.execute(
                """
                UPDATE conversations
                SET text=?, text_sha256=?, raw_expired_at=?
                WHERE message_id=?
                """,
                (
                    replacement_text,
                    digest,
                    expired_at or datetime.now(timezone.utc).isoformat(),
                    message_id,
                ),
            )
            self.record_event(
                "conversation.raw_expired",
                "conversation",
                message_id,
                {"message_id": message_id, "text_sha256": digest},
            )

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
                attention_reason=_work_item_attention_reason(
                    row=dict(row),
                    governance_reason=_governance_attention_reason(
                        self.work_item_governance_checklist(row["work_item_id"])
                    ),
                    now=now,
                ),
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
        latest_runs = self._latest_agent_runs()
        latest_lifecycle_actions = self._latest_agent_lifecycle_actions()
        agents = tuple(
            self._agent_status_from_row(
                row,
                latest_runs.get(row["role_instance_id"]),
                latest_lifecycle_actions.get(row["role_instance_id"]),
            )
            for row in self.connection.execute(
                """
                SELECT
                  a.role_instance_id,
                  a.container_state,
                  a.heartbeat_at,
                  a.updated_at AS last_activity_at,
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
            SELECT artifact_id, work_item_id, filename, title, relative_path, url, document_type, status, created_by_role
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
                url=row["url"],
            )
            for row in self.connection.execute(
                """
                SELECT filename, title, relative_path, url, document_type, status, created_by_role
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
                version_ref=row["version_ref"],
                approval_ref=row["approval_ref"],
                smoke_evidence=row["smoke_evidence"],
                closure_state=row["closure_state"],
            )
            for row in self.connection.execute(
                """
                SELECT release_id, status, scope, version_ref, approval_ref, deployment_result,
                       smoke_evidence, rollback_plan, residual_risks, closure_state
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
        agent_runs = tuple(
            _agent_run_status(row)
            for row in self.connection.execute(
                """
                SELECT run_id, role_instance_id, message_id, work_item_id, subject, status,
                       tool_calls_json, error, started_at, completed_at
                FROM agent_runs
                WHERE work_item_id=?
                ORDER BY completed_at ASC, run_id ASC
                """,
                (work_item_id,),
            )
        )
        deliveries = tuple(
            _delivery_status(row)
            for row in self.connection.execute(
                """
                SELECT delivery_id, call_id, role_instance_id, purpose, connector,
                       target_ref, thread_ref, status, created_at
                FROM outbound_deliveries
                WHERE work_item_id=?
                ORDER BY created_at ASC, delivery_id ASC
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
            artifact_refs=artifacts,
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
            agent_runs=agent_runs,
            deliveries=deliveries,
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

    def list_agent_runs(self, role_instance_id: str | None = None) -> list[dict[str, Any]]:
        if role_instance_id is None:
            rows = self.connection.execute(
                """
                SELECT run_id, role_instance_id, message_id, work_item_id, subject, status,
                       tool_calls_json, error, started_at, completed_at
                FROM agent_runs
                ORDER BY completed_at ASC, run_id ASC
                """
            )
        else:
            rows = self.connection.execute(
                """
                SELECT run_id, role_instance_id, message_id, work_item_id, subject, status,
                       tool_calls_json, error, started_at, completed_at
                FROM agent_runs
                WHERE role_instance_id=?
                ORDER BY completed_at ASC, run_id ASC
                """,
                (role_instance_id,),
            )
        return [_agent_run_row(row) for row in rows]

    def list_outbound_deliveries(self, work_item_id: str | None = None) -> list[dict[str, Any]]:
        if work_item_id is None:
            rows = self.connection.execute(
                """
                SELECT delivery_id, call_id, role_instance_id, work_item_id, purpose,
                       connector, target_ref, thread_ref, status, created_at
                FROM outbound_deliveries
                ORDER BY created_at ASC, delivery_id ASC
                """
            )
        else:
            rows = self.connection.execute(
                """
                SELECT delivery_id, call_id, role_instance_id, work_item_id, purpose,
                       connector, target_ref, thread_ref, status, created_at
                FROM outbound_deliveries
                WHERE work_item_id=?
                ORDER BY created_at ASC, delivery_id ASC
                """,
                (work_item_id,),
            )
        return [dict(row) for row in rows]

    def _latest_agent_runs(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.connection.execute(
            """
            SELECT run_id, role_instance_id, message_id, work_item_id, subject, status,
                   tool_calls_json, error, started_at, completed_at
            FROM agent_runs
            ORDER BY completed_at ASC, run_id ASC
            """
        ):
            latest[row["role_instance_id"]] = _agent_run_row(row)
        return latest

    def _latest_agent_lifecycle_actions(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.connection.execute(
            """
            SELECT aggregate_id, payload_json, created_at
            FROM events
            WHERE event_type='agent.lifecycle_action_recorded'
            ORDER BY created_at ASC, event_id ASC
            """
        ):
            payload = json.loads(row["payload_json"] or "{}")
            payload["created_at"] = row["created_at"]
            latest[row["aggregate_id"]] = payload
        return latest

    def _agent_status_from_row(
        self,
        row: sqlite3.Row,
        latest_run: dict[str, Any] | None,
        latest_lifecycle_action: dict[str, Any] | None,
    ) -> AgentStatus:
        return AgentStatus(
            role_instance_id=row["role_instance_id"],
            container_state=row["container_state"],
            heartbeat_at=row["heartbeat_at"],
            last_activity_at=row["last_activity_at"],
            current_work=row["current_work"],
            inbox_depth=row["inbox_depth"],
            dead_letter_depth=row["dead_letter_depth"],
            governance_waits=tuple(json.loads(row["governance_waits_json"] or "[]")),
            memory_count=row["memory_count"],
            last_memory_at=row["last_memory_at"],
            last_run_status=str(latest_run["status"]) if latest_run else None,
            last_run_at=str(latest_run["completed_at"]) if latest_run else None,
            last_run_error=str(latest_run["error"]) if latest_run and latest_run.get("error") else None,
            last_lifecycle_action=(
                str(latest_lifecycle_action["action"]) if latest_lifecycle_action and latest_lifecycle_action.get("action") else None
            ),
            last_lifecycle_reason=(
                str(latest_lifecycle_action["reason"]) if latest_lifecycle_action and latest_lifecycle_action.get("reason") else None
            ),
            last_lifecycle_service=(
                str(latest_lifecycle_action["service_name"])
                if latest_lifecycle_action and latest_lifecycle_action.get("service_name")
                else None
            ),
            last_lifecycle_exit_code=_optional_int(latest_lifecycle_action.get("exit_code") if latest_lifecycle_action else None),
            last_lifecycle_executed=(
                bool(latest_lifecycle_action["executed"])
                if latest_lifecycle_action and "executed" in latest_lifecycle_action
                else None
            ),
            last_lifecycle_at=(
                str(latest_lifecycle_action["created_at"])
                if latest_lifecycle_action and latest_lifecycle_action.get("created_at")
                else None
            ),
        )


def _tuple_strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item))
    return (str(value),)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _agent_run_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["tool_calls"] = tuple(json.loads(data.pop("tool_calls_json") or "[]"))
    return data


def _governance_record_aggregate_type(work_item_id: str) -> str:
    if work_item_id.startswith("message:"):
        return "conversation_message"
    return "work_item"


def role_from_instance_id(role_instance_id: str) -> str:
    return role_from_instance(role_instance_id)


def _agent_run_status(row: sqlite3.Row) -> AgentRunStatus:
    data = _agent_run_row(row)
    return AgentRunStatus(
        run_id=data["run_id"],
        role_instance_id=data["role_instance_id"],
        message_id=data["message_id"],
        work_item_id=data["work_item_id"],
        subject=data["subject"],
        status=data["status"],
        tool_calls=data["tool_calls"],
        error=data["error"],
        started_at=data["started_at"],
        completed_at=data["completed_at"],
    )


def _delivery_status(row: sqlite3.Row) -> DeliveryStatus:
    return DeliveryStatus(
        delivery_id=row["delivery_id"],
        call_id=row["call_id"],
        role_instance_id=row["role_instance_id"],
        purpose=row["purpose"],
        connector=row["connector"],
        target_ref=row["target_ref"],
        thread_ref=row["thread_ref"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _governance_attention_reason(checklist: GovernanceChecklist | None) -> str:
    if checklist is None or checklist.is_satisfied:
        return ""
    parts: list[str] = []
    if checklist.missing_consultations:
        parts.append(f"consultations={', '.join(checklist.missing_consultations)}")
    if checklist.missing_informed_updates:
        parts.append(f"informed_updates={', '.join(checklist.missing_informed_updates)}")
    if checklist.pending_sponsor_decisions:
        parts.append(f"sponsor_decisions={', '.join(checklist.pending_sponsor_decisions)}")
    if checklist.missing_required_evidence:
        parts.append(f"required_evidence={', '.join(checklist.missing_required_evidence)}")
    return "governance checklist has unresolved items: " + "; ".join(parts)


def _work_item_attention_reason(*, row: dict[str, Any], governance_reason: str = "", now: datetime) -> str:
    state = str(row["state"])
    if state in ATTENTION_STATES:
        return f"work item is in {state}"
    if governance_reason:
        return governance_reason
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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
