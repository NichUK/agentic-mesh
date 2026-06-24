from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def render_status(snapshot: dict[str, Any]) -> str:
    messages = snapshot["messages"]
    active = [item for item in messages if item["state"] in {"delivering", "active_turn", "steered"}]
    queued = [item for item in messages if item["state"] == "queued"]
    failed = [item for item in messages if item["state"] in {"failed", "dead_lettered"}]
    completed = [item for item in messages if item["state"] == "completed"]
    work_items = snapshot.get("work_items") or []
    artifacts = snapshot.get("artifacts") or []
    active_work = [
        item
        for item in work_items
        if str(item.get("state") or "").casefold()
        not in {"closed", "cancelled", "canceled", "released", "done", "complete", "completed", "superseded"}
    ]
    return _page(
        "Agentic Mesh V4 Status",
        [
            "<h1>Agentic Mesh V4 Status</h1>",
            _nav(),
            "<h2>Active Work Items And Human/Agent Waits</h2>",
            _work_item_table(active_work, artifacts, empty="No active work items."),
            "<h2>Queue</h2>",
            _message_table(queued, empty="No queued messages."),
            "<h2>Active Agent Turns</h2>",
            _message_table(active, empty="No active turns."),
            "<h2>Attention Needed</h2>",
            _message_table(failed, empty="No failed messages."),
            "<h2>Recent Completions</h2>",
            _message_table(completed[-20:], empty="No completions recorded."),
        ],
    )


def render_agents(snapshot: dict[str, Any]) -> str:
    rows = []
    for item in snapshot["roles"]:
        role_id = str(item["role_id"])
        thread_id = str(item.get("active_thread_id") or "")
        thread_cell = (
            f"<a href=\"/agent/{html.escape(role_id)}/thread\">{html.escape(_short_id(thread_id))}</a>"
            if thread_id
            else ""
        )
        current = item.get("current_message")
        if isinstance(current, dict):
            current_cell = (
                f"<a href=\"/status#{html.escape(str(current.get('message_id') or ''))}\">"
                f"{html.escape(_short_id(str(current.get('message_id') or '')))}</a>"
                f"<br><small>{html.escape(str(current.get('state') or ''))}</small>"
                f"<br>{html.escape(_truncate(str(current.get('text') or ''), 120))}"
            )
        else:
            queued = int(item.get("queued_messages") or 0)
            current_cell = f"{queued} queued" if queued else ""
        rows.append(
            "<tr>"
            f"<td><a href=\"/agent/{html.escape(role_id)}/thread\">{html.escape(item['display_name'])}</a></td>"
            f"<td>{html.escape(str(item.get('effective_state') or item['state']))}</td>"
            f"<td>{html.escape(item['authority'])}</td>"
            f"<td>{html.escape(item['codex_endpoint'])}</td>"
            f"<td>{thread_cell}</td>"
            f"<td>{current_cell}</td>"
            f"<td>{html.escape(str(item.get('memory_count') or 0))}</td>"
            "</tr>"
        )
    return _page(
        "Agentic Mesh V4 Agents",
        [
            "<h1>Agents</h1>",
            _nav(),
            "<table><thead><tr><th>Agent</th><th>State</th><th>Authority</th><th>Codex endpoint</th><th>Thread</th><th>Current message</th><th>Memory</th></tr></thead>"
            f"<tbody>{''.join(rows) or '<tr><td colspan=\"7\">No roles configured.</td></tr>'}</tbody></table>",
        ],
    )


