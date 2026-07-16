from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_mesh_v4.server import shared_fleet_status_payload
from agentic_mesh_v4.shared_fleet import SharedFleetConflict
from agentic_mesh_v4.shared_fleet import project_filtered_dashboard

from shared_fleet_support import shared_config


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


def test_dashboard_never_advertises_invalid_enabled_fleet_as_runnable(tmp_path: Path) -> None:
    config = shared_config(tmp_path)
    invalid_enabled = replace(
        config,
        shared_fleet=replace(config.shared_fleet, enabled=True),
    )
    payload = shared_fleet_status_payload(
        {"roles": [], "shared_fleet_activity": []},
        project_config=invalid_enabled,
        project_id="orchid",
    )
    assert payload["shared_fleet"]["enabled"] is True
    assert payload["shared_fleet"]["runnable"] is False


def test_existing_status_api_serializes_single_fleet_and_project_activity(tmp_path: Path) -> None:
    config = shared_config(tmp_path)
    fleet = {
        "identity_kind": "fleet_instance",
        "fleet_instance_id": "agentic-mesh.engineering.1",
        "role_instance_id": "agentic-mesh.engineering.1",
        "role_id": "engineering",
        "display_name": "Engineering",
        "state": "ready",
        "effective_state": "ready",
        "authority": "full",
        "codex_endpoint": "ws://agentic-mesh-engineering-1:4700",
    }
    assignment = {
        **fleet,
        "identity_kind": "project_assignment",
        "role_instance_id": "orchid.engineering.1",
    }
    snapshot = {
        "roles": [fleet, assignment],
        "shared_fleet_activity": [
            {"activity_id": "a", "project_id": "orchid", "fleet_instance_id": fleet["fleet_instance_id"]},
            {"activity_id": "b", "project_id": "cedar", "fleet_instance_id": fleet["fleet_instance_id"]},
        ],
    }
    orchid = shared_fleet_status_payload(snapshot, project_config=config, project_id="orchid")
    cedar = shared_fleet_status_payload(snapshot, project_config=config, project_id="cedar")
    assert len(orchid["roles"]) == len(cedar["roles"]) == 1
    assert orchid["roles"][0]["role_instance_id"] == "agentic-mesh.engineering.1"
    assert [item["activity_id"] for item in orchid["shared_fleet"]["activity"]] == ["a"]
    assert [item["activity_id"] for item in cedar["shared_fleet"]["activity"]] == ["b"]
    assert orchid["shared_fleet"]["enabled"] is False
    assert orchid["shared_fleet"]["runnable"] is False
    assert json.loads(json.dumps(orchid, sort_keys=True))["shared_fleet"]["fleet"][0]["fleet_instance_id"] == "agentic-mesh.engineering.1"

    enabled = shared_fleet_status_payload(
        snapshot,
        project_config=shared_config(tmp_path, enabled=True),
        project_id="orchid",
    )
    assert enabled["shared_fleet"]["enabled"] is True
    assert enabled["shared_fleet"]["runnable"] is True
    assert len(enabled["roles"]) == 1
