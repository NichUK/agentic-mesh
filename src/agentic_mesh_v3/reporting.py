from __future__ import annotations

import html
from dataclasses import dataclass
from urllib.parse import quote

from agentic_mesh_v3.documents import DocumentLibraryError
from agentic_mesh_v3.documents import framework_for
from agentic_mesh_v3.governance import GovernanceChecklist


@dataclass(frozen=True)
class AgentStatus:
    role_instance_id: str
    container_state: str
    heartbeat_at: str | None
    current_work: str | None = None
    inbox_depth: int = 0
    dead_letter_depth: int = 0
    governance_waits: tuple[str, ...] = ()
    memory_count: int = 0
    last_memory_at: str | None = None
    last_run_status: str | None = None
    last_run_at: str | None = None
    last_run_error: str | None = None
    last_lifecycle_action: str | None = None
    last_lifecycle_reason: str | None = None
    last_lifecycle_service: str | None = None
    last_lifecycle_exit_code: int | None = None
    last_lifecycle_executed: bool | None = None
    last_lifecycle_at: str | None = None
    last_activity_at: str | None = None
    last_lifecycle_error: str | None = None
    session_status: str | None = None
    session_provider: str | None = None
    session_mode: str | None = None
    session_last_hydrated_at: str | None = None


@dataclass(frozen=True)
class WorkItemStatus:
    work_item_id: str
    title: str
    state: str
    owner_role: str
    next_action: str
    artifact_count: int = 0
    updated_at: str | None = None
    attention_reason: str = ""


@dataclass(frozen=True)
class ArtifactStatus:
    filename: str
    title: str
    relative_path: str
    document_type: str
    status: str
    created_by_role: str
    url: str | None = None


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
    version_ref: str = "not-recorded"
    approval_ref: str = "not-recorded"
    smoke_evidence: str = "not-recorded"
    closure_state: str = "open"


@dataclass(frozen=True)
class GovernanceRecordStatus:
    record_id: str
    record_type: str
    role_instance_id: str
    target_ref: str | None
    summary: str
    status: str


@dataclass(frozen=True)
class AgentRunStatus:
    run_id: str
    role_instance_id: str
    message_id: str
    work_item_id: str | None
    subject: str
    status: str
    tool_calls: tuple[str, ...] = ()
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class DeliveryStatus:
    delivery_id: str
    call_id: str
    role_instance_id: str
    purpose: str
    connector: str
    target_ref: str
    thread_ref: str | None
    status: str
    created_at: str | None = None


@dataclass(frozen=True)
class MessageTraceStatus:
    message_id: str
    correlation_id: str
    direction: str
    stage: str
    status: str
    target_role: str | None = None
    role_instance_id: str | None = None
    broker_subject: str | None = None
    broker_consumer: str | None = None
    summary: str = ""
    created_at: str | None = None


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
    agent_runs: tuple[AgentRunStatus, ...] = ()
    deliveries: tuple[DeliveryStatus, ...] = ()
    message_trace: tuple[MessageTraceStatus, ...] = ()
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
            "<h2>Attention Needed</h2>",
            _attention_table(snapshot.work_items, include_stale=False),
            "<h2>Blocked Work</h2>",
            _blocked_work_table(snapshot.work_items),
            "<h2>Stale Work</h2>",
            _attention_table(snapshot.work_items, stale_only=True),
            "<h2>Governance Waits</h2>",
            _governance_waits_table(snapshot.agents),
            "<h2>Agent Lifecycle Alerts</h2>",
            _agent_lifecycle_alerts_table(snapshot.agents),
            "<h2>Active Work</h2>",
            _work_table(snapshot.work_items),
            "<h2>Recent Completions</h2>",
            _work_table(snapshot.recent_completions),
        ],
    )


