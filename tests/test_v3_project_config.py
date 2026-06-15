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
    instances: 1
  engineering:
    instances: 2
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
