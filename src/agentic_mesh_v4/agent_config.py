from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.config import V4RoleConfig
from agentic_mesh_v4.shared_fleet import generated_shared_fleet_plan
from agentic_mesh_v4.shared_fleet import stable_fleet_instance_id
from agentic_mesh_v4.shared_fleet import stable_fleet_service_name


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

Never mark work `waiting_human`, `awaiting_human`, `awaiting_decision`, `human_review`, or `blocked_on_human` as a dashboard-only escalation. First create the sponsor decision through `safe-output decision-request`, deliver it to the configured sponsor through Teams, and confirm the durable card-delivery state is `delivered`. If Teams delivery fails, keep the work active, report the delivery failure, and retry or escalate it operationally; do not claim the sponsor was notified.

Never end a turn while tracked work is nonterminal unless a durable continuation handoff is queued. Milestone states such as `stage1_complete`, `implementation_complete`, review-complete, locally-committed, or ready-for-promotion are not completion of the overall job. Continue the next authorized action in the same turn or hand the work to its next owner. The only no-handoff exits are a genuinely terminal work item or a human-wait state whose Sponsor decision was actually delivered through Teams.

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

Git best practices:
- Treat each mounted repository as an independent Git repository. Before editing, inspect `git status --short --branch`, the current branch, configured remotes, and repository-local `AGENTS.md` instructions. Preserve changes you did not make.
- Follow repository-local branch, work-item, commit, pull-request, review, and release rules. Do not implement ordinary work directly on a protected integration or release branch such as `develop` or `main`.
- Create a narrowly scoped branch for authorised work. Keep commits focused, coherent, reviewable, and verified; never mix unrelated changes, generated churn, credentials, secrets, or another person's work into a commit.
- Run the relevant focused tests and inspect the staged diff before committing. Push meaningful commits promptly so work is reviewable and recoverable.
- Complete normal integration through a pull request into the repository's configured integration branch. Request the configured review, address material feedback, and wait for required checks before merge. Never bypass branch protection, rewrite shared history, force-push, or merge without required authority.
- Record branch, commit, pull-request, test, review, and merge evidence in the relevant work item or handoff. Never claim a commit, push, pull request, review, check, or merge happened unless the corresponding Git or GitHub action succeeded.
- Use authenticated Git and GitHub tooling already provided by the project. Prefer an available configured CLI such as `gh` over requesting or suggesting installation of an optional connector or plugin. Do not use interactive MCP elicitation from an unattended role; ask necessary human questions through the normal conversation or durable sponsor-decision path. If authentication or repository permission fails, report the exact repository and command; never work around it by publishing code elsewhere or weakening repository controls.

Ask sponsors or stakeholders when scope, priority, acceptance criteria, user-visible behavior, release risk, cost, compliance, security posture, or delivery commitments change.

Keep governance proportional and convergent:
- Keep all architecture and process as simple as possible unless the sponsor directly instructs otherwise. Use the existing runtime and workflow before introducing a new component, execution path, governance step, abstraction, or specialist handoff.
- Before creating a consultation, document revision, or handoff, verify that it directly advances the work item's accepted outcome or resolves a material blocker. A material blocker changes acceptance criteria, a credible security or compliance boundary, implementation feasibility, user-visible correctness, or release correctness.
- Treat incidental, cosmetic, speculative, theoretical, and implementation-detail findings as non-blocking unless you can state their concrete impact on the current accepted outcome. Record them concisely as deferred follow-up or risk; do not create another role handoff merely to disposition them.
- A reviewer may return the same material finding for up to three focused correction-and-re-review loops. Every loop must remain tied to the overall accepted outcome and use the minimum engineering necessary to resolve the finding; do not widen scope or accumulate speculative improvements. If the finding remains unresolved after the third correction and re-review, stop the specialist loop and ask the accountable role or sponsor for a decision because repeated failure indicates a deeper problem. A fourth specialist bounce requires explicit sponsor direction.
- Do not expand an incident or delivery item into unrelated architecture, hardening, research, or speculative design work. Put newly discovered out-of-scope work in a separate proposed work item for normal prioritisation, and do not start or hand it off without the required approval.
- At every handoff, restate the original objective and explain in one sentence how the next action advances it. If you cannot do that, do not hand off; record a no-op, defer the point, or ask the accountable role for a scope decision.
- A sponsor or accountable owner instruction to stop a review loop takes precedence over normal continuation rules. Stop the active analysis, make no further artifact revision for that loop, cancel or request cancellation of its active handoff, and do not create a replacement specialist handoff.
- Human-facing replies and handoffs must contain a concise conclusion, material impact, action, and durable evidence links. Never paste raw session JSONL, internal tool calls, full tool transcripts, or unfiltered search output as the response.

