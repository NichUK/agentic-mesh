from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path

from agentic_mesh_v3.agent import RoleInstanceConfig
from agentic_mesh_v3.documents import framework_for
from agentic_mesh_v3.governance import RaciMatrix
from agentic_mesh_v3.lifecycle import RoleContainerSpec
from agentic_mesh_v3.project_config import V3ProjectConfig
from agentic_mesh_v3.project_config import V3RoleInstanceConfig
from agentic_mesh_v3.roles import load_role_template
from agentic_mesh_v3.tool_catalog import tool_catalog_for_role


@dataclass(frozen=True)
class MaterializedRoleInstance:
    role: V3RoleInstanceConfig
    instance_index: int
    container_spec: RoleContainerSpec
    role_service_config: RoleInstanceConfig
    written_files: tuple[Path, ...]


def materialize_agent_config(
    *,
    spec: RoleContainerSpec,
    system_instructions: str,
    role_prompt: str,
    organisation_instructions: str,
    project_instructions: str,
    tool_instructions: str,
    raci: RaciMatrix,
) -> list[Path]:
    """Write the mounted per-agent configuration folder.

    The image contains runtime code, not mutable project config. This function
    prepares the externally mounted folder that a role container reads at
    startup and after wake-up.
    """

    spec.agent_config_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    files = {
        "system.md": system_instructions.rstrip() + "\n",
        "role.md": role_prompt.rstrip() + "\n",
        "organisation.md": organisation_instructions.rstrip() + "\n",
        "project.md": project_instructions.rstrip() + "\n",
        "tools.md": tool_instructions.rstrip() + "\n",
        "raci.json": json.dumps([asdict(item) for item in raci.assignments], indent=2),
        "container.json": json.dumps(
            {
                "role_instance_id": spec.role_instance_id,
                "image": spec.image,
                "command": spec.service_command(),
                "mounts": spec.volume_mounts(),
                "target_repositories": {
                    repository_id: f"/mesh/workspaces/{repository_id}"
                    for repository_id in sorted(spec.target_repositories)
                },
                "environment": spec.environment,
                "prompt_paths": {
                    "system": "/mesh/agent/system.md",
                    "role": "/mesh/agent/role.md",
                    "organisation": "/mesh/agent/organisation.md",
                    "project": "/mesh/agent/project.md",
                    "raci": "/mesh/agent/raci.json",
                    "tools": "/mesh/agent/tools.md",
                },
            },
            indent=2,
            sort_keys=True,
        ),
    }
    for filename, content in files.items():
        target = spec.agent_config_dir / filename
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written


def build_role_instance_config(
    *,
    project_id: str,
    role_id: str,
    instance_id: str,
    agent_config_dir: Path,
    runtime_state_dir: Path,
    inbox_stream: str,
) -> RoleInstanceConfig:
    """Create the runtime role-service config from a mounted config folder."""

    role_instance_id = f"{project_id}.{role_id}.{instance_id}"
    return RoleInstanceConfig(
        project_id=project_id,
        role_id=role_id,
        instance_id=instance_id,
        role_prompt_path=agent_config_dir / "role.md",
        organisation_prompt_path=agent_config_dir / "organisation.md",
        project_prompt_path=agent_config_dir / "project.md",
        system_prompt_path=agent_config_dir / "system.md",
        raci_path=agent_config_dir / "raci.json",
        tools_prompt_path=agent_config_dir / "tools.md",
        memory_db_path=runtime_state_dir / "memory" / f"{role_instance_id}.sqlite3",
        inbox_stream=inbox_stream,
        inbox_consumer=role_instance_id,
    )


def materialize_project_agent_configs(
    *,
    project_config: V3ProjectConfig,
    image: str,
    source_repo: Path,
    organisation_config_repo: Path,
    project_config_repo: Path,
    agent_config_root: Path,
    runtime_state_dir: Path,
    document_library_root: Path,
    role_templates_dir: Path,
    system_instructions: str,
    organisation_instructions: str,
    raci: RaciMatrix,
    tool_instructions: str,
) -> tuple[MaterializedRoleInstance, ...]:
    """Materialize config folders for every configured role instance."""

    materialized: list[MaterializedRoleInstance] = []
    for role in project_config.roles:
        for instance_index in range(1, role.instances + 1):
            instance_id = str(instance_index)
            role_instance_id = f"{project_config.project_id}.{role.role_id}.{instance_id}"
            agent_config_dir = agent_config_root / role.role_id / instance_id
            container_spec = RoleContainerSpec(
                role_instance_id=role_instance_id,
                image=image,
                source_repo=source_repo,
                organisation_config_repo=organisation_config_repo,
                project_config_repo=project_config_repo,
                agent_config_dir=agent_config_dir,
                runtime_state_dir=runtime_state_dir,
                document_library_root=document_library_root,
                target_repositories={
                    repository.repository_id: repository.path
                    for repository in project_config.target_repositories
                },
                environment={
                    "PROJECT_ID": project_config.project_id,
                    "ROLE_ID": role.role_id,
                    "ROLE_INSTANCE_ID": role_instance_id,
                },
            )
            written_files = materialize_agent_config(
                spec=container_spec,
                system_instructions=system_instructions,
                role_prompt=_read_role_template(role_templates_dir, role),
                organisation_instructions=organisation_instructions,
                project_instructions=_role_project_instructions(role, project_config),
                tool_instructions=_role_tool_instructions(
                    role.role_id,
                    tool_instructions,
                    document_framework_id=project_config.document_library.structure_policy,
                ),
                raci=raci,
            )
            materialized.append(
                MaterializedRoleInstance(
                    role=role,
                    instance_index=instance_index,
                    container_spec=container_spec,
                    role_service_config=build_role_instance_config(
                        project_id=project_config.project_id,
                        role_id=role.role_id,
                        instance_id=instance_id,
                        agent_config_dir=agent_config_dir,
                        runtime_state_dir=runtime_state_dir,
                        inbox_stream=project_config.broker.stream,
                    ),
                    written_files=tuple(written_files),
                )
            )
    return tuple(materialized)


