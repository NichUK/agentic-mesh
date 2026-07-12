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

from agentic_mesh_v4.flow import ARCHITECTURE_DOMAINS


COMPLETION_ATTENTION_MESSAGE_STATES = {
    "blocked",
    "blocked_preflight",
    "completed_with_missing_evidence_warning",
    "completed_with_exception",
    "completed_with_missing_output",
    "failed_preflight",
}
TERMINAL_MESSAGE_STATES = {
    "cancelled",
    "completed",
    "dead_lettered",
    "failed",
    "steered",
    "superseded",
    *COMPLETION_ATTENTION_MESSAGE_STATES,
}


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
                  message_id TEXT,
                  turn_id TEXT,
                  work_item_id TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turn_completion_diagnostics (
                  diagnostic_id TEXT PRIMARY KEY,
                  message_id TEXT NOT NULL,
                  turn_id TEXT,
                  role_instance_id TEXT NOT NULL,
                  work_item_id TEXT,
                  state TEXT NOT NULL,
                  missing_predicates_json TEXT NOT NULL,
                  observed_outputs_json TEXT NOT NULL,
                  next_action TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS role_memory (
                  memory_id TEXT PRIMARY KEY,
                  role_instance_id TEXT NOT NULL,
                  project_id TEXT,
                  role_id TEXT,
                  scope TEXT NOT NULL DEFAULT 'role',
                  summary TEXT NOT NULL,
                  source_ref TEXT NOT NULL,
                  tags_json TEXT NOT NULL DEFAULT '[]',
                  status TEXT NOT NULL DEFAULT 'active',
                  updated_at TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_memory (
                  memory_id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  source_ref TEXT NOT NULL,
                  created_by_role_instance_id TEXT,
                  created_by_role_id TEXT,
                  tags_json TEXT NOT NULL DEFAULT '[]',
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
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
                  architecture_impact TEXT NOT NULL DEFAULT 'not_assessed',
                  architecture_impact_rationale TEXT NOT NULL DEFAULT '',
                  architecture_domains_json TEXT NOT NULL DEFAULT '[]',
                  architecture_reviewer_role TEXT,
                  architecture_decision_ref TEXT,
                  architecture_conformance TEXT NOT NULL DEFAULT 'not_required',
                  architecture_conformance_rationale TEXT NOT NULL DEFAULT '',
                  architecture_conformance_decision_ref TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS architecture_governance_records (
                  record_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  record_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  rationale TEXT NOT NULL,
                  affected_domains_json TEXT NOT NULL DEFAULT '[]',
                  actor_role TEXT NOT NULL,
                  decision_ref TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  path TEXT NOT NULL,
                  title TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS document_revisions (
                  revision_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  path TEXT NOT NULL,
                  document_type TEXT NOT NULL,
                  role_instance_id TEXT NOT NULL,
                  base_sha256 TEXT,
                  previous_sha256 TEXT,
                  content_sha256 TEXT NOT NULL,
                  base_revision_id TEXT,
                  base_etag TEXT,
                  status TEXT NOT NULL,
                  safe_output_call_id TEXT,
                  message_id TEXT,
                  turn_id TEXT,
                  source_ref TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS document_merge_tasks (
                  merge_task_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  path TEXT NOT NULL,
                  owner_role TEXT NOT NULL,
                  state TEXT NOT NULL,
                  base_sha256 TEXT,
                  current_sha256 TEXT,
                  proposed_sha256 TEXT NOT NULL,
                  base_content_path TEXT NOT NULL,
                  current_content_path TEXT NOT NULL,
                  proposed_content_path TEXT NOT NULL,
                  role_instance_id TEXT NOT NULL,
                  safe_output_call_id TEXT,
                  message_id TEXT,
                  turn_id TEXT,
                  source_ref TEXT,
                  diagnostic_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS document_write_warnings (
                  warning_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  path TEXT NOT NULL,
                  warning_type TEXT NOT NULL,
                  severity TEXT NOT NULL,
                  role_instance_id TEXT NOT NULL,
                  safe_output_call_id TEXT,
                  message_id TEXT,
                  turn_id TEXT,
                  diagnostic_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS preflight_results (
                  preflight_id TEXT PRIMARY KEY,
                  message_id TEXT NOT NULL,
                  handoff_id TEXT,
                  work_item_id TEXT,
                  role_id TEXT NOT NULL,
                  role_instance_id TEXT NOT NULL,
                  service_name TEXT NOT NULL,
                  lifecycle_state TEXT,
                  required_path TEXT,
                  canonical_path TEXT,
                  probe_type TEXT NOT NULL,
                  check_name TEXT NOT NULL,
                  status TEXT NOT NULL,
                  exit_code INTEGER,
                  stdout_excerpt TEXT,
                  stderr_excerpt TEXT,
                  diagnostic_json TEXT NOT NULL,
                  remediation TEXT NOT NULL,
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

                CREATE TABLE IF NOT EXISTS decision_records (
                  decision_id TEXT PRIMARY KEY,
                  work_item_id TEXT NOT NULL,
                  requester_role TEXT NOT NULL,
                  owner_role TEXT NOT NULL,
                  authority_label TEXT NOT NULL,
                  authorized_responders_json TEXT NOT NULL,
                  decision_type TEXT NOT NULL,
                  title TEXT NOT NULL,
                  question TEXT NOT NULL,
                  options_json TEXT NOT NULL,
                  recommended_option TEXT,
                  tradeoffs_json TEXT NOT NULL,
                  source_refs_json TEXT NOT NULL,
                  affected_refs_json TEXT NOT NULL,
                  link_effects_json TEXT NOT NULL,
                  sla_due_at TEXT,
                  sla_state TEXT NOT NULL,
                  status TEXT NOT NULL,
                  selected_option TEXT,
                  rationale TEXT,
                  responder_ref TEXT,
                  resolved_at TEXT,
                  cancelled_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS decision_card_deliveries (
                  delivery_id TEXT PRIMARY KEY,
                  decision_id TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  conversation_ref TEXT,
                  activity_id TEXT,
                  card_version TEXT NOT NULL,
                  state TEXT NOT NULL,
                  error TEXT,
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS decision_callbacks (
                  callback_id TEXT PRIMARY KEY,
                  decision_id TEXT NOT NULL,
                  delivery_id TEXT,
                  responder_ref TEXT NOT NULL,
                  submitted_option TEXT NOT NULL,
                  normalized_option TEXT,
                  raw_payload_hash TEXT NOT NULL,
                  state TEXT NOT NULL,
                  error TEXT,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS decision_links (
                  link_id TEXT PRIMARY KEY,
                  decision_id TEXT NOT NULL,
                  target_type TEXT NOT NULL,
                  target_id TEXT NOT NULL,
                  link_type TEXT NOT NULL,
                  effect_summary TEXT NOT NULL,
                  effect_payload_json TEXT NOT NULL,
                  applied_at TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS handoffs (
                  handoff_id TEXT PRIMARY KEY,
                  work_item_id TEXT,
                  from_role TEXT NOT NULL,
                  to_role TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  status TEXT NOT NULL,
                  state TEXT,
                  accepted_at TEXT,
                  completed_at TEXT,
                  closed_at TEXT,
                  actor_role TEXT,
                  superseded_by_handoff_id TEXT,
                  source_message_id TEXT,
                  target_message_id TEXT,
                  next_action TEXT,
                  updated_at TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS handoff_transitions (
                  transition_id TEXT PRIMARY KEY,
                  handoff_id TEXT NOT NULL,
                  work_item_id TEXT,
                  actor_role TEXT NOT NULL,
                  from_state TEXT,
                  to_state TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  superseded_by_handoff_id TEXT,
                  message_id TEXT,
                  safe_output_call_id TEXT,
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

                CREATE TABLE IF NOT EXISTS watchdog_sweep_runs (
                  sweep_run_id TEXT PRIMARY KEY,
                  initiator_role TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  completed_at TEXT,
                  status TEXT NOT NULL,
                  summary_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watchdog_findings (
                  finding_id TEXT PRIMARY KEY,
                  sweep_run_id TEXT NOT NULL,
                  finding_key TEXT NOT NULL UNIQUE,
                  finding_type TEXT NOT NULL,
                  severity TEXT NOT NULL,
                  state TEXT NOT NULL,
                  work_item_id TEXT,
                  handoff_id TEXT,
                  message_id TEXT,
                  target_role TEXT,
                  owner_role TEXT,
                  evidence_json TEXT NOT NULL,
                  next_action TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_message_queue_target_state
                  ON message_queue(target_role, state, created_at);
                CREATE INDEX IF NOT EXISTS idx_message_journal_message
                  ON message_journal(message_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_events_thread
                  ON agent_events(thread_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_events_role_created
                  ON agent_events(role_instance_id, created_at, event_id);
                CREATE INDEX IF NOT EXISTS idx_turn_completion_diagnostics_message
                  ON turn_completion_diagnostics(message_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_turn_completion_diagnostics_work_item
                  ON turn_completion_diagnostics(work_item_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_watchdog_findings_state
                  ON watchdog_findings(state, finding_type, updated_at);
                CREATE INDEX IF NOT EXISTS idx_watchdog_findings_work_item
                  ON watchdog_findings(work_item_id, state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_decision_records_status
                  ON decision_records(status, sla_state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_decision_records_work_item
                  ON decision_records(work_item_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_decision_deliveries_decision
                  ON decision_card_deliveries(decision_id, state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_decision_callbacks_decision
                  ON decision_callbacks(decision_id, state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_decision_links_decision
                  ON decision_links(decision_id, target_type, target_id);
                CREATE INDEX IF NOT EXISTS idx_document_revisions_path
                  ON document_revisions(path, created_at);
                CREATE INDEX IF NOT EXISTS idx_document_revisions_work_item
                  ON document_revisions(work_item_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_document_merge_tasks_state
                  ON document_merge_tasks(state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_document_merge_tasks_work_item
                  ON document_merge_tasks(work_item_id, state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_document_write_warnings_work_item
                  ON document_write_warnings(work_item_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_preflight_results_message
                  ON preflight_results(message_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_preflight_results_work_item
                  ON preflight_results(work_item_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_architecture_governance_work_item
                  ON architecture_governance_records(work_item_id, record_type, created_at);
                """
            )
            _ensure_column(self.connection, "codex_threads", "sandbox_mode", "TEXT")
            _ensure_column(self.connection, "codex_threads", "approval_policy", "TEXT")
            _ensure_column(self.connection, "codex_threads", "agent_config_hash", "TEXT")
            self.connection.execute("ALTER TABLE role_instances ALTER COLUMN inbox_depth SET DEFAULT 0")
            self.connection.execute("ALTER TABLE role_instances ALTER COLUMN memory_version SET DEFAULT 0")
            self.connection.execute("ALTER TABLE message_queue ALTER COLUMN steering SET DEFAULT 0")
            self.connection.execute("ALTER TABLE message_queue ALTER COLUMN delivery_attempts SET DEFAULT 0")
            _ensure_column(self.connection, "safe_output_calls", "message_id", "TEXT")
            _ensure_column(self.connection, "safe_output_calls", "turn_id", "TEXT")
            _ensure_column(self.connection, "safe_output_calls", "work_item_id", "TEXT")
            _ensure_column(self.connection, "role_memory", "project_id", "TEXT")
            _ensure_column(self.connection, "role_memory", "role_id", "TEXT")
            _ensure_column(self.connection, "role_memory", "scope", "TEXT NOT NULL DEFAULT 'role'")
            _ensure_column(self.connection, "role_memory", "tags_json", "TEXT NOT NULL DEFAULT '[]'")
            _ensure_column(self.connection, "role_memory", "status", "TEXT NOT NULL DEFAULT 'active'")
            _ensure_column(self.connection, "role_memory", "updated_at", "TEXT")
            _ensure_column(
                self.connection,
                "work_items",
                "architecture_impact",
                "TEXT NOT NULL DEFAULT 'not_assessed'",
            )
            _ensure_column(
                self.connection,
                "work_items",
                "architecture_impact_rationale",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_column(
                self.connection,
                "work_items",
                "architecture_domains_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            _ensure_column(self.connection, "work_items", "architecture_reviewer_role", "TEXT")
            _ensure_column(self.connection, "work_items", "architecture_decision_ref", "TEXT")
            _ensure_column(
                self.connection,
                "work_items",
                "architecture_conformance",
                "TEXT NOT NULL DEFAULT 'not_required'",
            )
            _ensure_column(
                self.connection,
                "work_items",
                "architecture_conformance_rationale",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_column(self.connection, "work_items", "architecture_conformance_decision_ref", "TEXT")
            _ensure_column(self.connection, "handoffs", "state", "TEXT")
            _ensure_column(self.connection, "handoffs", "accepted_at", "TEXT")
            _ensure_column(self.connection, "handoffs", "completed_at", "TEXT")
            _ensure_column(self.connection, "handoffs", "closed_at", "TEXT")
            _ensure_column(self.connection, "handoffs", "actor_role", "TEXT")
            _ensure_column(self.connection, "handoffs", "superseded_by_handoff_id", "TEXT")
            _ensure_column(self.connection, "handoffs", "source_message_id", "TEXT")
            _ensure_column(self.connection, "handoffs", "target_message_id", "TEXT")
            _ensure_column(self.connection, "handoffs", "next_action", "TEXT")
            _ensure_column(self.connection, "handoffs", "updated_at", "TEXT")
            self.connection.execute(
                """
                UPDATE handoffs
                SET state=COALESCE(state, status),
                    updated_at=COALESCE(updated_at, created_at)
                WHERE state IS NULL OR updated_at IS NULL
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_safe_output_calls_message
                  ON safe_output_calls(message_id, turn_id, role_instance_id)
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_safe_output_calls_work_item
                  ON safe_output_calls(work_item_id, role_instance_id, created_at)
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_handoffs_active_work
                  ON handoffs(work_item_id, state, updated_at)
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_handoffs_target_state
                  ON handoffs(to_role, state, updated_at)
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_handoff_transitions_handoff
                  ON handoff_transitions(handoff_id, created_at)
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_role_memory_scope
                  ON role_memory(project_id, role_id, scope, status, updated_at)
                """
            )
            self.connection.execute(
                """
                DO $migration$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname=current_schema() AND indexname='idx_role_memory_source'
                  ) THEN
                    DELETE FROM role_memory
                    WHERE memory_id IN (
                      SELECT memory_id FROM (
                        SELECT memory_id,
                               ROW_NUMBER() OVER (
                                 PARTITION BY project_id, role_id, scope, source_ref
                                 ORDER BY updated_at DESC NULLS LAST, created_at DESC, memory_id DESC
                               ) AS duplicate_number
                        FROM role_memory
                      ) duplicate_memories
                      WHERE duplicate_number > 1
                    );
                    CREATE UNIQUE INDEX idx_role_memory_source
                      ON role_memory(project_id, role_id, scope, source_ref);
                  END IF;
                END
                $migration$
                """
            )
            self.connection.execute(
                """
                DO $migration$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname=current_schema() AND indexname='idx_artifacts_work_path'
                  ) THEN
                    DELETE FROM artifacts
                    WHERE artifact_id IN (
                      SELECT artifact_id FROM (
                        SELECT artifact_id,
                               ROW_NUMBER() OVER (
                                 PARTITION BY work_item_id, path
                                 ORDER BY created_at DESC, artifact_id DESC
                               ) AS duplicate_number
                        FROM artifacts
                      ) duplicate_artifacts
                      WHERE duplicate_number > 1
                    );
                    CREATE UNIQUE INDEX idx_artifacts_work_path
                      ON artifacts(work_item_id, path);
                  END IF;
                END
                $migration$
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_project_memory_project
                  ON project_memory(project_id, status, updated_at)
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

    def active_message_for_role(
        self,
        *,
        target_role: str,
        conversation_ref: str | None = None,
    ) -> dict[str, Any] | None:
        params: list[object] = [target_role]
        where = "target_role=? AND state IN ('delivering', 'active_turn')"
        if conversation_ref:
            where += " AND conversation_ref=?"
            params.append(conversation_ref)
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
        # Psycopg uses percent-style placeholders, so the SQL LIKE wildcard is escaped here.
        rows = [
            row
            for row in list(
                self.connection.execute(
                    """
                    SELECT m.message_id,
                           m.correlation_id,
                           m.locked_by,
                           m.locked_at,
                           m.updated_at,
                           COALESCE(
                             (
                               SELECT MAX(e.created_at)
                               FROM agent_events e
                               WHERE e.message_id=m.message_id
                                 AND e.event_type NOT LIKE 'turn/readTimeout%%'
                             ),
                             m.locked_at,
                             m.updated_at
                           ) AS last_real_activity_at
                    FROM message_queue m
                    WHERE m.target_role=? AND m.state IN ('delivering', 'active_turn')
                    ORDER BY last_real_activity_at ASC
                    """,
                    (target_role,),
                )
            )
            if stale_after_seconds is None
            or _is_stale_timestamp(str(row["last_real_activity_at"]), stale_after_seconds=stale_after_seconds)
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
        message_id: str | None = None,
        turn_id: str | None = None,
        work_item_id: str | None = None,
    ) -> str:
        call_id = call_id or f"call-{uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO safe_output_calls(
                  call_id, role_instance_id, tool_name, payload_json, durable,
                  message_id, turn_id, work_item_id, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    call_id,
                    role_instance_id,
                    tool_name,
                    json.dumps(payload, sort_keys=True),
                    1 if durable else 0,
                    message_id,
                    turn_id,
                    work_item_id,
                    utc_now(),
                ),
            )
        return call_id

    def list_safe_output_calls(
        self,
        *,
        role_instance_id: str | None = None,
        message_id: str | None = None,
        turn_id: str | None = None,
        work_item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[object] = []
        if role_instance_id is not None:
            clauses.append("role_instance_id=?")
            params.append(role_instance_id)
        if message_id is not None:
            clauses.append("message_id=?")
            params.append(message_id)
        if turn_id is not None:
            clauses.append("turn_id=?")
            params.append(turn_id)
        if work_item_id is not None:
            clauses.append("work_item_id=?")
            params.append(work_item_id)
        sql = "SELECT * FROM safe_output_calls"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        return [_row_dict(row) for row in self.connection.execute(sql, tuple(params))]

    def record_turn_completion_diagnostic(
        self,
        *,
        message_id: str,
        role_instance_id: str,
        state: str,
        missing_predicates: list[dict[str, Any]],
        observed_outputs: dict[str, Any],
        next_action: str,
        turn_id: str | None = None,
        work_item_id: str | None = None,
        diagnostic_id: str | None = None,
    ) -> str:
        if state not in COMPLETION_ATTENTION_MESSAGE_STATES:
            raise ValueError(f"unsupported completion diagnostic state: {state}")
        diagnostic_id = diagnostic_id or f"diag-{uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO turn_completion_diagnostics(
                  diagnostic_id, message_id, turn_id, role_instance_id, work_item_id,
                  state, missing_predicates_json, observed_outputs_json, next_action, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    diagnostic_id,
                    message_id,
                    turn_id,
                    role_instance_id,
                    work_item_id,
                    state,
                    json.dumps(missing_predicates, sort_keys=True),
                    json.dumps(observed_outputs, sort_keys=True),
                    next_action,
                    utc_now(),
                ),
            )
        return diagnostic_id

    def record_preflight_result(
        self,
        *,
        message_id: str,
        role_id: str,
        role_instance_id: str,
        service_name: str,
        probe_type: str,
        check_name: str,
        status: str,
        handoff_id: str | None = None,
        work_item_id: str | None = None,
        lifecycle_state: str | None = None,
        required_path: str | None = None,
        canonical_path: str | None = None,
        exit_code: int | None = None,
        stdout_excerpt: str | None = None,
        stderr_excerpt: str | None = None,
        diagnostic: dict[str, Any] | None = None,
        remediation: str = "",
        preflight_id: str | None = None,
    ) -> str:
        preflight_id = preflight_id or f"preflight-{uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO preflight_results(
                  preflight_id, message_id, handoff_id, work_item_id, role_id,
                  role_instance_id, service_name, lifecycle_state, required_path,
                  canonical_path, probe_type, check_name, status, exit_code,
                  stdout_excerpt, stderr_excerpt, diagnostic_json, remediation, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    preflight_id,
                    message_id,
                    handoff_id,
                    work_item_id,
                    role_id,
                    role_instance_id,
                    service_name,
                    lifecycle_state,
                    required_path,
                    canonical_path,
                    probe_type,
                    check_name,
                    status,
                    exit_code,
                    _excerpt(stdout_excerpt),
                    _excerpt(stderr_excerpt),
                    json.dumps(diagnostic or {}, sort_keys=True),
                    remediation,
                    utc_now(),
                ),
            )
        return preflight_id

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
        if existing is not None:
            _validate_architecture_transition(
                existing=_row_dict(existing),
                target_state=state if state is not None else str(existing["state"]),
                target_owner=owner_role if owner_role is not None else str(existing["owner_role"]),
            )
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
            row = self.connection.execute(
                """
                INSERT INTO artifacts(artifact_id, work_item_id, path, title, created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(work_item_id, path) DO UPDATE SET title=excluded.title
                RETURNING artifact_id
                """,
                (artifact_id, work_item_id, path, title, utc_now()),
            ).fetchone()
        return str(row["artifact_id"])

    def record_architecture_impact(
        self,
        *,
        work_item_id: str,
        classification: str,
        rationale: str,
        affected_domains: list[str],
        actor_role: str,
        decision_ref: str | None = None,
    ) -> str:
        allowed = {"none", "material", "uncertain"}
        if classification not in allowed:
            raise ValueError(f"unsupported architecture impact classification: {classification}")
        if not rationale.strip():
            raise ValueError("architecture impact rationale is required")
        existing = self.connection.execute(
            """
            SELECT architecture_impact, architecture_impact_rationale,
                   architecture_domains_json, architecture_reviewer_role,
                   architecture_decision_ref, architecture_conformance,
                   architecture_conformance_rationale,
                   architecture_conformance_decision_ref
            FROM work_items WHERE work_item_id=?
            """,
            (work_item_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown work item: {work_item_id}")
        domains = sorted({str(value).strip() for value in affected_domains if str(value).strip()})
        unsupported_domains = set(domains) - ARCHITECTURE_DOMAINS
        if unsupported_domains:
            raise ValueError(f"unsupported architecture domains: {sorted(unsupported_domains)}")
        if classification in {"material", "uncertain"} and not domains:
            raise ValueError("material or uncertain architecture impact requires at least one affected domain")
        normalized_rationale = rationale.strip()
        normalized_decision_ref = decision_ref or None
        existing_domains = _json_list(existing["architecture_domains_json"])
        semantic_impact_changed = (
            str(existing["architecture_impact"]) != classification
            or existing_domains != domains
            or (existing["architecture_decision_ref"] or None) != normalized_decision_ref
        )
        exact_repeat = (
            not semantic_impact_changed
            and str(existing["architecture_impact_rationale"]) == normalized_rationale
            and str(existing["architecture_reviewer_role"] or "") == actor_role
        )
        if exact_repeat:
            latest = self.connection.execute(
                """
                SELECT record_id FROM architecture_governance_records
                WHERE work_item_id=? AND record_type='impact'
                ORDER BY created_at DESC, record_id DESC LIMIT 1
                """,
                (work_item_id,),
            ).fetchone()
            if latest is not None:
                return str(latest["record_id"])
        if classification not in {"material", "uncertain"}:
            conformance = "not_required"
            conformance_rationale = ""
            conformance_decision_ref = None
        elif semantic_impact_changed:
            conformance = "pending"
            conformance_rationale = ""
            conformance_decision_ref = None
        else:
            conformance = str(existing["architecture_conformance"])
            conformance_rationale = str(existing["architecture_conformance_rationale"] or "")
            conformance_decision_ref = existing["architecture_conformance_decision_ref"]
        record_id = f"architecture-{uuid4().hex}"
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE work_items
                SET architecture_impact=?, architecture_impact_rationale=?,
                    architecture_domains_json=?, architecture_reviewer_role=?,
                    architecture_decision_ref=?, architecture_conformance=?,
                    architecture_conformance_rationale=?,
                    architecture_conformance_decision_ref=?, updated_at=?
                WHERE work_item_id=?
                """,
                (
                    classification,
                    normalized_rationale,
                    json.dumps(domains),
                    actor_role,
                    normalized_decision_ref,
                    conformance,
                    conformance_rationale,
                    conformance_decision_ref,
                    now,
                    work_item_id,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO architecture_governance_records(
                  record_id, work_item_id, record_type, status, rationale,
                  affected_domains_json, actor_role, decision_ref, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id,
                    work_item_id,
                    "impact",
                    classification,
                    normalized_rationale,
                    json.dumps(domains),
                    actor_role,
                    normalized_decision_ref,
                    now,
                ),
            )
        return record_id

    def record_architecture_conformance(
        self,
        *,
        work_item_id: str,
        status: str,
        rationale: str,
        actor_role: str,
        decision_ref: str | None = None,
    ) -> str:
        allowed = {"approved", "changes_requested", "exception"}
        if status not in allowed:
            raise ValueError(f"unsupported architecture conformance status: {status}")
        if not rationale.strip():
            raise ValueError("architecture conformance rationale is required")
        if status == "exception" and not decision_ref:
            raise ValueError("architecture conformance exceptions require a sponsor decision reference")
        existing = self.connection.execute(
            "SELECT architecture_impact FROM work_items WHERE work_item_id=?",
            (work_item_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown work item: {work_item_id}")
        if str(existing["architecture_impact"]) not in {"material", "uncertain"}:
            raise ValueError("architecture conformance is only recorded for material or uncertain impact")
        record_id = f"architecture-{uuid4().hex}"
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE work_items
                SET architecture_conformance=?, architecture_conformance_rationale=?,
                    architecture_conformance_decision_ref=?, updated_at=?
                WHERE work_item_id=?
                """,
                (status, rationale.strip(), decision_ref, now, work_item_id),
            )
            self.connection.execute(
                """
                INSERT INTO architecture_governance_records(
                  record_id, work_item_id, record_type, status, rationale,
                  affected_domains_json, actor_role, decision_ref, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id,
                    work_item_id,
                    "conformance",
                    status,
                    rationale.strip(),
                    "[]",
                    actor_role,
                    decision_ref,
                    now,
                ),
            )
        return record_id

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
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO handoffs(
                  handoff_id, work_item_id, from_role, to_role, reason, status, state, updated_at, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (handoff_id, work_item_id, from_role, to_role, reason, status, status, now, now),
            )
        return handoff_id

    def record_memory(
        self,
        *,
        role_instance_id: str,
        summary: str,
        source_ref: str,
        project_id: str | None = None,
        scope: str = "role",
        tags: list[str] | None = None,
        status: str = "active",
    ) -> str:
        if scope not in {"role", "work", "conversation"}:
            raise ValueError(f"unsupported role memory scope: {scope}")
        memory_id = f"mem-{uuid4().hex}"
        now = utc_now()
        role_id = _role_from_instance_id(role_instance_id)
        project_id = project_id or _project_from_instance_id(role_instance_id)
        with self.connection:
            row = self.connection.execute(
                """
                INSERT INTO role_memory(
                  memory_id, role_instance_id, project_id, role_id, scope,
                  summary, source_ref, tags_json, status, updated_at, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id, role_id, scope, source_ref) DO UPDATE SET
                  role_instance_id=excluded.role_instance_id,
                  summary=excluded.summary,
                  tags_json=excluded.tags_json,
                  status=excluded.status,
                  updated_at=excluded.updated_at
                RETURNING memory_id
                """,
                (
                    memory_id,
                    role_instance_id,
                    project_id,
                    role_id,
                    scope,
                    summary,
                    source_ref,
                    json.dumps(tags or [], sort_keys=True),
                    status,
                    now,
                    now,
                ),
            ).fetchone()
            self.connection.execute(
                """
                UPDATE role_instances
                SET memory_version=memory_version + 1,
                    updated_at=?
                WHERE role_instance_id=?
                """,
                (now, role_instance_id),
            )
        return str(row["memory_id"])

    def record_project_memory(
        self,
        *,
        project_id: str,
        summary: str,
        source_ref: str,
        created_by_role_instance_id: str | None = None,
        tags: list[str] | None = None,
        status: str = "active",
    ) -> str:
        memory_id = f"pmem-{uuid4().hex}"
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO project_memory(
                  memory_id, project_id, summary, source_ref,
                  created_by_role_instance_id, created_by_role_id,
                  tags_json, status, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    memory_id,
                    project_id,
                    summary,
                    source_ref,
                    created_by_role_instance_id,
                    _role_from_instance_id(created_by_role_instance_id) if created_by_role_instance_id else None,
                    json.dumps(tags or [], sort_keys=True),
                    status,
                    now,
                    now,
                ),
            )
        return memory_id

    def enqueue_project_manager_attention(
        self,
        *,
        project_id: str,
        source_role_instance_id: str | None,
        summary: str,
        reason: str,
        work_item_id: str | None = None,
        message_id: str | None = None,
        handoff_id: str | None = None,
        severity: str = "high",
        payload: dict[str, Any] | None = None,
    ) -> str | None:
        target_role = "project-manager"
        source_role = _role_from_instance_id(source_role_instance_id) if source_role_instance_id else None
        attention_payload = {
            "attention_type": "runtime_obligation_failure",
            "project_id": project_id,
            "source_role_instance_id": source_role_instance_id,
            "source_role": source_role,
            "reason": reason,
            "severity": severity,
            "work_item_id": work_item_id,
            "message_id": message_id,
            "handoff_id": handoff_id,
            **(payload or {}),
        }
        if source_role == target_role:
            self.record_agent_event(
                role_instance_id=source_role_instance_id or f"{project_id}.project-manager.1",
                event_type="project_manager_attention/self_escalation_recorded",
                content=summary,
                payload=attention_payload,
                message_id=message_id,
            )
            return None
        text = (
            "Runtime obligation failure requires Project Manager action.\n\n"
            f"Summary: {summary}\n"
            f"Reason: {reason}\n"
            f"Source role: {source_role or 'runtime'}\n"
            f"Work item: {work_item_id or 'none'}"
        )
        queued_message_id = self.enqueue_message(
            target_role=target_role,
            text=text,
            source="runtime-escalation",
            payload=attention_payload,
        )
        self.record_message_journal(
            message_id=queued_message_id,
            correlation_id=f"corr-{queued_message_id}",
            role_instance_id=source_role_instance_id,
            stage="project_manager_attention",
            status="queued",
            summary=summary,
            payload=attention_payload,
        )
        return queued_message_id

    def reset_runtime_state(self) -> None:
        tables = (
            "decision_links",
            "decision_callbacks",
            "decision_card_deliveries",
            "decision_records",
            "watchdog_findings",
            "watchdog_sweep_runs",
            "handoff_transitions",
            "handoffs",
            "releases",
            "approvals",
            "preflight_results",
            "document_write_warnings",
            "document_merge_tasks",
            "document_revisions",
            "artifacts",
            "work_items",
            "summaries",
            "project_memory",
            "role_memory",
            "turn_completion_diagnostics",
            "safe_output_calls",
            "agent_events",
            "message_journal",
            "message_queue",
            "codex_turns",
            "codex_threads",
            "agent_sessions",
            "role_instances",
        )
        with self.connection:
            for table in tables:
                self.connection.execute(f"DELETE FROM {table}")

    def start_watchdog_sweep(
        self,
        *,
        initiator_role: str,
        mode: str,
        sweep_run_id: str | None = None,
    ) -> str:
        sweep_run_id = sweep_run_id or f"sweep-{uuid4().hex}"
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO watchdog_sweep_runs(
                  sweep_run_id, initiator_role, mode, started_at, completed_at, status, summary_json
                ) VALUES(?,?,?,?,NULL,?,?)
                """,
                (sweep_run_id, initiator_role, mode, now, "running", "{}"),
            )
        return sweep_run_id

    def finish_watchdog_sweep(self, *, sweep_run_id: str, status: str, summary: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE watchdog_sweep_runs
                SET completed_at=?, status=?, summary_json=?
                WHERE sweep_run_id=?
                """,
                (utc_now(), status, json.dumps(summary, sort_keys=True), sweep_run_id),
            )

    def upsert_watchdog_finding(
        self,
        *,
        sweep_run_id: str,
        finding_key: str,
        finding_type: str,
        severity: str,
        work_item_id: str | None = None,
        handoff_id: str | None = None,
        message_id: str | None = None,
        target_role: str | None = None,
        owner_role: str | None = None,
        evidence: dict[str, Any] | None = None,
        next_action: str = "",
    ) -> str:
        existing = self.connection.execute(
            "SELECT finding_id, created_at FROM watchdog_findings WHERE finding_key=?",
            (finding_key,),
        ).fetchone()
        finding_id = str(existing["finding_id"]) if existing is not None else f"finding-{uuid4().hex}"
        created_at = str(existing["created_at"]) if existing is not None else utc_now()
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO watchdog_findings(
                  finding_id, sweep_run_id, finding_key, finding_type, severity, state,
                  work_item_id, handoff_id, message_id, target_role, owner_role,
                  evidence_json, next_action, created_at, updated_at, resolved_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(finding_key) DO UPDATE SET
                  sweep_run_id=excluded.sweep_run_id,
                  finding_type=excluded.finding_type,
                  severity=excluded.severity,
                  state='open',
                  work_item_id=excluded.work_item_id,
                  handoff_id=excluded.handoff_id,
                  message_id=excluded.message_id,
                  target_role=excluded.target_role,
                  owner_role=excluded.owner_role,
                  evidence_json=excluded.evidence_json,
                  next_action=excluded.next_action,
                  updated_at=excluded.updated_at,
                  resolved_at=NULL
                """,
                (
                    finding_id,
                    sweep_run_id,
                    finding_key,
                    finding_type,
                    severity,
                    "open",
                    work_item_id,
                    handoff_id,
                    message_id,
                    target_role,
                    owner_role,
                    json.dumps(evidence or {}, sort_keys=True),
                    next_action,
                    created_at,
                    now,
                ),
            )
        return finding_id

    def resolve_watchdog_findings(self, *, active_finding_keys: set[str], sweep_run_id: str) -> int:
        rows = [
            row
            for row in self.connection.execute(
                "SELECT finding_key FROM watchdog_findings WHERE state='open'"
            )
            if str(row["finding_key"]) not in active_finding_keys
        ]
        if not rows:
            return 0
        now = utc_now()
        with self.connection:
            for row in rows:
                self.connection.execute(
                    """
                    UPDATE watchdog_findings
                    SET state='resolved', sweep_run_id=?, updated_at=?, resolved_at=?
                    WHERE finding_key=? AND state='open'
                    """,
                    (sweep_run_id, now, now, row["finding_key"]),
                )
        return len(rows)

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
        memory_counts = {
            str(row["role_instance_id"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT role_instance_id, COUNT(*) AS count FROM role_memory WHERE status='active' GROUP BY role_instance_id"
            )
        }
        project_memory_counts = {
            str(row["project_id"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT project_id, COUNT(*) AS count FROM project_memory WHERE status='active' GROUP BY project_id"
            )
        }
        institutional_memory_total = sum(project_memory_counts.values())
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
            role["institutional_memory_count"] = institutional_memory_total
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
        for item in work_items:
            item["architecture_domains"] = _json_list(item.get("architecture_domains_json"))
        architecture_governance_records = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM architecture_governance_records ORDER BY created_at DESC"
            )
        ]
        handoffs = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM handoffs ORDER BY created_at DESC"
            )
        ]
        artifacts = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM artifacts ORDER BY created_at DESC"
            )
        ]
        document_revisions = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM document_revisions ORDER BY created_at DESC LIMIT 100"
            )
        ]
        document_merge_tasks = [
            _row_dict(row)
            for row in self.connection.execute(
                """
                SELECT * FROM document_merge_tasks
                WHERE state='open' OR resolved_at IS NOT NULL
                ORDER BY state='open' DESC, updated_at DESC
                LIMIT 100
                """
            )
        ]
        document_write_warnings = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM document_write_warnings ORDER BY created_at DESC LIMIT 100"
            )
        ]
        preflight_results = [
            _row_dict(row)
            for row in self.connection.execute(
                """
                SELECT * FROM preflight_results
                WHERE status!='passed'
                ORDER BY created_at DESC
                LIMIT 100
                """
            )
        ]
        turn_completion_diagnostics = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM turn_completion_diagnostics ORDER BY created_at DESC"
            )
        ]
        attention_needed = [
            item
            for item in messages
            if item["state"] in COMPLETION_ATTENTION_MESSAGE_STATES
        ]
        completion_attention = _completion_attention_items(
            messages=messages,
            diagnostics=turn_completion_diagnostics,
        )
        evidence_contract_warnings = _evidence_contract_items(
            diagnostics=turn_completion_diagnostics,
            state="completed_with_missing_evidence_warning",
        )
        evidence_contract_failures = _evidence_contract_items(
            diagnostics=turn_completion_diagnostics,
            state="rejected_missing_evidence",
        )
        watchdog_findings = _enrich_watchdog_findings(
            [
                _row_dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM watchdog_findings WHERE state='open' ORDER BY updated_at DESC"
                )
            ]
        )
        watchdog_sweep_runs = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM watchdog_sweep_runs ORDER BY started_at DESC LIMIT 20"
            )
        ]
        planned_not_dispatched = [
            item
            for item in watchdog_findings
            if item["finding_type"] == "planned_not_dispatched"
        ]
        planned_not_running = list(planned_not_dispatched)
        not_running = [
            item
            for item in watchdog_findings
            if item["finding_type"] in {"planned_not_dispatched", "open_handoff_without_message", "multiple_active_handoffs"}
        ]
        watchdog_attention = [item for item in watchdog_findings if item.get("severity") == "high"]
        decision_records = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM decision_records ORDER BY updated_at DESC"
            )
        ]
        decision_card_deliveries = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM decision_card_deliveries ORDER BY updated_at DESC"
            )
        ]
        decision_callbacks = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM decision_callbacks ORDER BY updated_at DESC"
            )
        ]
        decision_links = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM decision_links ORDER BY created_at DESC"
            )
        ]
        decision_attention = _decision_attention_items(
            decisions=decision_records,
            deliveries=decision_card_deliveries,
            callbacks=decision_callbacks,
        )
        handoff_transitions = [
            _row_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM handoff_transitions ORDER BY created_at DESC"
            )
        ]
        active_owner_paths = _active_owner_paths(handoffs)
        handoff_conflicts = [
            item
            for item in active_owner_paths
            if int(item.get("conflict_count") or 0) > 1
        ]
        blocked_handoff_attention = _blocked_handoff_attention(handoffs)
        document_attention = _document_attention_items(
            merge_tasks=document_merge_tasks,
            warnings=document_write_warnings,
        )
        preflight_attention = _preflight_attention_items(preflight_results)
        return {
            "roles": roles,
            "project_memory_counts": project_memory_counts,
            "institutional_memory_total": institutional_memory_total,
            "messages": messages,
            "work_items": work_items,
            "architecture_governance_records": architecture_governance_records,
            "handoffs": handoffs,
            "handoff_transitions": handoff_transitions,
            "active_owner_paths": active_owner_paths,
            "handoff_conflicts": handoff_conflicts,
            "artifacts": artifacts,
            "document_revisions": document_revisions,
            "document_merge_tasks": document_merge_tasks,
            "document_write_warnings": document_write_warnings,
            "document_attention": document_attention,
            "preflight_results": preflight_results,
            "preflight_attention": preflight_attention,
            "events": events,
            "turn_completion_diagnostics": turn_completion_diagnostics,
            "attention_needed": attention_needed,
            "completion_attention": completion_attention,
            "evidence_contract_warnings": evidence_contract_warnings,
            "evidence_contract_failures": evidence_contract_failures,
            "watchdog_findings": watchdog_findings,
            "watchdog_sweep_runs": watchdog_sweep_runs,
            "watchdog_attention": watchdog_attention,
            "attention_needed": [*attention_needed, *watchdog_attention],
            "planned_not_dispatched": planned_not_dispatched,
            "planned_not_running": planned_not_running,
            "not_running": not_running,
            "decision_records": decision_records,
            "decision_card_deliveries": decision_card_deliveries,
            "decision_callbacks": decision_callbacks,
            "decision_links": decision_links,
            "decision_attention": decision_attention,
            "blocked_handoff_attention": blocked_handoff_attention,
            "queue_depth": sum(1 for item in messages if item["state"] not in TERMINAL_MESSAGE_STATES),
        }



def _enrich_watchdog_findings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for item in items:
        evidence = _json_object(item.get("evidence_json"))
        copy = dict(item)
        copy["evidence"] = evidence
        copy["age_seconds"] = _int_or_none(evidence.get("age_seconds"))
        copy["threshold_seconds"] = _int_or_none(evidence.get("threshold_seconds"))
        copy["handoff_id"] = copy.get("handoff_id") or evidence.get("handoff_id")
        copy["message_id"] = copy.get("message_id") or evidence.get("message_id")
        copy["work_item_id"] = copy.get("work_item_id") or evidence.get("work_item_id")
        copy["target_role"] = copy.get("target_role") or evidence.get("target_role") or evidence.get("to_role")
        copy["owner_role"] = copy.get("owner_role") or copy.get("target_role")
        enriched.append(copy)
    return enriched


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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


def _completion_attention_items(
    *,
    messages: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    diagnostics_by_message: dict[str, dict[str, Any]] = {}
    for diagnostic in diagnostics:
        message_id = str(diagnostic.get("message_id") or "")
        if message_id and message_id not in diagnostics_by_message:
            diagnostics_by_message[message_id] = diagnostic
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.get("state") not in COMPLETION_ATTENTION_MESSAGE_STATES:
            continue
        message_id = str(message.get("message_id") or "")
        diagnostic = diagnostics_by_message.get(message_id, {})
        payload = _json_object(message.get("payload_json"))
        missing_predicates = _json_list(diagnostic.get("missing_predicates_json"))
        items.append(
            {
                "message_id": message_id,
                "state": message.get("state"),
                "role": message.get("target_role"),
                "work_item_id": diagnostic.get("work_item_id") or payload.get("work_item_id"),
                "missing_predicates": missing_predicates,
                "missing_predicate": _missing_predicate_summary(missing_predicates),
                "next_action": diagnostic.get("next_action") or "",
                "updated_at": message.get("updated_at"),
                "diagnostic_id": diagnostic.get("diagnostic_id"),
                "turn_id": diagnostic.get("turn_id"),
            }
        )
    return items


def _missing_predicate_summary(items: list[object]) -> str:
    summaries: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        predicate = str(item.get("predicate") or "")
        path = str(item.get("path") or "")
        tool_name = str(item.get("tool_name") or "")
        parts = [part for part in (predicate, path, tool_name) if part]
        if parts:
            summaries.append(" ".join(parts))
    return "; ".join(summaries)


def _evidence_contract_items(*, diagnostics: list[dict[str, Any]], state: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for diagnostic in diagnostics:
        if diagnostic.get("state") != state:
            continue
        missing = _json_list(diagnostic.get("missing_predicates_json"))
        observed = _json_object(diagnostic.get("observed_outputs_json"))
        for item in missing:
            if not isinstance(item, dict):
                continue
            items.append(
                {
                    "diagnostic_id": diagnostic.get("diagnostic_id"),
                    "message_id": diagnostic.get("message_id"),
                    "turn_id": diagnostic.get("turn_id"),
                    "role_instance_id": diagnostic.get("role_instance_id"),
                    "work_item_id": diagnostic.get("work_item_id") or item.get("work_item_id"),
                    "state": state,
                    "contract_id": item.get("contract_id") or observed.get("contract_id"),
                    "enforcement": item.get("enforcement") or observed.get("enforcement"),
                    "lifecycle_state": item.get("lifecycle_state") or observed.get("lifecycle_state"),
                    "owner_role": item.get("owner_role") or observed.get("owner_role"),
                    "work_item_type": item.get("work_item_type") or observed.get("work_item_type"),
                    "predicate": item.get("predicate"),
                    "path": item.get("path"),
                    "tool_name": item.get("tool_name"),
                    "remediation": item.get("remediation") or item.get("next_action") or diagnostic.get("next_action"),
                    "created_at": diagnostic.get("created_at"),
                }
            )
    return items


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list[object]:
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _validate_architecture_transition(
    *,
    existing: dict[str, Any],
    target_state: str,
    target_owner: str,
) -> None:
    current_state = str(existing.get("state") or "")
    impact = str(existing.get("architecture_impact") or "not_assessed")
    conformance = str(existing.get("architecture_conformance") or "not_required")
    if current_state == "product_definition" and target_state != current_state and impact == "not_assessed":
        raise ValueError("product definition cannot complete until architecture impact is recorded")
    if (
        current_state == "experience_design"
        and target_state == "solution_design"
        and impact in {"material", "uncertain"}
        and target_owner == "solution-architect"
    ):
        raise ValueError("material or uncertain architecture impact must route through enterprise_alignment")
    conformance_required_states = {
        "implementation_planning",
        "quality_planning",
        "implementation",
        "quality_review",
        "documentation_readiness",
        "delivery_readiness",
        "release_review",
        "released",
        "closed",
        "completed",
    }
    if (
        impact in {"material", "uncertain"}
        and target_state in conformance_required_states
        and conformance not in {"approved", "exception"}
    ):
        raise ValueError(
            "material or uncertain architecture impact requires approved conformance or a sponsor-approved exception"
        )


def _decision_attention_items(
    *,
    decisions: list[dict[str, Any]],
    deliveries: list[dict[str, Any]],
    callbacks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deliveries_by_decision: dict[str, list[dict[str, Any]]] = {}
    callbacks_by_decision: dict[str, list[dict[str, Any]]] = {}
    for delivery in deliveries:
        deliveries_by_decision.setdefault(str(delivery.get("decision_id") or ""), []).append(delivery)
    for callback in callbacks:
        callbacks_by_decision.setdefault(str(callback.get("decision_id") or ""), []).append(callback)
    attention: list[dict[str, Any]] = []
    for decision in decisions:
        status = str(decision.get("status") or "")
        sla_state = str(decision.get("sla_state") or "")
        decision_id = str(decision.get("decision_id") or "")
        decision_deliveries = deliveries_by_decision.get(decision_id, [])
        decision_callbacks = callbacks_by_decision.get(decision_id, [])
        failed_delivery = next((item for item in decision_deliveries if str(item.get("state") or "").endswith("failed")), None)
        failed_callback = next((item for item in decision_callbacks if str(item.get("state") or "").endswith("failed")), None)
        update_failed = next((item for item in decision_deliveries if item.get("state") == "update_failed"), None)
        if status == "resolved" and update_failed is None:
            continue
        if status == "cancelled":
            continue
        if status not in {"pending", "delivery_failed_pending", "resolution_failed"} and not (failed_delivery or failed_callback or update_failed):
            continue
        reason = status
        if update_failed is not None:
            reason = "update_failed"
        elif failed_callback is not None:
            reason = "callback_failed"
        elif failed_delivery is not None:
            reason = "delivery_failed"
        elif sla_state in {"due_soon", "overdue", "escalated"}:
            reason = sla_state
        attention.append(
            {
                "decision_id": decision_id,
                "work_item_id": decision.get("work_item_id"),
                "decision_type": decision.get("decision_type"),
                "title": decision.get("title"),
                "authority_label": decision.get("authority_label"),
                "owner_role": decision.get("owner_role"),
                "status": status,
                "sla_state": sla_state,
                "sla_due_at": decision.get("sla_due_at"),
                "reason": reason,
                "next_action": "Await authorized human decision; SLA expiry does not resolve this decision.",
                "updated_at": decision.get("updated_at"),
            }
        )
    return attention


def _active_owner_paths(handoffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_by_work: dict[str, list[dict[str, Any]]] = {}
    for handoff in handoffs:
        work_item_id = str(handoff.get("work_item_id") or "")
        if not work_item_id:
            continue
        state = str(handoff.get("state") or handoff.get("status") or "")
        if state not in {"open", "accepted", "blocked"}:
            continue
        active_by_work.setdefault(work_item_id, []).append(handoff)
    paths: list[dict[str, Any]] = []
    for work_item_id, items in active_by_work.items():
        newest = sorted(items, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[0]
        paths.append(
            {
                "work_item_id": work_item_id,
                "handoff_id": newest.get("handoff_id"),
                "from_role": newest.get("from_role"),
                "to_role": newest.get("to_role"),
                "state": newest.get("state") or newest.get("status"),
                "next_action": newest.get("next_action") or newest.get("reason"),
                "conflict_count": len(items),
                "conflict_handoff_ids": [item.get("handoff_id") for item in items],
                "updated_at": newest.get("updated_at") or newest.get("created_at"),
            }
        )
    return paths


def _blocked_handoff_attention(handoffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attention: list[dict[str, Any]] = []
    for handoff in handoffs:
        state = str(handoff.get("state") or handoff.get("status") or "")
        if state != "blocked":
            continue
        attention.append(
            {
                "handoff_id": handoff.get("handoff_id"),
                "work_item_id": handoff.get("work_item_id"),
                "owner_role": handoff.get("to_role"),
                "reason": handoff.get("reason"),
                "next_action": handoff.get("next_action") or handoff.get("reason"),
                "updated_at": handoff.get("updated_at") or handoff.get("created_at"),
            }
        )
    return attention


def _document_attention_items(
    *,
    merge_tasks: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    attention: list[dict[str, Any]] = []
    for task in merge_tasks:
        if str(task.get("state") or "") != "open":
            continue
        path = str(task.get("path") or "")
        attention.append(
            {
                "attention_type": "document_merge_task",
                "merge_task_id": task.get("merge_task_id"),
                "work_item_id": task.get("work_item_id"),
                "path": path,
                "owner_role": task.get("owner_role"),
                "severity": "high",
                "reason": _json_object(task.get("diagnostic_json")).get("reason") or "merge_required",
                "next_action": f"Resolve document merge task for {path}; current canonical content was preserved.",
                "updated_at": task.get("updated_at"),
                "current_sha256": task.get("current_sha256"),
                "proposed_sha256": task.get("proposed_sha256"),
            }
        )
    for warning in warnings:
        path = str(warning.get("path") or "")
        attention.append(
            {
                "attention_type": "document_write_warning",
                "warning_id": warning.get("warning_id"),
                "work_item_id": warning.get("work_item_id"),
                "path": path,
                "owner_role": _role_from_instance_id(str(warning.get("role_instance_id") or "")),
                "severity": warning.get("severity"),
                "reason": warning.get("warning_type"),
                "next_action": "Review document write warning; backend comment metadata preservation was not claimed.",
                "updated_at": warning.get("created_at"),
            }
        )
    return attention


def _preflight_attention_items(preflight_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_message: dict[str, dict[str, Any]] = {}
    for item in preflight_results:
        message_id = str(item.get("message_id") or "")
        if message_id and message_id not in latest_by_message:
            latest_by_message[message_id] = item
    return [
        {
            "attention_type": "role_artifact_preflight",
            "message_id": item.get("message_id"),
            "handoff_id": item.get("handoff_id"),
            "work_item_id": item.get("work_item_id"),
            "role_id": item.get("role_id"),
            "service_name": item.get("service_name"),
            "required_path": item.get("required_path"),
            "canonical_path": item.get("canonical_path"),
            "check_name": item.get("check_name"),
            "status": item.get("status"),
            "exit_code": item.get("exit_code"),
            "stderr_excerpt": item.get("stderr_excerpt"),
            "remediation": item.get("remediation"),
            "created_at": item.get("created_at"),
        }
        for item in latest_by_message.values()
    ]


def _role_from_instance_id(role_instance_id: str) -> str:
    parts = role_instance_id.split(".")
    if len(parts) >= 3:
        return parts[-2]
    return role_instance_id


def _project_from_instance_id(role_instance_id: str) -> str:
    parts = role_instance_id.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:-2])
    return "default"


def _excerpt(value: str | None, *, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = "".join(char if char.isprintable() or char in "\n\t" else "?" for char in str(value))
    return text[:limit]


def _ensure_column(connection: _PostgresConnection, table: str, column: str, declaration: str) -> None:
    if not table.replace("_", "").isalnum() or not column.replace("_", "").isalnum():
        raise ValueError("table and column names must be simple identifiers")
    connection.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {declaration}")


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
