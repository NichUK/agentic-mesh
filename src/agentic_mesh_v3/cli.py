from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agentic_mesh_v3.agent import build_role_memory
from agentic_mesh_v3.agent import EchoWorker
from agentic_mesh_v3.agent import RoleAgentService
from agentic_mesh_v3.broker import build_broker_adapter
from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.config_materializer import build_role_instance_config
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.demo import run_demo_slice
from agentic_mesh_v3.dogfood import run_local_e2e_dogfood_slice
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.documents import build_document_library_adapter
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.observability import TelemetrySettings
from agentic_mesh_v3.observability import configure_observability
from agentic_mesh_v3.project_config import load_project_config
from agentic_mesh_v3.project_config import V3ProjectConfig
from agentic_mesh_v3.project_config import V3WorkerConfig
from agentic_mesh_v3.server import serve
from agentic_mesh_v3.sweeps import ProjectSweepService
from agentic_mesh_v3.teams_ingress import TeamsActivityRouter
from agentic_mesh_v3.teams_ingress import teams_role_identities_from_project_config
from agentic_mesh_v3.tool_mcp import run_v3_mcp_stdio
from agentic_mesh_v3.tools import V3ToolService
from agentic_mesh_v3.topology import V3Topology
from agentic_mesh_v3.topology import validate_topology
from agentic_mesh_v3.worker_adapters import build_worker_adapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-mesh-v3")
    parser.add_argument("--db", type=Path, default=Path(".tmp/v3/agentic-mesh-v3.sqlite3"))
    parser.add_argument("--project-id", default="agentic-mesh-dev")
    parser.add_argument("--project-config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")
    subparsers.add_parser("status-json")
    sweep_parser = subparsers.add_parser("sweep-project")
    sweep_parser.add_argument("--stale-after-seconds", type=int, default=3600)
    approval_parser = subparsers.add_parser("record-approval-response")
    approval_parser.add_argument("--approval-id", required=True)
    approval_parser.add_argument("--status", required=True, choices=["approved", "rejected", "changes_requested"])
    approval_parser.add_argument("--response", required=True)
    approval_parser.add_argument("--responder-ref", default="sponsor")
    demo_parser = subparsers.add_parser("demo-slice")
    demo_parser.add_argument("--document-library-root", type=Path)

    dogfood_parser = subparsers.add_parser("local-e2e-dogfood")
    dogfood_parser.add_argument("--document-library-root", type=Path, required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8080)
    serve_parser.add_argument("--document-library-root", type=Path)

    run_agent_parser = subparsers.add_parser("run-agent-once")
    run_agent_parser.add_argument("--role-id", required=True)
    run_agent_parser.add_argument("--instance-id", default="1")
    run_agent_parser.add_argument("--agent-config-dir", type=Path, required=True)
    run_agent_parser.add_argument("--runtime-state-dir", type=Path, required=True)
    run_agent_parser.add_argument("--max-messages", type=int, default=1)
    run_agent_parser.add_argument("--worker", choices=["echo", "safe-output-subprocess", "codex-cli"])
    run_agent_parser.add_argument("--worker-command-json")
    run_agent_parser.add_argument("--worker-timeout-seconds", type=int)
    run_agent_parser.add_argument("--worker-model")
    run_agent_parser.add_argument("--worker-reasoning-effort")
    run_agent_parser.add_argument("--worker-sandbox-mode")

    tool_parser = subparsers.add_parser("tool-call")
    tool_parser.add_argument("--role-instance-id", required=True)
    tool_parser.add_argument("--tool-name", required=True)
    tool_parser.add_argument("--payload-json", required=True)
    tool_parser.add_argument("--document-library-root", type=Path)
    tool_parser.add_argument("--terminal", action="store_true")

    subparsers.add_parser("run-tool-mcp-stdio")

    topology_parser = subparsers.add_parser("validate-topology")
    topology_parser.add_argument("--source-repo", type=Path, required=True)
    topology_parser.add_argument("--deployed-runtime", type=Path, required=True)
    topology_parser.add_argument("--runtime-state", type=Path, required=True)
    topology_parser.add_argument("--organisation-config-repo", type=Path, required=True)
    topology_parser.add_argument("--project-config-repo", type=Path, required=True)
    topology_parser.add_argument("--document-library-root", type=Path, required=True)
    topology_parser.add_argument("--local-dev-override", action="store_true")

    args = parser.parse_args(argv)
    configure_observability(TelemetrySettings.from_env(service_name=f"agentic-mesh-v3.{args.command}"))
    if args.command == "init-db":
        db = V3Database(args.db)
        try:
            db.migrate()
        finally:
            db.close()
        print(json.dumps({"database": str(args.db), "status": "initialized"}))
        return 0
    if args.command == "status-json":
        db = V3Database(args.db)
        try:
            db.migrate()
            snapshot = db.status_snapshot(project_id=args.project_id)
            print(
                json.dumps(
                    {
                        "project_id": snapshot.project_id,
                        "backlog": [item.__dict__ for item in snapshot.backlog],
                        "work_items": [item.__dict__ for item in snapshot.work_items],
                        "agents": [item.__dict__ for item in snapshot.agents],
                        "recent_completions": [item.__dict__ for item in snapshot.recent_completions],
                    },
                    indent=2,
                )
            )
        finally:
            db.close()
        return 0
    if args.command == "sweep-project":
        db = V3Database(args.db)
        try:
            db.migrate()
            findings = ProjectSweepService(db).sweep(stale_after_seconds=args.stale_after_seconds)
            print(json.dumps({"findings": [finding.to_dict() for finding in findings]}, indent=2))
        finally:
            db.close()
        return 0
    if args.command == "record-approval-response":
        db = V3Database(args.db)
        try:
            db.migrate()
            db.record_approval_response(
                approval_id=args.approval_id,
                status=args.status,
                response=args.response,
                responder_ref=args.responder_ref,
            )
            print(
                json.dumps(
                    {
                        "approval_id": args.approval_id,
                        "status": args.status,
                        "responder_ref": args.responder_ref,
                    },
                    indent=2,
                )
            )
        finally:
            db.close()
        return 0
    if args.command == "serve":
        serve(
            db_path=args.db,
            project_id=args.project_id,
            host=args.host,
            port=args.port,
            document_library=_document_library_adapter(args),
            teams_activity_router=_teams_activity_router(args),
        )
        return 0
    if args.command == "run-agent-once":
        results = _run_agent_once(args)
        print(json.dumps({"results": [result.__dict__ for result in results]}, indent=2))
        return 0
    if args.command == "tool-call":
        db = V3Database(args.db)
        try:
            db.migrate()
            adapter = _document_library_adapter(args)
            result = V3ToolService(db, adapter).call(
                role_instance_id=args.role_instance_id,
                tool_name=args.tool_name,
                payload=json.loads(args.payload_json),
                terminal=args.terminal,
            )
            print(json.dumps(result.__dict__, indent=2))
        finally:
            db.close()
        return 0
    if args.command == "run-tool-mcp-stdio":
        db = V3Database(args.db)
        try:
            db.migrate()
            run_v3_mcp_stdio(db)
        finally:
            db.close()
        return 0
    if args.command == "demo-slice":
        db = V3Database(args.db)
        try:
            db.migrate()
            adapter = _document_library_adapter(args)
            run_demo_slice(db, project_id=args.project_id, document_library=adapter)
        finally:
            db.close()
        print(json.dumps({"database": str(args.db), "status": "demo_slice_complete"}))
        return 0
    if args.command == "local-e2e-dogfood":
        db = V3Database(args.db)
        try:
            db.migrate()
            work_item_id = run_local_e2e_dogfood_slice(
                db=db,
                project_id=args.project_id,
                document_library=_document_library_adapter(args, required=True),
            )
        finally:
            db.close()
        print(
            json.dumps(
                {
                    "database": str(args.db),
                    "status": "local_e2e_dogfood_complete",
                    "work_item_id": work_item_id,
                }
            )
        )
        return 0
    if args.command == "validate-topology":
        topology = V3Topology(
            source_repo=args.source_repo,
            deployed_runtime=args.deployed_runtime,
            runtime_state=args.runtime_state,
            organisation_config_repo=args.organisation_config_repo,
            project_config_repo=args.project_config_repo,
            document_library_root=args.document_library_root,
            local_dev_override=args.local_dev_override,
        )
        errors = validate_topology(topology)
        print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
        return 1 if errors else 0
    raise AssertionError(f"unhandled command: {args.command}")


