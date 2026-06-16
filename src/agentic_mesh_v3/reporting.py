from __future__ import annotations

import html
import json
from dataclasses import dataclass

from agentic_mesh_v3.governance import GovernanceChecklist


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
class ArtifactStatus:
    filename: str
    title: str
    relative_path: str
    document_type: str
    status: str
    created_by_role: str


@dataclass(frozen=True)
class ApprovalStatus:
    approval_id: str
    requested_by_role: str
    question: str
    status: str
    response: str | None = None


@dataclass(frozen=True)
class ReleaseStatus:
    release_id: str
    status: str
    scope: str
    deployment_result: str
    rollback_plan: str
    residual_risks: str


@dataclass(frozen=True)
class GovernanceRecordStatus:
    record_id: str
    record_type: str
    role_instance_id: str
    target_ref: str | None
    summary: str
    status: str


@dataclass(frozen=True)
class WorkItemDetail:
    work_item_id: str
    title: str
    description: str
    state: str
    owner_role: str
    current_phase: str | None
    next_action: str
    governance: dict[str, object]
    artifacts: tuple[ArtifactStatus, ...] = ()
    approvals: tuple[ApprovalStatus, ...] = ()
    releases: tuple[ReleaseStatus, ...] = ()
    governance_records: tuple[GovernanceRecordStatus, ...] = ()
    governance_checklist: GovernanceChecklist | None = None


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


def render_work_item_detail_page(detail: WorkItemDetail | None, work_item_id: str) -> str:
    if detail is None:
        return _page("Work Item Not Found", [f"<h1>Work item not found</h1><p>{html.escape(work_item_id)}</p>"])
    return _page(
        detail.title,
        [
            f"<h1>{html.escape(detail.title)}</h1>",
            "<section>",
            f"<p><strong>Work item:</strong> {html.escape(detail.work_item_id)}</p>",
            f"<p><strong>Description:</strong> {html.escape(detail.description)}</p>",
            f"<p><strong>State:</strong> {html.escape(detail.state)}</p>",
            f"<p><strong>Owner:</strong> {html.escape(detail.owner_role)}</p>",
            f"<p><strong>Phase:</strong> {html.escape(detail.current_phase or '')}</p>",
            f"<p><strong>Next action:</strong> {html.escape(detail.next_action)}</p>",
            "</section>",
            "<h2>Governance</h2>",
            f"<pre>{html.escape(json.dumps(detail.governance, indent=2, sort_keys=True))}</pre>",
            "<h2>Governance Checklist</h2>",
            _governance_checklist_section(detail.governance_checklist),
            "<h2>Governance Records</h2>",
            _governance_record_table(detail.governance_records),
            "<h2>Artifacts</h2>",
            _artifact_table(detail.work_item_id, detail.artifacts),
            "<h2>Approvals</h2>",
            _approval_table(detail.approvals),
            "<h2>Releases</h2>",
            _release_table(detail.releases),
        ],
    )


def artifact_viewer_path(work_item_id: str, artifact_filename: str) -> str:
    clean_name = artifact_filename.replace("\\", "/").lstrip("/")
    if "\x00" in clean_name or clean_name.startswith("../") or "/../" in clean_name:
        raise ValueError("invalid artifact filename")
    return f"work-items/{work_item_id}/{clean_name}"


def artifact_viewer_url(work_item_id: str, artifact_filename: str) -> str:
    from urllib.parse import quote

    return f"/artifact-viewer/{quote(work_item_id)}/{quote(artifact_filename)}"


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


def _artifact_table(work_item_id: str, items: tuple[ArtifactStatus, ...]) -> str:
    rows = [
        "<tr><th>Artifact</th><th>Type</th><th>Status</th><th>Created By</th><th>Path</th></tr>"
    ]
    for item in items:
        url = artifact_viewer_url(work_item_id, item.filename)
        rows.append(
            "<tr>"
            f"<td><a href=\"{html.escape(url)}\">{html.escape(item.title)}</a></td>"
            f"<td>{html.escape(item.document_type)}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.created_by_role)}</td>"
            f"<td>{html.escape(item.relative_path)}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"5\">No artifacts recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _approval_table(items: tuple[ApprovalStatus, ...]) -> str:
    rows = ["<tr><th>Approval</th><th>Requested By</th><th>Status</th><th>Question</th><th>Response</th></tr>"]
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.approval_id)}</td>"
            f"<td>{html.escape(item.requested_by_role)}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.question)}</td>"
            f"<td>{html.escape(item.response or '')}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"5\">No approvals recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _release_table(items: tuple[ReleaseStatus, ...]) -> str:
    rows = [
        "<tr><th>Release</th><th>Status</th><th>Scope</th><th>Deployment</th><th>Rollback</th><th>Risks</th></tr>"
    ]
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.release_id)}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.scope)}</td>"
            f"<td>{html.escape(item.deployment_result)}</td>"
            f"<td>{html.escape(item.rollback_plan)}</td>"
            f"<td>{html.escape(item.residual_risks)}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"6\">No releases recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _governance_record_table(items: tuple[GovernanceRecordStatus, ...]) -> str:
    rows = ["<tr><th>Type</th><th>Role</th><th>Target</th><th>Status</th><th>Summary</th></tr>"]
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.record_type)}</td>"
            f"<td>{html.escape(item.role_instance_id)}</td>"
            f"<td>{html.escape(item.target_ref or '')}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.summary)}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"5\">No governance records recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _governance_checklist_section(checklist: GovernanceChecklist | None) -> str:
    if checklist is None:
        return "<p>No governance checklist available.</p>"
    rows = [
        "<tr><th>Area</th><th>Outstanding</th></tr>",
        _checklist_row("Consultations", checklist.missing_consultations),
        _checklist_row("Informed updates", checklist.missing_informed_updates),
        _checklist_row("Sponsor decisions", checklist.pending_sponsor_decisions),
    ]
    if checklist.recorded_exceptions:
        rows.append(_checklist_row("Recorded exceptions", checklist.recorded_exceptions))
    if checklist.is_satisfied:
        rows.append("<tr><td colspan=\"2\">Governance checklist is currently satisfied.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _checklist_row(label: str, values: tuple[str, ...]) -> str:
    text = ", ".join(values) if values else "None"
    return f"<tr><td>{html.escape(label)}</td><td>{html.escape(text)}</td></tr>"


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
