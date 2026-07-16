from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest

from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.runtime import V4Runtime
from agentic_mesh_v4.shared_fleet import CapturedBindingController
from agentic_mesh_v4.shared_fleet import FleetActivity
from agentic_mesh_v4.shared_fleet import FleetBinding
from agentic_mesh_v4.shared_fleet import MigrationTraffic
from agentic_mesh_v4.shared_fleet import SharedFleetConflict
from agentic_mesh_v4.shared_fleet import SharedFleetOperationGuard
from agentic_mesh_v4.shared_fleet import evaluate_migration_guard

from shared_fleet_support import shared_config


WORK_ITEM_ID = "work-v4-single-shared-fleet-migration-20260716"


def test_binding_requires_idle_matching_project_and_generation(tmp_path: Path) -> None:
    config = shared_config(tmp_path)
    controller = CapturedBindingController(config.shared_fleet.project_assignments)
    initial = FleetBinding("agentic-mesh.engineering.1")

    orchid_binding, orchid = controller.bind(initial, project_id="orchid", activity=FleetActivity())
    assert orchid.project_id == "orchid"
    assert orchid.generation == 1
    assert "cedar" not in orchid.document_root
    assert controller.require_operation(
        orchid_binding,
        project_id="orchid",
        generation=1,
    ) == orchid

    with pytest.raises(SharedFleetConflict, match="project binding mismatch"):
        controller.require_operation(orchid_binding, project_id="cedar", generation=1)
    with pytest.raises(SharedFleetConflict, match="generation mismatch"):
        controller.require_operation(orchid_binding, project_id="orchid", generation=0)
    with pytest.raises(SharedFleetConflict, match="must be idle"):
        controller.bind(
            orchid_binding,
            project_id="cedar",
            activity=FleetActivity(active_turn=True),
        )
    with pytest.raises(SharedFleetConflict, match="must be unbound"):
        controller.bind(orchid_binding, project_id="cedar", activity=FleetActivity())


def test_migration_guard_allows_only_control_item_and_zero_quantauma_work() -> None:
    control = MigrationTraffic("m1", "safe-output", "engineering", "active_turn", WORK_ITEM_ID)
    unrelated = MigrationTraffic("m2", "teams", "engineering", "queued", "other-work")
    terminal = MigrationTraffic("m3", "safe-output", "engineering", "completed", "other-work")

    assert evaluate_migration_guard(
        agentic_mesh_messages=(control, terminal),
        quantauma_messages=(),
        expected_work_item_id=WORK_ITEM_ID,
    ).allowed
    blocked = evaluate_migration_guard(
        agentic_mesh_messages=(control, unrelated),
        quantauma_messages=(),
        expected_work_item_id=WORK_ITEM_ID,
    )
    assert not blocked.allowed and blocked.unrelated == (unrelated,)
    quantauma_blocked = evaluate_migration_guard(
        agentic_mesh_messages=(control,),
        quantauma_messages=(MigrationTraffic("q1", "safe-output", "qa-engineer", "queued", WORK_ITEM_ID),),
        expected_work_item_id=WORK_ITEM_ID,
    )
    assert not quantauma_blocked.allowed
    assert not evaluate_migration_guard(
        agentic_mesh_messages=(control,),
        quantauma_messages=(),
        expected_work_item_id=WORK_ITEM_ID,
        cutover=True,
    ).allowed


def test_runtime_and_database_adapter_share_one_project_qualified_binding(tmp_path: Path) -> None:
    config = replace(shared_config(tmp_path), project_id="orchid")
    controller = CapturedBindingController(config.shared_fleet.project_assignments)
    binding, expected = controller.bind(
        FleetBinding("agentic-mesh.engineering.1"),
        project_id="orchid",
        activity=FleetActivity(),
    )
    guard = SharedFleetOperationGuard(controller=controller, binding=binding)
    db = object.__new__(V4Database)
    db.shared_fleet_guard = guard
    runtime = V4Runtime(db=db, project_config=config, shared_fleet_guard=guard)

    assert runtime._require_project_operation("orchid") == expected
    assert db.require_project_operation(project_id="orchid", generation=1) == expected
    with pytest.raises(SharedFleetConflict, match="project binding mismatch"):
        runtime._require_project_operation("cedar")
    with pytest.raises(SharedFleetConflict, match="generation mismatch"):
        db.require_project_operation(project_id="orchid", generation=2)
