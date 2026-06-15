from pathlib import Path
import argparse
import subprocess
import sys

from agentic_mesh_v3.cli import _teams_activity_router
from agentic_mesh_v3.cli import _ensure_agent_stream
from agentic_mesh_v3.cli import _worker_from_args
from agentic_mesh_v3.cli import main
from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.db import V3Database
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
                '"governance_state":"ready"}'
            ),
        ]
    )

    assert result == 0
    assert '"tool_name": "document.write_work_item_index"' in capsys.readouterr().out
    assert (docs_root / "work-items" / "work-1" / "index.md").exists()


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
