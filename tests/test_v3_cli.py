from pathlib import Path
import argparse
import json
import subprocess
import sys
from io import StringIO

import pytest

from agentic_mesh_v3.cli import _teams_activity_router
from agentic_mesh_v3.cli import _broker_inspection_payload
from agentic_mesh_v3.cli import _ensure_agent_stream
from agentic_mesh_v3.cli import _stakeholder_bridge
from agentic_mesh_v3.cli import _worker_from_args
from agentic_mesh_v3.cli import main
from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.project_config import load_project_config
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.worker_adapters import CodexCliWorker
from agentic_mesh_v3.worker_adapters import SafeOutputSubprocessWorker


def test_cli_validate_topology_reports_valid_paths(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    result = main(
        [
            "validate-topology",
            "--source-repo",
            str(tmp_path / "source"),
            "--deployed-runtime",
            str(tmp_path / "runtime"),
            "--runtime-state",
            str(tmp_path / "state"),
            "--organisation-config-repo",
            str(tmp_path / "org"),
            "--project-config-repo",
            str(tmp_path / "project"),
            "--document-library-root",
            str(tmp_path / "documents"),
        ]
    )

    output = capsys.readouterr().out

    assert result == 0
    assert '"valid": true' in output


def test_cli_validate_topology_fails_collapsed_paths(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    result = main(
        [
            "validate-topology",
            "--source-repo",
            str(tmp_path / "source"),
            "--deployed-runtime",
            str(tmp_path / "source"),
            "--runtime-state",
            str(tmp_path / "state"),
            "--organisation-config-repo",
            str(tmp_path / "org"),
            "--project-config-repo",
            str(tmp_path / "project"),
            "--document-library-root",
            str(tmp_path / "documents"),
        ]
    )

    output = capsys.readouterr().out

    assert result == 1
    assert '"valid": false' in output
    assert "source_repo and deployed_runtime" in output


def test_cli_module_entrypoint_runs(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh_v3.cli",
            "--db",
            str(tmp_path / "v3.sqlite3"),
            "init-db",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert '"status": "initialized"' in completed.stdout


def test_cli_local_e2e_dogfood_releases_and_closes_slice(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    docs_root = tmp_path / "documents"

    result = main(
        [
            "--db",
            str(db_path),
            "--project-id",
            "agentic-mesh-dev",
            "local-e2e-dogfood",
            "--document-library-root",
            str(docs_root),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    db = V3Database(db_path)
    try:
        db.migrate()
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
        detail = db.work_item_detail("work-v3-local-e2e")
        release = db.connection.execute(
            """
            SELECT status, deployment_result, closure_state
            FROM releases
            WHERE work_item_id=?
            """,
            ("work-v3-local-e2e",),
        ).fetchone()
    finally:
        db.close()

    assert result == 0
    assert output["status"] == "local_e2e_dogfood_complete"
    assert output["work_item_id"] == "work-v3-local-e2e"
    assert snapshot.backlog == ()
    assert snapshot.work_items == ()
    assert detail is not None
    assert detail.state == "closed"
    assert detail.owner_role == "project-manager"
    assert release["status"] == "deployed"
    assert "v3 local smoke deployed" in release["deployment_result"]
    assert release["closure_state"] == "closed"
    assert (docs_root / "work-items" / "work-v3-local-e2e" / "index.md").exists()
    assert (docs_root / "work-items" / "index.md").exists()


def test_cli_local_e2e_dogfood_uses_project_config_document_library(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    docs_root = tmp_path / "project-documents"
    project_config = tmp_path / "project.yaml"
    python_exe = sys.executable.replace("\\", "/")
    project_config.write_text(
        f"""
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: filesystem
  root: {docs_root.as_posix()}
release_deployment_targets:
  local-smoke:
    type: command
    command:
      - '{python_exe}'
      - -c
      - print('configured dogfood deployed')
    timeout_seconds: 30
    rollback_summary: Re-run the previous configured dogfood target.
roles:
  product-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "local-e2e-dogfood",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    db = V3Database(db_path)
    try:
        db.migrate()
        detail = db.work_item_detail("work-v3-local-e2e")
        release = db.connection.execute(
            """
            SELECT status, deployment_result
            FROM releases
            WHERE work_item_id=?
            """,
            ("work-v3-local-e2e",),
        ).fetchone()
    finally:
        db.close()

    assert result == 0
    assert output["status"] == "local_e2e_dogfood_complete"
    assert detail is not None
    assert detail.state == "closed"
    assert release["status"] == "deployed"
    assert "configured dogfood deployed" in release["deployment_result"]
    assert (docs_root / "work-items" / "work-v3-local-e2e" / "index.md").exists()
    assert (docs_root / "work-items" / "index.md").exists()


def test_cli_local_e2e_dogfood_uses_project_sponsor_contact_for_approval(
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    docs_root = tmp_path / "project-documents"
    project_config = tmp_path / "project.yaml"
    python_exe = sys.executable.replace("\\", "/")
    project_config.write_text(
        f"""
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: filesystem
  root: {docs_root.as_posix()}
connectors:
  teams:
    adapter: local
stakeholder_contacts:
  sponsor:
    display_name: Sponsor
    connector: teams
    target_ref: dm:sponsor
release_deployment_targets:
  local-smoke:
    type: command
    command:
      - '{python_exe}'
      - -c
      - print('configured sponsor dogfood deployed')
    timeout_seconds: 30
    rollback_summary: Re-run the previous configured dogfood target.
roles:
  product-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "local-e2e-dogfood",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    db = V3Database(db_path)
    try:
        db.migrate()
        approval = db.approval_detail("approval-v3-local-product")
        deliveries = db.list_outbound_deliveries(work_item_id="work-v3-local-e2e")
        release = db.connection.execute(
            """
            SELECT status, deployment_result
            FROM releases
            WHERE work_item_id=?
            """,
            ("work-v3-local-e2e",),
        ).fetchone()
    finally:
        db.close()

    assert result == 0
    assert output["status"] == "local_e2e_dogfood_complete"
    assert approval is not None
    assert approval["status"] == "approved"
    assert approval["response"] == "approval-v3-local-product approved"
    assert any(delivery["purpose"] == "approval.request" for delivery in deliveries)
    assert any(delivery["purpose"] == "messaging.send" for delivery in deliveries)
    assert any(delivery["target_ref"] == "dm:sponsor" for delivery in deliveries)
    assert release["status"] == "deployed"
    assert "configured sponsor dogfood deployed" in release["deployment_result"]


def test_cli_audit_dogfood_passes_after_local_e2e(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    docs_root = tmp_path / "documents"
    project_config = tmp_path / "project.yaml"
    python_exe = sys.executable.replace("\\", "/")
    project_config.write_text(
        f"""
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: filesystem
  root: {docs_root.as_posix()}
connectors:
  teams:
    adapter: local
stakeholder_contacts:
  sponsor:
    display_name: Sponsor
    connector: teams
    target_ref: dm:sponsor
release_deployment_targets:
  local-smoke:
    type: command
    command:
      - '{python_exe}'
      - -c
      - print('audit dogfood deployed')
roles:
  product-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--db",
                str(db_path),
                "--project-config",
                str(project_config),
                "local-e2e-dogfood",
            ]
        )
        == 0
    )
    capsys.readouterr()

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "audit-dogfood",
        ]
    )

    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["passed"] is True
    check_ids = {check["check_id"] for check in output["checks"]}
    assert "release.deployed" in check_ids
    assert "documents.work_item_index_exists" in check_ids


def test_cli_audit_dogfood_returns_failure_for_missing_evidence(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    docs_root = tmp_path / "documents"
    assert main(["--db", str(db_path), "init-db"]) == 0
    capsys.readouterr()

    result = main(
        [
            "--db",
            str(db_path),
            "audit-dogfood",
            "--document-library-root",
            str(docs_root),
        ]
    )

    output = json.loads(capsys.readouterr().out)

    assert result == 1
    assert output["passed"] is False
    assert output["checks"][0]["check_id"] == "work_item.exists"


def test_cli_local_e2e_dogfood_requires_onedrive_token_for_project_document_library(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AGENTIC_MESH_ONEDRIVE_TOKEN", raising=False)
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: onedrive
  drive_id: drive-123
  root_path: /documents
roles:
  product-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )

    try:
        main(
            [
                "--db",
                str(tmp_path / "v3.sqlite3"),
                "--project-config",
                str(project_config),
                "local-e2e-dogfood",
            ]
        )
    except ValueError as exc:
        assert "AGENTIC_MESH_ONEDRIVE_TOKEN" in str(exc)
    else:
        raise AssertionError("OneDrive-backed dogfood runs should require an access token")


def test_cli_tool_call_uses_project_config_document_library(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    docs_root = tmp_path / "documents"
    project_config.write_text(
        f"""
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: filesystem
  root: {docs_root.as_posix()}
roles:
  product-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )

    result = main(
        [
            "--db",
            str(tmp_path / "v3.sqlite3"),
            "--project-config",
            str(project_config),
            "tool-call",
            "--role-instance-id",
            "agentic-mesh-dev.product-manager.1",
            "--tool-name",
            "document.write_work_item_index",
            "--payload-json",
            (
                '{"work_item_id":"work-1","title":"Work One","status":"active",'
                '"owner_role":"product-manager","raci_summary":"PM A/R",'
                '"governance_state":"ready","next_action":"Continue product shaping."}'
            ),
        ]
    )

    assert result == 0
    assert '"tool_name": "document.write_work_item_index"' in capsys.readouterr().out
    assert (docs_root / "work-items" / "work-1" / "index.md").exists()


def test_cli_tool_call_requires_onedrive_token_for_project_config(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AGENTIC_MESH_ONEDRIVE_TOKEN", raising=False)
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: onedrive
  drive_id: drive-123
  root_path: /documents
roles:
  product-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )

    try:
        main(
            [
                "--db",
                str(tmp_path / "v3.sqlite3"),
                "--project-config",
                str(project_config),
                "tool-call",
                "--role-instance-id",
                "agentic-mesh-dev.product-manager.1",
                "--tool-name",
                "document.write_work_item_index",
                "--payload-json",
                (
                    '{"work_item_id":"work-1","title":"Work One","status":"active",'
                    '"owner_role":"product-manager","raci_summary":"PM A/R",'
                    '"governance_state":"ready","next_action":"Continue product shaping."}'
                ),
            ]
        )
    except ValueError as exc:
        assert "AGENTIC_MESH_ONEDRIVE_TOKEN" in str(exc)
    else:
        raise AssertionError("OneDrive project document libraries should require an access token")


def test_cli_tool_call_uses_project_config_deployment_targets(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    docs_root = tmp_path / "documents"
    python_exe = Path(sys.executable).as_posix()
    project_config.write_text(
        f"""
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: filesystem
  root: {docs_root.as_posix()}
release_deployment_targets:
  local-smoke:
    type: command
    command:
      - "{python_exe}"
      - -c
      - "print('configured deploy')"
    rollback_summary: Re-run previous target.
roles:
  release-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "v3.sqlite3"
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Release work",
            description="Needs deployment.",
            state="release_review",
            owner_role="release-manager",
        )
    finally:
        db.close()

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "tool-call",
            "--role-instance-id",
            "agentic-mesh-dev.release-manager.1",
            "--tool-name",
            "release.deploy",
            "--payload-json",
            (
                '{"work_item_id":"work-1","target_id":"local-smoke",'
                '"scope":"CLI configured deployment","version_ref":"commit:abc123",'
                '"approval_ref":"approval-cli-release-1","smoke_evidence":"CLI smoke passed."}'
            ),
        ]
    )

    output = capsys.readouterr().out
    db = V3Database(db_path)
    try:
        db.migrate()
        detail = db.work_item_detail("work-1")
    finally:
        db.close()

    assert result == 0
    assert '"tool_name": "release.deploy"' in output
    assert detail is not None
    assert detail.releases[0].status == "deployed"
    assert "configured deploy" in detail.releases[0].deployment_result


def test_cli_tool_call_uses_project_config_stakeholder_bridge(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
connectors:
  teams:
    adapter: local
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    result = main(
        [
            "--db",
            str(tmp_path / "v3.sqlite3"),
            "--project-config",
            str(project_config),
            "tool-call",
            "--role-instance-id",
            "agentic-mesh-dev.product-manager.1",
            "--tool-name",
            "status.reply",
            "--payload-json",
            '{"connector":"teams","target_ref":"dm:sponsor","text_markdown":"**Received.**"}',
        ]
    )

    output = capsys.readouterr().out

    assert result == 0
    assert '"tool_name": "status.reply"' in output


def test_cli_stakeholder_bridge_uses_local_teams_adapter(tmp_path: Path) -> None:
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
connectors:
  teams:
    adapter: local
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )

    bridge = _stakeholder_bridge(argparse.Namespace(project_config=project_config))

    assert isinstance(bridge, LocalTeamsBridge)


def test_cli_stakeholder_bridge_requires_graph_token(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
connectors:
  teams:
    adapter: teams-bot-connector
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENTIC_MESH_TEAMS_TOKEN", raising=False)

    try:
        _stakeholder_bridge(argparse.Namespace(project_config=project_config))
    except ValueError as exc:
        assert str(exc) == "AGENTIC_MESH_TEAMS_TOKEN is required for Graph-backed Teams outbound messaging"
    else:
        raise AssertionError("Graph-backed stakeholder bridge should require AGENTIC_MESH_TEAMS_TOKEN")


def test_cli_mcp_stdio_uses_project_config_document_library(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    docs_root = tmp_path / "documents"
    project_config.write_text(
        f"""
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: filesystem
  root: {docs_root.as_posix()}
roles:
  project-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )
    request = {
        "jsonrpc": "2.0",
        "id": "mcp-docs",
        "method": "tools/call",
        "params": {
            "name": "agentic_mesh_v3.tool_call",
            "arguments": {
                "role_instance_id": "agentic-mesh-dev.project-manager.1",
                "tool_name": "document.write_work_item_index",
                "payload": {
                    "work_item_id": "work-1",
                    "title": "Work One",
                    "status": "active",
                    "owner_role": "project-manager",
                    "raci_summary": "PM A/R",
                    "governance_state": "ready",
                    "next_action": "Continue project control.",
                },
            },
        },
    }
    output = StringIO()
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(sys, "stdout", output)

    result = main(
        [
            "--db",
            str(tmp_path / "v3.sqlite3"),
            "--project-config",
            str(project_config),
            "run-tool-mcp-stdio",
        ]
    )

    response = json.loads(output.getvalue())
    assert result == 0
    assert response["result"]["structuredContent"]["tool_name"] == "document.write_work_item_index"
    assert (docs_root / "work-items" / "work-1" / "index.md").exists()


def test_cli_tool_catalog_lists_role_allowed_tools(capsys) -> None:  # type: ignore[no-untyped-def]
    result = main(["tool-catalog", "--role-id", "release-manager"])

    output = capsys.readouterr().out

    assert result == 0
    assert '"role_id": "release-manager"' in output
    assert '"tool_name": "release.deploy"' in output
    assert '"allowed": true' in output


def test_cli_broker_inspection_payload_lists_pending_and_dead_letters() -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "agent.engineering"])
    broker.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")
    pending = broker.publish("agent-inbox", "agent.product-manager", {"text": "shape"})
    dead_lettered = broker.publish("agent-inbox", "agent.product-manager", {"text": "poison"})
    fetched = broker.fetch("agent-inbox", "pm-1", batch=1)[0]
    assert fetched.message_id == pending.message_id
    broker.ack("agent-inbox", "pm-1", fetched.message_id)
    fetched = broker.fetch("agent-inbox", "pm-1", batch=1)[0]
    assert fetched.message_id == dead_lettered.message_id
    broker.dead_letter("agent-inbox", "pm-1", fetched.message_id, reason="invalid payload")
    waiting = broker.publish("agent-inbox", "agent.product-manager", {"text": "next"})

    payload = _broker_inspection_payload(broker, stream="agent-inbox", consumer="pm-1")

    assert payload["stream"] == "agent-inbox"
    assert payload["consumer"] == "pm-1"
    assert payload["pending"] == [
        {
            "message_id": waiting.message_id,
            "subject": "agent.product-manager",
            "payload": {"text": "next"},
            "created_at": waiting.created_at,
            "delivery_count": 0,
        }
    ]
    assert payload["dead_letters"] == [
        {
            "message_id": dead_lettered.message_id,
            "subject": "agent.product-manager",
            "payload": {"text": "poison", "dead_letter_reason": "invalid payload"},
            "created_at": dead_lettered.created_at,
            "delivery_count": 0,
        }
    ]


def test_cli_broker_inspect_requires_project_config() -> None:
    try:
        main(["broker-inspect"])
    except ValueError as exc:
        assert str(exc) == "--project-config is required for broker-inspect"
    else:
        raise AssertionError("broker-inspect should require --project-config")


def test_cli_lifecycle_plan_reports_decisions_from_agent_snapshot(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.engineering.1",
                container_state="running",
                heartbeat_at="2000-01-01T00:00:00+00:00",
                inbox_depth=0,
            )
        )
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.engineering.2",
                container_state="running",
                heartbeat_at="2000-01-01T00:00:00+00:00",
                inbox_depth=0,
            )
        )
    finally:
        db.close()

    result = main(
        [
            "--db",
            str(db_path),
            "--project-id",
            "agentic-mesh-dev",
            "lifecycle-plan",
            "--idle-after-seconds",
            "1",
            "--min-warm-instances-per-role",
            "1",
        ]
    )

    output = capsys.readouterr().out

    assert result == 0
    assert '"action": "hibernate"' in output
    assert '"minimum warm pool would be violated"' in output
    assert '"role_instance_id": "agentic-mesh-dev.engineering.1"' in output
    assert '"role_instance_id": "agentic-mesh-dev.engineering.2"' in output


def test_cli_lifecycle_plan_rejects_invalid_policy() -> None:
    try:
        main(["lifecycle-plan", "--idle-after-seconds", "0"])
    except ValueError as exc:
        assert str(exc) == "--idle-after-seconds must be positive"
    else:
        raise AssertionError("lifecycle-plan should reject invalid idle threshold")


def test_cli_lifecycle_apply_dry_runs_compose_actions(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                container_state="hibernated",
                heartbeat_at="2026-06-15T10:00:00+00:00",
                inbox_depth=1,
            )
        )
    finally:
        db.close()

    result = main(
        [
            "--db",
            str(db_path),
            "--project-id",
            "agentic-mesh-dev",
            "lifecycle-apply",
            "--compose-file",
            str(tmp_path / "compose.yml"),
            "--working-directory",
            str(tmp_path),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["execute"] is False
    assert output["results"][0]["action"] == "wake"
    assert output["results"][0]["service_name"] == "agentic-mesh-dev-product-manager-1"
    assert output["results"][0]["command"][-3:] == ["up", "-d", "agentic-mesh-dev-product-manager-1"]
    assert output["results"][0]["executed"] is False
    assert output["results"][0]["working_directory"] == str(tmp_path)

    db = V3Database(db_path)
    try:
        events = db.connection.execute(
            "SELECT event_type, aggregate_id, payload_json FROM events WHERE event_type='agent.lifecycle_action_recorded'"
        ).fetchall()
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()
    assert len(events) == 1
    assert events[0]["aggregate_id"] == "agentic-mesh-dev.product-manager.1"
    assert json.loads(events[0]["payload_json"])["executed"] is False
    assert snapshot.agents[0].container_state == "hibernated"


def test_cli_project_supervisor_tick_combines_lifecycle_and_sweep(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
roles:
  project-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                container_state="hibernated",
                heartbeat_at="2026-06-15T10:00:00+00:00",
                inbox_depth=1,
            )
        )
        db.upsert_work_item(
            work_item_id="work-blocked",
            title="Blocked work",
            description="Blocked.",
            state="blocked",
            owner_role="engineering",
            next_action="Chase blocker.",
        )
    finally:
        db.close()

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "run-project-supervisor-tick",
            "--compose-file",
            str(tmp_path / "compose.yml"),
            "--working-directory",
            str(tmp_path),
            "--publish-sweep-to-project-manager",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["lifecycle"]["result_count"] == 1
    assert output["lifecycle"]["results"][0]["action"] == "wake"
    assert output["lifecycle"]["results"][0]["executed"] is False
    assert output["sweep"]["finding_count"] == 1
    assert output["sweep"]["findings"][0]["work_item_id"] == "work-blocked"
    assert output["sweep"]["published_message_count"] == 1

    db = V3Database(db_path)
    try:
        events = db.connection.execute(
            "SELECT event_type, aggregate_id FROM events WHERE event_type='project_supervisor.tick'"
        ).fetchall()
    finally:
        db.close()
    assert [(row["event_type"], row["aggregate_id"]) for row in events] == [
        ("project_supervisor.tick", "agentic-mesh-dev")
    ]


def test_cli_project_supervisor_loop_runs_bounded_cycles(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
roles:
  project-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "run-project-supervisor-loop",
            "--cycles",
            "2",
            "--poll-seconds",
            "0",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["cycle_count"] == 2
    assert output["lifecycle_result_count"] == 0
    assert output["sweep_finding_count"] == 0


def test_cli_project_supervisor_service_runs_bounded_mode(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
roles:
  project-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "run-project-supervisor-service",
            "--cycles",
            "1",
            "--poll-seconds",
            "0",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["mode"] == "bounded"
    assert output["interrupted"] is False
    assert output["cycle_count"] == 1


def test_cli_project_supervisor_rejects_missing_project_config() -> None:
    try:
        main(["run-project-supervisor-tick"])
    except ValueError as exc:
        assert "--project-config is required" in str(exc)
    else:
        raise AssertionError("supervisor tick should require project config")


def test_cli_teams_activity_router_is_none_without_project_config() -> None:
    assert _teams_activity_router(argparse.Namespace(project_config=None)) is None


def test_cli_teams_activity_router_uses_project_broker_and_roles(tmp_path: Path) -> None:
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
roles:
  product-manager:
    instances: 1
connectors:
  teams:
    role_bots:
      product-manager:
        display_name: AM-Product Manager
        bot_id_ref: bot-product
""",
        encoding="utf-8",
    )

    router = _teams_activity_router(argparse.Namespace(project_config=project_config))

    assert router is not None
    subjects = router.route_activity(
        {
            "id": "msg-1",
            "text": "Status please.",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        }
    )
    assert subjects == ["agent.product-manager"]


def test_cli_run_agent_once_uses_project_config_and_mounted_paths(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )
    agent_config_dir = tmp_path / "agent"
    agent_config_dir.mkdir()

    result = main(
        [
            "--project-config",
            str(project_config),
            "run-agent-once",
            "--role-id",
            "product-manager",
            "--agent-config-dir",
            str(agent_config_dir),
            "--runtime-state-dir",
            str(tmp_path / "state"),
        ]
    )

    assert result == 0
    assert '"results": []' in capsys.readouterr().out


def test_cli_run_agent_service_reports_idle_agent_status(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )
    agent_config_dir = tmp_path / "agent"
    agent_config_dir.mkdir()
    db_path = tmp_path / "v3.sqlite3"

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "run-agent-service",
            "--role-id",
            "product-manager",
            "--agent-config-dir",
            str(agent_config_dir),
            "--runtime-state-dir",
            str(tmp_path / "state"),
            "--poll-interval-seconds",
            "0",
            "--max-ticks",
            "1",
        ]
    )

    assert result == 0
    assert '"results": []' in capsys.readouterr().out
    db = V3Database(db_path)
    try:
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()
    assert len(snapshot.agents) == 1
    assert snapshot.agents[0].role_instance_id == "agentic-mesh-dev.product-manager.1"
    assert snapshot.agents[0].container_state == "running"
    assert snapshot.agents[0].inbox_depth == 0


def test_cli_materialize_agent_configs_writes_configs_and_compose(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
roles:
  product-manager:
    template: product-manager
    instances: 1
    instructions:
      - Shape the product.
  engineering:
    template: engineering
    instances: 1
    instructions:
      - Build the product.
""",
        encoding="utf-8",
    )
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "product-manager.yaml").write_text(_role_template("product-manager"), encoding="utf-8")
    (roles_dir / "engineering.yaml").write_text(_role_template("engineering"), encoding="utf-8")
    org_file = tmp_path / "org.md"
    org_file.write_text("Organisation rule.", encoding="utf-8")
    system_file = tmp_path / "system.md"
    system_file.write_text("System rule.", encoding="utf-8")
    tools_file = tmp_path / "tools.md"
    tools_file.write_text("Use safe-output tools.", encoding="utf-8")
    compose_output = tmp_path / "deploy" / "compose.yaml"

    result = main(
        [
            "--project-config",
            str(project_config),
            "materialize-agent-configs",
            "--image",
            "agentic-mesh-v3:local",
            "--source-repo",
            str(tmp_path / "source"),
            "--deployed-runtime",
            str(tmp_path / "runtime"),
            "--organisation-config-repo",
            str(tmp_path / "org"),
            "--project-config-repo",
            str(tmp_path / "project"),
            "--agent-config-root",
            str(tmp_path / "agents"),
            "--runtime-state-dir",
            str(tmp_path / "state"),
            "--document-library-root",
            str(tmp_path / "documents"),
            "--role-templates-dir",
            str(roles_dir),
            "--system-instructions-file",
            str(system_file),
            "--organisation-instructions-file",
            str(org_file),
            "--tool-instructions-file",
            str(tools_file),
            "--compose-output",
            str(compose_output),
            "--compose-network",
            "mesh-test",
            "--compose-include-nats",
            "--compose-nats-port",
            "14222:4222",
            "--compose-include-supervisor",
            "--compose-supervisor-compose-file",
            "/mesh/state/roles.yml",
            "--compose-supervisor-execute",
            "--compose-supervisor-mount-docker-socket",
        ]
    )

    output = capsys.readouterr().out

    assert result == 0
    assert "agentic-mesh-dev.product-manager.1" in output
    assert (tmp_path / "agents" / "product-manager" / "1" / "container.json").exists()
    assert "Organisation rule." in (tmp_path / "agents" / "engineering" / "1" / "organisation.md").read_text(
        encoding="utf-8"
    )
    assert "System rule." in (tmp_path / "agents" / "engineering" / "1" / "system.md").read_text(encoding="utf-8")
    compose_text = compose_output.read_text(encoding="utf-8")
    assert "run-agent-service" in compose_text
    assert "mesh-test" in compose_text
    assert "nats:2.10-alpine" in compose_text
    assert "14222:4222" in compose_text
    assert "depends_on" in compose_text
    assert "v3-supervisor" in compose_text
    assert "run-project-supervisor-service" in compose_text
    assert "/mesh/state/roles.yml" in compose_text
    assert "/var/run/docker.sock:/var/run/docker.sock" in compose_text


def test_cli_materialize_agent_configs_rejects_collapsed_topology(tmp_path: Path) -> None:
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
roles:
  product-manager:
    template: product-manager
    instances: 1
""",
        encoding="utf-8",
    )
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "product-manager.yaml").write_text(_role_template("product-manager"), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid V3 topology"):
        main(
            [
                "--project-config",
                str(project_config),
                "materialize-agent-configs",
                "--image",
                "agentic-mesh-v3:local",
                "--source-repo",
                str(tmp_path / "source"),
                "--deployed-runtime",
                str(tmp_path / "source"),
                "--organisation-config-repo",
                str(tmp_path / "org"),
                "--project-config-repo",
                str(tmp_path / "project"),
                "--agent-config-root",
                str(tmp_path / "agents"),
                "--runtime-state-dir",
                str(tmp_path / "state"),
                "--document-library-root",
                str(tmp_path / "documents"),
                "--role-templates-dir",
                str(roles_dir),
            ]
        )


def test_cli_materialize_agent_configs_rejects_target_repository_in_runtime_state(tmp_path: Path) -> None:
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        f"""
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
target_repositories:
  app:
    type: git
    path: {(tmp_path / "state" / "workspaces" / "app").as_posix()}
roles:
  product-manager:
    template: product-manager
    instances: 1
""",
        encoding="utf-8",
    )
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "product-manager.yaml").write_text(_role_template("product-manager"), encoding="utf-8")

    with pytest.raises(ValueError, match="target_repository.app must not live inside runtime_state"):
        main(
            [
                "--project-config",
                str(project_config),
                "materialize-agent-configs",
                "--image",
                "agentic-mesh-v3:local",
                "--source-repo",
                str(tmp_path / "source"),
                "--deployed-runtime",
                str(tmp_path / "runtime"),
                "--organisation-config-repo",
                str(tmp_path / "org"),
                "--project-config-repo",
                str(tmp_path / "project"),
                "--agent-config-root",
                str(tmp_path / "agents"),
                "--runtime-state-dir",
                str(tmp_path / "state"),
                "--document-library-root",
                str(tmp_path / "documents"),
                "--role-templates-dir",
                str(roles_dir),
            ]
        )


def test_cli_materialize_agent_configs_uses_flow_config_raci(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
roles:
  product-manager:
    template: product-manager
    instances: 1
""",
        encoding="utf-8",
    )
    flow_config = tmp_path / "flow.yaml"
    flow_config.write_text(
        """
flow_id: test-flow
raci:
  - phase: product-shaping
    accountable: product-manager
    responsible: [product-manager]
    consulted: [business-analyst]
    informed: [project-manager]
""",
        encoding="utf-8",
    )
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "product-manager.yaml").write_text(_role_template("product-manager"), encoding="utf-8")

    result = main(
        [
            "--project-config",
            str(project_config),
            "materialize-agent-configs",
            "--image",
            "agentic-mesh-v3:local",
            "--source-repo",
            str(tmp_path / "source"),
            "--deployed-runtime",
            str(tmp_path / "runtime"),
            "--organisation-config-repo",
            str(tmp_path / "org"),
            "--project-config-repo",
            str(tmp_path / "project"),
            "--agent-config-root",
            str(tmp_path / "agents"),
            "--runtime-state-dir",
            str(tmp_path / "state"),
            "--document-library-root",
            str(tmp_path / "documents"),
            "--role-templates-dir",
            str(roles_dir),
            "--flow-config",
            str(flow_config),
        ]
    )

    raci = json.loads((tmp_path / "agents" / "product-manager" / "1" / "raci.json").read_text(encoding="utf-8"))
    assert result == 0
    assert "agentic-mesh-dev.product-manager.1" in capsys.readouterr().out
    assert raci == [
        {
            "phase": "product-shaping",
            "accountable": "product-manager",
            "responsible": ["product-manager"],
            "consulted": ["business-analyst"],
            "informed": ["project-manager"],
        }
    ]


def test_cli_materialize_agent_configs_uses_project_flow_template_raci(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: documents
flow:
  template: sdlc
roles:
  release-manager:
    template: release-manager
    instances: 1
""",
        encoding="utf-8",
    )
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    (roles_dir / "release-manager.yaml").write_text(_role_template("release-manager"), encoding="utf-8")

    result = main(
        [
            "--project-config",
            str(project_config),
            "materialize-agent-configs",
            "--image",
            "agentic-mesh-v3:local",
            "--source-repo",
            str(tmp_path / "source"),
            "--deployed-runtime",
            str(tmp_path / "runtime"),
            "--organisation-config-repo",
            str(tmp_path / "org"),
            "--project-config-repo",
            str(tmp_path / "project"),
            "--agent-config-root",
            str(tmp_path / "agents"),
            "--runtime-state-dir",
            str(tmp_path / "state"),
            "--document-library-root",
            str(tmp_path / "documents"),
            "--role-templates-dir",
            str(roles_dir),
        ]
    )

    raci = json.loads((tmp_path / "agents" / "release-manager" / "1" / "raci.json").read_text(encoding="utf-8"))
    deployment = next(item for item in raci if item["phase"] == "deployment")
    assert result == 0
    assert "agentic-mesh-dev.release-manager.1" in capsys.readouterr().out
    assert deployment["accountable"] == "release-manager"
    assert deployment["responsible"] == ["platform-engineer", "engineering"]
    system_prompt = (tmp_path / "agents" / "release-manager" / "1" / "system.md").read_text(encoding="utf-8")
    tools_prompt = (tmp_path / "agents" / "release-manager" / "1" / "tools.md").read_text(encoding="utf-8")
    assert "Do not expose credentials" in system_prompt
    assert "Every worker run must emit both" in tools_prompt


def _role_template(role_id: str) -> str:
    return f"""
role_id: {role_id}
purpose: Test role.
role_profile: Act as a specialist role for tests.
accountabilities:
  - Do the role work.
decision_rights:
  owns:
    - Own role decisions.
  advises:
    - Advise related roles.
  escalates:
    - Escalate blockers.
boundaries:
  - Stay inside role authority.
collaboration_style:
  - Be concise.
quality_bar:
  - Evidence is recorded.
memory_focus:
  - Useful recurring context.
core_workflows:
  - workflow_id: test-workflow
    trigger: Test trigger.
    inputs:
      - Input
    outputs:
      - Output
    artifacts:
      - documents/work-items/{{work_item_id}}/index.md
standards_references:
  - name: Test Standard
    url: docs/test.md
    applies_to: Tests
anti_patterns:
  - Pretending work happened.
standing_instructions:
  - Use tools honestly.
"""


def test_cli_ensure_agent_stream_sets_direct_and_relevance_subjects() -> None:
    broker = InMemoryBrokerAdapter()

    _ensure_agent_stream(broker, stream="agent-inbox", role_ids=("product-manager",))

    broker.publish("agent-inbox", "agent.product-manager", {"text": "direct"})
    broker.publish("agent-inbox", "agent.product-manager.relevance", {"text": "relevance"})
    assert broker.depth("agent-inbox").pending == 2


def test_cli_worker_from_args_builds_subprocess_worker() -> None:
    worker = _worker_from_args(
        argparse.Namespace(
            worker="safe-output-subprocess",
            worker_command_json='["python","-c","print({})"]',
            worker_timeout_seconds=7,
            worker_model=None,
            worker_reasoning_effort=None,
            worker_sandbox_mode=None,
        )
    )

    assert isinstance(worker, SafeOutputSubprocessWorker)
    assert worker.command == ("python", "-c", "print({})")
    assert worker.timeout_seconds == 7


def test_cli_worker_from_args_builds_codex_cli_worker() -> None:
    worker = _worker_from_args(
        argparse.Namespace(
            worker="codex-cli",
            worker_command_json='["codex","exec"]',
            worker_timeout_seconds=600,
            worker_model="gpt-5.5",
            worker_reasoning_effort="high",
            worker_sandbox_mode="workspace-write",
        )
    )

    assert isinstance(worker, CodexCliWorker)
    assert worker.command == ("codex", "exec")
    assert worker.timeout_seconds == 600
    assert worker.model == "gpt-5.5"
    assert worker.reasoning_effort == "high"
    assert worker.sandbox_mode == "workspace-write"


def test_cli_worker_from_args_uses_project_configured_worker(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
document_library:
  adapter: filesystem
  root: documents
roles:
  product-manager:
    instances: 1
    worker:
      adapter: codex-cli
      command:
        - codex
        - exec
      timeout_seconds: 1200
      model: gpt-5.5
      reasoning_effort: high
      sandbox_mode: workspace-write
""",
        encoding="utf-8",
    )

    worker = _worker_from_args(
        argparse.Namespace(
            role_id="product-manager",
            worker=None,
            worker_command_json=None,
            worker_timeout_seconds=None,
            worker_model=None,
            worker_reasoning_effort=None,
            worker_sandbox_mode=None,
        ),
        project_config=load_project_config(project_file),
    )

    assert isinstance(worker, CodexCliWorker)
    assert worker.command == ("codex", "exec")
    assert worker.timeout_seconds == 1200
    assert worker.model == "gpt-5.5"
    assert worker.reasoning_effort == "high"
    assert worker.sandbox_mode == "workspace-write"


def test_cli_record_approval_response_updates_approval(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Approval work",
            description="Needs approval.",
            state="waiting_human",
            owner_role="product-manager",
        )
        db.request_approval(
            approval_id="approval-1",
            work_item_id="work-1",
            requested_by_role="product-manager",
            question="Approve product definition?",
        )
    finally:
        db.close()

    result = main(
        [
            "--db",
            str(db_path),
            "record-approval-response",
            "--approval-id",
            "approval-1",
            "--status",
            "approved",
            "--response",
            "Approved.",
            "--responder-ref",
            "nicholas",
        ]
    )

    assert result == 0
    assert '"status": "approved"' in capsys.readouterr().out
    db = V3Database(db_path)
    try:
        detail = db.work_item_detail("work-1")
    finally:
        db.close()
    assert detail is not None
    assert detail.approvals[0].response == "Approved."


def test_cli_record_approval_response_publishes_to_requesting_role(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    import agentic_mesh_v3.cli as cli_module

    db_path = tmp_path / "v3.sqlite3"
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )
    broker = InMemoryBrokerAdapter()
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Approval work",
            description="Needs approval.",
            state="waiting_human",
            owner_role="product-manager",
        )
        db.request_approval(
            approval_id="approval-1",
            work_item_id="work-1",
            requested_by_role="product-manager",
            question="Approve product definition?",
        )
    finally:
        db.close()
    monkeypatch.setattr(cli_module, "build_broker_adapter", lambda **_: broker)

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "record-approval-response",
            "--approval-id",
            "approval-1",
            "--status",
            "approved",
            "--response",
            "Approved.",
            "--responder-ref",
            "nicholas",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    pending = broker.pending("agent-inbox")
    assert result == 0
    assert output["published_message_id"] == pending[0].message_id
    assert pending[0].subject == "agent.product-manager"
    assert pending[0].payload["message_type"] == "approval.response_recorded"
    assert pending[0].payload["approval_id"] == "approval-1"
    assert pending[0].payload["work_item_id"] == "work-1"
    assert pending[0].payload["status"] == "approved"


def test_cli_teams_activity_router_records_approval_responses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agentic_mesh_v3.cli as cli_module

    db_path = tmp_path / "v3.sqlite3"
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
roles:
  product-manager:
    instances: 1
connectors:
  teams:
    role_bots:
      product-manager:
        display_name: AM-Product Manager
        bot_id_ref: bot-product
""",
        encoding="utf-8",
    )
    broker = InMemoryBrokerAdapter()
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Approval work",
            description="Needs approval.",
            state="waiting_human",
            owner_role="sponsor",
        )
        db.request_approval(
            approval_id="approval-1",
            work_item_id="work-1",
            requested_by_role="product-manager",
            question="Approve product definition?",
        )
    finally:
        db.close()
    monkeypatch.setattr(cli_module, "build_broker_adapter", lambda **_: broker)

    router = _teams_activity_router(
        argparse.Namespace(
            db=db_path,
            project_config=project_config,
        )
    )
    assert router is not None

    subjects = router.route_activity(
        {
            "id": "msg-approval",
            "text": "approval-1 approved",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        }
    )

    db = V3Database(db_path)
    try:
        db.migrate()
        approval = db.approval_detail("approval-1")
    finally:
        db.close()
    pending = broker.pending("agent-inbox")

    assert subjects == ["agent.product-manager"]
    assert approval is not None
    assert approval["status"] == "approved"
    assert pending[0].payload["message_type"] == "approval.response_recorded"


def test_cli_teams_activity_router_records_stakeholder_question_responses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agentic_mesh_v3.cli as cli_module

    db_path = tmp_path / "v3.sqlite3"
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        """
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
roles:
  product-manager:
    instances: 1
connectors:
  teams:
    role_bots:
      product-manager:
        display_name: AM-Product Manager
        bot_id_ref: bot-product
""",
        encoding="utf-8",
    )
    broker = InMemoryBrokerAdapter()
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Question work",
            description="Needs sponsor answer.",
            state="waiting_human",
            owner_role="sponsor",
            current_phase="product-shaping",
        )
        db.record_governance_record(
            record_id="question-1",
            work_item_id="work-1",
            record_type="stakeholder.ask_question",
            role_instance_id="agentic-mesh-dev.product-manager.1",
            target_ref="dm:sponsor",
            summary="Use DMs for approvals?",
            status="requested",
            payload={"question": "Use DMs for approvals?"},
        )
    finally:
        db.close()
    monkeypatch.setattr(cli_module, "build_broker_adapter", lambda **_: broker)

    router = _teams_activity_router(
        argparse.Namespace(
            db=db_path,
            project_config=project_config,
        )
    )
    assert router is not None

    subjects = router.route_activity(
        {
            "id": "msg-question-answer",
            "text": "question-1: yes, use DMs for approvals.",
            "conversation": {"id": "dm-1", "conversationType": "personal"},
            "from": {"id": "user-1"},
            "recipient": {"id": "bot-product", "name": "AM-Product Manager"},
        }
    )

    db = V3Database(db_path)
    try:
        db.migrate()
        detail = db.work_item_detail("work-1")
    finally:
        db.close()
    pending = broker.pending("agent-inbox")

    assert subjects == ["agent.product-manager"]
    assert detail is not None
    assert detail.state == "waiting_agent"
    assert detail.owner_role == "product-manager"
    assert pending[0].payload["message_type"] == "stakeholder.question_answered"
    assert pending[0].payload["question_id"] == "question-1"
