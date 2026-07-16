from __future__ import annotations

import pytest

from agentic_mesh_v4.shared_fleet import SharedFleetConflict
from agentic_mesh_v4.shared_fleet import project_filtered_dashboard


def test_dashboard_lists_fleet_once_and_filters_only_activity() -> None:
    fleet_rows = (
        {
            "identity_kind": "fleet_instance",
            "fleet_instance_id": "agentic-mesh.engineering.1",
            "role_id": "engineering",
            "state": "ready",
            "health": "healthy",
        },
        {
            "identity_kind": "project_assignment",
            "fleet_instance_id": "agentic-mesh.engineering.1",
            "role_id": "engineering",
            "state": "ready",
        },
    )
    activity = (
        {"activity_id": "a", "project_id": "agentic-mesh-dev", "fleet_instance_id": "agentic-mesh.engineering.1"},
        {"activity_id": "b", "project_id": "quantauma", "fleet_instance_id": "agentic-mesh.engineering.1"},
    )

    all_projects = project_filtered_dashboard(fleet_rows=fleet_rows, activity_rows=activity)
    agentic_mesh = project_filtered_dashboard(
        fleet_rows=fleet_rows,
        activity_rows=activity,
        project_id="agentic-mesh-dev",
    )
    quantauma = project_filtered_dashboard(
        fleet_rows=fleet_rows,
        activity_rows=activity,
        project_id="quantauma",
    )
    assert len(all_projects["fleet"]) == len(agentic_mesh["fleet"]) == len(quantauma["fleet"]) == 1
    assert [item["activity_id"] for item in agentic_mesh["activity"]] == ["a"]
    assert [item["activity_id"] for item in quantauma["activity"]] == ["b"]


def test_dashboard_rejects_divergent_duplicate_physical_identity() -> None:
    rows = (
        {"identity_kind": "fleet_instance", "fleet_instance_id": "agentic-mesh.engineering.1", "role_id": "engineering", "state": "ready", "health": "healthy"},
        {"identity_kind": "fleet_instance", "fleet_instance_id": "agentic-mesh.engineering.1", "role_id": "engineering", "state": "active", "health": "healthy"},
    )
    with pytest.raises(SharedFleetConflict, match="diverges"):
        project_filtered_dashboard(fleet_rows=rows, activity_rows=())
