from pathlib import Path

import pytest

from agentic_mesh_v2.project_config import load_role_worker_config
from agentic_mesh_v2.project_config import load_role_memory_config
from agentic_mesh_v2.project_config import load_role_memory_context
from agentic_mesh_v2.project_config import load_teams_connector_config
from agentic_mesh_v2.project_config import list_project_role_service_configs
from agentic_mesh_v2.project_config import list_release_deployment_target_configs


DOGFOOD_PROJECT_FILE = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml")


def test_load_role_worker_config_resolves_safe_output_file_path(tmp_path: Path) -> None:
    project_file = tmp_path / "agentic-mesh" / "project.yaml"
    project_file.parent.mkdir()
    project_file.write_text(
        """
project_id: test-project
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: workers/product-manager-calls.json
""",
        encoding="utf-8",
    )

    config = load_role_worker_config(project_file, role_id="product-manager")

    assert config["adapter"] == "safe-output-file"
    assert config["path"] == str(project_file.parent / "workers" / "product-manager-calls.json")


def test_load_teams_connector_config_from_dogfood_project() -> None:
    config = load_teams_connector_config(DOGFOOD_PROJECT_FILE, external_base_url="http://linuxch:8100")

    assert config.connector_id == "teams-agentic-mesh-dev"
    assert config.project_team_ref == "e664f0d3-2d3f-4ef4-9102-2b99e1601169"
    assert config.default_project_channel_ref == "19:fS0LN3jkUb5T7hMvueOm-o1PHRoIAk4lm_8MscXSCXE1@thread.tacv2"
    assert config.external_base_url == "http://linuxch:8100"
    assert config.role_identities["product-manager"].display_name == "AM-Product Manager"
    assert config.role_identities["product-manager"].mention_handle == "@AM-Product Manager"


def test_load_release_deployment_targets_from_dogfood_project() -> None:
    targets = list_release_deployment_target_configs(DOGFOOD_PROJECT_FILE)

    assert len(targets) == 1
    target = targets[0]
    assert target.target_id == "dogfood_compose"
    assert target.project_id == "agentic-mesh-dev"
    assert target.target_type == "command"
    assert target.command == ("sh", "scripts/release-linuxch-compose.sh")
    assert target.working_directory is not None
    assert target.working_directory.as_posix().endswith("/mesh/workspaces/agentic-mesh")
    assert target.timeout_seconds == 900
    assert "runtime_code" in target.impact_categories
    assert target.smoke["route_label"] == "/status"


