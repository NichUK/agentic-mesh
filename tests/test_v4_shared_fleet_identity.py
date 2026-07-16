from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from agentic_mesh_v4.agent_config import materialize_agent_configs
from agentic_mesh_v4.compose import render_compose
from agentic_mesh_v4.shared_fleet import SharedFleetActivationClosed
from agentic_mesh_v4.shared_fleet import generated_shared_fleet_plan
from agentic_mesh_v4.shared_fleet import stable_fleet_instance_id

from shared_fleet_support import ROLE_ID
from shared_fleet_support import shared_config


def test_default_off_plan_has_one_stable_identity_and_no_executable_service(tmp_path: Path) -> None:
    config = shared_config(tmp_path)
    plan = generated_shared_fleet_plan(config)

    assert plan["enabled"] is False
    assert plan["runnable"] is False
    assert plan["physical_instances"] == [
        {
            "fleet_instance_id": "agentic-mesh.engineering.1",
            "ordinal": 1,
            "role_id": ROLE_ID,
            "service_name": "agentic-mesh-engineering-1",
        }
    ]
    assert [item["project_id"] for item in plan["project_assignments"]] == ["cedar", "orchid"]

    output = tmp_path / "generated"
    templates = Path(__file__).parents[1] / "config" / "roles"
    materialize_agent_configs(project_config=config, output_root=output, role_templates_dir=templates)
    written = json.loads((output / "shared-fleet-plan.json").read_text(encoding="utf-8"))
    assert written == plan

    compose = yaml.safe_load(render_compose(config))
    assert "agentic-mesh-engineering-1" not in compose["services"]
    assert "synthetic-network-engineering-1" in compose["services"]
    assert compose["services"]["synthetic-network-engineering-1"]["profiles"] == ["roles"]


def test_false_path_preserves_project_prefixed_runtime_and_true_path_is_closed(tmp_path: Path) -> None:
    disabled = shared_config(tmp_path)
    enabled = shared_config(tmp_path, enabled=True)

    assert disabled.role_instance_id(ROLE_ID) == "synthetic-control.engineering.1"
    assert disabled.role(ROLE_ID).service_name == "synthetic-network-engineering-1"
    with pytest.raises(SharedFleetActivationClosed, match="Stage 1"):
        render_compose(enabled)
    with pytest.raises(ValueError, match="stable fleet_id"):
        stable_fleet_instance_id(fleet_id="orchid", role_id=ROLE_ID)


def test_synthetic_compose_config_contains_no_stable_service(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is required for synthetic configuration validation")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text(render_compose(shared_config(tmp_path)), encoding="utf-8")
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "config", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "agentic-mesh-engineering-1" not in compose_path.read_text(encoding="utf-8")
