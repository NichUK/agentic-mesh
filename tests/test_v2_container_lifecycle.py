from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v2.container_lifecycle import ComposeRoleLifecycleConfig
from agentic_mesh_v2.container_lifecycle import plan_compose_lifecycle_action
from agentic_mesh_v2.project_config import load_role_container_lifecycle_config


def test_container_lifecycle_config_loads_project_defaults_and_role_overrides(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
container_lifecycle:
  adapter: docker-compose
  compose_files:
    - docker-compose.yml
  service_name_template: "{project_id}-{role_id}-{index}"
  working_directory: deploy
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
    container_lifecycle:
      service_name_template: "pm-{index}"
""",
        encoding="utf-8",
    )

    config = load_role_container_lifecycle_config(project_file, role_id="product-manager")

    assert config["adapter"] == "docker-compose"
    assert config["compose_files"] == [str(tmp_path / "docker-compose.yml")]
    assert config["service_name_template"] == "pm-{index}"
    assert config["working_directory"] == str(tmp_path / "deploy")


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        ({}, "adapter must be `docker-compose`"),
        ({"adapter": "docker"}, "adapter must be `docker-compose`"),
        ({"adapter": "docker-compose"}, "requires compose_files"),
        (
            {"adapter": "docker-compose", "compose_files": [123]},
            "compose_files item 0 must be a non-empty string",
        ),
        (
            {"adapter": "docker-compose", "compose_files": ["compose.yml"]},
            "requires service_name_template",
        ),
        (
            {
                "adapter": "docker-compose",
                "compose_files": ["compose.yml"],
                "service_name_template": "{unknown}",
            },
            "unsupported service_name_template field",
        ),
        (
            {
                "adapter": "docker-compose",
                "compose_files": ["compose.yml"],
                "service_name_template": "{project_id[0]}",
            },
            "unsupported service_name_template field",
        ),
        (
            {
                "adapter": "docker-compose",
                "compose_files": ["compose.yml"],
                "service_name_template": "{project_id.__class__}",
            },
            "unsupported service_name_template field",
        ),
    ],
)
def test_compose_role_lifecycle_config_rejects_invalid_shapes(
    mapping: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ComposeRoleLifecycleConfig.from_mapping(mapping)


def test_container_lifecycle_config_rejects_non_string_compose_files(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
container_lifecycle:
  adapter: docker-compose
  compose_files:
    - 123
  service_name_template: "{project_id}-{role_id}-{index}"
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="compose_files item 0 must be a non-empty string"):
        load_role_container_lifecycle_config(project_file, role_id="product-manager")


def test_compose_role_lifecycle_plans_stop_and_start_commands(tmp_path: Path) -> None:
    config = ComposeRoleLifecycleConfig.from_mapping(
        {
            "adapter": "docker-compose",
            "compose_files": [str(tmp_path / "compose.yml")],
            "service_name_template": "{project_id}-{role_id}-{index}",
            "working_directory": str(tmp_path),
        }
    )

    stop = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )
    start = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hydrating",
        reason="Queued work arrived.",
    )
    idle = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="idle",
        reason="No lifecycle action.",
    )

    assert stop is not None
    assert stop.action == "stop"
    assert stop.service_name == "test-project-product-manager-1"
    assert stop.command == (
        "docker",
        "compose",
        "-f",
        str(tmp_path / "compose.yml"),
        "stop",
        "test-project-product-manager-1",
    )
    assert start is not None
    assert start.action == "start"
    assert start.command[-3:] == ("up", "-d", "test-project-product-manager-1")
    assert idle is None


def test_compose_role_lifecycle_requires_numeric_role_instance_index(tmp_path: Path) -> None:
    config = ComposeRoleLifecycleConfig.from_mapping(
        {
            "adapter": "docker-compose",
            "compose_files": [str(tmp_path / "compose.yml")],
            "service_name_template": "{role_id}-{index}",
        }
    )

    with pytest.raises(ValueError, match="numeric instance index"):
        plan_compose_lifecycle_action(
            config=config,
            project_id="test-project",
            role_id="product-manager",
            role_instance_id="product-manager-main",
            status="hibernated",
            reason="Idle grace elapsed.",
        )