def test_load_role_worker_config_preserves_future_adapter_config(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
roles:
  engineering:
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-team
""",
        encoding="utf-8",
    )

    config = load_role_worker_config(project_file, role_id="engineering")

    assert config == {
        "adapter": "codex-cli",
        "model": "codex",
        "auth": {"credential": "codex-team"},
    }


def test_list_project_role_service_configs_expands_instances(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
roles:
  engineering:
    instances: 2
    worker:
      adapter: safe-output-subprocess
      command: ["python", "-c", "print('{}')"]
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    configs = list_project_role_service_configs(project_file)

    assert [config.role_instance_id for config in configs] == [
        "test-project.engineering.1",
        "test-project.engineering.2",
        "test-project.product-manager.1",
    ]
    assert configs[0].worker_config["adapter"] == "safe-output-subprocess"
    assert configs[-1].worker_config["path"] == str(tmp_path / "calls.json")


def test_load_role_memory_context_reads_project_local_role_memory(tmp_path: Path) -> None:
    project_root = tmp_path / "demo-project"
    project_file = project_root / "agentic-mesh" / "project.yaml"
    role_dir = project_root / "agentic-mesh" / "roles" / "product-manager"
    role_dir.mkdir(parents=True)
    (role_dir / "role.yaml").write_text(
        """
schema_version: role-runtime-config-v0
role_id: product-manager
memory:
  file: MEMORY.md
""",
        encoding="utf-8",
    )
    (role_dir / "MEMORY.md").write_text(
        "# Product Manager Memory\n\n- Source: work-123. Sponsor cares about compact dashboards.",
        encoding="utf-8",
    )
    project_file.write_text(
        """
project_id: test-project
role_memory:
  enabled: true
  backend: filesystem
  config_root: agentic-mesh/roles
  memory_filename: MEMORY.md
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    config = load_role_memory_config(project_file, role_id="product-manager")
    context = load_role_memory_context(project_file, role_id="product-manager")

    assert config.enabled is True
    assert config.config_root == project_root / "agentic-mesh" / "roles"
    assert config.memory_path == role_dir / "MEMORY.md"
    assert len(context) == 1
    assert "Role memory file:" in context[0]
    assert "Sponsor cares about compact dashboards." in context[0]


def test_load_role_memory_context_returns_empty_when_disabled(tmp_path: Path) -> None:
    project_file = tmp_path / "agentic-mesh" / "project.yaml"
    project_file.parent.mkdir()
    project_file.write_text(
        """
project_id: test-project
role_memory:
  enabled: false
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    assert load_role_memory_context(project_file, role_id="product-manager") == ()


def test_load_role_memory_context_returns_empty_when_enabled_file_missing(tmp_path: Path) -> None:
    project_file = tmp_path / "demo-project" / "agentic-mesh" / "project.yaml"
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        """
project_id: test-project
role_memory:
  enabled: true
  backend: filesystem
  config_root: agentic-mesh/roles
  memory_filename: MEMORY.md
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    assert load_role_memory_context(project_file, role_id="product-manager") == ()


def test_load_role_memory_config_rejects_config_root_escape(tmp_path: Path) -> None:
    project_file = tmp_path / "demo-project" / "agentic-mesh" / "project.yaml"
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        """
project_id: test-project
role_memory:
  enabled: true
  backend: filesystem
  config_root: ../outside-project
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="role_memory.config_root must resolve inside project root"):
        load_role_memory_config(project_file, role_id="product-manager")


def test_load_role_memory_config_rejects_memory_file_escape(tmp_path: Path) -> None:
    project_root = tmp_path / "demo-project"
    project_file = project_root / "agentic-mesh" / "project.yaml"
    role_dir = project_root / "agentic-mesh" / "roles" / "product-manager"
    role_dir.mkdir(parents=True)
    (role_dir / "role.yaml").write_text(
        """
schema_version: role-runtime-config-v0
role_id: product-manager
memory:
  file: ../outside.md
""",
        encoding="utf-8",
    )
    project_file.write_text(
        """
project_id: test-project
role_memory:
  enabled: true
  backend: filesystem
  config_root: agentic-mesh/roles
  memory_filename: MEMORY.md
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="role.memory.file must be a single relative file name"):
        load_role_memory_config(project_file, role_id="product-manager")


def test_load_role_memory_config_rejects_role_id_escape(tmp_path: Path) -> None:
    project_file = tmp_path / "demo-project" / "agentic-mesh" / "project.yaml"
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        """
project_id: test-project
role_memory:
  enabled: true
  backend: filesystem
  config_root: agentic-mesh/roles
  memory_filename: MEMORY.md
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="role_id must resolve inside project root"):
        load_role_memory_config(project_file, role_id="../product-manager")


@pytest.mark.parametrize(
    ("body", "role_id", "message"),
    [
        ("[]", "product-manager", "project config must be a mapping"),
        ("project_id: test", "product-manager", "project config must define roles"),
        ("roles: {}", "product-manager", "does not define role"),
        ("roles:\n  product-manager: {}", "product-manager", "must define worker config"),
        (
            "roles:\n  product-manager:\n    worker:\n      model: codex",
            "product-manager",
            "requires adapter",
        ),
    ],
)
def test_load_role_worker_config_rejects_invalid_project_config(
    tmp_path: Path,
    body: str,
    role_id: str,
    message: str,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_role_worker_config(project_file, role_id=role_id)


def test_list_project_role_service_configs_rejects_invalid_instances(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test
roles:
  product-manager:
    instances: 0
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="instances must be a positive integer"):
        list_project_role_service_configs(project_file)