def _read_role_template(role_templates_dir: Path, role: V3RoleInstanceConfig) -> str:
    template_id = role.template or role.role_id
    path = role_templates_dir / f"{template_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"role template `{template_id}` not found at {path}")
    return load_role_template(path, expected_role_id=template_id).as_prompt_text()


def _role_project_instructions(role: V3RoleInstanceConfig, project_config: V3ProjectConfig) -> str:
    lines = [
        "## Project Document Library",
        "",
        f"- Document library root path: `{project_config.document_library.root_path}`",
        f"- Documentation framework: `{project_config.document_library.structure_policy}`",
        "- Work-item dossiers live under `/documents/work-items/{work_item_id}`.",
        "- Each work-item dossier must maintain `index.md`; the root work-item index is `/documents/work-items/index.md`.",
        "- Durable system, architecture, product, engineering, QA, operations, release, risk, decision, and programming documentation also belongs in the document library.",
        "",
        "## Release Deployment Targets",
        "",
        *_release_deployment_target_lines(project_config),
        "",
        "## Role Instructions",
        "",
    ]
    if not role.instructions:
        lines.append("- No project-specific role instructions.")
    else:
        lines.extend(f"- {instruction}" for instruction in role.instructions)
    return "\n".join(lines)


def _release_deployment_target_lines(project_config: V3ProjectConfig) -> list[str]:
    if not project_config.release_deployment_targets:
        return [
            "- No release deployment targets are configured.",
            "- Development work that needs activation must be blocked with the exact missing deployment target/config instead of closed as released.",
        ]
    lines = [
        "- Release Manager must choose an appropriate configured target when deployment is required.",
        "- Development slices require a deployment target unless explicitly classified as spike, planning-only, design-only, documentation-only, analysis-only, research-only, or no-runtime-change.",
    ]
    for target in project_config.release_deployment_targets:
        lines.append(f"- `{target.target_id}`: type `{target.target_type}`.")
        if target.command:
            lines.append(f"  - Command: `{' '.join(target.command)}`")
        if target.working_directory is not None:
            lines.append(f"  - Working directory: `{target.working_directory}`")
        lines.append(f"  - Timeout seconds: `{target.timeout_seconds}`")
        lines.append(f"  - Rollback plan: {target.rollback_plan}")
        if target.reason:
            lines.append(f"  - No-deployment reason/description: {target.reason}")
    return lines


def _role_tool_instructions(role_id: str, base_instructions: str, *, document_framework_id: str) -> str:
    framework = framework_for(document_framework_id)
    lines = [
        base_instructions.rstrip(),
        "",
        "## Role-Scoped Safe-Output Tool Catalog",
        "",
        "Allowed tools for this role are marked `allowed`; blocked tools are shown so agents do not guess authority.",
        "A valid run must record at least one allowed DO tool and at least one allowed REPLY tool; a single tool can satisfy both when it is marked both.",
    ]
    for entry in tool_catalog_for_role(role_id):
        status = "allowed" if entry.allowed else "blocked"
        terminal = " terminal" if entry.terminal else ""
        categories = ", ".join(
            category
            for category, enabled in (
                ("DO", entry.do_tool),
                ("REPLY", entry.reply_tool),
            )
            if enabled
        )
        categories = categories or "none"
        required_fields = ", ".join(entry.required_fields) if entry.required_fields else "none"
        lines.append(
            f"- `{entry.tool_name}`: {status}{terminal}; categories: {categories}. {entry.description} "
            f"Required fields: {required_fields}."
        )
    lines.extend(
        [
            "",
            "## Document Framework Catalog",
            "",
            f"Selected framework: `{framework.framework_id}`.",
            "Use these `document_type` values and paths when calling `artifact.link` for typed document-library evidence.",
            "Use `artifact` only for supporting evidence with no standard document slot yet.",
        ]
    )
    for rule in framework.document_types:
        if rule.path_template is None:
            lines.append(f"- `{rule.document_type}`: {rule.title}; path: flexible supporting artifact.")
        else:
            lines.append(f"- `{rule.document_type}`: {rule.title}; path: `{rule.path_template}`.")
    return "\n".join(lines).rstrip()