def render_agent_thread(
    role_id: str,
    events: list[dict[str, Any]],
    messages: list[dict[str, Any]] | None = None,
) -> str:
    messages = messages or []
    message_rows = []
    for item in messages:
        message_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('updated_at') or ''))}</td>"
            f"<td>{html.escape(str(item.get('state') or ''))}</td>"
            f"<td><code>{html.escape(str(item.get('message_id') or ''))}</code></td>"
            f"<td>{html.escape(str(item.get('text') or ''))}</td>"
            "</tr>"
        )
    rows = []
    for item in reversed(events):
        payload = str(item.get("payload_json") or "{}")
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('created_at') or ''))}</td>"
            f"<td>{html.escape(str(item.get('event_type') or ''))}</td>"
            f"<td><code>{html.escape(str(item.get('turn_id') or ''))}</code><br>"
            f"<small><code>{html.escape(str(item.get('message_id') or ''))}</code></small></td>"
            f"<td><pre>{html.escape(str(item.get('content') or ''))}</pre>"
            f"<details><summary>Raw event</summary><pre>{html.escape(_pretty_json(payload))}</pre></details></td>"
            "</tr>"
        )
    return _page(
        f"{role_id} Thread",
        [
            f"<h1>{html.escape(role_id)} Thread</h1>",
            _nav(),
            '<p><a href="thread.json">Thread JSON</a>. <span id="live-status">Live push stream connected.</span></p>',
            "<h2>Recent messages</h2>",
            '<table><thead><tr><th>Updated</th><th>State</th><th>Message</th><th>Text</th></tr></thead>'
            f"<tbody id=\"agent-messages\">{''.join(message_rows) or '<tr><td colspan=\"4\">No messages recorded for this role.</td></tr>'}</tbody></table>",
            "<h2>Agent event stream</h2>",
            '<table><thead><tr><th>Time</th><th>Event</th><th>Turn</th><th>Content</th></tr></thead>'
            f"<tbody id=\"agent-events\">{''.join(rows) or '<tr><td colspan=\"4\">No stream events recorded.</td></tr>'}</tbody></table>",
            _agent_thread_live_script(),
        ],
    )