def render_agents_page(snapshot: ReportingSnapshot) -> str:
    rows = [
        "<tr><th>Agent</th><th>Container</th><th>Heartbeat</th><th>Last Activity</th><th>Inbox</th><th>Dead Letters</th><th>Session</th><th>Lifecycle</th><th>Memory</th><th>Last Run</th><th>Current Work</th><th>Governance Waits</th></tr>"
    ]
    for agent in snapshot.agents:
        memory = f"{agent.memory_count} entries"
        if agent.last_memory_at:
            memory = f"{memory}<br><small>Last: {html.escape(agent.last_memory_at)}</small>"
        last_run = _agent_last_run_cell(agent)
        lifecycle = _agent_lifecycle_cell(agent)
        rows.append(
            "<tr>"
            f"<td>{html.escape(agent.role_instance_id)}</td>"
            f"<td>{html.escape(agent.container_state)}</td>"
            f"<td>{html.escape(agent.heartbeat_at or 'unknown')}</td>"
            f"<td>{html.escape(agent.last_activity_at or '')}</td>"
            f"<td>{agent.inbox_depth}</td>"
            f"<td>{agent.dead_letter_depth}</td>"
            f"<td>{_agent_session_cell(agent)}</td>"
            f"<td>{lifecycle}</td>"
            f"<td>{memory}</td>"
            f"<td>{last_run}</td>"
            f"<td>{_agent_current_work_cell(agent.current_work)}</td>"
            f"<td>{_list_cell(_visible_governance_waits(agent))}</td>"
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


def render_work_item_detail_page(
    detail: WorkItemDetail | None,
    work_item_id: str,
    *,
    document_framework_id: str = "togaf-sdlc-v1",
) -> str:
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
            f"<p><strong>Document framework:</strong> {html.escape(document_framework_id)}</p>",
            "</section>",
            "<h2>RACI</h2>",
            _raci_table(detail.governance),
            "<h2>Governance</h2>",
            _governance_summary_table(detail.governance),
            "<h2>Governance Checklist</h2>",
            _governance_checklist_section(detail.governance_checklist),
            "<h2>Consultations</h2>",
            _consultation_table(detail.governance_records),
            "<h2>Informed Updates</h2>",
            _informed_update_table(detail.governance_records),
            "<h2>Blockers</h2>",
            _blocker_table(detail.governance_records),
            "<h2>Decisions</h2>",
            _decision_table(detail.governance_records),
            "<h2>Risks</h2>",
            _risk_table(detail.governance_records),
            "<h2>Governance Records</h2>",
            _governance_record_table(detail.governance_records),
            "<h2>Artifacts</h2>",
            _artifact_table(detail.work_item_id, detail.artifacts, document_framework_id=document_framework_id),
            "<h2>Approvals</h2>",
            _approval_table(detail.approvals),
            "<h2>Releases</h2>",
            _release_table(detail.releases),
            "<h2>Agent Runs</h2>",
            _agent_run_table(detail.agent_runs),
            "<h2>Outbound Deliveries</h2>",
            _delivery_table(detail.deliveries),
            "<h2>Message Trace</h2>",
            _message_trace_table(detail.message_trace),
        ],
    )


def artifact_viewer_path(work_item_id: str, artifact_filename: str) -> str:
    clean_name = artifact_filename.replace("\\", "/").lstrip("/")
    if "\x00" in clean_name or clean_name.startswith("../") or "/../" in clean_name:
        raise ValueError("invalid artifact filename")
    return f"work-items/{work_item_id}/{clean_name}"


def artifact_viewer_url(work_item_id: str, artifact_filename: str) -> str:
    return f"/artifact-viewer/{quote(artifact_viewer_path(work_item_id, artifact_filename), safe='')}"


def work_item_url(work_item_id: str) -> str:
    return f"/work-item/{quote(work_item_id, safe='')}"


def _backlog_table(items: tuple[BacklogItemStatus, ...]) -> str:
    rows = ["<tr><th>Queue Item</th><th>Title</th><th>Status</th><th>Owner</th><th>Linked Work</th></tr>"]
    for item in items:
        linked_work = (
            f"<a href=\"{html.escape(work_item_url(item.linked_work_item_id))}\">{html.escape(item.linked_work_item_id)}</a>"
            if item.linked_work_item_id
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.queue_item_id)}</td>"
            f"<td>{html.escape(item.title)}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.owner_role)}</td>"
            f"<td>{linked_work}</td>"
            "</tr>"
        )
    return f"<table>{''.join(rows)}</table>"


def _agent_current_work_cell(current_work: str | None) -> str:
    if not current_work:
        return ""
    return f"<a href=\"{html.escape(work_item_url(current_work))}\">{html.escape(current_work)}</a>"


