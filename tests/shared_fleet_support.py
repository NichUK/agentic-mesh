from __future__ import annotations

from pathlib import Path

from agentic_mesh_v4.config import V4ProjectAssignmentConfig
from agentic_mesh_v4.config import DEFAULT_ROLE_IDS
from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.config import V4RoleConfig
from agentic_mesh_v4.config import V4SharedFleetConfig
from agentic_mesh_v4.shared_fleet import SourceRoleIdentity


ROLE_ID = "engineering"


def assignment(root: Path, project_id: str, schema: str) -> V4ProjectAssignmentConfig:
    project = root / project_id
    return V4ProjectAssignmentConfig(
        project_id=project_id,
        database_schema=schema,
        database_credential_ref=f"secret://{project_id}/postgres",
        document_root=str(project / "documents"),
        project_root=str(project / "project"),
        workspace_root=str(project / "workspace"),
        repository_roots=(str(project / "repository"),),
        codex_home=str(project / "codex-home"),
    )


def shared_config(root: Path, *, enabled: bool = False) -> V4ProjectConfig:
    role = V4RoleConfig(
        role_id=ROLE_ID,
        display_name="Engineering",
        template=ROLE_ID,
        agent_network_id="synthetic-network",
        authority="full",
        sandbox_mode="danger-full-access",
        approval_policy="never",
        codex_port=4700,
    )
    roles = (role,)
    if enabled:
        roles = tuple(
            role
            if role_id == ROLE_ID
            else V4RoleConfig(
                role_id=role_id,
                display_name=role_id.replace("-", " ").title(),
                template=role_id,
                agent_network_id="synthetic-network",
                authority="full",
                sandbox_mode="danger-full-access",
                approval_policy="never",
                codex_port=4700 + index,
            )
            for index, role_id in enumerate(DEFAULT_ROLE_IDS)
        )
    return V4ProjectConfig(
        project_id="synthetic-control",
        agent_network_id="synthetic-network",
        name="Synthetic shared fleet",
        goal="Stage 1 captured tests",
        roles=roles,
        shared_fleet=V4SharedFleetConfig(
            enabled=enabled,
            fleet_id="agentic-mesh",
            project_assignments=(
                assignment(root, "orchid", "orchid"),
                assignment(root, "cedar", "cedar"),
            ),
        ),
    )


def source(project_id: str, *, fingerprint: str = "role-v1") -> SourceRoleIdentity:
    return SourceRoleIdentity(
        project_id=project_id,
        role_instance_id=f"{project_id}.{ROLE_ID}.1",
        role_id=ROLE_ID,
        ordinal=1,
        canonical_role_fingerprint=fingerprint,
    )
