from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from agentic_mesh.capabilities import capability_prompt_context
from agentic_mesh.document_library import document_library_context
from agentic_mesh.models import FlowState
from agentic_mesh.models import MeshConfig
from agentic_mesh.models import Message
from agentic_mesh.models import ProjectConfig
from agentic_mesh.models import RoleInstanceConfig
from agentic_mesh.prompt_templates import load_prompt_template
from agentic_mesh.prompt_templates import render_prompt_template
from agentic_mesh.safe_outputs import safe_output_tools_prompt


def render_worker_prompt(
    *,
    project: ProjectConfig,
    mesh_config: MeshConfig | None,
    instance: RoleInstanceConfig,
    message: Message,
    flow_state: FlowState,
    workspace_root: Path,
    state_root: Path,
) -> str:
    runtime = message.payload.get("runtime_instructions") or {}
    repositories = {
        repository_id: {
            "type": repository.type,
            "path": repository.path,
            "absolute_path": str(
                Path(repository.path)
                if Path(repository.path).is_absolute()
                else (workspace_root / repository.path).resolve()
            ),
            "default_branch": repository.default_branch,
        }
        for repository_id, repository in project.workspace.repositories.items()
    }
    capability_context = (
        capability_prompt_context(
            mesh_config=mesh_config,
            instance=instance,
            state_root=state_root,
        )
        if mesh_config is not None
        else {
            "schema_version": "capability-prompt-context-v0",
            "availability_statement": "Capability availability has not been validated for this role instance.",
            "configured_required": [],
            "available": [],
            "missing_required": [],
            "fallbacks": [],
            "waivers": [],
            "redaction_applied": True,
        }
    )
    assignment_task = _assignment_task(message, flow_state, runtime)
    return "\n".join(
        [
            "<system>",
            _section(
                "security",
                load_prompt_template("worker-system-security.md"),
            ),
            _section(
                "safe-outputs",
                "\n\n".join(
                    [
                        safe_output_tools_prompt(),
                        load_prompt_template("worker-safe-output-rails.md"),
                    ]
                ),
            ),
            "</system>",
            "",
            "<role>",
            _section(
                "identity",
                f"""
You are the `{instance.role_id}` role agent for Agentic Mesh project `{instance.project_id}`.

Purpose: {instance.template.purpose}

Role profile:
{instance.template.role_profile}
""".strip(),
            ),
            _section(
                "accountability",
                _yamlish(
                    {
                        "accountabilities": instance.template.accountabilities,
                        "decision_rights": instance.template.decision_rights,
                        "boundaries": instance.template.boundaries,
                        "collaboration_style": instance.template.collaboration_style,
                        "quality_bar": instance.template.quality_bar,
                        "memory_focus": instance.template.memory_focus,
                        "core_workflows": instance.template.core_workflows,
                        "standards_references": instance.template.standards_references,
                        "anti_patterns": instance.template.anti_patterns,
                        "standing_instructions": instance.template.standing_instructions,
                        "project_role_instructions": instance.override.instructions,
                    }
                ),
            ),
            _section("capabilities", _yamlish(capability_context)),
            "</role>",
            "",
            "<project>",
            _section(
                "goal",
                _yamlish(
                    {
                        "description": project.goal.description,
                        "success_measures": project.goal.success_measures,
                        "constraints": project.goal.constraints,
                        "guidance": project.goal.guidance,
                    }
                ),
            ),
            _section(
                "workspace",
                _yamlish(
                    {
                        "current_working_directory": str(workspace_root),
                        "workspace_root": project.workspace.root,
                        "default_repository": project.workspace.default_repository,
                        "repositories": repositories,
                        "allowed_write_paths": instance.override.write_paths,
                    }
                ),
            ),
            _section(
                "document-library-and-memory",
                _yamlish(document_library_context(workspace_root, project)),
            ),
            "</project>",
            "",
            "<assignment>",
            _section("work-item", _yamlish(message.payload)),
            _section(
                "current-flow-state",
                _yamlish(
                    {
                        "state_id": flow_state.state_id,
                        "purpose": flow_state.purpose,
                        "artifact_path": flow_state.artifact_path,
                        "available_handoffs": runtime.get("available_handoffs", []),
                        "available_consults": runtime.get("available_consults", []),
                        "gates": [
                            {
                                "gate_id": gate.gate_id,
                                "type": gate.type,
                                "required_documents": gate.required_documents,
                                "required_review_status": gate.required_review_status,
                                "reviewer_role": gate.reviewer_role,
                                "affected_roles": gate.affected_roles,
                                "review_outcomes": gate.review_outcomes,
                                "response_type": gate.response_type,
                                "requested_from": gate.requested_from,
                                "channel": gate.channel,
                            }
                            for gate in flow_state.gates
                        ],
                    }
                ),
            ),
            _section("current-task", assignment_task),
            "</assignment>",
            "",
            "<instructions>",
            load_prompt_template("worker-general-instructions.md"),
            "</instructions>",
        ]
    )


def _assignment_task(
    message: Message,
    flow_state: FlowState,
    runtime: dict[str, Any],
) -> str:
    direct = flow_state.state_id == "direct_targeted" or runtime.get("lifecycle_state") is None
    no_routes = not runtime.get("available_handoffs") and not runtime.get("available_consults")
    text = str(message.payload.get("text") or message.payload.get("summary") or "")
    if direct and no_routes:
        return render_prompt_template(
            "direct-conversation-assignment.md",
            {"sponsor_instruction": text},
        )
    return render_prompt_template(
        "lifecycle-assignment.md",
        {
            "state_id": flow_state.state_id,
            "state_purpose": flow_state.purpose,
        },
    )


def _section(name: str, body: str) -> str:
    return f"<{name}>\n{body}\n</{name}>"


def _yamlish(value: Any) -> str:
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    return html.escape(text, quote=False)
