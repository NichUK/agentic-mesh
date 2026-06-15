from pathlib import Path

from agentic_mesh_v3.project_config import load_project_config


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
