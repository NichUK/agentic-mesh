from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from agentic_mesh_v4.agent_config import materialize_agent_configs
from agentic_mesh_v4.compose import render_compose
from agentic_mesh_v4.config import DEFAULT_ROLE_IDS
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.shared_fleet import SharedFleetActivationClosed
from agentic_mesh_v4.shared_fleet import generated_shared_fleet_plan


PROJECT_FILE = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")


def test_step11_renderer_materializes_exact_dormant_target_and_assignment_boundaries(tmp_path: Path) -> None:
    config = load_project_config(_complete_config(tmp_path))
    plan = generated_shared_fleet_plan(config)
    compose_text = render_compose(config)
    compose = yaml.safe_load(compose_text)
    assert generated_shared_fleet_plan(config) == plan
    assert render_compose(config) == compose_text

    assert plan["enabled"] is False
    assert plan["runnable"] is False
    assert plan["activation_gate"] == "stages_2_4_closed"
    assert len(plan["physical_instances"]) == 15
    assert len(plan["physical_assignments"]) == 30
    assert len({item["fleet_instance_id"] for item in plan["physical_instances"]}) == 15

    stable_services = {
        f"agentic-mesh-{role_id}-1" for role_id in DEFAULT_ROLE_IDS
    }
    rendered_shared_services = {
        name
        for name, service in compose["services"].items()
        if service.get("labels", {}).get("agentic-mesh.shared-fleet.enabled") == "false"
    }
    assert rendered_shared_services == stable_services
    assert not {
        name for name in compose["services"] if name.startswith("agentic-mesh-dev-")
    }

    expected_refs = {
        "agentic-mesh.assignment.cedar",
        "agentic-mesh.assignment.orchid",
    }
    for service_name in stable_services:
        service = compose["services"][service_name]
        assert service["profiles"] == ["shared-fleet-activation-closed"]
        assert service["deploy"] == {"replicas": 0}
        assert service["restart"] == "no"
        assert service["environment"]["AGENTIC_MESH_SHARED_FLEET_ENABLED"] == "0"
        assert service["environment"]["AGENTIC_MESH_SHARED_FLEET_RUNNABLE"] == "0"
        assert service["environment"]["AGENTIC_MESH_SHARED_FLEET_ACTIVATION_GATE"] == "stages_2_4_closed"
        assert set(json.loads(service["environment"]["AGENTIC_MESH_SHARED_FLEET_ASSIGNMENT_ALLOWLIST_REFS"])) == expected_refs
        assert "volumes" not in service
        assert "networks" not in service
        service_without_refs = json.loads(json.dumps(service))
        service_without_refs["environment"].pop("AGENTIC_MESH_SHARED_FLEET_ASSIGNMENT_ALLOWLIST_REFS")
        assert "orchid" not in json.dumps(service_without_refs, sort_keys=True)
        assert "cedar" not in json.dumps(service_without_refs, sort_keys=True)

    extension = compose["x-agentic-mesh-shared-fleet"]
    assert extension["enabled"] is False
    assert extension["runnable"] is False
    assert extension["activation_gate"] == "stages_2_4_closed"
    assert set(extension["assignment_allowlists"]) == expected_refs
    _assert_bilateral_allowlists(extension["assignment_allowlists"], tmp_path)

    output = tmp_path / "agent-configs"
    written = materialize_agent_configs(
        project_config=config,
        output_root=output,
        role_templates_dir="config/roles",
    )
    containers = [path for path in written if path.name == "container.json"]
    assert len(containers) == 15
    for path in containers:
        container = json.loads(path.read_text(encoding="utf-8"))
        assert container["service_name"] in stable_services
        assert container["role_instance_id"].startswith("agentic-mesh.")
        assert container["shared_fleet"] == {
            "activation_gate": "stages_2_4_closed",
            "assignment_allowlist_refs": sorted(expected_refs),
            "bound_project_id": None,
            "enabled": False,
            "mounts": [],
            "networks": [],
            "runnable": False,
        }

    generated = compose_text + json.dumps(plan, sort_keys=True) + "".join(
        path.read_text(encoding="utf-8") for path in containers
    )
    for forbidden in ("PRIVATE KEY", "synthetic-password-value", "synthetic-token-value", "ws-token"):
        assert forbidden not in generated


