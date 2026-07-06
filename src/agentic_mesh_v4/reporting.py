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
    completion_attention = snapshot.get("completion_attention") or []
    decision_attention = snapshot.get("decision_attention") or []
    decision_records = snapshot.get("decision_records") or []
    active_owner_paths = snapshot.get("active_owner_paths") or []
    blocked_handoff_attention = snapshot.get("blocked_handoff_attention") or []
    handoff_conflicts = snapshot.get("handoff_conflicts") or []
    document_merge_tasks = snapshot.get("document_merge_tasks") or []
    document_write_warnings = snapshot.get("document_write_warnings") or []
    preflight_attention = snapshot.get("preflight_attention") or []
    evidence_contract_warnings = snapshot.get("evidence_contract_warnings") or []
    evidence_contract_failures = snapshot.get("evidence_contract_failures") or []
    watchdog_findings = snapshot.get("watchdog_findings") or []
    watchdog_attention = snapshot.get("watchdog_attention") or [
        item for item in watchdog_findings if str(item.get("severity") or "") == "high"
    ]
    planned_not_running = snapshot.get("planned_not_running") or snapshot.get("planned_not_dispatched") or []
    not_running = snapshot.get("not_running") or []
    watchdog_sweep_runs = snapshot.get("watchdog_sweep_runs") or []
    work_items = snapshot.get("work_items") or []
    handoffs = snapshot.get("handoffs") or []
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
            "<h2>Active Owner Paths</h2>",
            _active_owner_path_table(active_owner_paths, empty="No active handoff owner paths."),
            "<h2>Watchdog Sweeps</h2>",
            _watchdog_sweep_table(watchdog_sweep_runs, empty="No watchdog sweeps recorded."),
            "<h2>Planned / Not Running</h2>",
            _watchdog_finding_table(planned_not_running, empty="No planned work missing an active owner path."),
            "<h2>Not Running Handoffs</h2>",
            _watchdog_finding_table(not_running, empty="No not-running handoff/work findings."),
            "<h2>Queue</h2>",
            _message_table(queued, empty="No queued messages."),
            "<h2>Active Handoffs</h2>",
            _handoff_table(_active_handoffs(handoffs), empty="No active handoffs."),
            "<h2>Active Agent Turns</h2>",
            _message_table(active, empty="No active turns."),
            "<h2>Attention Needed</h2>",
            _document_merge_task_table(document_merge_tasks, empty="No document merge tasks."),
            _document_warning_table(document_write_warnings, empty="No document write warnings."),
            _preflight_attention_table(preflight_attention, empty="No role artifact preflight failures."),
            _evidence_contract_table(evidence_contract_warnings, empty="No evidence contract warnings."),
            _evidence_contract_table(evidence_contract_failures, empty="No evidence contract failures."),
            _blocked_handoff_table(blocked_handoff_attention, empty="No blocked handoffs."),
            _handoff_conflict_table(handoff_conflicts, empty="No handoff conflicts."),
            _decision_attention_table(decision_attention, empty="No pending human decisions."),
            _completion_attention_table(completion_attention, empty="No completion diagnostics."),
            _watchdog_finding_table(watchdog_attention, empty="No severe watchdog findings."),
            _message_table(failed, empty="No failed messages."),
            "<h2>Recent Completions</h2>",
            _message_table(completed[-20:], empty="No completions recorded."),
            "<h2>Recent Human Decisions</h2>",
            _decision_history_table(decision_records[:20], empty="No decisions recorded."),
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
    message_rows = []
    for item in messages or []:
        message_rows.append(
            "<tr>"
            f"<td>{html.escape(item.get('updated_at') or '')}</td>"
            f"<td>{html.escape(item.get('state') or '')}</td>"
            f"<td>{html.escape(item.get('message_id') or '')}</td>"
            f"<td>{html.escape(item.get('text') or '')}</td>"
            "</tr>"
        )
    console = _agent_console(events)
    return _page(
        f"{role_id} Thread",
        [
            f"<h1>{html.escape(role_id)} Thread</h1>",
            _nav(),
            "<h2>Current Messages</h2>",
            "<table><thead><tr><th>Updated</th><th>State</th><th>Message</th><th>Text</th></tr></thead>"
            f"<tbody>{''.join(message_rows) or '<tr><td colspan=\"4\">No active messages.</td></tr>'}</tbody></table>",
            "<h2>Live Output</h2>",
            "<p id=\"stream-state\">Connecting live push stream...</p>",
            f"<pre id=\"agent-output\" class=\"agent-console\" aria-live=\"polite\">{html.escape(console['text'])}</pre>",
            """
<script>
const streamState = document.getElementById("stream-state");
const output = document.getElementById("agent-output");
const maxConsoleChars = 120000;
let currentTurnId = __INITIAL_TURN_ID__;
const events = new EventSource("thread/events");
events.onopen = () => { streamState.textContent = "Live push stream connected."; };
events.onmessage = (event) => {
  if (!event.data) return;
  let fragment = event.data;
  try {
    const parsed = JSON.parse(event.data);
    fragment = consoleFragment(parsed);
  } catch (_) {
    fragment = event.data + "\\n";
  }
  if (!fragment) return;
  output.textContent += fragment;
  if (output.textContent.length > maxConsoleChars) {
    output.textContent = output.textContent.slice(-maxConsoleChars);
  }
  output.scrollTop = output.scrollHeight;
};
events.onerror = () => { streamState.textContent = "Live push stream disconnected; retrying."; };

function consoleFragment(item) {
  const eventType = String(item.event_type || item.method || "");
  const content = normalizeConsoleText(String(item.content || item.delta || item.text || ""));
  if (isConsoleNoise(eventType, content)) {
    return "";
  }
  const turnId = String(item.turn_id || item.turnId || "");
  const createdAt = String(item.created_at || "");
  let prefix = "";
  if (turnId && turnId !== currentTurnId) {
    currentTurnId = turnId;
    prefix = createdAt ? `\\n[${createdAt}]\\n` : "\\n";
  }
  if (eventType === "item/agentMessage/delta") {
    return prefix + content;
  }
  if (eventType === "item/commandExecution/outputDelta" || eventType.endsWith("/outputDelta")) {
    return prefix + content;
  }
  const suffix = content ? ` ${content}` : "";
  return `${prefix}\\n${eventType}${suffix}\\n`;
}

function isConsoleNoise(eventType, content) {
  if (!eventType) return !content;
  if (eventType === "item/completed" || eventType === "item/started") return true;
  if (eventType === "turn/completed" || eventType === "turn/diff/updated") return true;
  if (eventType === "serverRequest/resolved") return true;
  if (eventType === "item/commandExecution/requestApproval") return true;
  if (eventType === "item/commandExecution/requestApproval/autoAccepted") return true;
  if (eventType === "item/fileChange/requestApproval") return true;
  if (eventType === "item/fileChange/requestApproval/autoAccepted") return true;
  if (eventType === "item/permissions/requestApproval") return true;
  if (eventType === "item/permissions/requestApproval/autoAccepted") return true;
  if (eventType.startsWith("thread/tokenUsage/")) return true;
  if (eventType.startsWith("account/rateLimits/")) return true;
  if (eventType.startsWith("thread/status/") || eventType === "thread/status") return true;
  if (eventType.startsWith("turn/status/") || eventType === "turn/status") return true;
  if (content && content === eventType && !eventType.endsWith("/outputDelta")) return true;
  return false;
}

function normalizeConsoleText(value) {
  return value
    .replace(/\\\\r\\\\n/g, "\\n")
    .replace(/\\\\n/g, "\\n")
    .replace(/\\\\r/g, "\\r")
    .replace(/\\\\t/g, "\\t")
    .replace(/\\\\\"/g, '"')
    .replace(/\\\\'/g, "'");
}
</script>
""".replace("__INITIAL_TURN_ID__", json.dumps(console["last_turn_id"])),
        ],
    )


