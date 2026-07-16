from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v4.lifecycle import ComposeLifecycle
from agentic_mesh_v4.safe_output_proxy import validated_cli_argv
from agentic_mesh_v4.shared_fleet import CapturedBindingController
from agentic_mesh_v4.shared_fleet import FleetActivity
from agentic_mesh_v4.shared_fleet import FleetBinding
from agentic_mesh_v4.shared_fleet import SharedFleetActivationClosed
from agentic_mesh_v4.shared_fleet import SharedFleetConflict
from agentic_mesh_v4.shared_fleet import SharedFleetOperationGuard

from shared_fleet_support import shared_config


def test_captured_a_b_a_uses_only_selected_project_context(tmp_path: Path) -> None:
    config = shared_config(tmp_path)
    for assignment in config.shared_fleet.project_assignments:
        for path in (
            assignment.document_root,
            assignment.project_root,
            assignment.workspace_root,
            assignment.codex_home,
            *assignment.repository_roots,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / f"{assignment.project_id}.marker").write_text(assignment.project_id, encoding="utf-8")

    controller = CapturedBindingController(config.shared_fleet.project_assignments)
    binding = FleetBinding("agentic-mesh.engineering.1")
    a_binding, a = controller.bind(binding, project_id="orchid", activity=FleetActivity())
    b_binding, b = controller.bind(
        controller.unbind(a_binding, activity=FleetActivity()),
        project_id="cedar",
        activity=FleetActivity(),
    )
    a_return_binding, a_return = controller.bind(
        controller.unbind(b_binding, activity=FleetActivity()),
        project_id="orchid",
        activity=FleetActivity(),
    )

    assert a.fleet_instance_id == b.fleet_instance_id == a_return.fleet_instance_id
    assert (a.generation, b.generation, a_return.generation) == (1, 2, 3)
    assert a.document_root == a_return.document_root
    assert a.database_schema == a_return.database_schema == "orchid"
    assert b.database_schema == "cedar"
    assert all("cedar" not in value for value in _context_paths(a))
    assert all("orchid" not in value for value in _context_paths(b))
    assert controller.require_operation(
        a_return_binding,
        project_id="orchid",
        generation=3,
    ) == a_return

    own_marker = Path(a_return.document_root) / "orchid.marker"
    foreign_marker = Path(b.document_root) / "cedar.marker"
    assert a_return.require_path(own_marker, kind="document").read_text(encoding="utf-8") == "orchid"
    with pytest.raises(SharedFleetConflict, match="foreign document mount denied"):
        a_return.require_path(foreign_marker, kind="document")
    with pytest.raises(SharedFleetConflict, match="foreign repository mount denied"):
        a_return.require_path(Path(b.repository_roots[0]) / "cedar.marker", kind="repository")
    with pytest.raises(SharedFleetConflict, match="foreign workspace mount denied"):
        a_return.require_path(Path(b.workspace_root) / "cedar.marker", kind="workspace")
    with pytest.raises(SharedFleetConflict, match="foreign project mount denied"):
        a_return.require_path(Path(b.project_root) / "cedar.marker", kind="project")
    with pytest.raises(SharedFleetConflict, match="foreign codex_home mount denied"):
        a_return.require_path(Path(b.codex_home) / "cedar.marker", kind="codex_home")
    a_return.require_database(
        schema="orchid",
        credential_ref="secret://orchid/postgres",
    )
    with pytest.raises(SharedFleetConflict, match="foreign database credential denied"):
        a_return.require_database(
            schema="orchid",
            credential_ref="secret://cedar/postgres",
        )
    with pytest.raises(SharedFleetConflict, match="foreign database schema denied"):
        a_return.require_database(
            schema="cedar",
            credential_ref="secret://orchid/postgres",
        )

    guard = SharedFleetOperationGuard(controller=controller, binding=a_return_binding)
    validated = validated_cli_argv(
        request_argv=["safe-output", "artifact-link", "--role-id", "engineering"],
        role_id="engineering",
        project_config=tmp_path / "project-v4.yaml",
        shared_fleet_guard=guard,
        binding_project_id="orchid",
        binding_generation=3,
    )
    assert validated[-3:] == ["artifact-link", "--role-id", "engineering"]
    with pytest.raises(SharedFleetConflict, match="project binding mismatch"):
        validated_cli_argv(
            request_argv=["safe-output", "artifact-link", "--role-id", "engineering"],
            role_id="engineering",
            project_config=tmp_path / "project-v4.yaml",
            shared_fleet_guard=guard,
            binding_project_id="cedar",
            binding_generation=3,
        )

    lifecycle = ComposeLifecycle(
        compose_files=(),
        shared_fleet_guard=guard,
        binding_project_id="orchid",
        binding_generation=3,
    )
    with pytest.raises(SharedFleetActivationClosed, match="stable service start is closed"):
        lifecycle.wake_service("agentic-mesh-engineering-1")


def _context_paths(context: object) -> tuple[str, ...]:
    return (
        context.document_root,
        context.project_root,
        context.workspace_root,
        context.codex_home,
        *context.repository_roots,
    )
