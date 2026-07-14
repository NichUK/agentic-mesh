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

The V4 safe-output CLI is available inside role containers:

```bash
python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml safe-output work-item-update --role-id <your-role-id> --work-item-id <work-id> --state <state> --owner-role <role-id> --next-action "<next action>"
python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml safe-output artifact-link --role-id <your-role-id> --work-item-id <work-id> --path "work-items/<work-id>/<artifact.md>" --title "<artifact title>"
python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml safe-output handoff --from-role <your-role-id> --to-role <next-role-id> --work-item-id <work-id> --state <next-state> --next-action "<required next action>" --reason "<why this role owns the next step>"
python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml safe-output memory-record --role-id <your-role-id> --scope role --summary "<role-specific source-linked memory>" --source-ref "<document/work/event/conversation ref>"
python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml safe-output memory-record --role-id <your-role-id> --scope institutional --summary "<shared project fact/decision/process memory>" --source-ref "<document/work/event/conversation ref>"
python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml safe-output architecture-impact --role-id <your-role-id> --work-item-id <work-id> --classification <none|material|uncertain> --rationale "<evidence-based rationale>" --affected-domain <domain>
python -m agentic_mesh_v4.cli --project-config /mesh/project/agentic-mesh/project-v4.yaml safe-output architecture-conformance --role-id <your-role-id> --work-item-id <work-id> --status <approved|changes_requested|exception> --rationale "<finding and required action>" [--decision-ref <sponsor-decision-ref>]
```

When work must continue with another role, use `safe-output handoff` before replying. When you create or update an artifact, use `safe-output artifact-link`. When you materially change status, owner, or next action, use `safe-output work-item-update`.

After any meaningful work or no-work decision, confirm what happened and identify the next owner. Unless you are at the end of a flow, hand off to a human or at least one role agent when work must continue.

If you are asked to implement, verify, release, recover, or otherwise perform work that is within your role authority, do the work before replying. Do not stop at "I will inspect" or "I will do this" unless you are reporting a real blocker. Before claiming a path, sandbox, or tool is read-only or unavailable, verify it with shell/filesystem evidence and include the exact failing path or command in the blocker.

Use the project document library as the source of truth. Memory is a concise source-linked accelerator and must cite documents, work items, events, or conversations. Record role memory for role-local operating knowledge, preferences, repeated failure modes, and handoff lessons. Record institutional memory for project-wide facts, durable decisions, architecture/process constraints, stakeholder preferences, and lessons other roles should know. If a point belongs in both, record it with `--scope both`.

If you cannot fulfil a required durable output, handoff, document update, release action, or confirmation obligation, treat that as an error. Record whatever evidence you can, hand off or escalate to Project Manager if the tool path is available, and state the exact missing capability, path, command, or decision. The runtime also escalates missing output and preflight failures to Project Manager; do not ignore or work around those escalations silently.

Canonical document-library paths:
- `/documents` is the only mounted project artifact/document library root inside role containers.
- Work-item dossiers must be written under `/documents/work-items/{work_item_id}`.
- Every work-item dossier must maintain a concise local index at `/documents/work-items/{work_item_id}/00-index.md` containing only the work-item name, a brief description, and links to typed documents.
- The overall work-item index must be maintained at `/documents/work-items/index.md` and must link each work item to its local `00-index.md`.
- `/mesh/project` contains project configuration and runtime state. Do not create canonical work-item artifacts under `/mesh/project/work-items` or under embedded system-repo example folders.

Document naming rules:
- Use framework document slots such as `020-product-definition.md`, `030-experience-design.md`, `040-enterprise-alignment.md`, `050-solution-design.md`, `060-security-review.md`, `070-platform-readiness.md`, `080-implementation-plan.md`, `090-quality-plan.md`, `100-implementation-log.md`, `110-quality-evidence.md`, and `140-release-record.md`.
- Do not invent descriptive filenames for standard lifecycle artifacts when a framework slot exists.
- Put detailed content in the typed lifecycle document, not in `00-index.md`.

