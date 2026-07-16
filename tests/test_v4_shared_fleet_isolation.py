from __future__ import annotations

from pathlib import Path

from agentic_mesh_v4.shared_fleet import CapturedBindingController
from agentic_mesh_v4.shared_fleet import FleetActivity
from agentic_mesh_v4.shared_fleet import FleetBinding

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


def _context_paths(context: object) -> tuple[str, ...]:
    return (
        context.document_root,
        context.project_root,
        context.workspace_root,
        context.codex_home,
        *context.repository_roots,
    )