def _agent_last_run_cell(agent: AgentStatus) -> str:
    if not agent.last_run_status:
        return ""
    parts = [f"<strong>{html.escape(agent.last_run_status)}</strong>"]
    if agent.last_run_at:
        parts.append(f"<br><small>{html.escape(agent.last_run_at)}</small>")
    if agent.last_run_error:
        parts.append(f"<br><small>{html.escape(agent.last_run_error)}</small>")
    return "".join(parts)


def _agent_lifecycle_cell(agent: AgentStatus) -> str:
    if not agent.last_lifecycle_action:
        return ""
    parts = [f"<strong>{html.escape(agent.last_lifecycle_action)}</strong>"]
    if agent.last_lifecycle_service:
        parts.append(f"<br><small>{html.escape(agent.last_lifecycle_service)}</small>")
    if agent.last_lifecycle_at:
        parts.append(f"<br><small>{html.escape(agent.last_lifecycle_at)}</small>")
    if agent.last_lifecycle_executed is not None:
        parts.append(f"<br><small>Executed: {str(agent.last_lifecycle_executed).lower()}</small>")
    if agent.last_lifecycle_exit_code is not None:
        parts.append(f"<br><small>Exit: {agent.last_lifecycle_exit_code}</small>")
    if agent.last_lifecycle_error and agent_has_actionable_lifecycle_alert(agent):
        parts.append(f"<br><small>{html.escape(agent.last_lifecycle_error)}</small>")
    if agent.last_lifecycle_reason:
        parts.append(f"<br><small>{html.escape(agent.last_lifecycle_reason)}</small>")
    return "".join(parts)


def _agent_session_cell(agent: AgentStatus) -> str:
    if not agent.session_status and not agent.session_provider and not agent.session_mode:
        return ""
    parts = []
    if agent.session_status:
        parts.append(f"<strong>{html.escape(agent.session_status)}</strong>")
    if agent.session_provider:
        parts.append(f"<br><small>{html.escape(agent.session_provider)}</small>")
    if agent.session_mode:
        parts.append(f"<br><small>{html.escape(agent.session_mode)}</small>")
    if agent.session_last_hydrated_at:
        parts.append(f"<br><small>Hydrated: {html.escape(agent.session_last_hydrated_at)}</small>")
    return "".join(parts)


def _visible_governance_waits(agent: AgentStatus) -> tuple[str, ...]:
    return tuple(item for item in agent.governance_waits if not item.startswith("Lifecycle "))


def agent_has_actionable_lifecycle_alert(agent: AgentStatus) -> bool:
    legacy_lifecycle_waits = tuple(wait for wait in agent.governance_waits if wait.startswith("Lifecycle "))
    has_failed_lifecycle = agent.container_state == "lifecycle_failed" or (
        agent.last_lifecycle_exit_code is not None and agent.last_lifecycle_exit_code != 0
    )
    if not has_failed_lifecycle and not legacy_lifecycle_waits:
        return False
    if (
        agent.last_lifecycle_action in {"start", "wake"}
        and agent.last_lifecycle_exit_code is not None
        and agent.last_lifecycle_exit_code != 0
        and not agent.current_work
        and agent.inbox_depth == 0
        and agent.dead_letter_depth == 0
        and not legacy_lifecycle_waits
    ):
        return False
    return True


def _list_cell(items: tuple[str, ...]) -> str:
    if not items:
        return ""
    return "<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>"


def _work_table(items: tuple[WorkItemStatus, ...]) -> str:
    rows = ["<tr><th>Work Item</th><th>Title</th><th>State</th><th>Owner</th><th>Next Action</th><th>Updated</th></tr>"]
    for item in items:
        url = work_item_url(item.work_item_id)
        rows.append(
            "<tr>"
            f"<td><a href=\"{html.escape(url)}\">{html.escape(item.work_item_id)}</a></td>"
            f"<td>{html.escape(item.title)}</td>"
            f"<td>{html.escape(item.state)}</td>"
            f"<td>{html.escape(item.owner_role)}</td>"
            f"<td>{html.escape(item.next_action)}</td>"
            f"<td>{html.escape(item.updated_at or '')}</td>"
            "</tr>"
        )
    return f"<table>{''.join(rows)}</table>"