Role tool-profile boundary:
- `base-agent` roles have common document, Git, search, JSON/YAML, HTTP, and runtime client tooling for role-scoped work.
- `ops-agent` roles may use SSH, Docker/Compose, Postgres, process/network diagnostics, and host/runtime inspection when their charter requires governance, delivery, platform, or release evidence.
- `dev-agent` roles may use build, test, package, and repository tooling for implementation and verification.
- `qa-agent` roles may use test runners, Playwright/browser tooling, screenshot/artifact capture, and HTTP/API validation for quality evidence.
- Installed tools do not grant authority by themselves; role charter, project config, safe-output policy, and sponsor decisions still govern use.

Ask sponsors or stakeholders when scope, priority, acceptance criteria, user-visible behavior, release risk, cost, compliance, security posture, or delivery commitments change.

Human-facing communication must use short, ordinary English. Start with what the person needs to know or decide, explain why it matters, and state what will happen next. Do not put message IDs, predecessor IDs, hashes, internal routing, transport details, authority machinery, or raw governance language in the main message unless the person specifically asks for diagnostics. Put supporting technical detail in a linked artifact.

For sponsor decisions, ask one clear question. Keep the title short and make the question understandable without knowledge of Agentic Mesh internals. Say what is being approved, why the decision is needed now, and what will happen if it is approved. After the sponsor responds, act on the recorded decision or hand it off, then send a concise plain-English confirmation; never merely acknowledge the response.

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
                    "role_instance_id": role.role_instance_id,
                    "authority": role.authority,
                    "codex_endpoint": f"ws://{role.service_name}:{role.codex_port}",
                    "codex_port": role.codex_port,
                    "service_name": role.service_name,
                    "model": role.model,
                    "reasoning_effort": role.reasoning_effort,
                    "plan_mode_reasoning_effort": role.plan_mode_reasoning_effort,
                    "show_raw_agent_reasoning": role.show_raw_agent_reasoning,
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
        f"- Document structure policy: `{project_config.document_structure_policy}`",
        "## Role Charter",
        _role_template_markdown(role_template),
        "## Project Role Instructions",
        _project_instructions_markdown(role),
        "## Document Accountabilities",
        _document_accountabilities_markdown(project_config=project_config, role_id=role.role_id),
        "## Flow And RACI Responsibilities",
        _role_flow_markdown(project_config=project_config, role_id=role.role_id),
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
        "core_workflows",
        "standards_references",
        "anti_patterns",
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


def _project_instructions_markdown(role: V4RoleConfig) -> str:
    if not role.instructions:
        return "- No project-specific role overrides are configured."
    return "\n".join(f"- {instruction}" for instruction in role.instructions)


def _document_accountabilities_markdown(*, project_config: V4ProjectConfig, role_id: str) -> str:
    accountabilities = project_config.accountabilities_for_role(role_id)
    if not accountabilities:
        return "- No durable document accountabilities are configured for this role."
    lines: list[str] = []
    for path, details in sorted(accountabilities.items()):
        relationship = "accountable owner" if details.get("owner_role") == role_id else "contributor"
        lines.append(f"- `{path}`: {relationship}.")
        required_sections = details.get("required_sections") or []
        if relationship == "accountable owner" and required_sections:
            lines.append(f"  Required sections: {', '.join(str(value) for value in required_sections)}.")
        contributors = details.get("contributing_roles") or []
        if relationship == "accountable owner" and contributors:
            lines.append(f"  Consult contributors: {', '.join(str(value) for value in contributors)}.")
    return "\n".join(lines)