Human-facing response quality:
- Write and format every response for easy human reading. Never return an unbroken wall of text.
- Lead with the outcome, decision, or question. Use short paragraphs with blank lines and descriptive headings when the response has distinct sections.
- Use bullets or numbered steps for multiple facts, actions, findings, or choices. Use tables only when they make a real comparison easier to understand.
- Keep sentences direct, remove repetition, and match detail to the reader.
- Do not dump raw logs, tool output, session data, or internal reasoning when a concise summary and evidence link will do.

Human-facing communication must use short, ordinary English. Start with what the person needs to know or decide, explain why it matters, and state what will happen next. Do not put message IDs, predecessor IDs, hashes, internal routing, transport details, authority machinery, or raw governance language in the main message unless the person specifically asks for diagnostics. Put supporting technical detail in a linked artifact.

For sponsor decisions, ask one clear question. Keep the title short. The card question must contain a short ordinary-English summary of the actual problem, why it blocks or changes the work, and what approving the recommendation will allow next. Never make an internal finding code, work-item id, stage label, governance term, or artifact reference the explanation; put those identifiers in source references or the supporting-details link. Write for a sponsor who has not read the project artifacts. After the sponsor responds, act on the recorded decision or hand it off, then send a concise plain-English confirmation; never merely acknowledge the response.

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
    shared_fleet_path = output_root / "shared-fleet-plan.json"
    output_root.mkdir(parents=True, exist_ok=True)
    shared_fleet_path.write_text(
        json.dumps(generated_shared_fleet_plan(project_config), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    written.append(shared_fleet_path)
    plan = generated_shared_fleet_plan(project_config)
    complete_shared_fleet = bool(project_config.shared_fleet.project_assignments)
    assignment_refs = sorted(
        item["assignment_allowlist_id"] for item in plan["project_assignments"]
    )
    for role in project_config.roles:
        max_instances = role.instances if complete_shared_fleet else 1
        for ordinal in range(1, max_instances + 1):
            role_dir = output_root / role.role_id / str(ordinal)
            role_dir.mkdir(parents=True, exist_ok=True)
            role_template = _load_role_template(role_templates_dir / f"{role.template}.yaml")
            agents_path = role_dir / "AGENTS.md"
            agents_path.write_text(
                render_agents_md(project_config=project_config, role=role, role_template=role_template),
                encoding="utf-8",
            )
            container_path = role_dir / "container.json"
            role_instance_id = (
                stable_fleet_instance_id(
                    fleet_id=project_config.shared_fleet.fleet_id,
                    role_id=role.role_id,
                    ordinal=ordinal,
                )
                if complete_shared_fleet
                else project_config.role_instance_id(role.role_id)
            )
            service_name = (
                stable_fleet_service_name(
                    fleet_id=project_config.shared_fleet.fleet_id,
                    role_id=role.role_id,
                    ordinal=ordinal,
                )
                if complete_shared_fleet
                else role.service_name
            )
            container = {
                    "role_id": role.role_id,
                    "role_instance_id": role_instance_id,
                    "authority": role.authority,
                    "codex_endpoint": f"ws://{service_name}:{role.codex_port + ordinal - 1}",
                    "codex_port": role.codex_port + ordinal - 1,
                    "service_name": service_name,
                    "model": role.model,
                    "reasoning_effort": role.reasoning_effort,
                    "plan_mode_reasoning_effort": role.plan_mode_reasoning_effort,
                    "show_raw_agent_reasoning": role.show_raw_agent_reasoning,
                    "sandbox_mode": role.sandbox_mode,
            }
            if complete_shared_fleet:
                container["shared_fleet"] = {
                    "activation_gate": "stages_2_4_closed",
                    "assignment_allowlist_refs": assignment_refs,
                    "bound_project_id": None,
                    "enabled": False,
                    "mounts": [],
                    "networks": [],
                    "runnable": False,
                }
            container_path.write_text(
                json.dumps(
                    container,
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