def _attention_table(
    items: tuple[WorkItemStatus, ...],
    *,
    include_stale: bool = True,
    stale_only: bool = False,
) -> str:
    rows = [
        "<tr><th>Work Item</th><th>Title</th><th>State</th><th>Owner</th><th>Reason</th><th>Next Action</th><th>Updated</th></tr>"
    ]
    for item in items:
        reason = item.attention_reason.strip()
        if not reason:
            continue
        is_stale = reason.startswith("work item has not changed")
        if stale_only and not is_stale:
            continue
        if not include_stale and is_stale:
            continue
        url = work_item_url(item.work_item_id)
        rows.append(
            "<tr>"
            f"<td><a href=\"{html.escape(url)}\">{html.escape(item.work_item_id)}</a></td>"
            f"<td>{html.escape(item.title)}</td>"
            f"<td>{html.escape(item.state)}</td>"
            f"<td>{html.escape(item.owner_role)}</td>"
            f"<td>{html.escape(reason)}</td>"
            f"<td>{html.escape(item.next_action)}</td>"
            f"<td>{html.escape(item.updated_at or '')}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"7\">None recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _blocked_work_table(items: tuple[WorkItemStatus, ...]) -> str:
    rows = ["<tr><th>Work Item</th><th>Title</th><th>Owner</th><th>Reason</th><th>Next Action</th><th>Updated</th></tr>"]
    for item in items:
        if item.state != "blocked":
            continue
        url = work_item_url(item.work_item_id)
        rows.append(
            "<tr>"
            f"<td><a href=\"{html.escape(url)}\">{html.escape(item.work_item_id)}</a></td>"
            f"<td>{html.escape(item.title)}</td>"
            f"<td>{html.escape(item.owner_role)}</td>"
            f"<td>{html.escape(item.attention_reason or 'Blocked')}</td>"
            f"<td>{html.escape(item.next_action)}</td>"
            f"<td>{html.escape(item.updated_at or '')}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"6\">No blocked work recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _governance_waits_table(items: tuple[AgentStatus, ...]) -> str:
    rows = ["<tr><th>Agent</th><th>Current Work</th><th>Governance Waits</th></tr>"]
    for item in items:
        governance_waits = _visible_governance_waits(item)
        if not governance_waits:
            continue
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.role_instance_id)}</td>"
            f"<td>{html.escape(item.current_work or '')}</td>"
            f"<td>{html.escape(', '.join(governance_waits))}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"3\">None recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _agent_lifecycle_alerts_table(items: tuple[AgentStatus, ...]) -> str:
    rows = ["<tr><th>Agent</th><th>Service</th><th>Action</th><th>Problem</th><th>Updated</th></tr>"]
    for item in items:
        if not agent_has_actionable_lifecycle_alert(item):
            continue
        legacy_lifecycle_waits = tuple(wait for wait in item.governance_waits if wait.startswith("Lifecycle "))
        problem = item.last_lifecycle_error or "; ".join(legacy_lifecycle_waits) or "Lifecycle action failed."
        if item.last_lifecycle_exit_code is not None and not item.last_lifecycle_error:
            problem = f"{problem} Exit code: {item.last_lifecycle_exit_code}."
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.role_instance_id)}</td>"
            f"<td>{html.escape(item.last_lifecycle_service or '')}</td>"
            f"<td>{html.escape(item.last_lifecycle_action or '')}</td>"
            f"<td>{html.escape(problem)}</td>"
            f"<td>{html.escape(item.last_lifecycle_at or item.last_activity_at or '')}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"5\">No agent lifecycle alerts recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _artifact_table(work_item_id: str, items: tuple[ArtifactStatus, ...], *, document_framework_id: str) -> str:
    rows = [
        "<tr><th>Artifact</th><th>Type</th><th>Status</th><th>Created By</th><th>Path</th><th>Framework Path</th></tr>"
    ]
    for item in items:
        url = item.url or artifact_viewer_url(work_item_id, item.filename)
        framework_path = _artifact_framework_path(
            document_framework_id=document_framework_id,
            work_item_id=work_item_id,
            document_type=item.document_type,
        )
        rows.append(
            "<tr>"
            f"<td><a href=\"{html.escape(url)}\" target=\"_blank\" rel=\"noopener noreferrer\">{html.escape(item.title)}</a></td>"
            f"<td>{html.escape(item.document_type)}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.created_by_role)}</td>"
            f"<td>{html.escape(item.relative_path)}</td>"
            f"<td>{html.escape(framework_path)}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"6\">No artifacts recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _artifact_framework_path(*, document_framework_id: str, work_item_id: str, document_type: str) -> str:
    try:
        rule = framework_for(document_framework_id).rule_for(document_type)
    except DocumentLibraryError:
        return "Unknown framework"
    if rule is None:
        return "Unknown document type"
    if rule.path_template is None:
        return "Flexible supporting artifact"
    return rule.path_for(work_item_id=work_item_id)


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
        "<tr><th>Release</th><th>Status</th><th>Scope</th><th>Version</th><th>Approval</th><th>Deployment / Smoke</th><th>Rollback</th><th>Risks</th><th>Closure</th></tr>"
    ]
    for item in items:
        deployment = html.escape(item.deployment_result)
        if item.smoke_evidence:
            deployment = f"{deployment}<br><small>Smoke: {html.escape(item.smoke_evidence)}</small>"
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.release_id)}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.scope)}</td>"
            f"<td>{html.escape(item.version_ref)}</td>"
            f"<td>{html.escape(item.approval_ref)}</td>"
            f"<td>{deployment}</td>"
            f"<td>{html.escape(item.rollback_plan)}</td>"
            f"<td>{html.escape(item.residual_risks)}</td>"
            f"<td>{html.escape(item.closure_state)}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"9\">No releases recorded.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _agent_run_table(items: tuple[AgentRunStatus, ...]) -> str:
    rows = [
        "<tr><th>Role Instance</th><th>Status</th><th>Message</th><th>Tool Calls</th><th>Error</th><th>Completed</th></tr>"
    ]
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.role_instance_id)}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.message_id)}<br><small>{html.escape(item.subject)}</small></td>"
            f"<td>{html.escape(str(len(item.tool_calls)))}</td>"
            f"<td>{html.escape(item.error or '')}</td>"
            f"<td>{html.escape(item.completed_at or '')}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"6\">No agent runs recorded for this work item.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _delivery_table(items: tuple[DeliveryStatus, ...]) -> str:
    rows = [
        "<tr><th>Purpose</th><th>Role Instance</th><th>Target</th><th>Status</th><th>Delivery</th><th>Created</th></tr>"
    ]
    for item in items:
        target = html.escape(item.target_ref)
        if item.thread_ref:
            target = f"{target}<br><small>Thread: {html.escape(item.thread_ref)}</small>"
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.purpose)}</td>"
            f"<td>{html.escape(item.role_instance_id)}</td>"
            f"<td>{target}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.delivery_id)}<br><small>{html.escape(item.connector)}</small></td>"
            f"<td>{html.escape(item.created_at or '')}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"6\">No outbound deliveries recorded for this work item.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _message_trace_table(items: tuple[MessageTraceStatus, ...]) -> str:
    rows = [
        "<tr><th>Time</th><th>Message</th><th>Correlation</th><th>Direction</th><th>Stage</th><th>Status</th><th>Role</th><th>Broker</th><th>Summary</th></tr>"
    ]
    for item in items:
        role = item.role_instance_id or item.target_role or ""
        broker = "<br>".join(
            html.escape(part)
            for part in (item.broker_subject or "", item.broker_consumer or "")
            if part
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.created_at or '')}</td>"
            f"<td>{html.escape(item.message_id)}</td>"
            f"<td>{html.escape(item.correlation_id)}</td>"
            f"<td>{html.escape(item.direction)}</td>"
            f"<td>{html.escape(item.stage)}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(role)}</td>"
            f"<td>{broker}</td>"
            f"<td>{html.escape(item.summary)}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append("<tr><td colspan=\"9\">No message trace recorded for this work item.</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _governance_summary_table(governance: dict[str, object]) -> str:
    rows = [
        "<tr><th>Field</th><th>Value</th></tr>",
        _governance_row("Phase", governance.get("phase")),
        _governance_row("Accountable", governance.get("accountable_role")),
        _governance_row("Responsible", governance.get("responsible_roles")),
        _governance_row("Consulted", governance.get("consulted_roles")),
        _governance_row("Informed", governance.get("informed_roles")),
        _governance_row("Sponsor decisions", governance.get("sponsor_decision_points")),
        _governance_row("Required evidence", governance.get("required_evidence")),
        _governance_row("Consultation exceptions", governance.get("consultation_exceptions")),
    ]
    known = {
        "phase",
        "accountable_role",
        "responsible_roles",
        "consulted_roles",
        "informed_roles",
        "sponsor_decision_points",
        "required_evidence",
        "consultation_exceptions",
    }
    extra = {key: value for key, value in governance.items() if key not in known}
    if extra:
        rows.append(_governance_row("Additional governance data", extra))
    return f"<table>{''.join(rows)}</table>"


