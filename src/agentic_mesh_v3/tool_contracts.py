from __future__ import annotations

from typing import Any


TERMINAL_TOOLS = {
    "approval.request",
    "blocker.raise",
    "messaging.send",
    "noop",
    "report.incomplete",
    "stakeholder.ask_question",
    "status.complete",
    "status.reply",
}

DO_TOOLS = {
    "agent.heartbeat",
    "agent.delegate",
    "approval.request",
    "artifact.link",
    "backlog.upsert",
    "blocker.raise",
    "consult.request",
    "conversation.compact_context",
    "decision.record",
    "document.write_artifact",
    "document.write_root_work_item_index",
    "document.write_work_item_index",
    "governance.record_exception",
    "handoff.require",
    "informed.update",
    "memory.propose_update",
    "messaging.send",
    "noop",
    "relevance.record",
    "release.close",
    "release.deploy",
    "release.record",
    "report.incomplete",
    "risk.register",
    "runtime.broker.inspect",
    "runtime.broker.retry_dead_letter",
    "runtime.lifecycle.request",
    "runtime.message_journal.inspect",
    "runtime.status.inspect",
    "runtime.sweep.request",
    "stakeholder.ask_question",
    "status.update",
    "work_item.reopen",
    "work_item.update_state",
    "work_item.upsert",
}

REPLY_TOOLS = {
    "approval.request",
    "blocker.raise",
    "messaging.send",
    "report.incomplete",
    "stakeholder.ask_question",
    "status.complete",
    "status.reply",
}


TOOL_DESCRIPTIONS = {
    "agent.heartbeat": "Report role-instance heartbeat, inbox depth, and current work.",
    "agent.delegate": (
        "Send a focused agent-to-agent task to another role inbox. Use this for operational diagnostics, "
        "specialist follow-up, or lightweight role assistance that does not yet need a full governed handoff. "
        "Include work_item_id when the task relates to tracked work."
    ),
    "approval.request": "Ask a sponsor or stakeholder for approval and optionally deliver the request.",
    "artifact.link": "Link an existing work-item artifact or document-library path into the work-item evidence list.",
    "backlog.upsert": "Create or update a backlog or queue item.",
    "blocker.raise": "Record a blocker, move the work item to blocked, and make the required next action visible.",
    "consult.request": "Request input from a consulted role or stakeholder.",
    "conversation.compact_context": (
        "Record a concise, source-linked conversation summary so important stakeholder context can be loaded "
        "without retaining or rereading the full raw thread."
    ),
    "decision.record": "Record a durable work-item decision as source-linked governance evidence.",
    "document.write_artifact": "Write a typed work-item document-library artifact and link it to the work item.",
    "document.write_root_work_item_index": "Regenerate the root work-item index in the document library.",
    "document.write_work_item_index": "Write or update a work-item index document.",
    "governance.record_exception": "Record a justified governance/RACI exception.",
    "handoff.require": (
        "Require another role to take the next accountable/responsible action with work id, phase, "
        "accountable role, next action, acceptance/evidence requirements, artifact links, open decisions/risks, "
        "consulted/informed roles, and stakeholder follow-up."
    ),
    "informed.update": "Notify an informed role about a relevant change or decision.",
    "memory.propose_update": "Record a source-linked role-memory update.",
    "messaging.send": "Send a stakeholder-facing message through a configured connector.",
    "noop": "Record that no durable action is appropriate. Requires `reason`.",
    "relevance.record": (
        "Record a role's relevance score and rationale for a shared project-channel or direct context message. "
        "Use this when a role decides whether it has something useful to add without creating fake work."
    ),
    "release.close": "Close release work after deployment or no-deployment evidence exists.",
    "release.deploy": "Execute a configured deployment target or no-deployment disposition.",
    "release.record": "Record release evidence without executing a deployment target.",
    "report.incomplete": (
        "Record and communicate an explicit incomplete result when the role cannot complete the assignment. "
        "Requires `reason`."
    ),
    "risk.register": "Register a work-item risk with impact or mitigation context.",
    "runtime.broker.inspect": (
        "Inspect broker inbox pressure, role consumer pending counts, and dead letters so an agent can diagnose "
        "routing or stuck-work issues without privileged shell access."
    ),
    "runtime.broker.retry_dead_letter": (
        "Re-publish a broker dead-letter message back to a role inbox after the cause has been inspected and "
        "a concrete retry reason is recorded. This is a recovery action, not a status-only acknowledgement."
    ),
    "runtime.lifecycle.request": (
        "Ask the runtime lifecycle controller to reconcile, wake, or hibernate role services through the bounded "
        "Compose lifecycle path. This is not arbitrary shell access: wake/start commands must target one role "
        "service with --no-deps and --no-recreate, and hibernate commands must stop one role service."
    ),
    "runtime.message_journal.inspect": (
        "Inspect durable message journal entries by message, conversation, work item, role, stage, or status. "
        "Use this to diagnose whether Teams/API/CLI input was received, routed, published, claimed, replied, "
        "acked, nacked, dead-lettered, or failed without privileged database access."
    ),
    "runtime.status.inspect": (
        "Inspect the current runtime status read model: queue/backlog, active or waiting work, recent completions, "
        "agent state, lifecycle alerts, and governance waits. This is read-only and intended for Project Manager "
        "style operational triage without shell access."
    ),
    "runtime.sweep.request": (
        "Ask the runtime to perform an immediate project-health sweep and publish actionable findings "
        "to the Project Manager inbox."
    ),
    "stakeholder.ask_question": "Ask a stakeholder a clarification or decision question.",
    "status.complete": "Report successful completion. Requires `summary`.",
    "status.reply": "Send or record a Markdown reply to a conversation or stakeholder. Requires `text_markdown`.",
    "status.update": "Record progress or status without completing the run.",
    "work_item.reopen": "Reopen terminal work with explicit product/project/release authority and a recorded reason.",
    "work_item.update_state": "Move a work item to a new state with next-action context.",
    "work_item.upsert": "Create or update a work item.",
}


