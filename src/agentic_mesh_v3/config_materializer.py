from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from agentic_mesh_v3.governance import RaciMatrix
from agentic_mesh_v3.lifecycle import RoleContainerSpec


def materialize_agent_config(
    *,
    spec: RoleContainerSpec,
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
        "role.md": role_prompt.rstrip() + "\n",
        "organisation.md": organisation_instructions.rstrip() + "\n",
        "project.md": project_instructions.rstrip() + "\n",
        "tools.md": tool_instructions.rstrip() + "\n",
        "raci.json": json.dumps([asdict(item) for item in raci.assignments], indent=2),
        "container.json": json.dumps(
            {
                "role_instance_id": spec.role_instance_id,
                "image": spec.image,
                "mounts": spec.volume_mounts(),
                "environment": spec.environment,
                "prompt_paths": {
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