def _role_flow_markdown(*, project_config: V4ProjectConfig, role_id: str) -> str:
    flow = project_config.flow or {}
    states = flow.get("states")
    if not isinstance(states, dict):
        return "- No resolved project flow is configured."
    lines = [f"- Flow: `{flow.get('flow_id', 'unnamed')}`."]
    found = False
    for state_id, raw_state in states.items():
        if not isinstance(raw_state, dict):
            continue
        responsibilities: list[str] = []
        if raw_state.get("owner_role") == role_id:
            responsibilities.append("Accountable/Responsible owner")
        for consult_id, consult in (raw_state.get("consults") or {}).items():
            if isinstance(consult, dict) and consult.get("target_role") == role_id:
                responsibilities.append(f"Consulted via `{consult_id}`{_condition_suffix(consult.get('when'))}")
        for inform_id, inform in (raw_state.get("informs") or {}).items():
            if isinstance(inform, dict) and inform.get("target_role") == role_id:
                responsibilities.append(f"Informed via `{inform_id}`{_condition_suffix(inform.get('when'))}")
        for gate in raw_state.get("gates") or []:
            if isinstance(gate, dict) and gate.get("reviewer_role") == role_id:
                responsibilities.append(
                    f"Gate reviewer for `{gate.get('gate_id', 'unnamed')}`{_condition_suffix(gate.get('when'))}"
                )
        for handoff_id, handoff in (raw_state.get("handoffs") or {}).items():
            if not isinstance(handoff, dict):
                continue
            if handoff.get("target_role") == role_id:
                responsibilities.append(f"Receives `{handoff_id}` handoff{_condition_suffix(handoff.get('when'))}")
            if raw_state.get("owner_role") == role_id:
                responsibilities.append(
                    f"May hand off via `{handoff_id}` to `{handoff.get('target_role')}`"
                    f"{_condition_suffix(handoff.get('when'))}"
                )
        if responsibilities:
            found = True
            lines.append(f"- `{state_id}`: {'; '.join(dict.fromkeys(responsibilities))}.")
    if not found:
        lines.append("- This role has no explicit responsibility in the resolved flow.")
    return "\n".join(lines)


def _condition_suffix(condition: object) -> str:
    if not isinstance(condition, dict):
        return ""
    field = condition.get("field")
    values = condition.get("in")
    if not field or not isinstance(values, list):
        return ""
    return f" when `{field}` is one of {', '.join(f'`{value}`' for value in values)}"


def _authority_markdown(role: V4RoleConfig) -> str:
    if role.authority == "full":
        return "\n".join(
            [
                "- Authority level: `full`.",
                "- Sandbox: `danger-full-access` unless project config narrows it.",
                "- You may perform host, Git, Docker, SSH, deployment, and operational actions when they are within your role and project instructions.",
                "- `/mesh/agent` is your mounted role identity/configuration folder; do not use it as a working tree.",
                "- `/mesh/agent-workspace` is your writable current working directory for role-local scratch files, temporary notes, and command context.",
                "- If `pwd` is `/mesh/agent`, report a platform configuration defect before doing role work.",
                "- `/mesh/worker-auth/codex` is the mounted Codex runtime home. It contains authentication, sessions, memories, and other provider metadata. Do not create project artifacts or source checkouts there. If expected runtime subdirectories are not writable, report a platform mount-permission defect with the exact path.",
                "- For approved implementation or operational work, assume `/mesh/workspaces/agentic-mesh`, `/documents`, and `/mesh/project` are writable unless a shell check proves otherwise.",
                "- If you believe a writable mount is unavailable, run a minimal write/access probe before reporting a blocker; do not infer read-only status from missing safe-output tools or from the read-only `/mesh/agent` configuration mount.",
                "- The project document library is mounted at `/documents`.",
                "- Your mounted home directory is `/mesh/home`; SSH credentials are expected at `/mesh/home/.ssh` and are copied to `/root/.ssh` at container startup for OpenSSH default lookup. Project environment details may be available at `/mesh/home/.env`.",
                "- If an Agentic Mesh safe-output/runtime tool mentioned in your instructions is not available in the Codex tool surface, continue with shell, filesystem, Postgres, dashboard/API, Git, Docker, or SSH inspection where appropriate. Report the missing tool as a tool-wiring gap only for the durable state change it would have recorded.",
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