def render_work_item(work_item_id: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        body = [f"<h1>{html.escape(work_item_id)}</h1>", _nav(), "<p>Work item not found.</p>"]
    else:
        item = rows[0]
        body = [
            f"<h1>{html.escape(item['title'])}</h1>",
            _nav(),
            "<table><tbody>"
            f"<tr><th>Work item</th><td>{html.escape(item['work_item_id'])}</td></tr>"
            f"<tr><th>State</th><td>{html.escape(item['state'])}</td></tr>"
            f"<tr><th>Owner</th><td>{html.escape(item['owner_role'])}</td></tr>"
            f"<tr><th>Next action</th><td>{html.escape(item['next_action'])}</td></tr>"
            "</tbody></table>",
        ]
    return _page(f"Work item {work_item_id}", body)


def render_artifact(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return _page("Artifact not found", ["<h1>Artifact not found</h1>", _nav()])
    text = path.read_text(encoding="utf-8", errors="replace")
    return _page(path.name, [f"<h1>{html.escape(path.name)}</h1>", _nav(), f"<pre>{html.escape(text)}</pre>"])


def _message_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        role_id = str(item["target_role"])
        rows.append(
            f"<tr id=\"{html.escape(item['message_id'])}\">"
            f"<td>{html.escape(item['message_id'])}</td>"
            f"<td><a href=\"/agent/{html.escape(role_id)}/thread\">{html.escape(role_id)}</a></td>"
            f"<td>{html.escape(item['state'])}</td>"
            f"<td>{html.escape(item['text'])}</td>"
            f"<td>{html.escape(item['updated_at'])}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"5\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Message</th><th>Role</th><th>State</th><th>Text</th><th>Updated</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _work_item_table(items: list[dict[str, Any]], artifacts: list[dict[str, Any]], *, empty: str) -> str:
    artifacts_by_work: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        work_item_id = str(artifact.get("work_item_id") or "")
        artifacts_by_work.setdefault(work_item_id, []).append(artifact)
    rows = []
    for item in items:
        work_item_id = str(item.get("work_item_id") or "")
        artifact_links = []
        for artifact in artifacts_by_work.get(work_item_id, [])[:8]:
            path = str(artifact.get("path") or "")
            title = str(artifact.get("title") or Path(path).name or "artifact")
            artifact_links.append(
                f"<a href=\"/artifact-viewer/{html.escape(path)}\">{html.escape(title)}</a>"
            )
        artifact_cell = "<br>".join(artifact_links)
        rows.append(
            "<tr>"
            f"<td><a href=\"/work-item/{html.escape(work_item_id)}\">{html.escape(work_item_id)}</a></td>"
            f"<td>{html.escape(str(item.get('title') or ''))}</td>"
            f"<td>{html.escape(str(item.get('state') or ''))}</td>"
            f"<td>{html.escape(str(item.get('owner_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('next_action') or ''))}</td>"
            f"<td>{artifact_cell}</td>"
            f"<td>{html.escape(str(item.get('updated_at') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"7\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Work item</th><th>Title</th><th>State</th><th>Owner</th>"
        "<th>Next action</th><th>Artifacts</th><th>Updated</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _nav() -> str:
    return '<nav><a href="/status">Status</a> <a href="/agents">Agents</a> <a href="/status.json">JSON</a></nav>'


def _short_id(value: str) -> str:
    if len(value) <= 18:
        return value
    return f"{value[:8]}...{value[-6:]}"


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _pretty_json(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return json.dumps(parsed, indent=2, sort_keys=True)


def _agent_thread_live_script() -> str:
    return r"""
<script>
const eventSource = new EventSource("thread/events");

function setText(element, value) {
  element.textContent = value == null ? "" : String(value);
  return element;
}

function makeCell(value, tag = "td") {
  return setText(document.createElement(tag), value);
}

function makeCode(value) {
  const code = document.createElement("code");
  return setText(code, value);
}

function makePre(value) {
  const pre = document.createElement("pre");
  return setText(pre, value);
}

function renderMessages(messages) {
  const body = document.getElementById("agent-messages");
  body.replaceChildren();
  if (!messages.length) {
    const row = document.createElement("tr");
    const cell = makeCell("No messages recorded for this role.");
    cell.colSpan = 4;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const item of messages) {
    const row = document.createElement("tr");
    row.appendChild(makeCell(item.updated_at));
    row.appendChild(makeCell(item.state));
    const message = document.createElement("td");
    message.appendChild(makeCode(item.message_id));
    row.appendChild(message);
    row.appendChild(makeCell(item.text));
    body.appendChild(row);
  }
}

function renderEvents(events) {
  const body = document.getElementById("agent-events");
  body.replaceChildren();
  if (!events.length) {
    const row = document.createElement("tr");
    const cell = makeCell("No stream events recorded.");
    cell.colSpan = 4;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const item of events.slice().reverse()) {
    const row = document.createElement("tr");
    row.appendChild(makeCell(item.created_at));
    row.appendChild(makeCell(item.event_type));
    const turn = document.createElement("td");
    turn.appendChild(makeCode(item.turn_id || ""));
    turn.appendChild(document.createElement("br"));
    const small = document.createElement("small");
    small.appendChild(makeCode(item.message_id || ""));
    turn.appendChild(small);
    row.appendChild(turn);

    const content = document.createElement("td");
    content.appendChild(makePre(item.content || ""));
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Raw event";
    details.appendChild(summary);
    let raw = item.payload_json || "{}";
    try {
      raw = JSON.stringify(JSON.parse(raw), null, 2);
    } catch (_) {}
    details.appendChild(makePre(raw));
    content.appendChild(details);
    row.appendChild(content);
    body.appendChild(row);
  }
}

eventSource.addEventListener("snapshot", (event) => {
  const snapshot = JSON.parse(event.data);
  renderMessages(snapshot.messages || []);
  renderEvents(snapshot.events || []);
});

eventSource.onerror = () => {
  const marker = document.getElementById("live-status");
  if (marker) marker.textContent = "Live stream disconnected; reload to reconnect.";
};
</script>
"""


def _page(title: str, parts: list[str]) -> str:
    return "\n".join(
        [
            "<!doctype html>",
            "<html><head>",
            f"<title>{html.escape(title)}</title>",
            "<style>body{font-family:system-ui,Segoe UI,sans-serif;margin:2rem}table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #d0d7de;padding:.45rem;text-align:left;vertical-align:top}th{background:#f6f8fa}pre{white-space:pre-wrap;background:#f6f8fa;padding:1rem}</style>",
            "</head><body>",
            *parts,
            "</body></html>",
        ]
    )
