from __future__ import annotations

import html
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.observability import configure_observability
from agentic_mesh_v2.observability import span


class V2StatusHandler(BaseHTTPRequestHandler):
    db_path: Path

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        with span("v2.http_request", http_method="GET", http_route=path):
            if path in {"/", "/status"}:
                self._send_html(self._render_status())
                return
            if path == "/status.json":
                self._send_json(self._snapshot())
                return
            if path == "/healthz":
                self._send_json({"status": "ok", "runtime": "agentic_mesh_v2"})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _snapshot(self) -> dict[str, object]:
        db = V2Database(self.db_path)
        try:
            db.migrate()
            return db.status_snapshot()
        finally:
            db.close()

    def _render_status(self) -> str:
        snapshot = self._snapshot()
        counts = snapshot["counts"]
        metrics = snapshot.get("connector_metrics") if isinstance(snapshot.get("connector_metrics"), dict) else {}
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Agentic Mesh V2 Status</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.35; color: #111827; }}
    a {{ color: #1d4ed8; }}
    .banner {{ border: 1px solid #cbd5e1; background: #f8fafc; padding: 0.8rem 1rem; margin: 1rem 0; }}
    .tiles {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1rem 0; }}
    .tile {{ border: 1px solid #d1d5db; padding: 0.55rem 0.7rem; min-width: 8rem; }}
    .tile span {{ color: #4b5563; display: block; font-size: 0.8rem; }}
    .tile strong {{ display: block; font-size: 1.25rem; }}
    table {{ border-collapse: collapse; width: 100%; margin: 0.8rem 0 1.5rem; table-layout: fixed; font-size: 0.92rem; }}
    th, td {{ border: 1px solid #d1d5db; padding: 0.38rem 0.5rem; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ background: #f3f4f6; text-align: left; }}
    .status {{ font-weight: 700; }}
    .muted {{ color: #6b7280; }}
    .small {{ font-size: 0.8rem; }}
    .section-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(22rem, 1fr)); gap: 1rem; }}
    .section-grid > section {{ min-width: 0; }}
    code {{ background: #f3f4f6; border: 1px solid #e5e7eb; padding: 0.05rem 0.2rem; }}
  </style>
</head>
<body>
  <h1>Agentic Mesh V2 Status</h1>
  <p><a href="/status.json">Status JSON</a> · <a href="/healthz">Health</a></p>
  <div class="banner">
    <strong>Runtime:</strong> agentic_mesh_v2<br>
    <strong>Database:</strong> {html.escape(str(snapshot["database"]))}<br>
    <strong>OTEL:</strong> {html.escape(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "not configured"))}
  </div>
  <h2>Counts</h2>
  <div class="tiles">
    {self._count_tile("Queue items", counts["queue_items"])}
    {self._count_tile("Work items", counts["work_items"])}
    {self._count_tile("Agent runs", counts["agent_runs"])}
    {self._count_tile("Safe outputs", counts["safe_output_calls"])}
    {self._count_tile("Artifacts", counts["artifacts"])}
    {self._count_tile("Releases", counts["releases"])}
    {self._count_tile("Deployment targets", counts.get("deployment_targets", 0))}
    {self._count_tile("Deployment runs", counts.get("deployment_runs", 0))}
    {self._count_tile("Connectors", counts["connectors"])}
    {self._count_tile("Role identities", counts["connector_participants"])}
    {self._count_tile("Role instances", counts.get("role_instance_statuses", 0))}
    {self._count_tile("Container actions", counts.get("role_container_lifecycle_actions", 0))}
    {self._count_tile("Conversation events", counts["conversation_events"])}
    {self._count_tile("Deliveries", counts["delivery_records"])}
    {self._count_tile("Relevance checks", counts["relevance_checks"])}
    {self._count_tile("Context summaries", counts["context_summaries"])}
    {self._count_tile("Permission checks", counts["connector_permission_checks"])}
    {self._count_tile("Connector attention", counts["connector_attention_items"])}
    {self._count_tile("Runtime attention", counts.get("runtime_attention_items", 0))}
  </div>
  <h2>Connector Metrics</h2>
  <div class="tiles">
    {self._count_tile("Inbound events", metrics.get("inbound_events", 0))}
    {self._count_tile("Duplicates suppressed", metrics.get("duplicates_suppressed", 0))}
    {self._count_tile("Active conversations", metrics.get("active_conversations", 0))}
    {self._count_tile("Delivery failures", metrics.get("delivery_failures", 0))}
    {self._count_tile("Ambiguous bindings", metrics.get("ambiguous_bindings", 0))}
    {self._count_tile("Private DM promotions", metrics.get("private_dm_promotions", 0))}
    {self._count_tile("Compactions", metrics.get("compaction_count", 0))}
    {self._count_tile("Permission failures", metrics.get("permission_failures", 0))}
  </div>
  <h2>Connector Health</h2>
  {self._connector_table(snapshot["connectors"])}
  <div class="section-grid">
    <section>
      <h2>Role Identities</h2>
      {self._role_identity_table(snapshot["connector_participants"])}
    </section>
    <section>
      <h2>Role Assignments</h2>
      {self._role_assignment_table(snapshot["role_assignments"])}
    </section>
    <section>
      <h2>Role Instances</h2>
      {self._role_instance_table(snapshot["role_instance_statuses"])}
    </section>
    <section>
      <h2>Channel Bindings</h2>
      {self._channel_binding_table(snapshot["connectors"])}
    </section>
  </div>
  <h2>Conversations</h2>
  {self._conversation_table(snapshot["conversations"])}
  <h2>Conversation Events</h2>
  {self._conversation_event_table(snapshot["conversation_events"])}
  <h2>Delivery Records</h2>
  {self._delivery_table(snapshot["delivery_records"])}
  <h2>Permission Checks</h2>
  {self._permission_table(snapshot["connector_permission_checks"])}
  <h2>Relevance Checks</h2>
  {self._relevance_table(snapshot["relevance_checks"])}
  <h2>Work Proposals</h2>
  {self._proposal_table(snapshot["work_proposals"])}
  <h2>Human Responses</h2>
  {self._human_response_table(snapshot["human_response_requests"])}
  <h2>Context Summaries</h2>
  {self._context_summary_table(snapshot["context_summaries"])}
  <h2>Connector Attention</h2>
  {self._connector_attention_table(snapshot["connector_attention_items"])}
  <h2>Runtime Attention</h2>
  {self._runtime_attention_table(snapshot.get("runtime_attention_items", []))}
  <h2>Work Items</h2>
  {self._work_items_table(snapshot["work_items"])}
  <h2>Queue</h2>
  {self._queue_table(snapshot["queue_items"])}
  <h2>Releases</h2>
  {self._release_table(snapshot["releases"])}
  <h2>Deployment Targets</h2>
  {self._deployment_target_table(snapshot["deployment_targets"])}
  <h2>Deployment Runs</h2>
  {self._deployment_run_table(snapshot["deployment_runs"])}
  <h2>Role Container Lifecycle Actions</h2>
  {self._role_container_lifecycle_table(snapshot["role_container_lifecycle_actions"])}
  <h2>Release Evidence Links</h2>
  {self._release_evidence_table(snapshot["release_evidence_links"])}
  <h2>Recent Events</h2>
  {self._events_table(snapshot["recent_events"])}
</body>
</html>"""

    def _count_tile(self, label: str, value: object) -> str:
        return f'<div class="tile"><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></div>'

    def _work_items_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 work items.</p>'
        body = []
        for row in items:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['work_item_id'])}</code></td>"
                f"<td>{_e(row['title'])}<br><span class=\"muted\">{_e(row['description'])}</span></td>"
                f"<td><span class=\"status\">{_e(row['state'])}</span><br>{_e(row.get('current_role') or '')}</td>"
                f"<td>{_e(row.get('next_action') or '')}</td>"
                f"<td>{_e(row['updated_at'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Item</th><th>Summary</th><th>State / Role</th><th>Next Action</th><th>Updated</th></tr>" + "".join(body) + "</table>"

    def _connector_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 connectors.</p>'
        body = []
        for row in items:
            health = row.get("health") if isinstance(row.get("health"), dict) else {}
            body.append(
                "<tr>"
                f"<td><code>{_e(row['connector_id'])}</code><br><span class=\"muted\">{_e(row['display_name'])}</span></td>"
                f"<td>{_e(row['connector_type'])}<br>{_e(row['project_id'])}</td>"
                f"<td><span class=\"status\">{_e(row['status'])}</span></td>"
                f"<td>{_e(health.get('project_team_ref', ''))}<br>{_e(health.get('default_project_channel_ref', ''))}</td>"
                f"<td>{_e(row['updated_at'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Connector</th><th>Type / Project</th><th>Status</th><th>Team / Channel</th><th>Updated</th></tr>" + "".join(body) + "</table>"

    def _delivery_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 delivery records.</p>'
        body = []
        for row in items[:20]:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['delivery_id'])}</code><br><span class=\"muted\">{_e(row['purpose'])}</span></td>"
                f"<td><span class=\"status\">{_e(row['status'])}</span><br>{_e(row.get('error_class') or '')}</td>"
                f"<td>{_e(row['destination_type'])}<br>{_e(row['destination_ref'])}</td>"
                f"<td>{_e(row.get('external_message_id') or '')}</td>"
                f"<td>{_e(row['updated_at'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Delivery</th><th>Status / Error</th><th>Destination</th><th>External Message</th><th>Updated</th></tr>" + "".join(body) + "</table>"

    def _role_identity_table(self, rows: object) -> str:
        items = [
            row for row in (list(rows) if isinstance(rows, list) else [])
            if row.get("participant_type") == "role"
        ]
        if not items:
            return '<p class="muted">No v2 role identities.</p>'
        body = []
        for row in items:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            body.append(
                "<tr>"
                f"<td>{_e(row.get('role_id'))}<br><span class=\"muted\">{_e(row.get('display_name'))}</span></td>"
                f"<td>{_e(row.get('external_ref'))}<br>{_e(metadata.get('mention_handle', ''))}</td>"
                f"<td>{_e(metadata.get('identity_model', ''))}<br>{_e(metadata.get('alias', ''))}</td>"
                f"<td>{_e(metadata.get('enabled', ''))}</td>"
                "</tr>"
            )
        return "<table><tr><th>Role</th><th>External / Mention</th><th>Model / Alias</th><th>Enabled</th></tr>" + "".join(body) + "</table>"

    def _role_assignment_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 role assignments.</p>'
        body = []
        for row in items[:30]:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['assignment_id'])}</code><br>{_e(row['assignment_type'])}</td>"
                f"<td>{_e(row['role_id'])}<br>{_e(row.get('role_instance_id') or '')}</td>"
                f"<td><span class=\"status\">{_e(row['status'])}</span><br>{_e(row.get('terminal_tool') or '')}</td>"
                f"<td>{_e(row['title'])}<br><span class=\"muted\">{_e(row['summary'])}</span></td>"
                f"<td>{_e(row.get('work_item_id') or '')}<br>{_e(row.get('source_ref') or '')}</td>"
                f"<td>{_e(row.get('run_id') or '')}<br>{_e(row.get('failure_reason') or '')}</td>"
                f"<td>{_e(row.get('claim_expires_at') or '')}<br>{_e(row.get('recovery_count') or 0)}</td>"
                "</tr>"
            )
        return "<table><tr><th>Assignment</th><th>Role / Instance</th><th>Status / Terminal</th><th>Summary</th><th>Work / Source</th><th>Run / Failure</th><th>Lease / Recoveries</th></tr>" + "".join(body) + "</table>"

    def _role_instance_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 role instance status.</p>'
        body = []
        for row in items[:30]:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['role_instance_id'])}</code><br>{_e(row['role_id'])}</td>"
                f"<td><span class=\"status\">{_e(row['status'])}</span><br>{_e(row.get('detail') or '')}</td>"
                f"<td>{_e(row.get('current_assignment_id') or '')}<br>{_e(row.get('last_run_id') or '')}</td>"
                f"<td>{_e(row.get('processed_count') or 0)}</td>"
                f"<td>{_e(row['heartbeat_at'])}<br>{_e(row.get('hibernated_at') or '')}</td>"
                f"<td>{_e(row.get('hibernation_reason') or '')}<br>{_e(row.get('wake_reason') or '')}</td>"
                "</tr>"
            )
        return "<table><tr><th>Instance / Role</th><th>Status / Detail</th><th>Assignment / Run</th><th>Processed</th><th>Heartbeat / Hibernated</th><th>Hibernate / Wake Reason</th></tr>" + "".join(body) + "</table>"

    def _role_container_lifecycle_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 role container lifecycle actions.</p>'
        body = []
        for row in items[:30]:
            command = row.get("command")
            command_text = " ".join(str(item) for item in command) if isinstance(command, list) else ""
            body.append(
                "<tr>"
                f"<td><code>{_e(row['action_id'])}</code><br>{_e(row['role_id'])}<br>{_e(row['role_instance_id'])}</td>"
                f"<td><span class=\"status\">{_e(row['status'])}</span><br>{_e(row['action'])}<br>{_e(row['service_name'])}</td>"
                f"<td>{_e(command_text)}<br><span class=\"muted\">{_e(row.get('working_directory') or '')}</span></td>"
                f"<td>{_e(row.get('exit_code') if row.get('exit_code') is not None else '')}<br>{_e(row.get('stderr') or '')}</td>"
                f"<td>{_e(row.get('reason') or '')}</td>"
                "</tr>"
            )
        return "<table><tr><th>Action / Role</th><th>Status / Service</th><th>Command / CWD</th><th>Exit / Error</th><th>Reason</th></tr>" + "".join(body) + "</table>"

    def _channel_binding_table(self, rows: object) -> str:
        connectors = list(rows) if isinstance(rows, list) else []
        bindings = []
        for connector in connectors:
            health = connector.get("health") if isinstance(connector.get("health"), dict) else {}
            default = health.get("default_project_channel_ref")
            if default:
                bindings.append(
                    {
                        "channel_ref": default,
                        "scope_type": "project",
                        "display_name": "Project",
                        "visibility": "project",
                        "work_scope": "",
                        "private": False,
                    }
                )
            configured_bindings = health.get("channel_bindings", [])
            if not isinstance(configured_bindings, list):
                configured_bindings = []
            for item in configured_bindings:
                if isinstance(item, dict):
                    bindings.append(item)
        if not bindings:
            return '<p class="muted">No v2 channel bindings.</p>'
        body = []
        for binding in bindings:
            body.append(
                "<tr>"
                f"<td><code>{_e(binding.get('channel_ref'))}</code><br>{_e(binding.get('display_name'))}</td>"
                f"<td>{_e(binding.get('scope_type'))}<br>{_e(binding.get('work_scope') or '')}</td>"
                f"<td>{_e(binding.get('visibility'))}<br>{_e(binding.get('private'))}</td>"
                "</tr>"
            )
        return "<table><tr><th>Channel</th><th>Scope / Work</th><th>Visibility / Private</th></tr>" + "".join(body) + "</table>"

    def _conversation_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 conversations.</p>'
        body = []
        for row in items[:20]:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['conversation_id'])}</code><br>{_e(row['connector'])}</td>"
                f"<td>{_e(row['external_ref'])}</td>"
                f"<td>{_e(row.get('sponsor_ref') or '')}</td>"
                f"<td>{_e(row['updated_at'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Conversation</th><th>External Ref</th><th>Sponsor</th><th>Updated</th></tr>" + "".join(body) + "</table>"

    def _conversation_event_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 conversation events.</p>'
        body = []
        for row in items[:20]:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            channel = payload.get("channel_scope") if isinstance(payload.get("channel_scope"), dict) else {}
            body.append(
                "<tr>"
                f"<td><code>{_e(row['conversation_event_id'])}</code><br>{_e(row['event_type'])}</td>"
                f"<td>{_e(row['visibility_scope'])}<br>{_e(row.get('role_id') or '')}</td>"
                f"<td>{_e(channel.get('display_name', ''))}<br>{_e(channel.get('work_scope', ''))}</td>"
                f"<td>{_e(row.get('body_preview', ''))}</td>"
                "</tr>"
            )
        return "<table><tr><th>Event</th><th>Visibility / Role</th><th>Channel / Work</th><th>Preview</th></tr>" + "".join(body) + "</table>"

    def _permission_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 permission checks.</p>'
        body = []
        for row in items[:30]:
            body.append(
                "<tr>"
                f"<td>{_e(row['check_type'])}<br><code>{_e(row['capability'])}</code></td>"
                f"<td><span class=\"status\">{_e(row['status'])}</span><br>{_e(row['actual_status'])}</td>"
                f"<td>{_e(row['phase'])}<br>{_e(row.get('consent_type') or '')}</td>"
                f"<td>{_e(row.get('permission_name') or '')}<br>{_e('broad' if row.get('broad_graph') else '')}</td>"
                f"<td>{_e(row.get('approval_ref') or '')}<br><span class=\"small\">{_e(row['next_action'])}</span></td>"
                "</tr>"
            )
        return "<table><tr><th>Check</th><th>Status / Actual</th><th>Phase / Consent</th><th>Permission</th><th>Approval / Action</th></tr>" + "".join(body) + "</table>"

    def _relevance_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 relevance checks.</p>'
        body = []
        for row in items[:20]:
            body.append(
                "<tr>"
                f"<td>{_e(row['role_id'])}<br><code>{_e(row['conversation_event_id'])}</code></td>"
                f"<td><span class=\"status\">{_e(row['decision'])}</span><br>{_e(row['score'])} / {_e(row['threshold'])}</td>"
                f"<td>{_e(row['reason'])}</td>"
                f"<td>{_e(row.get('noop'))}<br>{_e(row.get('delivery_ref') or '')}</td>"
                "</tr>"
            )
        return "<table><tr><th>Role / Event</th><th>Decision / Score</th><th>Reason</th><th>No-op / Delivery</th></tr>" + "".join(body) + "</table>"

    def _proposal_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 work proposals.</p>'
        body = []
        for row in items[:20]:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['queue_item_id'])}</code><br>{_e(row['work_type'])}</td>"
                f"<td>{_e(row['classification'])}<br>{_e(row['urgency'])}</td>"
                f"<td>{_e(row['proposed_by_role'])}<br>{_e(row['suggested_owner'])}</td>"
                f"<td>{_e(row['rationale'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Queue / Type</th><th>Class / Urgency</th><th>Proposer / Owner</th><th>Rationale</th></tr>" + "".join(body) + "</table>"

    def _human_response_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 human response requests.</p>'
        body = []
        for row in items[:20]:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['request_id'])}</code><br>{_e(row['request_type'])}</td>"
                f"<td><span class=\"status\">{_e(row['status'])}</span><br>{_e(row['required_authority'])}</td>"
                f"<td>{_e(row['title'])}<br><span class=\"muted\">{_e(row['question'])}</span></td>"
                f"<td>{_e(row.get('work_item_id') or '')}<br>{_e(row.get('gate_id') or '')}</td>"
                "</tr>"
            )
        return "<table><tr><th>Request</th><th>Status / Authority</th><th>Title / Question</th><th>Work / Gate</th></tr>" + "".join(body) + "</table>"

    def _context_summary_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 context summaries.</p>'
        body = []
        for row in items[:20]:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['summary_id'])}</code><br>{_e(row['classification'])}</td>"
                f"<td>{_e(row['visibility_scope'])}<br>{_e(row['retention_key'])}</td>"
                f"<td>{_e(row['summary'])}</td>"
                f"<td>{_e(', '.join(row.get('durable_refs') or []))}<br>{_e(row.get('target_ref') or '')}</td>"
                "</tr>"
            )
        return "<table><tr><th>Summary</th><th>Visibility / Retention</th><th>Text</th><th>Durable / Target</th></tr>" + "".join(body) + "</table>"

    def _connector_attention_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 connector attention items.</p>'
        body = []
        for row in items:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['attention_id'])}</code><br>{_e(row['connector_id'])}</td>"
                f"<td><span class=\"status\">{_e(row['reason_class'])}</span><br>{_e(row['status'])}</td>"
                f"<td>{_e(row['owner'])}</td>"
                f"<td>{_e(row['next_action'])}</td>"
                f"<td>{_e(row['retryable'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Attention</th><th>Reason / Status</th><th>Owner</th><th>Next Action</th><th>Retryable</th></tr>" + "".join(body) + "</table>"

    def _runtime_attention_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 runtime attention items.</p>'
        body = []
        for row in items:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['attention_id'])}</code><br>{_e(row['source_type'])}<br>{_e(row['source_ref'])}</td>"
                f"<td><span class=\"status\">{_e(row['reason_class'])}</span><br>{_e(row['status'])}</td>"
                f"<td>{_e(row['owner'])}</td>"
                f"<td>{_e(row['next_action'])}</td>"
                f"<td>{_e(row['retryable'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Attention / Source</th><th>Reason / Status</th><th>Owner</th><th>Next Action</th><th>Retryable</th></tr>" + "".join(body) + "</table>"

    def _queue_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 queue items.</p>'
        body = []
        for row in items:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['queue_item_id'])}</code></td>"
                f"<td>{_e(row['title'])}<br><span class=\"muted\">{_e(row['summary'])}</span></td>"
                f"<td>{_e(row['status'])}</td>"
                f"<td>{_e(row['owner_role'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Queue Item</th><th>Summary</th><th>Status</th><th>Owner</th></tr>" + "".join(body) + "</table>"

    def _release_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 releases.</p>'
        body = []
        for row in items:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['release_id'])}</code><br><span class=\"muted\">{_e(row['work_item_id'])}</span></td>"
                f"<td>{_e(row['status'])}</td>"
                f"<td>{_e(row['deployment_result'] or '')}<br>{_e(row['smoke_result'] or '')}</td>"
                f"<td>{_e(row['rollback_plan'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Release</th><th>Status</th><th>Deployment / Smoke</th><th>Rollback</th></tr>" + "".join(body) + "</table>"

    def _deployment_target_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 deployment targets.</p>'
        body = []
        for row in items:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['target_id'])}</code><br>{_e(row['target_type'])}</td>"
                f"<td>{_e(row['project_id'])}<br>{_e(row.get('connector_id') or '')}</td>"
                f"<td><span class=\"status\">{_e(row['status'])}</span><br>{_e(row.get('disable_reason') or '')}</td>"
                f"<td>{_e(row['service_name'])}<br>{_e(row.get('external_base_url') or '')}</td>"
                "</tr>"
            )
        return "<table><tr><th>Target</th><th>Project / Connector</th><th>Status / Disable Reason</th><th>Service / URL</th></tr>" + "".join(body) + "</table>"

    def _deployment_run_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 deployment runs.</p>'
        body = []
        for row in items:
            body.append(
                "<tr>"
                f"<td><code>{_e(row['run_id'])}</code><br>{_e(row['target_id'])}</td>"
                f"<td>{_e(row['work_item_id'])}<br>{_e(row['release_id'])}</td>"
                f"<td><span class=\"status\">{_e(row['status'])}</span><br>{_e(row['smoke_result'])}</td>"
                f"<td>{_e(row['rollback_plan'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Run / Target</th><th>Work / Release</th><th>Status / Smoke</th><th>Rollback</th></tr>" + "".join(body) + "</table>"

    def _release_evidence_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 release evidence links.</p>'
        body = []
        for row in items:
            body.append(
                "<tr>"
                f"<td>{_e(row['artifact_type'])}<br>{_e(row['role_id'])}</td>"
                f"<td><code>{_e(row['artifact_ref'])}</code></td>"
                f"<td>{_e(row['status'])}</td>"
                f"<td>{_e(row['release_id'])}</td>"
                "</tr>"
            )
        return "<table><tr><th>Type / Role</th><th>Artifact</th><th>Status</th><th>Release</th></tr>" + "".join(body) + "</table>"

    def _events_table(self, rows: object) -> str:
        items = list(rows) if isinstance(rows, list) else []
        if not items:
            return '<p class="muted">No v2 events.</p>'
        body = []
        for row in reversed(items[-20:]):
            body.append(
                "<tr>"
                f"<td>{_e(row['event_id'])}</td>"
                f"<td>{_e(row['event_type'])}</td>"
                f"<td><code>{_e(row['aggregate_id'])}</code></td>"
                "</tr>"
            )
        return "<table><tr><th>#</th><th>Event</th><th>Aggregate</th></tr>" + "".join(body) + "</table>"

    def _send_json(self, payload: object) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(*, host: str, port: int, db_path: Path) -> None:
    configure_observability("agentic-mesh-v2-runtime")
    db = V2Database(db_path)
    try:
        db.migrate()
    finally:
        db.close()
    V2StatusHandler.db_path = Path(db_path)
    server = ThreadingHTTPServer((host, port), V2StatusHandler)
    server.serve_forever()


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))