def _agent_console(events: list[dict[str, Any]]) -> dict[str, str]:
    parts: list[str] = []
    current_turn_id = ""
    for item in events:
        event_type = str(item.get("event_type") or "")
        content = _normalise_console_text(str(item.get("content") or ""))
        if _is_console_noise(event_type, content):
            continue
        turn_id = str(item.get("turn_id") or "")
        if turn_id and turn_id != current_turn_id:
            current_turn_id = turn_id
            created_at = str(item.get("created_at") or "")
            parts.append(f"\n[{created_at}]\n" if created_at else "\n")
        if event_type == "item/agentMessage/delta":
            parts.append(content)
            continue
        if event_type == "item/commandExecution/outputDelta" or event_type.endswith("/outputDelta"):
            parts.append(content)
            continue
        suffix = f" {content}" if content else ""
        parts.append(f"\n{event_type}{suffix}\n")
    return {"text": "".join(parts).lstrip(), "last_turn_id": current_turn_id}


def _is_console_noise(event_type: str, content: str) -> bool:
    if not event_type:
        return not content
    if event_type in {
        "item/completed",
        "item/started",
        "turn/completed",
        "turn/diff/updated",
        "serverRequest/resolved",
        "item/commandExecution/requestApproval",
        "item/commandExecution/requestApproval/autoAccepted",
        "item/fileChange/requestApproval",
        "item/fileChange/requestApproval/autoAccepted",
        "item/permissions/requestApproval",
        "item/permissions/requestApproval/autoAccepted",
    }:
        return True
    if content and content == event_type and not event_type.endswith("/outputDelta"):
        return True
    return any(
        event_type == prefix.removesuffix("/")
        or event_type.startswith(prefix)
        for prefix in (
            "thread/tokenUsage/",
            "account/rateLimits/",
            "thread/status/",
            "turn/status/",
        )
    )


