from __future__ import annotations

from typing import Any


TERMINAL_TOOLS = {"status.reply", "status.complete", "noop", "report.incomplete"}

DO_TOOLS = {
    "agent.heartbeat",
    "approval.request",
    "artifact.link",
    "backlog.upsert",
    "blocker.raise",
    "consult.request",
    "decision.record",
    "document.write_root_work_item_index",
    "document.write_work_item_index",
    "governance.record_exception",
    "handoff.require",
    "informed.update",
    "memory.propose_update",
    "messaging.send",
    "noop",
    "release.close",
    "release.deploy",
    "release.record",
    "risk.register",
    "stakeholder.ask_question",
    "status.update",
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
    "approval.request": "Ask a sponsor or stakeholder for approval and optionally deliver the request.",
    "artifact.link": "Link an existing work-item artifact or document-library path into the work-item evidence list.",
    "backlog.upsert": "Create or update a backlog or queue item.",
    "blocker.raise": "Record a blocker, move the work item to blocked, and make the required next action visible.",
    "consult.request": "Request input from a consulted role or stakeholder.",
    "decision.record": "Record a durable work-item decision as source-linked governance evidence.",
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
    "noop": "Record that no durable action is appropriate.",
    "release.close": "Close release work after deployment or no-deployment evidence exists.",
    "release.deploy": "Execute a configured deployment target or no-deployment disposition.",
    "release.record": "Record release evidence without executing a deployment target.",
    "report.incomplete": "End the run with an explicit incomplete result.",
    "risk.register": "Register a work-item risk with impact or mitigation context.",
    "stakeholder.ask_question": "Ask a stakeholder a clarification or decision question.",
    "status.complete": "Report successful completion.",
    "status.reply": "Send or record a Markdown reply to a conversation or stakeholder. Requires `text_markdown`.",
    "status.update": "Record progress or status without completing the run.",
    "work_item.update_state": "Move a work item to a new state with next-action context.",
    "work_item.upsert": "Create or update a work item.",
}


TOOL_REQUIRED_FIELDS = {
    "approval.request": ("work_item_id", "question"),
    "artifact.link": ("work_item_id", "relative_path"),
    "backlog.upsert": ("queue_item_id", "title", "summary", "owner_role"),
    "blocker.raise": ("work_item_id", "summary", "next_action"),
    "consult.request": ("work_item_id", "target_role", "question"),
    "decision.record": ("work_item_id", "summary"),
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
    "release.close": ("work_item_id",),
    "release.deploy": ("work_item_id", "target_id"),
    "release.record": ("work_item_id", "scope", "deployment_result", "rollback_plan"),
    "risk.register": ("work_item_id", "summary"),
    "stakeholder.ask_question": ("work_item_id", "question"),
    "status.reply": ("text_markdown",),
    "work_item.update_state": ("work_item_id", "state"),
    "work_item.upsert": ("work_item_id", "title", "description", "owner_role"),
}


def validate_tool_required_fields(tool_name: str, payload: dict[str, Any]) -> None:
    for field_name in TOOL_REQUIRED_FIELDS.get(tool_name, ()):
        value = payload.get(field_name)
        if value is None or str(value) == "":
            raise ValueError(f"{field_name} is required for {tool_name}")
