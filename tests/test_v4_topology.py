from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v4.topology import RuntimeTopologyError
from agentic_mesh_v4.topology import validate_runtime_topology


def test_v4_runtime_topology_accepts_separate_source_and_workspace(tmp_path: Path) -> None:
    system_root = tmp_path / "system"
    workspace_root = tmp_path / "workspace"
    (system_root / "src" / "agentic_mesh_v4").mkdir(parents=True)
    workspace_root.mkdir()

    validate_runtime_topology(system_root=system_root, workspace_root=workspace_root, enforce=True)


def test_v4_runtime_topology_rejects_same_mounted_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "src" / "agentic_mesh_v4").mkdir(parents=True)

    with pytest.raises(RuntimeTopologyError, match="must be separate mounts"):
        validate_runtime_topology(system_root=checkout, workspace_root=checkout, enforce=True)


def test_v4_runtime_topology_rejects_nested_workspace(tmp_path: Path) -> None:
    system_root = tmp_path / "system"
    workspace_root = system_root / "project"
    (system_root / "src" / "agentic_mesh_v4").mkdir(parents=True)
    workspace_root.mkdir()

    with pytest.raises(RuntimeTopologyError, match="must be separate mounts"):
        validate_runtime_topology(system_root=system_root, workspace_root=workspace_root, enforce=True)
