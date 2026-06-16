from __future__ import annotations

from dataclasses import dataclass

from agentic_mesh_v3.authority import ToolAuthorityPolicy
from agentic_mesh_v3.tools import TERMINAL_TOOLS


@dataclass(frozen=True)
class ToolCatalogEntry:
    tool_name: str
    allowed: bool
    terminal: bool
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "allowed": self.allowed,
            "terminal": self.terminal,
            "description": self.description,
        }


TOOL_DESCRIPTIONS = {
    "agent.heartbeat": "Report role-instance heartbeat, inbox depth, and current work.",
    "approval.request": "Ask a sponsor or stakeholder for approval and optionally deliver the request.",
    "backlog.upsert": "Create or update a backlog or queue item.",
    "consult.request": "Request input from a consulted role or stakeholder.",
    "document.write_root_work_item_index": "Regenerate the root work-item index in the document library.",
    "document.write_work_item_index": "Write or update a work-item index document.",
    "governance.record_exception": "Record a justified governance/RACI exception.",
    "handoff.require": "Require another role to take the next accountable/responsible action.",
    "informed.update": "Notify an informed role about a relevant change or decision.",
    "memory.propose_update": "Record a source-linked role-memory update.",
    "messaging.send": "Send a stakeholder-facing message through a configured connector.",
    "noop": "Record that no durable action is appropriate.",
    "release.close": "Close release work after deployment or no-deployment evidence exists.",
    "release.deploy": "Execute a configured deployment target or no-deployment disposition.",
    "release.record": "Record release evidence without executing a deployment target.",
    "report.incomplete": "End the run with an explicit incomplete result.",
    "stakeholder.ask_question": "Ask a stakeholder a clarification or decision question.",
    "status.complete": "Report successful completion.",
    "status.reply": "Send or record a Markdown reply to a conversation or stakeholder.",
    "status.update": "Record progress or status without completing the run.",
    "work_item.update_state": "Move a work item to a new state with next-action context.",
    "work_item.upsert": "Create or update a work item.",
}


def tool_catalog_for_role(
    role_id: str,
    *,
    authority_policy: ToolAuthorityPolicy | None = None,
) -> tuple[ToolCatalogEntry, ...]:
    policy = authority_policy or ToolAuthorityPolicy.default()
    allowed = policy.allowed_tools_for_role(role_id)
    all_tools = sorted(set(TOOL_DESCRIPTIONS) | allowed)
    return tuple(
        ToolCatalogEntry(
            tool_name=tool_name,
            allowed=tool_name in allowed,
            terminal=tool_name in TERMINAL_TOOLS,
            description=TOOL_DESCRIPTIONS.get(tool_name, "No description configured."),
        )
        for tool_name in all_tools
    )
