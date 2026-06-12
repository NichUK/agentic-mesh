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


@dataclass(frozen=True)
class ExternalEventReceipt:
    receipt_id: str
    duplicate: bool


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

                CREATE TABLE IF NOT EXISTS connectors (
                  connector_id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  connector_type TEXT NOT NULL,
                  display_name TEXT NOT NULL,
                  status TEXT NOT NULL,
                  health_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS connector_participants (
                  participant_id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  participant_type TEXT NOT NULL,
                  display_name TEXT NOT NULL,
                  external_ref TEXT NOT NULL,
                  role_id TEXT,
                  authority_json TEXT NOT NULL DEFAULT '[]',
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(connector_id, external_ref)
                );

                CREATE TABLE IF NOT EXISTS external_event_receipts (
                  receipt_id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  idempotency_key TEXT NOT NULL UNIQUE,
                  external_event_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS conversation_events (
                  conversation_event_id TEXT PRIMARY KEY,
                  conversation_id TEXT NOT NULL,
                  receipt_id TEXT REFERENCES external_event_receipts(receipt_id),
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  event_type TEXT NOT NULL,
                  sender_participant_id TEXT REFERENCES connector_participants(participant_id),
                  body_preview TEXT NOT NULL DEFAULT '',
                  visibility_scope TEXT NOT NULL,
                  role_id TEXT,
                  thread_ref TEXT,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS thread_bindings (
                  thread_binding_id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  conversation_id TEXT NOT NULL,
                  external_thread_ref TEXT NOT NULL,
                  binding_type TEXT NOT NULL,
                  target_ref TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(connector_id, external_thread_ref, binding_type)
                );

                CREATE TABLE IF NOT EXISTS delivery_records (
                  delivery_id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  source_ref TEXT NOT NULL,
                  destination_ref TEXT NOT NULL,
                  destination_type TEXT NOT NULL,
                  purpose TEXT NOT NULL,
                  status TEXT NOT NULL,
                  role_id TEXT,
                  external_message_id TEXT,
                  error_class TEXT,
                  error_detail TEXT,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS delivery_attempts (
                  attempt_id TEXT PRIMARY KEY,
                  delivery_id TEXT NOT NULL REFERENCES delivery_records(delivery_id),
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  attempt_number INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  external_message_id TEXT,
                  error_class TEXT,
                  error_detail TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(delivery_id, attempt_number)
                );

                CREATE TABLE IF NOT EXISTS connector_attention_items (
                  attention_id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  owner TEXT NOT NULL,
                  reason_class TEXT NOT NULL,
                  next_action TEXT NOT NULL,
                  retryable INTEGER NOT NULL,
                  source_ref TEXT,
                  status TEXT NOT NULL DEFAULT 'open',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS connector_permission_checks (
                  check_id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  check_type TEXT NOT NULL,
                  capability TEXT NOT NULL,
                  status TEXT NOT NULL,
                  required_status TEXT NOT NULL,
                  actual_status TEXT NOT NULL,
                  phase TEXT NOT NULL,
                  consent_type TEXT,
                  permission_name TEXT,
                  required INTEGER NOT NULL,
                  broad_graph INTEGER NOT NULL,
                  approval_ref TEXT,
                  next_action TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS role_assignments (
                  assignment_id TEXT PRIMARY KEY,
                  role_id TEXT NOT NULL,
                  role_instance_id TEXT,
                  work_item_id TEXT REFERENCES work_items(work_item_id),
                  conversation_id TEXT,
                  source_ref TEXT NOT NULL,
                  title TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  status TEXT NOT NULL,
                  assignment_type TEXT NOT NULL,
                  visibility_scope TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS relevance_checks (
                  relevance_check_id TEXT PRIMARY KEY,
                  conversation_event_id TEXT NOT NULL,
                  role_id TEXT NOT NULL,
                  score REAL NOT NULL,
                  threshold REAL NOT NULL,
                  decision TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  noop INTEGER NOT NULL,
                  exception_reason TEXT,
                  safe_output_ref TEXT,
                  delivery_ref TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(conversation_event_id, role_id)
                );

                CREATE TABLE IF NOT EXISTS work_proposals (
                  proposal_id TEXT PRIMARY KEY,
                  queue_item_id TEXT NOT NULL REFERENCES queue_items(queue_item_id),
                  source_conversation_id TEXT,
                  source_conversation_event_id TEXT,
                  source_receipt_id TEXT,
                  source_ref TEXT NOT NULL,
                  proposed_by_role TEXT NOT NULL,
                  initiated_by TEXT NOT NULL,
                  classification TEXT NOT NULL,
                  redaction TEXT NOT NULL,
                  target_artifact TEXT,
                  rationale TEXT NOT NULL,
                  urgency TEXT NOT NULL,
                  suggested_owner TEXT NOT NULL,
                  work_type TEXT NOT NULL,
                  safe_output_ref TEXT NOT NULL REFERENCES safe_output_calls(call_id),
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS human_response_requests (
                  request_id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  source_ref TEXT NOT NULL,
                  request_type TEXT NOT NULL,
                  title TEXT NOT NULL,
                  question TEXT NOT NULL,
                  status TEXT NOT NULL,
                  required_authority TEXT NOT NULL,
                  response_contract_id TEXT NOT NULL,
                  created_by_role TEXT NOT NULL,
                  destination_ref TEXT NOT NULL,
                  destination_type TEXT NOT NULL,
                  work_item_id TEXT,
                  gate_id TEXT,
                  target_ref TEXT,
                  thread_ref TEXT,
                  delivery_ref TEXT,
                  response_value TEXT,
                  responder_ref TEXT,
                  card_update_ref TEXT,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS human_response_submissions (
                  submission_id TEXT PRIMARY KEY,
                  request_id TEXT NOT NULL REFERENCES human_response_requests(request_id),
                  responder_ref TEXT NOT NULL,
                  response_value TEXT NOT NULL,
                  normalized_value TEXT NOT NULL,
                  status TEXT NOT NULL,
                  comment TEXT,
                  authority_json TEXT NOT NULL DEFAULT '[]',
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS conversation_context_summaries (
                  summary_id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  conversation_id TEXT NOT NULL,
                  visibility_scope TEXT NOT NULL,
                  classification TEXT NOT NULL,
                  retention_key TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  source_refs_json TEXT NOT NULL DEFAULT '[]',
                  durable_refs_json TEXT NOT NULL DEFAULT '[]',
                  target_ref TEXT,
                  created_by_role TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS retention_expiry_records (
                  expiry_id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL REFERENCES connectors(connector_id),
                  source_table TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  retention_key TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  expired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(source_table, source_id)
                );
                """
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._ensure_column("connector_participants", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        existing = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def get_queue_item(self, queue_item_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM queue_items WHERE queue_item_id = ?",
            (queue_item_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def record_work_proposal(
        self,
        *,
        proposal_id: str,
        queue_item_id: str,
        source_ref: str,
        proposed_by_role: str,
        initiated_by: str,
        classification: str,
        redaction: str,
        rationale: str,
        urgency: str,
        suggested_owner: str,
        work_type: str,
        safe_output_ref: str,
        source_conversation_id: str | None = None,
        source_conversation_event_id: str | None = None,
        source_receipt_id: str | None = None,
        target_artifact: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO work_proposals(
                  proposal_id, queue_item_id, source_conversation_id, source_conversation_event_id,
                  source_receipt_id, source_ref, proposed_by_role, initiated_by, classification,
                  redaction, target_artifact, rationale, urgency, suggested_owner, work_type,
                  safe_output_ref
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    queue_item_id,
                    source_conversation_id,
                    source_conversation_event_id,
                    source_receipt_id,
                    source_ref,
                    proposed_by_role,
                    initiated_by,
                    classification,
                    redaction,
                    target_artifact,
                    rationale,
                    urgency,
                    suggested_owner,
                    work_type,
                    safe_output_ref,
                ),
            )
            self.append_event(
                "work_proposal.recorded",
                "queue_item",
                queue_item_id,
                {
                    "proposal_id": proposal_id,
                    "proposed_by_role": proposed_by_role,
                    "source_ref": source_ref,
                    "classification": classification,
                    "work_type": work_type,
                },
            )

    def create_human_response_request(
        self,
        *,
        request_id: str,
        connector_id: str,
        source_ref: str,
        request_type: str,
        title: str,
        question: str,
        required_authority: str,
        response_contract_id: str,
        created_by_role: str,
        destination_ref: str,
        destination_type: str,
        work_item_id: str | None = None,
        gate_id: str | None = None,
        target_ref: str | None = None,
        thread_ref: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO human_response_requests(
                  request_id, connector_id, source_ref, request_type, title, question, status,
                  required_authority, response_contract_id, created_by_role, destination_ref,
                  destination_type, work_item_id, gate_id, target_ref, thread_ref, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, 'awaiting_response', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    connector_id,
                    source_ref,
                    request_type,
                    title,
                    question,
                    required_authority,
                    response_contract_id,
                    created_by_role,
                    destination_ref,
                    destination_type,
                    work_item_id,
                    gate_id,
                    target_ref,
                    thread_ref,
                    json.dumps(payload or {}, sort_keys=True),
                ),
            )
            self.append_event(
                "human_response.requested",
                "human_response_request",
                request_id,
                {
                    "request_type": request_type,
                    "required_authority": required_authority,
                    "work_item_id": work_item_id,
                    "gate_id": gate_id,
                },
            )

    def get_human_response_request(self, request_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM human_response_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def update_human_response_request_delivery(
        self,
        *,
        request_id: str,
        delivery_ref: str | None = None,
        card_update_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE human_response_requests
                SET delivery_ref = COALESCE(?, delivery_ref),
                    card_update_ref = COALESCE(?, card_update_ref),
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ?
                """,
                (delivery_ref, card_update_ref, request_id),
            )

    def complete_human_response_request(
        self,
        *,
        request_id: str,
        response_value: str,
        responder_ref: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE human_response_requests
                SET status = 'responded',
                    response_value = ?,
                    responder_ref = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ?
                """,
                (response_value, responder_ref, request_id),
            )
            self.append_event(
                "human_response.responded",
                "human_response_request",
                request_id,
                {"response_value": response_value, "responder_ref": responder_ref},
            )

    def record_human_response_submission(
        self,
        *,
        submission_id: str,
        request_id: str,
        responder_ref: str,
        response_value: str,
        normalized_value: str,
        status: str,
        authority: list[str],
        comment: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO human_response_submissions(
                  submission_id, request_id, responder_ref, response_value, normalized_value,
                  status, comment, authority_json, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    request_id,
                    responder_ref,
                    response_value,
                    normalized_value,
                    status,
                    comment,
                    json.dumps(authority, sort_keys=True),
                    json.dumps(payload or {}, sort_keys=True),
                ),
            )
            self.append_event(
                "human_response.submission_recorded",
                "human_response_request",
                request_id,
                {"submission_id": submission_id, "status": status, "normalized_value": normalized_value},
            )

    def record_context_summary(
        self,
        *,
        summary_id: str,
        connector_id: str,
        conversation_id: str,
        visibility_scope: str,
        classification: str,
        retention_key: str,
        summary: str,
        source_refs: list[str],
        durable_refs: list[str],
        created_by_role: str,
        target_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO conversation_context_summaries(
                  summary_id, connector_id, conversation_id, visibility_scope, classification,
                  retention_key, summary, source_refs_json, durable_refs_json, target_ref,
                  created_by_role
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_id,
                    connector_id,
                    conversation_id,
                    visibility_scope,
                    classification,
                    retention_key,
                    summary,
                    json.dumps(source_refs, sort_keys=True),
                    json.dumps(durable_refs, sort_keys=True),
                    target_ref,
                    created_by_role,
                ),
            )
            self.append_event(
                "conversation_context.compacted",
                "conversation",
                conversation_id,
                {
                    "summary_id": summary_id,
                    "visibility_scope": visibility_scope,
                    "classification": classification,
                    "source_refs": source_refs,
                    "durable_refs": durable_refs,
                },
            )

    def record_retention_expiry(
        self,
        *,
        expiry_id: str,
        connector_id: str,
        source_table: str,
        source_id: str,
        retention_key: str,
        content_sha256: str,
        metadata: dict[str, Any],
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO retention_expiry_records(
                  expiry_id, connector_id, source_table, source_id, retention_key,
                  content_sha256, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    expiry_id,
                    connector_id,
                    source_table,
                    source_id,
                    retention_key,
                    content_sha256,
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            created = cursor.rowcount > 0
            if created:
                self.append_event(
                    "retention.raw_expired",
                    source_table,
                    source_id,
                    {
                        "expiry_id": expiry_id,
                        "retention_key": retention_key,
                        "content_sha256": content_sha256,
                    },
                )
            return created

    def expire_conversation_event_raw(
        self,
        *,
        conversation_event_id: str,
        content_sha256: str,
        retention_key: str,
    ) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT payload_json FROM conversation_events WHERE conversation_event_id = ?",
                (conversation_event_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown conversation event `{conversation_event_id}`")
            payload = json.loads(row["payload_json"])
            payload.pop("body", None)
            payload["raw_content_expired"] = True
            payload["content_sha256"] = content_sha256
            payload["retention_key"] = retention_key
            self.connection.execute(
                """
                UPDATE conversation_events
                SET body_preview = '[expired raw content]',
                    payload_json = ?
                WHERE conversation_event_id = ?
                """,
                (json.dumps(payload, sort_keys=True), conversation_event_id),
            )

    def expire_external_receipt_raw(
        self,
        *,
        receipt_id: str,
        content_sha256: str,
        retention_key: str,
    ) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT payload_json FROM external_event_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown external receipt `{receipt_id}`")
            payload = json.loads(row["payload_json"])
            payload.pop("body", None)
            payload["raw_content_expired"] = True
            payload["content_sha256"] = content_sha256
            payload["retention_key"] = retention_key
            self.connection.execute(
                """
                UPDATE external_event_receipts
                SET payload_json = ?,
                    status = 'expired'
                WHERE receipt_id = ?
                """,
                (json.dumps(payload, sort_keys=True), receipt_id),
            )

    def expire_delivery_record_raw(
        self,
        *,
        delivery_id: str,
        content_sha256: str,
        retention_key: str,
    ) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT payload_json FROM delivery_records WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown delivery `{delivery_id}`")
            payload = json.loads(row["payload_json"])
            payload.pop("body", None)
            payload.pop("card", None)
            payload["raw_content_expired"] = True
            payload["content_sha256"] = content_sha256
            payload["retention_key"] = retention_key
            self.connection.execute(
                """
                UPDATE delivery_records
                SET payload_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (json.dumps(payload, sort_keys=True), delivery_id),
            )

    def upsert_connector(
        self,
        *,
        connector_id: str,
        project_id: str,
        connector_type: str,
        display_name: str,
        status: str = "configured",
        health: dict[str, Any] | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO connectors(connector_id, project_id, connector_type, display_name, status, health_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(connector_id) DO UPDATE SET
                  project_id = excluded.project_id,
                  connector_type = excluded.connector_type,
                  display_name = excluded.display_name,
                  status = excluded.status,
                  health_json = excluded.health_json,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    connector_id,
                    project_id,
                    connector_type,
                    display_name,
                    status,
                    json.dumps(health or {}, sort_keys=True),
                ),
            )
            self.append_event(
                "connector.configured",
                "connector",
                connector_id,
                {"project_id": project_id, "connector_type": connector_type, "status": status},
            )

    def upsert_connector_participant(
        self,
        *,
        participant_id: str,
        connector_id: str,
        participant_type: str,
        display_name: str,
        external_ref: str,
        role_id: str | None = None,
        authority: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO connector_participants(
                  participant_id, connector_id, participant_type, display_name, external_ref,
                  role_id, authority_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(participant_id) DO UPDATE SET
                  participant_type = excluded.participant_type,
                  display_name = excluded.display_name,
                  external_ref = excluded.external_ref,
                  role_id = excluded.role_id,
                  authority_json = excluded.authority_json,
                  metadata_json = excluded.metadata_json,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    participant_id,
                    connector_id,
                    participant_type,
                    display_name,
                    external_ref,
                    role_id,
                    json.dumps(authority or [], sort_keys=True),
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )

    def record_external_event_receipt(
        self,
        *,
        receipt_id: str,
        connector_id: str,
        idempotency_key: str,
        external_event_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> ExternalEventReceipt:
        with self.connection:
            existing = self.connection.execute(
                """
                SELECT receipt_id FROM external_event_receipts
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                self.connection.execute(
                    """
                    UPDATE external_event_receipts
                    SET last_seen_at = CURRENT_TIMESTAMP
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                )
                self.append_event(
                    "connector.event_duplicate_suppressed",
                    "connector",
                    connector_id,
                    {"receipt_id": str(existing["receipt_id"]), "event_type": event_type},
                )
                return ExternalEventReceipt(str(existing["receipt_id"]), True)
            self.connection.execute(
                """
                INSERT INTO external_event_receipts(
                  receipt_id, connector_id, idempotency_key, external_event_id, event_type, status, payload_json
                )
                VALUES (?, ?, ?, ?, ?, 'received', ?)
                """,
                (
                    receipt_id,
                    connector_id,
                    idempotency_key,
                    external_event_id,
                    event_type,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            self.append_event(
                "connector.event_received",
                "connector",
                connector_id,
                {"receipt_id": receipt_id, "event_type": event_type},
            )
            return ExternalEventReceipt(receipt_id, False)

    def upsert_conversation(
        self,
        *,
        conversation_id: str,
        connector: str,
        external_ref: str,
        sponsor_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO conversations(conversation_id, connector, external_ref, sponsor_ref)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                  sponsor_ref = COALESCE(excluded.sponsor_ref, sponsor_ref),
                  updated_at = CURRENT_TIMESTAMP
                """,
                (conversation_id, connector, external_ref, sponsor_ref),
            )

    def record_conversation_event(
        self,
        *,
        conversation_event_id: str,
        conversation_id: str,
        receipt_id: str | None,
        connector_id: str,
        event_type: str,
        sender_participant_id: str | None,
        body_preview: str,
        visibility_scope: str,
        payload: dict[str, Any],
        role_id: str | None = None,
        thread_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO conversation_events(
                  conversation_event_id, conversation_id, receipt_id, connector_id, event_type,
                  sender_participant_id, body_preview, visibility_scope, role_id, thread_ref, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_event_id,
                    conversation_id,
                    receipt_id,
                    connector_id,
                    event_type,
                    sender_participant_id,
                    body_preview,
                    visibility_scope,
                    role_id,
                    thread_ref,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            self.append_event(
                "conversation.event_recorded",
                "conversation",
                conversation_id,
                {"event_type": event_type, "connector_id": connector_id, "visibility_scope": visibility_scope},
            )

    def get_conversation_event(self, conversation_event_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM conversation_events WHERE conversation_event_id = ?",
            (conversation_event_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def bind_thread(
        self,
        *,
        thread_binding_id: str,
        connector_id: str,
        conversation_id: str,
        external_thread_ref: str,
        binding_type: str,
        target_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO thread_bindings(
                  thread_binding_id, connector_id, conversation_id, external_thread_ref, binding_type, target_ref
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(connector_id, external_thread_ref, binding_type) DO UPDATE SET
                  conversation_id = excluded.conversation_id,
                  target_ref = excluded.target_ref
                """,
                (thread_binding_id, connector_id, conversation_id, external_thread_ref, binding_type, target_ref),
            )

    def create_delivery_record(
        self,
        *,
        delivery_id: str,
        connector_id: str,
        source_ref: str,
        destination_ref: str,
        destination_type: str,
        purpose: str,
        idempotency_key: str,
        payload: dict[str, Any],
        status: str = "pending",
        role_id: str | None = None,
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO delivery_records(
                  delivery_id, connector_id, source_ref, destination_ref, destination_type,
                  purpose, status, role_id, idempotency_key, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    connector_id,
                    source_ref,
                    destination_ref,
                    destination_type,
                    purpose,
                    status,
                    role_id,
                    idempotency_key,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            created = cursor.rowcount > 0
            if created:
                self.append_event(
                    "connector.delivery_created",
                    "connector",
                    connector_id,
                    {"delivery_id": delivery_id, "status": status, "purpose": purpose},
                )
            return created

    def get_delivery_record(self, delivery_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM delivery_records WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_delivery_record_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM delivery_records WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def update_delivery_record(
        self,
        delivery_id: str,
        *,
        status: str,
        external_message_id: str | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE delivery_records
                SET status = ?,
                    external_message_id = ?,
                    error_class = ?,
                    error_detail = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ?
                """,
                (status, external_message_id, error_class, error_detail, delivery_id),
            )
            row = self.connection.execute(
                "SELECT connector_id FROM delivery_records WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is not None:
                self.append_event(
                    "connector.delivery_updated",
                    "connector",
                    str(row["connector_id"]),
                    {"delivery_id": delivery_id, "status": status, "error_class": error_class},
                )

    def record_delivery_attempt(
        self,
        *,
        attempt_id: str,
        delivery_id: str,
        connector_id: str,
        attempt_number: int,
        status: str,
        external_message_id: str | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO delivery_attempts(
                  attempt_id, delivery_id, connector_id, attempt_number, status,
                  external_message_id, error_class, error_detail
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    delivery_id,
                    connector_id,
                    attempt_number,
                    status,
                    external_message_id,
                    error_class,
                    error_detail,
                ),
            )
            self.append_event(
                "connector.delivery_attempt_recorded",
                "connector",
                connector_id,
                {
                    "attempt_id": attempt_id,
                    "delivery_id": delivery_id,
                    "attempt_number": attempt_number,
                    "status": status,
                    "error_class": error_class,
                },
            )

    def count_delivery_attempts(self, delivery_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM delivery_attempts WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def create_connector_attention_item(
        self,
        *,
        attention_id: str,
        connector_id: str,
        owner: str,
        reason_class: str,
        next_action: str,
        retryable: bool,
        source_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO connector_attention_items(
                  attention_id, connector_id, owner, reason_class, next_action, retryable, source_ref
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attention_id) DO UPDATE SET
                  owner = excluded.owner,
                  reason_class = excluded.reason_class,
                  next_action = excluded.next_action,
                  retryable = excluded.retryable,
                  source_ref = excluded.source_ref,
                  status = 'open',
                  updated_at = CURRENT_TIMESTAMP
                """,
                (attention_id, connector_id, owner, reason_class, next_action, 1 if retryable else 0, source_ref),
            )
            self.append_event(
                "connector.attention_created",
                "connector",
                connector_id,
                {"attention_id": attention_id, "reason_class": reason_class, "retryable": retryable},
            )

    def record_connector_permission_check(
        self,
        *,
        check_id: str,
        connector_id: str,
        check_type: str,
        capability: str,
        status: str,
        required_status: str,
        actual_status: str,
        phase: str,
        next_action: str,
        consent_type: str | None = None,
        permission_name: str | None = None,
        required: bool = True,
        broad_graph: bool = False,
        approval_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO connector_permission_checks(
                  check_id, connector_id, check_type, capability, status, required_status,
                  actual_status, phase, consent_type, permission_name, required, broad_graph,
                  approval_ref, next_action
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(check_id) DO UPDATE SET
                  status = excluded.status,
                  required_status = excluded.required_status,
                  actual_status = excluded.actual_status,
                  phase = excluded.phase,
                  consent_type = excluded.consent_type,
                  permission_name = excluded.permission_name,
                  required = excluded.required,
                  broad_graph = excluded.broad_graph,
                  approval_ref = excluded.approval_ref,
                  next_action = excluded.next_action,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    check_id,
                    connector_id,
                    check_type,
                    capability,
                    status,
                    required_status,
                    actual_status,
                    phase,
                    consent_type,
                    permission_name,
                    1 if required else 0,
                    1 if broad_graph else 0,
                    approval_ref,
                    next_action,
                ),
            )
            self.append_event(
                "connector.permission_checked",
                "connector",
                connector_id,
                {
                    "check_id": check_id,
                    "check_type": check_type,
                    "capability": capability,
                    "status": status,
                    "phase": phase,
                },
            )

    def create_role_assignment(
        self,
        *,
        assignment_id: str,
        role_id: str,
        source_ref: str,
        title: str,
        summary: str,
        assignment_type: str,
        visibility_scope: str,
        payload: dict[str, Any],
        work_item_id: str | None = None,
        conversation_id: str | None = None,
        role_instance_id: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO role_assignments(
                  assignment_id, role_id, role_instance_id, work_item_id, conversation_id,
                  source_ref, title, summary, status, assignment_type, visibility_scope, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    assignment_id,
                    role_id,
                    role_instance_id,
                    work_item_id,
                    conversation_id,
                    source_ref,
                    title,
                    summary,
                    assignment_type,
                    visibility_scope,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            self.append_event(
                "role_assignment.created",
                "role_assignment",
                assignment_id,
                {
                    "role_id": role_id,
                    "assignment_type": assignment_type,
                    "conversation_id": conversation_id,
                    "visibility_scope": visibility_scope,
                },
            )

    def complete_role_assignment(self, assignment_id: str, *, role_instance_id: str | None = None) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE role_assignments
                SET status = 'completed',
                    role_instance_id = COALESCE(?, role_instance_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE assignment_id = ?
                """,
                (role_instance_id, assignment_id),
            )
            self.append_event(
                "role_assignment.completed",
                "role_assignment",
                assignment_id,
                {"role_instance_id": role_instance_id},
            )

    def record_relevance_check(
        self,
        *,
        relevance_check_id: str,
        conversation_event_id: str,
        role_id: str,
        score: float,
        threshold: float,
        decision: str,
        reason: str,
        noop: bool,
        exception_reason: str | None = None,
        safe_output_ref: str | None = None,
        delivery_ref: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO relevance_checks(
                  relevance_check_id, conversation_event_id, role_id, score, threshold,
                  decision, reason, noop, exception_reason, safe_output_ref, delivery_ref
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_event_id, role_id) DO UPDATE SET
                  score = excluded.score,
                  threshold = excluded.threshold,
                  decision = excluded.decision,
                  reason = excluded.reason,
                  noop = excluded.noop,
                  exception_reason = excluded.exception_reason,
                  safe_output_ref = excluded.safe_output_ref,
                  delivery_ref = excluded.delivery_ref,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    relevance_check_id,
                    conversation_event_id,
                    role_id,
                    score,
                    threshold,
                    decision,
                    reason,
                    1 if noop else 0,
                    exception_reason,
                    safe_output_ref,
                    delivery_ref,
                ),
            )
            self.append_event(
                "connector.relevance_recorded",
                "conversation_event",
                conversation_event_id,
                {
                    "role_id": role_id,
                    "score": score,
                    "threshold": threshold,
                    "decision": decision,
                    "noop": noop,
                    "safe_output_ref": safe_output_ref,
                    "delivery_ref": delivery_ref,
                },
            )

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

    def list_queue_items(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM queue_items
            ORDER BY created_at DESC, queue_item_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_work_proposals(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM work_proposals
            ORDER BY created_at DESC, proposal_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_human_response_requests(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM human_response_requests
            ORDER BY updated_at DESC, request_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_human_response_submissions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM human_response_submissions
            ORDER BY created_at DESC, submission_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_context_summaries(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM conversation_context_summaries
            ORDER BY created_at DESC, summary_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_retention_expiry_record_by_source(
        self,
        *,
        source_table: str,
        source_id: str,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM retention_expiry_records
            WHERE source_table = ?
              AND source_id = ?
            """,
            (source_table, source_id),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_context_summary(self, summary_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM conversation_context_summaries WHERE summary_id = ?",
            (summary_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_retention_expiry_records(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM retention_expiry_records
            ORDER BY expired_at DESC, expiry_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_work_items(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT wi.*, a.owner AS attention_owner, a.reason_class, a.next_action, a.retryable
            FROM work_items wi
            LEFT JOIN work_item_attention a ON a.work_item_id = wi.work_item_id
            ORDER BY wi.updated_at DESC, wi.work_item_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_agent_runs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM agent_runs
            ORDER BY started_at DESC, run_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_safe_output_calls(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM safe_output_calls
            ORDER BY created_at DESC, call_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_artifacts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM artifacts
            ORDER BY created_at DESC, artifact_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_releases(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM releases
            ORDER BY updated_at DESC, release_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_connectors(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM connectors
            ORDER BY updated_at DESC, connector_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_connector_participants(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM connector_participants
            ORDER BY updated_at DESC, participant_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_connector_participant_by_external_ref(
        self,
        *,
        connector_id: str,
        external_ref: str,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM connector_participants
            WHERE connector_id = ?
              AND external_ref = ?
            LIMIT 1
            """,
            (connector_id, external_ref),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_conversations(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM conversations
            ORDER BY updated_at DESC, conversation_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_conversation_events(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM conversation_events
            ORDER BY created_at DESC, conversation_event_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_external_event_receipts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM external_event_receipts
            ORDER BY last_seen_at DESC, receipt_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_thread_bindings(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM thread_bindings
            ORDER BY created_at DESC, thread_binding_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def find_thread_binding(
        self,
        *,
        connector_id: str,
        external_thread_ref: str,
        binding_type: str | None = None,
    ) -> dict[str, Any] | None:
        if binding_type is None:
            row = self.connection.execute(
                """
                SELECT *
                FROM thread_bindings
                WHERE connector_id = ?
                  AND external_thread_ref = ?
                ORDER BY
                  CASE binding_type
                    WHEN 'human_question' THEN 0
                    WHEN 'teams_thread' THEN 1
                    ELSE 2
                  END,
                  created_at DESC
                LIMIT 1
                """,
                (connector_id, external_thread_ref),
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT *
                FROM thread_bindings
                WHERE connector_id = ?
                  AND external_thread_ref = ?
                  AND binding_type = ?
                LIMIT 1
                """,
                (connector_id, external_thread_ref, binding_type),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_delivery_records(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM delivery_records
            ORDER BY updated_at DESC, delivery_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_delivery_attempts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM delivery_attempts
            ORDER BY created_at DESC, attempt_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_connector_attention_items(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM connector_attention_items
            ORDER BY updated_at DESC, attention_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_connector_permission_checks(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM connector_permission_checks
            ORDER BY updated_at DESC, check_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_role_assignments(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM role_assignments
            ORDER BY updated_at DESC, assignment_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_relevance_checks(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM relevance_checks
            ORDER BY updated_at DESC, relevance_check_id
            """
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_relevance_checks_for_run(self, run_id: str, role_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT relevance_checks.*
            FROM relevance_checks
            JOIN safe_output_calls
              ON safe_output_calls.call_id = relevance_checks.safe_output_ref
            WHERE safe_output_calls.run_id = ?
              AND relevance_checks.role_id = ?
            ORDER BY relevance_checks.updated_at DESC, relevance_checks.relevance_check_id
            """,
            (run_id, role_id),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def status_snapshot(self) -> dict[str, Any]:
        work_items = self.list_work_items()
        queue_items = self.list_queue_items()
        releases = self.list_releases()
        connectors = self.list_connectors()
        connector_events = self.list_conversation_events()
        redacted_connector_events = _redact_private_conversation_events(connector_events)
        external_event_receipts = self.list_external_event_receipts()
        redacted_external_event_receipts = _redact_private_external_event_receipts(external_event_receipts)
        delivery_records = self.list_delivery_records()
        redacted_delivery_records = _redact_private_delivery_records(delivery_records)
        delivery_attempts = self.list_delivery_attempts()
        connector_attention_items = self.list_connector_attention_items()
        connector_permission_checks = self.list_connector_permission_checks()
        role_assignments = self.list_role_assignments()
        relevance_checks = self.list_relevance_checks()
        work_proposals = self.list_work_proposals()
        human_response_requests = self.list_human_response_requests()
        redacted_human_response_requests = _redact_private_human_response_requests(human_response_requests)
        human_response_submissions = self.list_human_response_submissions()
        context_summaries = self.list_context_summaries()
        redacted_context_summaries = _redact_private_context_summaries(context_summaries)
        retention_expiry_records = self.list_retention_expiry_records()
        safe_output_calls = self.list_safe_output_calls()
        redacted_safe_output_calls = _redact_private_safe_output_calls(safe_output_calls)
        states: dict[str, int] = {}
        for item in work_items:
            state = str(item["state"])
            states[state] = states.get(state, 0) + 1
        queue_statuses: dict[str, int] = {}
        for item in queue_items:
            status = str(item["status"])
            queue_statuses[status] = queue_statuses.get(status, 0) + 1
        delivery_statuses: dict[str, int] = {}
        for record in delivery_records:
            status = str(record["status"])
            delivery_statuses[status] = delivery_statuses.get(status, 0) + 1
        relevance_decisions: dict[str, int] = {}
        for record in relevance_checks:
            decision = str(record["decision"])
            relevance_decisions[decision] = relevance_decisions.get(decision, 0) + 1
        ambiguous_binding_reasons = {
            "ambiguous_binding",
            "unbound_thread_reply",
            "unbound_private_channel",
            "unknown_role_mention",
            "unrouteable_role_direct_message",
            "connector_permission_failed",
        }
        connector_metrics = {
            "inbound_events": len(external_event_receipts),
            "duplicates_suppressed": sum(
                1
                for event in self.list_events()
                if event.get("event_type") == "connector.event_duplicate_suppressed"
            ),
            "active_conversations": len(self.list_conversations()),
            "delivery_failures": sum(
                1
                for record in delivery_records
                if str(record.get("status")) in {"failed_transient", "failed_permanent", "unknown"}
            ),
            "relevance_decisions": relevance_decisions,
            "ambiguous_bindings": sum(
                1
                for item in connector_attention_items
                if item.get("reason_class") in ambiguous_binding_reasons
            ),
            "private_dm_promotions": sum(
                1
                for proposal in work_proposals
                if proposal.get("redaction") == "private_source_redacted"
            ),
            "compaction_count": len(context_summaries),
            "permission_failures": sum(
                1
                for check in connector_permission_checks
                if check.get("status") == "fail"
            ),
        }
        return {
            "database": str(self.path),
            "counts": {
                "queue_items": len(queue_items),
                "work_items": len(work_items),
                "agent_runs": len(self.list_agent_runs()),
                "safe_output_calls": len(safe_output_calls),
                "artifacts": len(self.list_artifacts()),
                "releases": len(releases),
                "connectors": len(connectors),
                "connector_participants": len(self.list_connector_participants()),
                "conversations": len(self.list_conversations()),
                "conversation_events": len(connector_events),
                "external_event_receipts": len(external_event_receipts),
                "thread_bindings": len(self.list_thread_bindings()),
                "delivery_records": len(delivery_records),
                "delivery_attempts": len(delivery_attempts),
                "connector_attention_items": len(connector_attention_items),
                "connector_permission_checks": len(connector_permission_checks),
                "role_assignments": len(role_assignments),
                "relevance_checks": len(relevance_checks),
                "work_proposals": len(work_proposals),
                "human_response_requests": len(human_response_requests),
                "human_response_submissions": len(human_response_submissions),
                "context_summaries": len(context_summaries),
                "retention_expiry_records": len(retention_expiry_records),
            },
            "work_item_states": states,
            "queue_statuses": queue_statuses,
            "delivery_statuses": delivery_statuses,
            "connector_metrics": connector_metrics,
            "queue_items": queue_items,
            "work_items": work_items,
            "agent_runs": self.list_agent_runs(),
            "safe_output_calls": redacted_safe_output_calls,
            "artifacts": self.list_artifacts(),
            "releases": releases,
            "connectors": connectors,
            "connector_participants": self.list_connector_participants(),
            "conversations": self.list_conversations(),
            "conversation_events": redacted_connector_events,
            "external_event_receipts": redacted_external_event_receipts,
            "thread_bindings": self.list_thread_bindings(),
            "delivery_records": redacted_delivery_records,
            "delivery_attempts": delivery_attempts,
            "connector_attention_items": connector_attention_items,
            "connector_permission_checks": connector_permission_checks,
            "role_assignments": role_assignments,
            "relevance_checks": relevance_checks,
            "work_proposals": work_proposals,
            "human_response_requests": redacted_human_response_requests,
            "human_response_submissions": human_response_submissions,
            "context_summaries": redacted_context_summaries,
            "retention_expiry_records": retention_expiry_records,
            "recent_events": self.list_events()[-50:],
        }


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in (
        "payload_json",
        "health_json",
        "authority_json",
        "metadata_json",
        "source_refs_json",
        "durable_refs_json",
    ):
        if isinstance(result.get(key), str):
            result[key.removesuffix("_json")] = json.loads(result[key])
            del result[key]
    for key in ("retryable", "noop", "required", "broad_graph"):
        if key in result and result[key] is not None:
            result[key] = bool(result[key])
    return result


def _redact_private_conversation_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("visibility_scope") == "private":
            item["body_preview"] = "[redacted private conversation]"
            payload = item.get("payload")
            if isinstance(payload, dict):
                item["payload"] = {**payload, "body_redacted": True}
        redacted.append(item)
    return redacted


def _redact_private_external_event_receipts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        payload = item.get("payload")
        if isinstance(payload, dict) and payload.get("source_type") in {"dm", "private_channel"}:
            item["payload"] = _redact_payload_body(payload)
        redacted.append(item)
    return redacted


def _redact_private_delivery_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        payload = item.get("payload")
        if item.get("destination_type") == "dm" and isinstance(payload, dict):
            item["payload"] = _redact_payload_body(payload)
            item["payload"] = _redact_payload_card(item["payload"])
        redacted.append(item)
    return redacted


def _redact_private_safe_output_calls(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    private_tools = {"status.reply", "sponsor.ask_question"}
    private_response_tools = {"human_response.request", "release.request_approval"}
    redacted: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        payload = item.get("payload")
        if item.get("tool_name") in private_tools and isinstance(payload, dict):
            scrubbed = dict(payload)
            for key in ("message", "question", "reason"):
                if key in scrubbed:
                    scrubbed[key] = "[redacted private conversation]"
                    scrubbed[f"{key}_redacted"] = True
            item["payload"] = scrubbed
        if (
            item.get("tool_name") in private_response_tools
            and isinstance(payload, dict)
            and payload.get("destination_type") == "dm"
        ):
            scrubbed = dict(payload)
            for key in ("title", "question"):
                if key in scrubbed:
                    scrubbed[key] = "[redacted private conversation]"
                    scrubbed[f"{key}_redacted"] = True
            item["payload"] = scrubbed
        redacted.append(item)
    return redacted


def _redact_private_human_response_requests(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("destination_type") == "dm":
            item["title"] = "[redacted private conversation]"
            item["title_redacted"] = True
            item["question"] = "[redacted private conversation]"
            item["question_redacted"] = True
            payload = item.get("payload")
            if isinstance(payload, dict):
                item["payload"] = _redact_payload_card(payload)
        redacted.append(item)
    return redacted


def _redact_private_context_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("visibility_scope") == "private":
            item["summary"] = "[redacted private conversation]"
            item["summary_redacted"] = True
        redacted.append(item)
    return redacted


def _redact_payload_body(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(payload)
    if "body" in redacted:
        redacted["body"] = "[redacted private conversation]"
        redacted["body_redacted"] = True
    return redacted


def _redact_payload_card(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(payload)
    card = redacted.get("card")
    if isinstance(card, dict):
        redacted_card = dict(card)
        for key in ("title", "question"):
            if key in redacted_card:
                redacted_card[key] = "[redacted private conversation]"
                redacted_card[f"{key}_redacted"] = True
        redacted["card"] = redacted_card
    return redacted
