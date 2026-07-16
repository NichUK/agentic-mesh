from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v4.shared_fleet import CapturedBindingController
from agentic_mesh_v4.shared_fleet import FleetActivity
from agentic_mesh_v4.shared_fleet import FleetBinding
from agentic_mesh_v4.shared_fleet import SharedFleetConflict

from shared_fleet_support import shared_config


def test_restart_resume_preserves_project_thread_and_rejects_stale_generation(tmp_path: Path) -> None:
    controller = CapturedBindingController(shared_config(tmp_path).shared_fleet.project_assignments)
    threads = {"orchid": "thread-orchid", "cedar": "thread-cedar"}
    state = FleetBinding("agentic-mesh.engineering.1")

    state, orchid = controller.bind(state, project_id="orchid", activity=FleetActivity())
    assert controller.require_operation(state, project_id="orchid", generation=1).codex_home == orchid.codex_home
    state = controller.unbind(state, activity=FleetActivity())
    state, cedar = controller.bind(state, project_id="cedar", activity=FleetActivity())
    assert threads[cedar.project_id] == "thread-cedar"
    stale_cedar_generation = state.generation
    state = controller.unbind(state, activity=FleetActivity())
    state, orchid_return = controller.bind(state, project_id="orchid", activity=FleetActivity())

    assert threads[orchid_return.project_id] == "thread-orchid"
    assert orchid_return.codex_home == orchid.codex_home
    with pytest.raises(SharedFleetConflict, match="project binding mismatch"):
        controller.require_operation(state, project_id="cedar", generation=stale_cedar_generation)
    unbound = controller.unbind(state, activity=FleetActivity())
    with pytest.raises(SharedFleetConflict, match="requires a bound project"):
        controller.require_operation(unbound, project_id="orchid", generation=3)