TOOL_REQUIRED_FIELDS = {
    "approval.request": ("work_item_id", "question"),
    "agent.delegate": ("target_role", "task", "reason", "expected_output"),
    "artifact.link": ("work_item_id", "relative_path"),
    "backlog.upsert": ("queue_item_id", "title", "summary", "owner_role"),
    "blocker.raise": ("work_item_id", "summary", "next_action", "owner_role"),
    "consult.request": ("work_item_id", "target_role", "question"),
    "conversation.compact_context": ("conversation_ref", "summary", "source_message_ids", "visibility"),
    "decision.record": ("work_item_id", "summary"),
    "document.write_artifact": ("work_item_id", "relative_path", "title", "document_type", "content_markdown"),
    "document.write_work_item_index": (
        "work_item_id",
        "title",
        "status",
        "owner_role",
        "raci_summary",
        "governance_state",
    ),
    "governance.record_exception": ("work_item_id", "reason"),
    "handoff.require": (
        "work_item_id",
        "target_role",
        "phase",
        "accountable_role",
        "required_next_action",
        "acceptance_criteria",
        "evidence_requirements",
        "artifact_links",
        "open_decisions",
        "open_risks",
        "consulted_roles",
        "informed_roles",
        "stakeholder_follow_up",
    ),
    "informed.update": ("work_item_id", "target_role", "message"),
    "memory.propose_update": ("summary", "source_ref"),
    "messaging.send": ("connector", "target_ref", "text_markdown"),
    "noop": ("reason",),
    "relevance.record": ("source_message_id", "relevance_score", "rationale"),
    "release.close": ("work_item_id",),
    "release.deploy": ("work_item_id", "target_id", "version_ref", "approval_ref"),
    "release.record": ("work_item_id", "scope", "version_ref", "approval_ref", "deployment_result", "smoke_evidence", "rollback_plan"),
    "report.incomplete": ("reason",),
    "risk.register": ("work_item_id", "summary"),
    "runtime.broker.inspect": ("reason",),
    "runtime.broker.retry_dead_letter": ("message_id", "reason"),
    "runtime.lifecycle.request": ("reason",),
    "runtime.message_journal.inspect": ("reason",),
    "runtime.status.inspect": ("reason",),
    "runtime.sweep.request": ("reason",),
    "stakeholder.ask_question": ("work_item_id", "question"),
    "status.complete": ("summary",),
    "status.reply": ("text_markdown",),
    "work_item.reopen": ("work_item_id", "state", "reason"),
    "work_item.update_state": ("work_item_id", "state"),
    "work_item.upsert": ("work_item_id", "title", "description", "owner_role"),
}


def validate_tool_required_fields(tool_name: str, payload: dict[str, Any]) -> None:
    for field_name in TOOL_REQUIRED_FIELDS.get(tool_name, ()):
        value = payload.get(field_name)
        if value is None or str(value) == "":
            raise ValueError(f"{field_name} is required for {tool_name}")
    if tool_name == "status.reply" and _has_any(payload, "target_ref", "reply_target_ref"):
        _require(payload, "connector", tool_name)
    elif tool_name == "stakeholder.ask_question" and _has_any(payload, "target_ref", "stakeholder_ref"):
        _require(payload, "connector", tool_name)
    elif tool_name == "approval.request" and _has_any(payload, "target_ref"):
        _require(payload, "connector", tool_name)


def _has_any(payload: dict[str, Any], *field_names: str) -> bool:
    return any(payload.get(field_name) is not None and str(payload.get(field_name)) != "" for field_name in field_names)


def _require(payload: dict[str, Any], field_name: str, tool_name: str) -> None:
    value = payload.get(field_name)
    if value is None or str(value) == "":
        raise ValueError(f"{field_name} is required for targeted {tool_name}")
