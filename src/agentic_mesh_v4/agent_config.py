from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.config import V4RoleConfig


SHARED_STANDING_INSTRUCTIONS = """\
# Agentic Mesh V4 Standing Instructions

You are a persistent Codex remote-control role agent inside Agentic Mesh.

You may answer normal conversational messages directly in Markdown. Durable project effects must be made through the configured safe-output tools. Durable effects include creating or changing work items, artifacts, documents, handoffs, consults, approvals, sponsor questions, memory, releases, blockers, risks, and decisions.

Tooling boundary: safe-output tools are required to record durable Agentic Mesh state changes, but missing safe-output tools do not remove your ordinary shell, filesystem, SSH, Git, Docker, or document-library access. Use the access granted by your authority level to inspect, diagnose, and perform role-appropriate operational work. If a durable state change is required but the matching safe-output tool is unavailable, say exactly what you inspected or did, what durable record could not be written, and who owns the tool-wiring follow-up.

After any meaningful work or no-work decision, confirm what happened and identify the next owner. Unless you are at the end of a flow, hand off to a human or at least one role agent when work must continue.

Use the project document library as the source of truth. Memory is a concise source-linked accelerator and must cite documents, work items, events, or conversations.

Ask sponsors or stakeholders when scope, priority, acceptance criteria, user-visible behavior, release risk, cost, compliance, security posture, or delivery commitments change.

Do not claim a durable action happened unless the corresponding tool call or evidence exists.
"""


def materialize_agent_configs(
    *,
    project_config: V4ProjectConfig,
    output_root: str | Path,
    role_templates_dir: str | Path,
) -> tuple[Path, ...]:
    output_root = Path(output_root)
    role_templates_dir = Path(role_templates_dir)
    written: list[Path] = []
    for role in project_config.roles:
        role_dir = output_root / role.role_id / "1"
        role_dir.mkdir(parents=True, exist_ok=True)
        role_template = _load_role_template(role_templates_dir / f"{role.template}.yaml")
        agents_path = role_dir / "AGENTS.md"
        agents_path.write_text(
            render_agents_md(project_config=project_config, role=role, role_template=role_template),
            encoding="utf-8",
        )
        container_path = role_dir / "container.json"
        container_path.write_text(
            json.dumps(
                {
                    "role_id": role.role_id,
                    "role_instance_id": f"{project_config.project_id}.{role.role_id}.1",
                    "authority": role.authority,
                    "codex_endpoint": f"ws://{role.service_name}:{role.codex_port}",
                    "codex_port": role.codex_port,
                    "service_name": role.service_name,
                    "model": role.model,
                    "reasoning_effort": role.reasoning_effort,
                    "sandbox_mode": role.sandbox_mode,
                },
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        written.extend([agents_path, container_path])
    return tuple(written)


def render_agents_md(
    *,
    project_config: V4ProjectConfig,
    role: V4RoleConfig,
    role_template: dict[str, Any],
) -> str:
    sections = [
        f"# Agentic Mesh Role: {role.display_name}",
        SHARED_STANDING_INSTRUCTIONS.strip(),
        "## Project",
        f"- Project: {project_config.name} (`{project_config.project_id}`)",
        f"- Goal: {project_config.goal or 'No project goal configured.'}",
        f"- Document library root: `{project_config.document_root}`",
        "## Role Charter",
        _role_template_markdown(role_template),
        "## Authority And Access",
        _authority_markdown(role),
        "## Runtime Contract",
        "- You are controlled through Codex app-server remote control.",
        "- The runtime may use `turn/start` for new work and `turn/steer` for explicit steering while you are active.",
        "- Normal conversation can be answered directly in Markdown.",
        "- Durable workflow effects must use safe-output tools.",
        "- Subagents may be used only for bounded parallel work where the added cost and context split are justified.",
    ]
    return "\n\n".join(section.rstrip() for section in sections) + "\n"


def _load_role_template(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"role_id": path.stem, "purpose": "No role template found."}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {"role_id": path.stem, "purpose": "Malformed role template."}
    return raw


def _role_template_markdown(template: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in (
        "purpose",
        "role_profile",
        "accountabilities",
        "decision_rights",
        "boundaries",
        "collaboration_style",
        "quality_bar",
        "memory_focus",
        "standing_instructions",
        "documentation_obligations",
        "handoff_targets",
    ):
        if key not in template:
            continue
        lines.append(f"### {key.replace('_', ' ').title()}")
        lines.extend(_markdown_value(template[key]))
        lines.append("")
    return "\n".join(lines).strip() or "No role charter supplied."


def _markdown_value(value: Any, *, indent: int = 0) -> list[str]:
    prefix = "  " * indent
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.extend(_markdown_value(item, indent=indent + 1))
            else:
                lines.append(f"{prefix}- {item}")
        return lines
    if isinstance(value, dict):
        lines = []
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                lines.append(f"{prefix}- {key}:")
                lines.extend(_markdown_value(child, indent=indent + 1))
            else:
                lines.append(f"{prefix}- {key}: {child}")
        return lines
    return [f"{prefix}{value}"]


def _authority_markdown(role: V4RoleConfig) -> str:
    if role.authority == "full":
        return "\n".join(
            [
                "- Authority level: `full`.",
                "- Sandbox: `danger-full-access` unless project config narrows it.",
                "- You may perform host, Git, Docker, SSH, deployment, and operational actions when they are within your role and project instructions.",
                "- The project document library is mounted at `/documents`.",
                "- Your mounted home directory is `/mesh/home`; SSH credentials are expected at `/mesh/home/.ssh` and are copied to `/root/.ssh` at container startup for OpenSSH default lookup. Project environment details may be available at `/mesh/home/.env`.",
                "- If an Agentic Mesh safe-output/runtime tool mentioned in your instructions is not available in the Codex tool surface, continue with shell, filesystem, SQLite, dashboard/API, Git, Docker, or SSH inspection where appropriate. Report the missing tool as a tool-wiring gap only for the durable state change it would have recorded.",
                "- Record risky actions, evidence, and next owner clearly.",
            ]
        )
    return "\n".join(
        [
            "- Authority level: `scoped`.",
            "- Use only the source, document, runtime, and communication access granted by project config.",
            "- Request specialist handoff or sponsor/operator escalation when required access is missing.",
        ]
    )