def test_step11_rendered_compose_is_syntactically_valid(tmp_path: Path) -> None:
    config = load_project_config(_complete_config(tmp_path))
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text(render_compose(config), encoding="utf-8")
    assert isinstance(yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"], dict)
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    result = subprocess.run(
        ["docker", "compose", "--profile", "shared-fleet-activation-closed", "-f", str(compose_path), "config", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda raw: raw["shared_fleet"].update({"unknown": True}), "unknown keys"),
        (lambda raw: raw["shared_fleet"]["project_assignments"][0].update({"unknown": True}), "unknown keys"),
        (lambda raw: raw["roles"].pop("qa-engineer"), "missing shared-fleet roles"),
        (lambda raw: raw["roles"].update({"unknown-role": {}}), "unknown shared-fleet roles"),
        (
            lambda raw: raw["shared_fleet"]["project_assignments"][0].pop("database_schema"),
            "database_schema is required",
        ),
        (
            lambda raw: raw["shared_fleet"]["project_assignments"].append(
                dict(raw["shared_fleet"]["project_assignments"][0])
            ),
            "duplicate shared-fleet project assignment",
        ),
    ),
)
def test_complete_shared_fleet_config_rejects_unknown_missing_and_duplicate_entries(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    raw = _complete_raw(tmp_path)
    mutation(raw)
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_project_config(path)


def test_duplicate_yaml_role_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-role.yaml"
    path.write_text(
        "project_id: synthetic\nroles:\n  project-manager: {}\n  project-manager: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate configuration key: project-manager"):
        load_project_config(path)


def test_activation_gate_remains_closed_for_complete_configuration(tmp_path: Path) -> None:
    raw = _complete_raw(tmp_path)
    raw["shared_fleet"]["enabled"] = True
    path = tmp_path / "enabled.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_project_config(path)
    with pytest.raises(SharedFleetActivationClosed, match="Stage 1"):
        render_compose(config)
    with pytest.raises(SharedFleetActivationClosed, match="Stage 1"):
        generated_shared_fleet_plan(config)


def test_complete_shared_fleet_config_rejects_multi_instance_roles(tmp_path: Path) -> None:
    raw = _complete_raw(tmp_path)
    raw["roles"]["engineering"]["instances"] = 2
    path = tmp_path / "multi-instance.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match=r"requires instances: 1 .*engineering"):
        load_project_config(path)


def test_incomplete_shared_fleet_keeps_single_project_service_and_container_config(tmp_path: Path) -> None:
    raw = yaml.safe_load(PROJECT_FILE.read_text(encoding="utf-8"))
    raw["roles"]["engineering"]["instances"] = 2
    path = tmp_path / "project-v4.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_project_config(path)

    compose = yaml.safe_load(render_compose(config))
    service_names = [name for name in compose["services"] if name == config.role("engineering").service_name]
    assert service_names == [config.role("engineering").service_name]

    output = tmp_path / "agent-configs"
    written = materialize_agent_configs(
        project_config=config,
        output_root=output,
        role_templates_dir="config/roles",
    )
    containers = [path for path in written if path.name == "container.json" and path.parent.parent.name == "engineering"]
    assert len(containers) == 1
    container = json.loads(containers[0].read_text(encoding="utf-8"))
    assert container["role_instance_id"] == config.role_instance_id("engineering")
    assert container["service_name"] == config.role("engineering").service_name


def _complete_config(tmp_path: Path) -> Path:
    path = tmp_path / "project-v4.yaml"
    path.write_text(yaml.safe_dump(_complete_raw(tmp_path), sort_keys=False), encoding="utf-8")
    return path


def _complete_raw(tmp_path: Path) -> dict:
    raw = yaml.safe_load(PROJECT_FILE.read_text(encoding="utf-8"))
    raw["shared_fleet"] = {
        "enabled": False,
        "fleet_id": "agentic-mesh",
        "project_assignments": [
            _assignment(tmp_path, "orchid", "orchid_schema"),
            _assignment(tmp_path, "cedar", "cedar_schema"),
        ],
    }
    return raw


def _assignment(tmp_path: Path, project_id: str, schema: str) -> dict:
    root = tmp_path / project_id
    return {
        "project_id": project_id,
        "database_schema": schema,
        "database_credential_ref": f"secret://{project_id}/postgres",
        "document_root": str(root / "documents"),
        "project_root": str(root / "project"),
        "workspace_root": str(root / "workspaces"),
        "repository_roots": [str(root / "repositories" / "primary")],
        "codex_home": str(root / "codex-home"),
    }


def _assert_bilateral_allowlists(allowlists: dict, tmp_path: Path) -> None:
    for project_id, foreign_id in (("orchid", "cedar"), ("cedar", "orchid")):
        item = allowlists[f"agentic-mesh.assignment.{project_id}"]
        serialized = json.dumps(item, sort_keys=True)
        assert item["project_id"] == project_id
        assert item["database_schema"] == f"{project_id}_schema"
        assert item["database_credential_ref"] == f"secret://{project_id}/postgres"
        assert item["networks"] == [f"agentic-mesh-{project_id}"]
        assert len(item["mounts"]) == 5
        assert {mount["kind"] for mount in item["mounts"]} == {
            "codex_home",
            "document",
            "project",
            "repository",
            "workspace",
        }
        assert all(mount["source"].startswith(str(tmp_path / project_id)) for mount in item["mounts"])
        assert foreign_id not in serialized