def _raci_table(governance: dict[str, object]) -> str:
    rows = [
        "<tr><th>Area</th><th>Roles / Evidence</th></tr>",
        _governance_row("Phase", governance.get("phase")),
        _governance_row("Accountable", governance.get("accountable_role")),
        _governance_row("Responsible", governance.get("responsible_roles")),
        _governance_row("Consulted", governance.get("consulted_roles")),
        _governance_row("Informed", governance.get("informed_roles")),
        _governance_row("Sponsor decision points", governance.get("sponsor_decision_points")),
        _governance_row("Required evidence before handoff", governance.get("required_evidence")),
    ]
    return f"<table>{''.join(rows)}</table>"


def _governance_row(label: str, value: object) -> str:
    return (
        "<tr>"
        f"<td>{html.escape(label)}</td>"
        f"<td>{_format_governance_value(value)}</td>"
        "</tr>"
    )


def _format_governance_value(value: object) -> str:
    if value is None or value == "":
        return "None"
    if isinstance(value, (list, tuple)):
        if not value:
            return "None"
        return "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in value) + "</ul>"
    if isinstance(value, dict):
        if not value:
            return "None"
        items = "".join(
            f"<li><strong>{html.escape(str(key))}:</strong> {_format_governance_value(raw)}</li>"
            for key, raw in sorted(value.items(), key=lambda item: str(item[0]))
        )
        return f"<ul>{items}</ul>"
    return html.escape(str(value))


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


