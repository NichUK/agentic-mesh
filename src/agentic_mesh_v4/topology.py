from __future__ import annotations

import os
from pathlib import Path


class RuntimeTopologyError(RuntimeError):
    """Raised when runtime source and project workspace boundaries collapse."""


def validate_runtime_topology(
    *,
    system_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    enforce: bool | None = None,
) -> None:
    """Fail fast when the installed runtime and mutable target workspace overlap."""

    if enforce is None:
        enforce = os.environ.get("AGENTIC_MESH_ENFORCE_RUNTIME_TOPOLOGY", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    system = Path(system_root or os.environ.get("AGENTIC_MESH_RUNTIME_SYSTEM_PATH", "/mesh/system"))
    workspace = Path(
        workspace_root
        or os.environ.get("AGENTIC_MESH_RUNTIME_WORKSPACE_PATH", "/mesh/workspaces/agentic-mesh")
    )
    if not enforce and (not system.exists() or not workspace.exists()):
        return
    missing = [str(path) for path in (system, workspace) if not path.exists()]
    if missing:
        raise RuntimeTopologyError(f"required V4 runtime topology path is missing: {', '.join(missing)}")

    resolved_system = system.resolve()
    resolved_workspace = workspace.resolve()
    same_mount = os.path.samefile(resolved_system, resolved_workspace)
    if (
        same_mount
        or resolved_system == resolved_workspace
        or resolved_system in resolved_workspace.parents
        or resolved_workspace in resolved_system.parents
    ):
        raise RuntimeTopologyError(
            "V4 runtime source and project workspace must be separate mounts: "
            f"system={resolved_system}, workspace={resolved_workspace}"
        )
    package_root = resolved_system / "src" / "agentic_mesh_v4"
    if not package_root.is_dir():
        raise RuntimeTopologyError(f"V4 runtime package is missing from system mount: {package_root}")