def _normalise_console_text(value: str) -> str:
    return (
        value.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\'", "'")
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
            f"<tr id=\"{html.escape(item['message_id'])}\">"
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


def _active_handoffs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if str(item.get("status") or "") in {"open", "accepted", "blocked"}
    ]


def _handoff_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        work_item_id = str(item.get("work_item_id") or "")
        work_cell = (
            f"<a href=\"/work-item/{html.escape(work_item_id)}\">{html.escape(work_item_id)}</a>"
            if work_item_id
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('handoff_id') or ''))}</td>"
            f"<td>{work_cell}</td>"
            f"<td>{html.escape(str(item.get('from_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('to_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('reason') or ''))}</td>"
            f"<td>{html.escape(str(item.get('created_at') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"7\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Handoff</th><th>Work item</th><th>From</th><th>To</th>"
        "<th>Status</th><th>Reason</th><th>Created</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _completion_attention_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        work_item_id = str(item.get("work_item_id") or "")
        work_cell = (
            f"<a href=\"/work-item/{html.escape(work_item_id)}\">{html.escape(work_item_id)}</a>"
            if work_item_id
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('message_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('state') or ''))}</td>"
            f"<td>{work_cell}</td>"
            f"<td>{html.escape(str(item.get('missing_predicate') or ''))}</td>"
            f"<td>{html.escape(str(item.get('next_action') or ''))}</td>"
            f"<td>{html.escape(str(item.get('updated_at') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"7\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Message</th><th>Role</th><th>State</th><th>Work item</th>"
        "<th>Missing predicate</th><th>Next action</th><th>Updated</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _decision_attention_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        work_item_id = str(item.get("work_item_id") or "")
        work_cell = (
            f"<a href=\"/work-item/{html.escape(work_item_id)}\">{html.escape(work_item_id)}</a>"
            if work_item_id
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('decision_id') or ''))}</td>"
            f"<td>{work_cell}</td>"
            f"<td>{html.escape(str(item.get('decision_type') or ''))}</td>"
            f"<td>{html.escape(str(item.get('title') or ''))}</td>"
            f"<td>{html.escape(str(item.get('authority_label') or ''))}</td>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('sla_state') or ''))}</td>"
            f"<td>{html.escape(str(item.get('reason') or ''))}</td>"
            f"<td>{html.escape(str(item.get('next_action') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"9\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Decision</th><th>Work item</th><th>Type</th><th>Title</th>"
        "<th>Authority</th><th>Status</th><th>SLA</th><th>Reason</th><th>Next action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _active_owner_path_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('work_item_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('handoff_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('from_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('to_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('state') or ''))}</td>"
            f"<td>{html.escape(str(item.get('next_action') or ''))}</td>"
            f"<td>{html.escape(str(item.get('conflict_count') or 0))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"7\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Work item</th><th>Handoff</th><th>From</th><th>To</th>"
        "<th>State</th><th>Next action</th><th>Active paths</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _blocked_handoff_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('handoff_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('work_item_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('owner_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('reason') or ''))}</td>"
            f"<td>{html.escape(str(item.get('next_action') or ''))}</td>"
            f"<td>{html.escape(str(item.get('updated_at') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"6\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Blocked handoff</th><th>Work item</th><th>Owner</th>"
        "<th>Reason</th><th>Next action</th><th>Updated</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _document_merge_task_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        if str(item.get("state") or "") != "open":
            continue
        diagnostic = _json_object(str(item.get("diagnostic_json") or ""))
        path = str(item.get("path") or "")
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('merge_task_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('work_item_id') or ''))}</td>"
            f"<td><a href=\"/artifact-viewer/{html.escape(path)}\">{html.escape(path)}</a></td>"
            f"<td>{html.escape(str(item.get('owner_role') or ''))}</td>"
            f"<td>{html.escape(str(diagnostic.get('reason') or 'merge_required'))}</td>"
            f"<td>{html.escape(_short_hash(str(item.get('current_sha256') or '')))}</td>"
            f"<td>{html.escape(_short_hash(str(item.get('proposed_sha256') or '')))}</td>"
            f"<td>{html.escape(str(item.get('updated_at') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"8\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Document merge</th><th>Work item</th><th>Path</th><th>Owner</th>"
        "<th>Reason</th><th>Current</th><th>Proposed</th><th>Updated</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _document_warning_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        diagnostic = _json_object(str(item.get("diagnostic_json") or ""))
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('warning_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('work_item_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('path') or ''))}</td>"
            f"<td>{html.escape(str(item.get('warning_type') or ''))}</td>"
            f"<td>{html.escape(str(item.get('severity') or ''))}</td>"
            f"<td>{html.escape(str(diagnostic.get('comment_metadata_detail') or ''))}</td>"
            f"<td>{html.escape(str(item.get('created_at') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"7\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Document warning</th><th>Work item</th><th>Path</th><th>Type</th>"
        "<th>Severity</th><th>Detail</th><th>Created</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _preflight_attention_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('message_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('role_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('service_name') or ''))}</td>"
            f"<td>{html.escape(str(item.get('work_item_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('check_name') or ''))}</td>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('canonical_path') or item.get('required_path') or ''))}</td>"
            f"<td>{html.escape(str(item.get('stderr_excerpt') or ''))}</td>"
            f"<td>{html.escape(str(item.get('remediation') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"9\">{html.escape(empty)}</td></tr>")
    return (
        "<h3>Role Artifact Preflight</h3>"
        "<table><thead><tr><th>Message</th><th>Role</th><th>Service</th><th>Work item</th>"
        "<th>Check</th><th>Status</th><th>Path</th><th>Error</th><th>Remediation</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _evidence_contract_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('contract_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('message_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('work_item_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('owner_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('lifecycle_state') or ''))}</td>"
            f"<td>{html.escape(str(item.get('predicate') or ''))}</td>"
            f"<td>{html.escape(str(item.get('path') or ''))}</td>"
            f"<td>{html.escape(str(item.get('remediation') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"8\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Evidence contract</th><th>Message</th><th>Work item</th><th>Owner</th>"
        "<th>Lifecycle</th><th>Predicate</th><th>Path</th><th>Remediation</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _handoff_conflict_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('work_item_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('handoff_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('to_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('conflict_count') or 0))}</td>"
            f"<td>{html.escape(', '.join(str(value) for value in item.get('conflict_handoff_ids') or []))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"5\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Work item</th><th>Tentative handoff</th><th>Tentative owner</th>"
        "<th>Conflict count</th><th>Active handoffs</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _decision_history_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('decision_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('work_item_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('decision_type') or ''))}</td>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('selected_option') or ''))}</td>"
            f"<td>{html.escape(str(item.get('responder_ref') or ''))}</td>"
            f"<td>{html.escape(str(item.get('updated_at') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"7\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Decision</th><th>Work item</th><th>Type</th><th>Status</th>"
        "<th>Selected</th><th>Responder</th><th>Updated</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _watchdog_finding_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items:
        work_item_id = str(item.get("work_item_id") or "")
        work_cell = (
            f"<a href=\"/work-item/{html.escape(work_item_id)}\">{html.escape(work_item_id)}</a>"
            if work_item_id
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('finding_key') or ''))}<br><small>{html.escape(str(item.get('finding_type') or ''))}</small></td>"
            f"<td>{html.escape(str(item.get('severity') or ''))}</td>"
            f"<td>{work_cell}</td>"
            f"<td>{html.escape(str(item.get('handoff_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('message_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('owner_role') or item.get('target_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('target_role') or ''))}</td>"
            f"<td>{html.escape(_age_label(item))}</td>"
            f"<td>{html.escape(str(item.get('next_action') or ''))}</td>"
            f"<td>{html.escape(str(item.get('updated_at') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"10\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Finding</th><th>Severity</th><th>Work item</th><th>Handoff</th><th>Message</th>"
        "<th>Owner/role</th><th>Target</th><th>Age</th><th>Next action</th><th>Updated</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _watchdog_sweep_table(items: list[dict[str, Any]], *, empty: str) -> str:
    rows = []
    for item in items[:10]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('sweep_run_id') or ''))}</td>"
            f"<td>{html.escape(str(item.get('initiator_role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('mode') or ''))}</td>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('started_at') or ''))}</td>"
            f"<td>{html.escape(str(item.get('completed_at') or ''))}</td>"
            f"<td><pre>{html.escape(_pretty_json(str(item.get('summary_json') or '{}')))}</pre></td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan=\"7\">{html.escape(empty)}</td></tr>")
    return (
        "<table><thead><tr><th>Sweep</th><th>Initiator</th><th>Mode</th><th>Status</th>"
        "<th>Started</th><th>Completed</th><th>Summary</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _age_label(item: dict[str, Any]) -> str:
    age = item.get("age_seconds")
    threshold = item.get("threshold_seconds")
    if age is None:
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        age = evidence.get("age_seconds")
        threshold = evidence.get("threshold_seconds")
    if age is None:
        return ""
    if threshold is None:
        return f"{age}s"
    return f"{age}s / threshold {threshold}s"




def _pretty_json(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return json.dumps(parsed, indent=2, sort_keys=True)

def _short_hash(value: str) -> str:
    return value[:12] if value else ""


def _json_object(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


def _page(title: str, parts: list[str]) -> str:
    styles = (
        "body{font-family:system-ui,Segoe UI,sans-serif;margin:2rem}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "th,td{border:1px solid #d0d7de;padding:.45rem;text-align:left;vertical-align:top}"
        "th{background:#f6f8fa}"
        "pre{white-space:pre-wrap;background:#f6f8fa;padding:1rem}"
        ".agent-console{"
        "box-sizing:border-box;min-height:24rem;max-height:65vh;overflow:auto;"
        "background:#0d1117;color:#d6deeb;border:1px solid #30363d;border-radius:6px;"
        "font:13px/1.45 ui-monospace,SFMono-Regular,Consolas,Liberation Mono,Menlo,monospace;"
        "white-space:pre-wrap;overflow-wrap:anywhere;"
        "}"
    )
    return "\n".join(
        [
            "<!doctype html>",
            "<html><head>",
            f"<title>{html.escape(title)}</title>",
            f"<style>{styles}</style>",
            "</head><body>",
            *parts,
            "</body></html>",
        ]
    )