def _document_library_adapter(args: argparse.Namespace, *, required: bool = False) -> DocumentLibraryAdapter | None:
    explicit_root = getattr(args, "document_library_root", None)
    if explicit_root is not None:
        return LocalDocumentLibraryAdapter(explicit_root)
    project_config = getattr(args, "project_config", None)
    if project_config is not None:
        config = load_project_config(project_config).document_library
        return build_document_library_adapter(
            config,
            access_token=os.environ.get("AGENTIC_MESH_ONEDRIVE_TOKEN"),
        )
    if required:
        raise ValueError("--document-library-root or --project-config is required")
    return None


def _teams_activity_router(args: argparse.Namespace) -> TeamsActivityRouter | None:
    project_config_path = getattr(args, "project_config", None)
    if project_config_path is None:
        return None
    config = load_project_config(project_config_path)
    broker = build_broker_adapter(adapter=config.broker.adapter, servers=config.broker.servers)
    role_ids = tuple(role.role_id for role in config.roles)
    _ensure_agent_stream(broker, stream=config.broker.stream, role_ids=role_ids)
    return TeamsActivityRouter(
        LocalTeamsBridge(broker, stream=config.broker.stream, role_ids=role_ids),
        role_identities=teams_role_identities_from_project_config(config),
    )


def _run_agent_once(args: argparse.Namespace):
    project_config_path = getattr(args, "project_config", None)
    if project_config_path is None:
        raise ValueError("--project-config is required for run-agent-once")
    config = load_project_config(project_config_path)
    broker = build_broker_adapter(adapter=config.broker.adapter, servers=config.broker.servers)
    _ensure_agent_stream(broker, stream=config.broker.stream, role_ids=tuple(role.role_id for role in config.roles))
    service_config = build_role_instance_config(
        project_id=config.project_id,
        role_id=args.role_id,
        instance_id=str(args.instance_id),
        agent_config_dir=args.agent_config_dir,
        runtime_state_dir=args.runtime_state_dir,
        inbox_stream=config.broker.stream,
    )
    service = RoleAgentService(
        config=service_config,
        broker=broker,
        worker=_worker_from_args(args, project_config=config),
        memory=build_role_memory(service_config),
    )
    return service.run_until_idle(max_messages=args.max_messages)