def _blocker_table(items: tuple[GovernanceRecordStatus, ...]) -> str:
    return _filtered_governance_record_table(
        items,
        record_type="blocker.raise",
        empty_message="No blockers recorded.",
    )


def _decision_table(items: tuple[GovernanceRecordStatus, ...]) -> str:
    return _filtered_governance_record_table(
        items,
        record_type="decision.record",
        empty_message="No decisions recorded.",
    )


def _risk_table(items: tuple[GovernanceRecordStatus, ...]) -> str:
    return _filtered_governance_record_table(
        items,
        record_type="risk.register",
        empty_message="No risks recorded.",
    )


def _consultation_table(items: tuple[GovernanceRecordStatus, ...]) -> str:
    return _filtered_governance_record_table(
        items,
        record_type="consult.request",
        empty_message="No consultations recorded.",
    )


def _informed_update_table(items: tuple[GovernanceRecordStatus, ...]) -> str:
    return _filtered_governance_record_table(
        items,
        record_type="informed.update",
        empty_message="No informed updates recorded.",
    )


def _filtered_governance_record_table(
    items: tuple[GovernanceRecordStatus, ...],
    *,
    record_type: str,
    empty_message: str,
) -> str:
    rows = ["<tr><th>Role</th><th>Target</th><th>Status</th><th>Summary</th></tr>"]
    for item in items:
        if item.record_type != record_type:
            continue
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.role_instance_id)}</td>"
            f"<td>{html.escape(item.target_ref or '')}</td>"
            f"<td>{html.escape(item.status)}</td>"
            f"<td>{html.escape(item.summary)}</td>"
            "</tr>"
        )
    if len(rows) == 1:
        rows.append(f"<tr><td colspan=\"4\">{html.escape(empty_message)}</td></tr>")
    return f"<table>{''.join(rows)}</table>"


def _governance_checklist_section(checklist: GovernanceChecklist | None) -> str:
    if checklist is None:
        return "<p>No governance checklist available.</p>"
    rows = [
        "<tr><th>Area</th><th>Outstanding</th></tr>",
        _checklist_row("Consultations", checklist.missing_consultations),
        _checklist_row("Informed updates", checklist.missing_informed_updates),
        _checklist_row("Sponsor decisions", checklist.pending_sponsor_decisions),
        _checklist_row("Required evidence", checklist.missing_required_evidence),
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
