from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentStatus:
    role_instance_id: str
    container_state: str
    heartbeat_at: str | None
    current_work: str | None = None
    inbox_depth: int = 0
    governance_waits: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkItemStatus:
    work_item_id: str
    title: str
    state: str
    owner_role: str
    next_action: str
    artifact_count: int = 0


@dataclass(frozen=True)
class BacklogItemStatus:
    queue_item_id: str
    title: str
    status: str
    owner_role: str
    linked_work_item_id: str | None = None


@dataclass(frozen=True)
class ReportingSnapshot:
    project_id: str
    backlog: tuple[BacklogItemStatus, ...] = ()
    work_items: tuple[WorkItemStatus, ...] = ()
    agents: tuple[AgentStatus, ...] = ()
    recent_completions: tuple[WorkItemStatus, ...] = ()


def render_status_page(snapshot: ReportingSnapshot) -> str:
    return _page(
        "Status",
        [
            f"<h1>Agentic Mesh V3 Status</h1><p><strong>Project:</strong> {html.escape(snapshot.project_id)}</p>",
            "<h2>Backlog / Queue</h2>",
            _backlog_table(snapshot.backlog),
            "<h2>Active Work</h2>",
            _work_table(snapshot.work_items),
            "<h2>Recent Completions</h2>",
            _work_table(snapshot.recent_completions),
        ],
    )


def render_agents_page(snapshot: ReportingSnapshot) -> str:
    rows = [
        "<tr><th>Agent</th><th>Container</th><th>Heartbeat</th><th>Inbox</th><th>Current Work</th><th>Governance Waits</th></tr>"
    ]
    for agent in snapshot.agents:
        rows.append(
            "<tr>"
            f"<td>{html.escape(agent.role_instance_id)}</td>"
            f"<td>{html.escape(agent.container_state)}</td>"
            f"<td>{html.escape(agent.heartbeat_at or 'unknown')}</td>"
            f"<td>{agent.inbox_depth}</td>"
            f"<td>{html.escape(agent.current_work or '')}</td>"
            f"<td>{html.escape(', '.join(agent.governance_waits))}</td>"
            "</tr>"
        )
    return _page("Agents", ["<h1>Agents</h1>", f"<table>{''.join(rows)}</table>"])


def render_work_item_page(snapshot: ReportingSnapshot, work_item_id: str) -> str:
    item = next((candidate for candidate in snapshot.work_items if candidate.work_item_id == work_item_id), None)
    if item is None:
        item = next((candidate for candidate in snapshot.recent_completions if candidate.work_item_id == work_item_id), None)
    if item is None:
        return _page("Work Item Not Found", [f"<h1>Work item not found</h1><p>{html.escape(work_item_id)}</p>"])
    return _page(
        item.title,
        [
            f"<h1>{html.escape(item.title)}</h1>",
            f"<p><strong>Work item:</strong> {html.escape(item.work_item_id)}</p>",
            f"<p><strong>State:</strong> {html.escape(item.state)}</p>",
            f"<p><strong>Owner:</strong> {html.escape(item.owner_role)}</p>",
            f"<p><strong>Next action:</strong> {html.escape(item.next_action)}</p>",
            f"<p><strong>Artifacts:</strong> {item.artifact_count}</p>",
        ],
    )


def artifact_viewer_path(work_item_id: str, artifact_filename: str) -> str:
    clean_name = artifact_filename.replace("\\", "/").lstrip("/")
    if "\x00" in clean_name or clean_name.startswith("../") or "/../" in clean_name:
        raise ValueError("invalid artifact filename")
    return f"work-items/{work_item_id}/{clean_name}"


def _backlog_table(items: tuple[BacklogItemStatus, ...]) -> str:
    rows = ["<tr><th>Queue Item</th><th>Title</th><th>Status</th><th>Owner</th><th>Linked Work</th></tr>"]
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.queue_item_id)}</td>"
            f"<td>{html.escape(item.title)}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.owner_role)}</td>"
            f"<td>{html.escape(item.linked_work_item_id or '')}</td>"
            "</tr>"
        )
    return f"<table>{''.join(rows)}</table>"


def _work_table(items: tuple[WorkItemStatus, ...]) -> str:
    rows = ["<tr><th>Work Item</th><th>Title</th><th>State</th><th>Owner</th><th>Next Action</th></tr>"]
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.work_item_id)}</td>"
            f"<td>{html.escape(item.title)}</td>"
            f"<td>{html.escape(item.state)}</td>"
            f"<td>{html.escape(item.owner_role)}</td>"
            f"<td>{html.escape(item.next_action)}</td>"
            "</tr>"
        )
    return f"<table>{''.join(rows)}</table>"


def _page(title: str, sections: list[str]) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.35}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "th,td{border:1px solid #d1d5db;padding:.4rem;text-align:left;vertical-align:top}"
        "th{background:#f3f4f6}</style></head><body>"
        + "\n".join(sections)
        + "</body></html>"
    )