def _ensure_agent_stream(broker: BrokerAdapter, *, stream: str, role_ids: tuple[str, ...]) -> None:
    subjects = ["project.context"]
    for role_id in role_ids:
        subjects.append(f"agent.{role_id}")
        subjects.append(f"agent.{role_id}.relevance")
    broker.ensure_stream(stream, subjects)


def _worker_from_args(args: argparse.Namespace, *, project_config: V3ProjectConfig | None = None):
    if args.worker == "echo":
        return EchoWorker()
    if args.worker is None:
        configured_worker = _configured_worker_for_role(project_config, role_id=args.role_id)
        if configured_worker is None or configured_worker.adapter is None:
            return EchoWorker()
        return build_worker_adapter(
            adapter=configured_worker.adapter,
            command=configured_worker.command or None,
            timeout_seconds=configured_worker.timeout_seconds,
            model=configured_worker.model,
            reasoning_effort=configured_worker.reasoning_effort,
            sandbox_mode=configured_worker.sandbox_mode,
        )
    command_raw = args.worker_command_json
    command = tuple(json.loads(command_raw)) if command_raw else None
    return build_worker_adapter(
        adapter=args.worker,
        command=command,
        timeout_seconds=args.worker_timeout_seconds,
        model=args.worker_model,
        reasoning_effort=args.worker_reasoning_effort,
        sandbox_mode=args.worker_sandbox_mode,
    )


def _configured_worker_for_role(project_config: V3ProjectConfig | None, *, role_id: str) -> V3WorkerConfig | None:
    if project_config is None:
        return None
    for role in project_config.roles:
        if role.role_id == role_id:
            return role.worker
    return None


if __name__ == "__main__":
    raise SystemExit(main())
