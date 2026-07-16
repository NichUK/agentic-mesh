from __future__ import annotations

from dataclasses import replace
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


def test_default_off_plan_has_one_stable_identity_and_one_dormant_service(tmp_path: Path) -> None:
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
    assert "synthetic-network-engineering-1" not in compose["services"]
    service = compose["services"]["agentic-mesh-engineering-1"]
    assert service["profiles"] == ["shared-fleet-activation-closed"]
    assert service["deploy"]["replicas"] == 0
    assert service["environment"]["AGENTIC_MESH_SHARED_FLEET_RUNNABLE"] == "0"
    assert "volumes" not in service
    assert "networks" not in service


def test_false_path_preserves_project_prefixed_runtime_and_true_path_is_closed(tmp_path: Path) -> None:
    disabled = shared_config(tmp_path)
    enabled = shared_config(tmp_path, enabled=True)

    assert disabled.role_instance_id(ROLE_ID) == "synthetic-control.engineering.1"
    assert disabled.role(ROLE_ID).service_name == "synthetic-network-engineering-1"
    with pytest.raises(SharedFleetActivationClosed, match="Stage 1"):
        render_compose(enabled)
    with pytest.raises(ValueError, match="stable fleet_id"):
        stable_fleet_instance_id(fleet_id="orchid", role_id=ROLE_ID)


def test_legacy_instances_greater_than_one_render_one_compatible_identity(tmp_path: Path) -> None:
    base = shared_config(tmp_path)
    config = replace(
        base,
        roles=(replace(base.roles[0], instances=2),),
        shared_fleet=replace(base.shared_fleet, project_assignments=()),
    )

    plan = generated_shared_fleet_plan(config)
    assert len(plan["physical_instances"]) == 1

    compose_text = render_compose(config)
    assert compose_text.count("\n  synthetic-network-engineering-1:\n") == 1
    compose = yaml.safe_load(compose_text)
    assert list(name for name in compose["services"] if name == "synthetic-network-engineering-1") == [
        "synthetic-network-engineering-1"
    ]

    output = tmp_path / "legacy-agent-configs"
    written = materialize_agent_configs(
        project_config=config,
        output_root=output,
        role_templates_dir=Path(__file__).parents[1] / "config" / "roles",
    )
    containers = [path for path in written if path.name == "container.json"]
    assert containers == [output / ROLE_ID / "1" / "container.json"]
    container = json.loads(containers[0].read_text(encoding="utf-8"))
    assert container["role_instance_id"] == "synthetic-control.engineering.1"
    assert container["service_name"] == "synthetic-network-engineering-1"
    assert not (output / ROLE_ID / "2").exists()


def test_complete_shared_fleet_instances_greater_than_one_fail_before_rendering(
    tmp_path: Path,
) -> None:
    base = shared_config(tmp_path)
    config = replace(base, roles=(replace(base.roles[0], instances=2),))

    with pytest.raises(ValueError, match="requires instances: 1.*engineering"):
        generated_shared_fleet_plan(config)
    with pytest.raises(ValueError, match="requires instances: 1.*engineering"):
        render_compose(config)

    output = tmp_path / "rejected-agent-configs"
    with pytest.raises(ValueError, match="requires instances: 1.*engineering"):
        materialize_agent_configs(
            project_config=config,
            output_root=output,
            role_templates_dir=Path(__file__).parents[1] / "config" / "roles",
        )
    assert not output.exists()


def test_synthetic_compose_config_contains_only_dormant_stable_service(tmp_path: Path) -> None:
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
    text = compose_path.read_text(encoding="utf-8")
    assert "agentic-mesh-engineering-1" in text
    assert "synthetic-network-engineering-1" not in text
