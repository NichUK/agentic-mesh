from __future__ import annotations

from dataclasses import dataclass

from agentic_mesh_v3.authority import ToolAuthorityPolicy
from agentic_mesh_v3.tool_contracts import TERMINAL_TOOLS
from agentic_mesh_v3.tool_contracts import TOOL_DESCRIPTIONS
from agentic_mesh_v3.tool_contracts import TOOL_REQUIRED_FIELDS


@dataclass(frozen=True)
class ToolCatalogEntry:
    tool_name: str
    allowed: bool
    terminal: bool
    description: str
    required_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "allowed": self.allowed,
            "terminal": self.terminal,
            "description": self.description,
            "required_fields": list(self.required_fields),
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
            required_fields=TOOL_REQUIRED_FIELDS.get(tool_name, ()),
        )
        for tool_name in all_tools
    )
