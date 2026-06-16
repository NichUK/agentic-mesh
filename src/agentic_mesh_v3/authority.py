from __future__ import annotations

from dataclasses import dataclass


BASE_TOOLS = {
    "agent.heartbeat",
    "artifact.link",
    "blocker.raise",
    "consult.request",
    "conversation.compact_context",
    "decision.record",
    "document.write_work_item_index",
    "document.write_root_work_item_index",
    "governance.record_exception",
    "handoff.require",
    "informed.update",
    "memory.propose_update",
    "messaging.send",
    "noop",
    "relevance.record",
    "report.incomplete",
    "risk.register",
    "stakeholder.ask_question",
    "status.complete",
    "status.reply",
    "status.update",
}

WORK_PROGRESSION_ROLES = {
    "business-analyst",
    "delivery-manager",
    "engineering",
    "enterprise-architect",
    "platform-engineer",
    "product-manager",
    "project-manager",
    "prompt-engineer",
    "qa-engineer",
    "release-manager",
    "research-analyst",
    "security-architect",
    "solution-architect",
    "technical-writer",
    "ux-designer",
}

ROLE_TOOLS = {
    "project-manager": {
        "approval.request",
        "backlog.upsert",
        "work_item.reopen",
        "work_item.upsert",
    },
    "product-manager": {
        "approval.request",
        "backlog.upsert",
        "work_item.reopen",
        "work_item.upsert",
    },
    "qa-engineer": {
        "approval.request",
    },
    "release-manager": {
        "approval.request",
        "release.close",
        "release.deploy",
        "release.record",
        "work_item.reopen",
    },
}

for _role_id in WORK_PROGRESSION_ROLES:
    ROLE_TOOLS.setdefault(_role_id, set()).add("work_item.update_state")


@dataclass(frozen=True)
class ToolAuthorityPolicy:
    role_tools: dict[str, set[str]]
    base_tools: set[str]

    @classmethod
    def default(cls) -> "ToolAuthorityPolicy":
        return cls(
            role_tools={role: set(tools) for role, tools in ROLE_TOOLS.items()},
            base_tools=set(BASE_TOOLS),
        )

    def allowed_tools_for_role(self, role_id: str) -> set[str]:
        return set(self.base_tools) | set(self.role_tools.get(role_id, set()))

    def assert_allowed(self, *, role_instance_id: str, tool_name: str) -> None:
        role_id = role_from_instance(role_instance_id)
        if tool_name not in self.allowed_tools_for_role(role_id):
            raise PermissionError(f"role `{role_id}` is not allowed to call `{tool_name}`")


def role_from_instance(role_instance_id: str) -> str:
    parts = role_instance_id.split(".")
    return parts[-2] if len(parts) >= 2 else role_instance_id
