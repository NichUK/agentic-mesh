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
    table {{ border-collapse: collapse; width: 100%; margin: 0.8rem 0 1.5rem; table-layout: fixed; }}
    th, td {{ border: 1px solid #d1d5db; padding: 0.45rem 0.55rem; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ background: #f3f4f6; text-align: left; }}
    .status {{ font-weight: 700; }}
    .muted {{ color: #6b7280; }}
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
    {self._count_tile("Connectors", counts["connectors"])}
    {self._count_tile("Conversation events", counts["conversation_events"])}
    {self._count_tile("Deliveries", counts["delivery_records"])}
    {self._count_tile("Connector attention", counts["connector_attention_items"])}
  </div>
  <h2>Connector Health</h2>
  {self._connector_table(snapshot["connectors"])}
  <h2>Delivery Records</h2>
  {self._delivery_table(snapshot["delivery_records"])}
  <h2>Connector Attention</h2>
  {self._connector_attention_table(snapshot["connector_attention_items"])}
  <h2>Work Items</h2>
  {self._work_items_table(snapshot["work_items"])}
  <h2>Queue</h2>
  {self._queue_table(snapshot["queue_items"])}
  <h2>Releases</h2>
  {self._release_table(snapshot["releases"])}
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
