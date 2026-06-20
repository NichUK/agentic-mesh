from __future__ import annotations

import html
from pathlib import Path
from typing import Any


def render_status(snapshot: dict[str, Any]) -> str:
    messages = snapshot["messages"]
    active = [item for item in messages if item["state"] in {"delivering", "active_turn", "steered"}]
    queued = [item for item in messages if item["state"] == "queued"]
    failed = [item for item in messages if item["state"] in {"failed", "dead_lettered"}]
    completed = [item for item in messages if item["state"] == "completed"]
    return _page(
        "Agentic Mesh V4 Status",
        [
            "<h1>Agentic Mesh V4 Status</h1>",
            _nav(),
            "<h2>Queue</h2>",
            _message_table(queued, empty="No queued messages."),
            "<h2>Active</h2>",
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
        rows.append(
            "<tr>"
            f"<td><a href=\"/agent/{html.escape(item['role_id'])}/thread\">{html.escape(item['display_name'])}</a></td>"
            f"<td>{html.escape(item['state'])}</td>"
            f"<td>{html.escape(item['authority'])}</td>"
            f"<td>{html.escape(item['codex_endpoint'])}</td>"
            f"<td>{html.escape(str(item.get('active_thread_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('memory_version') or 0))}</td>"
            "</tr>"
        )
    return _page(
        "Agentic Mesh V4 Agents",
        [
            "<h1>Agents</h1>",
            _nav(),
            "<table><thead><tr><th>Agent</th><th>State</th><th>Authority</th><th>Codex endpoint</th><th>Thread</th><th>Memory</th></tr></thead>"
            f"<tbody>{''.join(rows) or '<tr><td colspan=\"6\">No roles configured.</td></tr>'}</tbody></table>",
        ],
    )


def render_agent_thread(role_id: str, events: list[dict[str, Any]]) -> str:
    rows = []
    for item in events:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item['created_at'])}</td>"
            f"<td>{html.escape(item['event_type'])}</td>"
            f"<td>{html.escape(item.get('turn_id') or '')}</td>"
            f"<td>{html.escape(item.get('content') or '')}</td>"
            "</tr>"
        )
    return _page(
        f"{role_id} Thread",
        [
            f"<h1>{html.escape(role_id)} Thread</h1>",
            _nav(),
            "<table><thead><tr><th>Time</th><th>Event</th><th>Turn</th><th>Content</th></tr></thead>"
            f"<tbody>{''.join(rows) or '<tr><td colspan=\"4\">No stream events recorded.</td></tr>'}</tbody></table>",
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
        rows.append(
            "<tr>"
            f"<td>{html.escape(item['message_id'])}</td>"
            f"<td>{html.escape(item['target_role'])}</td>"
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


def _nav() -> str:
    return '<nav><a href="/status">Status</a> <a href="/agents">Agents</a> <a href="/status.json">JSON</a></nav>'


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
