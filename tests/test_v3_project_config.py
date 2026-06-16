from pathlib import Path

import pytest

from agentic_mesh_v3.project_config import load_project_config
from agentic_mesh_v3.project_config import resolve_project_flow_config_path


def test_load_project_config_reads_v3_broker_docs_and_roles(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: nats-jetstream
  stream: agent-inbox
  servers: nats://localhost:4222
document_library:
  adapter: onedrive
  drive_id: drive-123
  root_path: /documents
flow:
  template: sdlc
target_repositories:
  app:
    type: git
    path: app
    default_branch: develop
roles:
  project-manager:
    template: project-manager
    instances: 1
    worker:
      adapter: codex-cli
      command:
        - codex
        - exec
      timeout_seconds: 900
      model: codex
      reasoning_effort: high
      sandbox_mode: danger-full-access
      auth:
        credential: codex-agentic-mesh-dev-team-q
    instructions:
      - Keep work moving.
    write_paths:
      - docs/project/**
  engineering:
    instances: 2
release_deployment_targets:
  local-smoke:
    type: command
    command:
      - python
      - -c
      - print('deployed')
    working_directory: /tmp
    timeout_seconds: 30
    rollback_summary: Re-run the previous image.
  planning-only:
    type: no_deployment
    reason: Planning-only slice.
connectors:
  teams:
    adapter: teams-bot-connector
    graph_base_url: https://graph.test/v1.0
    role_bots:
      project-manager:
        display_name: AM-Project Manager
        bot_id_ref: teams-bot-project-manager-app-id
        secret_ref: teams-bot-project-manager-secret
""",
        encoding="utf-8",
    )

    config = load_project_config(project_file)

    assert config.project_id == "agentic-mesh-dev"
    assert config.broker.adapter == "nats-jetstream"
    assert config.broker.servers == "nats://localhost:4222"
    assert config.document_library.adapter == "onedrive"
    assert config.document_library.root_path == "/documents"
    assert config.flow.template == "sdlc"
    assert config.flow.path is None
    assert len(config.target_repositories) == 1
    assert config.target_repositories[0].repository_id == "app"
    assert config.target_repositories[0].path == (tmp_path / "app").resolve(strict=False)
    assert config.target_repositories[0].repository_type == "git"
    assert config.target_repositories[0].default_branch == "develop"
    assert {role.role_id: role.instances for role in config.roles} == {
        "engineering": 2,
        "project-manager": 1,
    }
    project_manager = next(role for role in config.roles if role.role_id == "project-manager")
    assert project_manager.template == "project-manager"
    assert project_manager.worker.adapter == "codex-cli"
    assert project_manager.worker.command == ("codex", "exec")
    assert project_manager.worker.timeout_seconds == 900
    assert project_manager.worker.reasoning_effort == "high"
    assert project_manager.worker.auth.credential == "codex-agentic-mesh-dev-team-q"
    assert project_manager.instructions == ("Keep work moving.",)
    assert project_manager.write_paths == ("docs/project/**",)
    assert project_manager.messaging_identity.display_name == "AM-Project Manager"
    assert project_manager.messaging_identity.mention_handle == "@AM-Project Manager"
    assert project_manager.messaging_identity.bot_id_ref == "teams-bot-project-manager-app-id"
    assert config.teams_connector.adapter == "teams-bot-connector"
    assert config.teams_connector.graph_base_url == "https://graph.test/v1.0"
    targets = {target.target_id: target for target in config.release_deployment_targets}
    assert targets["local-smoke"].target_type == "command"
    assert targets["local-smoke"].command == ("python", "-c", "print('deployed')")
    assert targets["local-smoke"].working_directory == Path("/tmp")
    assert targets["local-smoke"].timeout_seconds == 30
    assert targets["local-smoke"].rollback_plan == "Re-run the previous image."
    assert targets["planning-only"].target_type == "no_deployment"
    assert targets["planning-only"].reason == "Planning-only slice."


def test_resolve_project_flow_config_path_prefers_v3_sdlc_template(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
flow:
  template: sdlc
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    resolved = resolve_project_flow_config_path(project_file, load_project_config(project_file))

    assert resolved == Path("config/flows/sdlc-v3.yaml")


def test_load_project_config_reads_workspace_repositories_as_target_repositories(tmp_path: Path) -> None:
    project_file = tmp_path / "agentic-mesh" / "project.yaml"
    project_file.parent.mkdir()
    project_file.write_text(
        """
project_id: agentic-mesh-dev
workspace:
  repositories:
    agentic-mesh:
      type: git
      path: ../../source
      default_branch: develop
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    config = load_project_config(project_file)

    assert len(config.target_repositories) == 1
    repository = config.target_repositories[0]
    assert repository.repository_id == "agentic-mesh"
    assert repository.path == (project_file.parent / "../../source").resolve(strict=False)
    assert repository.default_branch == "develop"


def test_resolve_project_flow_config_path_allows_project_relative_path(tmp_path: Path) -> None:
    project_file = tmp_path / "agentic-mesh" / "project.yaml"
    project_file.parent.mkdir()
    project_file.write_text(
        """
project_id: agentic-mesh-dev
flow:
  path: flows/custom.yaml
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    resolved = resolve_project_flow_config_path(project_file, load_project_config(project_file))

    assert resolved == project_file.parent / "flows" / "custom.yaml"


def test_load_project_config_rejects_unsafe_flow_template(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
flow:
  template: ../sdlc
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"flow.template must match"):
        load_project_config(project_file)


def test_load_project_config_rejects_empty_roles(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
roles: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="roles must define at least one role"):
        load_project_config(project_file)


def test_load_project_config_rejects_non_positive_role_instances(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
roles:
  product-manager:
    instances: 0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"roles.product-manager.instances must be at least 1"):
        load_project_config(project_file)


def test_load_project_config_rejects_unsafe_project_id(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: Agentic Mesh Dev
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="project_id must match"):
        load_project_config(project_file)


def test_load_project_config_rejects_unsafe_role_id(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
roles:
  Product Manager:
    instances: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"roles.Product Manager must match"):
        load_project_config(project_file)


def test_load_project_config_rejects_unsafe_role_template(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
roles:
  product-manager:
    template: ../product-manager
    instances: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"roles.product-manager.template must match"):
        load_project_config(project_file)


def test_load_project_config_rejects_unsafe_target_repository_id(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
target_repositories:
  App Source:
    path: ../app
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"target_repositories.App Source must match"):
        load_project_config(project_file)


def test_load_project_config_rejects_unsafe_workspace_repository_id(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
workspace:
  repositories:
    App Source:
      path: ../app
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"target_repositories.App Source must match"):
        load_project_config(project_file)


def test_load_project_config_rejects_unsafe_release_deployment_target_id(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
release_deployment_targets:
  Runtime Deploy:
    type: command
    command:
      - python
      - -c
      - print('deployed')
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"release_deployment_targets.Runtime Deploy must match"):
        load_project_config(project_file)


def test_load_project_config_rejects_unsafe_teams_role_bot_id(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
roles:
  product-manager:
    instances: 1
connectors:
  teams:
    role_bots:
      Product Manager:
        display_name: AM-Product Manager
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"connectors.teams.role_bots.Product Manager must match"):
        load_project_config(project_file)


def test_load_project_config_rejects_unknown_teams_role_bot_id(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
roles:
  product-manager:
    instances: 1
connectors:
  teams:
    role_bots:
      delivery-manager:
        display_name: AM-Delivery Manager
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"connectors.teams.role_bots.delivery-manager must reference a configured role"):
        load_project_config(project_file)
