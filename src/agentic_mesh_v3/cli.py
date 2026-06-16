from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from agentic_mesh_v3.agent import AgentMemory
from agentic_mesh_v3.agent import DatabaseConversationContext
from agentic_mesh_v3.agent import DatabaseAgentStatusReporter
from agentic_mesh_v3.agent import DatabaseTerminalToolCallAudit
from agentic_mesh_v3.agent import DatabaseWorkItemGovernanceContextProvider
from agentic_mesh_v3.agent import EchoWorker
from agentic_mesh_v3.agent import RoleAgentService
from agentic_mesh_v3.agent import AgentStatusReporter
from agentic_mesh_v3.agent import TerminalToolCallAudit
from agentic_mesh_v3.agent import WorkItemGovernanceContextProvider
from agentic_mesh_v3.broker import build_broker_adapter
from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.broker import BrokerMessage
from agentic_mesh_v3.config_materializer import build_role_instance_config
from agentic_mesh_v3.config_materializer import materialize_project_agent_configs
from agentic_mesh_v3.compose import render_role_services_compose
from agentic_mesh_v3.connectors import GraphTeamsBridge
from agentic_mesh_v3.connectors import LocalTeamsBridge
from agentic_mesh_v3.connectors import StakeholderBridge
from agentic_mesh_v3.connectors import UrlLibGraphTeamsTransport
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.demo import run_demo_slice
from agentic_mesh_v3.deployment import DeploymentTarget
from agentic_mesh_v3.deployment import deployment_targets_from_project_config
from agentic_mesh_v3.dogfood import DogfoodSponsorContact
from agentic_mesh_v3.dogfood import run_local_e2e_dogfood_slice
from agentic_mesh_v3.dogfood_audit import audit_v3_dogfood_completion
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.documents import build_document_library_adapter
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.governance import RaciMatrix
from agentic_mesh_v3.governance import load_raci_matrix_from_flow
from agentic_mesh_v3.lifecycle import ComposeLifecycleConfig
from agentic_mesh_v3.lifecycle import ComposeLifecycleExecutor
from agentic_mesh_v3.lifecycle import HibernationPolicy
from agentic_mesh_v3.lifecycle import plan_lifecycle_actions
from agentic_mesh_v3.memory import DatabaseRoleMemory
from agentic_mesh_v3.observability import TelemetrySettings
from agentic_mesh_v3.observability import configure_observability
from agentic_mesh_v3.project_config import load_project_config
from agentic_mesh_v3.project_config import resolve_project_flow_config_path
from agentic_mesh_v3.project_config import V3ProjectConfig
from agentic_mesh_v3.project_config import V3WorkerConfig
from agentic_mesh_v3.server import serve
from agentic_mesh_v3.sweeps import ProjectSweepService
from agentic_mesh_v3.teams_ingress import TeamsActivityRouter
from agentic_mesh_v3.teams_ingress import DatabaseApprovalResponseRecorder
from agentic_mesh_v3.teams_ingress import DatabaseConversationRecorder
from agentic_mesh_v3.teams_ingress import DatabaseStakeholderQuestionResponseRecorder
from agentic_mesh_v3.teams_ingress import teams_role_identities_from_project_config
from agentic_mesh_v3.tool_mcp import run_v3_mcp_stdio
from agentic_mesh_v3.tool_catalog import tool_catalog_for_role
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
    sweep_parser.add_argument("--publish-to-project-manager", action="store_true")
    sweep_parser.add_argument("--project-manager-role-id", default="project-manager")
    broker_inspect_parser = subparsers.add_parser("broker-inspect")
    broker_inspect_parser.add_argument("--stream")
    broker_inspect_parser.add_argument("--consumer")
    broker_inspect_parser.add_argument("--limit", type=int, default=20)
    lifecycle_plan_parser = subparsers.add_parser("lifecycle-plan")
    lifecycle_plan_parser.add_argument("--idle-after-seconds", type=int, default=1800)
    lifecycle_plan_parser.add_argument("--min-warm-instances-per-role", type=int, default=1)
    lifecycle_apply_parser = subparsers.add_parser("lifecycle-apply")
    lifecycle_apply_parser.add_argument("--idle-after-seconds", type=int, default=1800)
    lifecycle_apply_parser.add_argument("--min-warm-instances-per-role", type=int, default=1)
    lifecycle_apply_parser.add_argument("--compose-file", type=Path, action="append", required=True)
    lifecycle_apply_parser.add_argument("--working-directory", type=Path)
    lifecycle_apply_parser.add_argument("--timeout-seconds", type=int, default=300)
    lifecycle_apply_parser.add_argument("--execute", action="store_true")
    supervisor_tick_parser = subparsers.add_parser("run-project-supervisor-tick")
    supervisor_tick_parser.add_argument("--stale-after-seconds", type=int, default=3600)
    supervisor_tick_parser.add_argument("--publish-sweep-to-project-manager", action="store_true")
    supervisor_tick_parser.add_argument("--project-manager-role-id", default="project-manager")
    supervisor_tick_parser.add_argument("--idle-after-seconds", type=int, default=1800)
    supervisor_tick_parser.add_argument("--min-warm-instances-per-role", type=int, default=1)
    supervisor_tick_parser.add_argument("--compose-file", type=Path, action="append")
    supervisor_tick_parser.add_argument("--working-directory", type=Path)
    supervisor_tick_parser.add_argument("--timeout-seconds", type=int, default=300)
    supervisor_tick_parser.add_argument("--execute", action="store_true")
    supervisor_loop_parser = subparsers.add_parser("run-project-supervisor-loop")
    supervisor_loop_parser.add_argument("--cycles", type=int, required=True)
    supervisor_loop_parser.add_argument("--poll-seconds", type=float, default=5.0)
    supervisor_loop_parser.add_argument("--stale-after-seconds", type=int, default=3600)
    supervisor_loop_parser.add_argument("--publish-sweep-to-project-manager", action="store_true")
    supervisor_loop_parser.add_argument("--project-manager-role-id", default="project-manager")
    supervisor_loop_parser.add_argument("--idle-after-seconds", type=int, default=1800)
    supervisor_loop_parser.add_argument("--min-warm-instances-per-role", type=int, default=1)
    supervisor_loop_parser.add_argument("--compose-file", type=Path, action="append")
    supervisor_loop_parser.add_argument("--working-directory", type=Path)
    supervisor_loop_parser.add_argument("--timeout-seconds", type=int, default=300)
    supervisor_loop_parser.add_argument("--execute", action="store_true")
    supervisor_service_parser = subparsers.add_parser("run-project-supervisor-service")
    supervisor_service_mode = supervisor_service_parser.add_mutually_exclusive_group(required=True)
    supervisor_service_mode.add_argument("--continuous", action="store_true")
    supervisor_service_mode.add_argument("--cycles", type=int)
    supervisor_service_parser.add_argument("--poll-seconds", type=float, default=5.0)
    supervisor_service_parser.add_argument("--stale-after-seconds", type=int, default=3600)
    supervisor_service_parser.add_argument("--publish-sweep-to-project-manager", action="store_true")
    supervisor_service_parser.add_argument("--project-manager-role-id", default="project-manager")
    supervisor_service_parser.add_argument("--idle-after-seconds", type=int, default=1800)
    supervisor_service_parser.add_argument("--min-warm-instances-per-role", type=int, default=1)
    supervisor_service_parser.add_argument("--compose-file", type=Path, action="append")
    supervisor_service_parser.add_argument("--working-directory", type=Path)
    supervisor_service_parser.add_argument("--timeout-seconds", type=int, default=300)
    supervisor_service_parser.add_argument("--execute", action="store_true")
    approval_parser = subparsers.add_parser("record-approval-response")
    approval_parser.add_argument("--approval-id", required=True)
    approval_parser.add_argument("--status", required=True, choices=["approved", "rejected", "changes_requested"])
    approval_parser.add_argument("--response", required=True)
    approval_parser.add_argument("--responder-ref", default="sponsor")
    demo_parser = subparsers.add_parser("demo-slice")
    demo_parser.add_argument("--document-library-root", type=Path)

    dogfood_parser = subparsers.add_parser("local-e2e-dogfood")
    dogfood_parser.add_argument("--document-library-root", type=Path)
    dogfood_parser.add_argument("--deployment-target-id")
    dogfood_audit_parser = subparsers.add_parser("audit-dogfood")
    dogfood_audit_parser.add_argument("--document-library-root", type=Path)
    dogfood_audit_parser.add_argument("--work-item-id", default="work-v3-local-e2e")
    dogfood_audit_parser.add_argument("--require-onedrive-artifacts", action="store_true")

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
    run_agent_parser.add_argument("--max-delivery-attempts", type=int, default=3)

    run_service_parser = subparsers.add_parser("run-agent-service")
    run_service_parser.add_argument("--role-id", required=True)
    run_service_parser.add_argument("--instance-id", default="1")
    run_service_parser.add_argument("--agent-config-dir", type=Path, required=True)
    run_service_parser.add_argument("--runtime-state-dir", type=Path, required=True)
    run_service_parser.add_argument("--max-messages", type=int, default=1)
    run_service_parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    run_service_parser.add_argument("--idle-exit-seconds", type=float)
    run_service_parser.add_argument("--max-ticks", type=int)
    run_service_parser.add_argument("--worker", choices=["echo", "safe-output-subprocess", "codex-cli"])
    run_service_parser.add_argument("--worker-command-json")
    run_service_parser.add_argument("--worker-timeout-seconds", type=int)
    run_service_parser.add_argument("--worker-model")
    run_service_parser.add_argument("--worker-reasoning-effort")
    run_service_parser.add_argument("--worker-sandbox-mode")
    run_service_parser.add_argument("--max-delivery-attempts", type=int, default=3)

    materialize_parser = subparsers.add_parser("materialize-agent-configs")
    materialize_parser.add_argument("--image", required=True)
    materialize_parser.add_argument("--source-repo", type=Path, required=True)
    materialize_parser.add_argument("--deployed-runtime", type=Path, required=True)
    materialize_parser.add_argument("--organisation-config-repo", type=Path, required=True)
    materialize_parser.add_argument("--project-config-repo", type=Path, required=True)
    materialize_parser.add_argument("--agent-config-root", type=Path, required=True)
    materialize_parser.add_argument("--runtime-state-dir", type=Path, required=True)
    materialize_parser.add_argument("--document-library-root", type=Path, required=True)
    materialize_parser.add_argument("--role-templates-dir", type=Path, required=True)
    materialize_parser.add_argument("--system-instructions-file", type=Path)
    materialize_parser.add_argument("--organisation-instructions-file", type=Path)
    materialize_parser.add_argument("--tool-instructions-file", type=Path)
    materialize_parser.add_argument("--flow-config", type=Path)
    materialize_parser.add_argument("--compose-output", type=Path)
    materialize_parser.add_argument("--compose-network", default="agentic-mesh")
    materialize_parser.add_argument("--compose-include-nats", action="store_true")
    materialize_parser.add_argument("--compose-nats-service-name", default="nats")
    materialize_parser.add_argument("--compose-nats-image", default="nats:2.10-alpine")
    materialize_parser.add_argument(
        "--compose-nats-port",
        action="append",
        dest="compose_nats_ports",
        default=None,
        help="NATS port mapping to include in generated Compose; repeatable.",
    )
    materialize_parser.add_argument("--compose-include-supervisor", action="store_true")
    materialize_parser.add_argument("--compose-supervisor-service-name", default="v3-supervisor")
    materialize_parser.add_argument("--compose-supervisor-image")
    materialize_parser.add_argument("--compose-supervisor-poll-seconds", type=float, default=30.0)
    materialize_parser.add_argument("--compose-supervisor-compose-file")
    materialize_parser.add_argument("--compose-supervisor-execute", action="store_true")
    materialize_parser.add_argument("--compose-supervisor-mount-docker-socket", action="store_true")
    materialize_parser.add_argument("--local-dev-override", action="store_true")

    tool_parser = subparsers.add_parser("tool-call")
    tool_parser.add_argument("--role-instance-id", required=True)
    tool_parser.add_argument("--tool-name", required=True)
    tool_parser.add_argument("--payload-json", required=True)
    tool_parser.add_argument("--document-library-root", type=Path)
    tool_parser.add_argument("--terminal", action="store_true")

    catalog_parser = subparsers.add_parser("tool-catalog")
    catalog_parser.add_argument("--role-id", required=True)

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
            service = ProjectSweepService(db)
            findings = service.sweep(stale_after_seconds=args.stale_after_seconds)
            published_message_ids: tuple[str, ...] = ()
            if args.publish_to_project_manager:
                if args.project_config is None:
                    raise ValueError("--project-config is required when publishing sweep findings")
                project_config = load_project_config(args.project_config)
                broker = build_broker_adapter(
                    adapter=project_config.broker.adapter,
                    servers=project_config.broker.servers,
                )
                published_message_ids = service.publish_findings(
                    broker,
                    stream=project_config.broker.stream,
                    findings=findings,
                    project_manager_role_id=args.project_manager_role_id,
                )
            print(
                json.dumps(
                    {
                        "findings": [finding.to_dict() for finding in findings],
                        "published_message_ids": list(published_message_ids),
                    },
                    indent=2,
                )
            )
        finally:
            db.close()
        return 0
    if args.command == "broker-inspect":
        if args.project_config is None:
            raise ValueError("--project-config is required for broker-inspect")
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        project_config = load_project_config(args.project_config)
        broker = build_broker_adapter(
            adapter=project_config.broker.adapter,
            servers=project_config.broker.servers,
        )
        stream = args.stream or project_config.broker.stream
        print(
            json.dumps(
                _broker_inspection_payload(
                    broker,
                    stream=stream,
                    consumer=args.consumer,
                    limit=args.limit,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "lifecycle-plan":
        if args.idle_after_seconds < 1:
            raise ValueError("--idle-after-seconds must be positive")
        if args.min_warm_instances_per_role < 0:
            raise ValueError("--min-warm-instances-per-role must be non-negative")
        db = V3Database(args.db)
        try:
            db.migrate()
            snapshot = db.status_snapshot(project_id=args.project_id)
            decisions = plan_lifecycle_actions(
                snapshot.agents,
                policy=HibernationPolicy(
                    idle_after_seconds=args.idle_after_seconds,
                    min_warm_instances_per_role=args.min_warm_instances_per_role,
                ),
            )
            print(
                json.dumps(
                    {
                        "project_id": snapshot.project_id,
                        "policy": {
                            "idle_after_seconds": args.idle_after_seconds,
                            "min_warm_instances_per_role": args.min_warm_instances_per_role,
                        },
                        "decisions": [decision.__dict__ for decision in decisions],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        finally:
            db.close()
        return 0
    if args.command == "lifecycle-apply":
        if args.idle_after_seconds < 1:
            raise ValueError("--idle-after-seconds must be positive")
        if args.min_warm_instances_per_role < 0:
            raise ValueError("--min-warm-instances-per-role must be non-negative")
        db = V3Database(args.db)
        try:
            db.migrate()
            snapshot = db.status_snapshot(project_id=args.project_id)
            decisions = plan_lifecycle_actions(
                snapshot.agents,
                policy=HibernationPolicy(
                    idle_after_seconds=args.idle_after_seconds,
                    min_warm_instances_per_role=args.min_warm_instances_per_role,
                ),
            )
            results = ComposeLifecycleExecutor(
                ComposeLifecycleConfig(
                    compose_files=tuple(args.compose_file),
                    working_directory=args.working_directory,
                    timeout_seconds=args.timeout_seconds,
                )
            ).apply(decisions, execute=args.execute)
            for result in results:
                db.record_agent_lifecycle_result(
                    role_instance_id=result.decision.role_instance_id,
                    action=result.decision.action,
                    reason=result.decision.reason,
                    service_name=result.service_name,
                    command=result.command,
                    working_directory=str(result.working_directory) if result.working_directory else None,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    executed=result.executed,
                )
            print(
                json.dumps(
                    {
                        "project_id": snapshot.project_id,
                        "execute": args.execute,
                        "decisions": [decision.__dict__ for decision in decisions],
                        "results": [_lifecycle_result_dict(result) for result in results],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        finally:
            db.close()
        return 0
    if args.command == "run-project-supervisor-tick":
        print(json.dumps(_run_project_supervisor_tick(args), indent=2, sort_keys=True))
        return 0
    if args.command == "run-project-supervisor-loop":
        print(json.dumps(_run_project_supervisor_loop(args), indent=2, sort_keys=True))
        return 0
    if args.command == "run-project-supervisor-service":
        print(json.dumps(_run_project_supervisor_service(args), indent=2, sort_keys=True))
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
            approval = db.approval_detail(args.approval_id)
            published_message_id = _publish_approval_response(args, approval=approval)
            print(
                json.dumps(
                    {
                        "approval_id": args.approval_id,
                        "status": args.status,
                        "responder_ref": args.responder_ref,
                        "published_message_id": published_message_id,
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
    if args.command == "run-agent-service":
        results = _run_agent_service(args)
        print(json.dumps({"results": [result.__dict__ for result in results]}, indent=2))
        return 0
    if args.command == "materialize-agent-configs":
        result = _materialize_agent_configs(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "tool-call":
        db = V3Database(args.db)
        try:
            db.migrate()
            adapter = _document_library_adapter(args)
            broker, broker_stream = _tool_broker(args)
            result = V3ToolService(
                db,
                adapter,
                deployment_targets=_deployment_targets(args),
                stakeholder_bridge=_stakeholder_bridge(args, broker=broker),
                broker=broker,
                broker_stream=broker_stream,
            ).call(
                role_instance_id=args.role_instance_id,
                tool_name=args.tool_name,
                payload=json.loads(args.payload_json),
                terminal=args.terminal,
            )
            print(json.dumps(result.__dict__, indent=2))
        finally:
            db.close()
        return 0
    if args.command == "tool-catalog":
        print(
            json.dumps(
                {
                    "role_id": args.role_id,
                    "tools": [entry.to_dict() for entry in tool_catalog_for_role(args.role_id)],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "run-tool-mcp-stdio":
        db = V3Database(args.db)
        try:
            db.migrate()
            broker, broker_stream = _tool_broker(args)
            service = V3ToolService(
                db,
                _document_library_adapter(args),
                deployment_targets=_deployment_targets(args),
                stakeholder_bridge=_stakeholder_bridge(args, broker=broker),
                broker=broker,
                broker_stream=broker_stream,
            )
            run_v3_mcp_stdio(db, service=service)
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
            deployment_targets = _deployment_targets(args)
            broker, broker_stream = _tool_broker(args)
            work_item_id = run_local_e2e_dogfood_slice(
                db=db,
                project_id=args.project_id,
                document_library=_document_library_adapter(args, required=True),
                deployment_targets=deployment_targets or None,
                deployment_target_id=_dogfood_deployment_target_id(args, deployment_targets),
                broker=broker,
                broker_stream=broker_stream or "agent-inbox",
                stakeholder_bridge=_stakeholder_bridge(args, broker=broker),
                sponsor_contact=_dogfood_sponsor_contact(args),
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
    if args.command == "audit-dogfood":
        db = V3Database(args.db)
        try:
            db.migrate()
            result = audit_v3_dogfood_completion(
                db=db,
                document_library=_document_library_adapter(args, required=True),
                work_item_id=args.work_item_id,
                require_onedrive_artifacts=(
                    args.require_onedrive_artifacts or _project_uses_onedrive_document_library(args)
                ),
            )
        finally:
            db.close()
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.passed else 1
    if args.command == "validate-topology":
        project_config = load_project_config(args.project_config) if args.project_config is not None else None
        topology = V3Topology(
            source_repo=args.source_repo,
            deployed_runtime=args.deployed_runtime,
            runtime_state=args.runtime_state,
            organisation_config_repo=args.organisation_config_repo,
            project_config_repo=args.project_config_repo,
            document_library_root=args.document_library_root,
            target_repositories=_target_repository_paths(project_config),
            local_dev_override=args.local_dev_override,
        )
        errors = validate_topology(topology)
        print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
        return 1 if errors else 0
    raise AssertionError(f"unhandled command: {args.command}")


def _broker_message_dict(message: BrokerMessage) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "subject": message.subject,
        "payload": message.payload,
        "created_at": message.created_at,
        "delivery_count": message.delivery_count,
    }


def _lifecycle_result_dict(result) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "action": result.decision.action,
        "role_instance_id": result.decision.role_instance_id,
        "reason": result.decision.reason,
        "service_name": result.service_name,
        "command": list(result.command),
        "working_directory": str(result.working_directory) if result.working_directory is not None else None,
        "executed": result.executed,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _run_project_supervisor_tick(args: argparse.Namespace) -> dict[str, object]:
    if args.project_config is None:
        raise ValueError("--project-config is required for run-project-supervisor-tick")
    _validate_supervisor_args(args)
    project_config = load_project_config(args.project_config)
    db = V3Database(args.db)
    try:
        db.migrate()
        lifecycle_payload = _run_supervisor_lifecycle(db, args)
        sweep_payload = _run_supervisor_sweep(db, args, project_config=project_config)
        payload = {
            "project_id": args.project_id,
            "lifecycle": lifecycle_payload,
            "sweep": sweep_payload,
        }
        with db.connection:
            db.record_event("project_supervisor.tick", "project", args.project_id, payload)
        return payload
    finally:
        db.close()


def _run_project_supervisor_loop(args: argparse.Namespace) -> dict[str, object]:
    if args.cycles < 1:
        raise ValueError("--cycles must be positive")
    if args.poll_seconds < 0:
        raise ValueError("--poll-seconds must be non-negative")
    cycles: list[dict[str, object]] = []
    for index in range(args.cycles):
        cycles.append(_run_project_supervisor_tick(args))
        if index < args.cycles - 1:
            time.sleep(args.poll_seconds)
    return {
        "project_id": args.project_id,
        "cycles": cycles,
        "cycle_count": len(cycles),
        "lifecycle_result_count": sum(
            int(cycle["lifecycle"]["result_count"]) for cycle in cycles  # type: ignore[index]
        ),
        "sweep_finding_count": sum(
            int(cycle["sweep"]["finding_count"]) for cycle in cycles  # type: ignore[index]
        ),
        "published_sweep_message_count": sum(
            int(cycle["sweep"]["published_message_count"]) for cycle in cycles  # type: ignore[index]
        ),
    }


def _run_project_supervisor_service(args: argparse.Namespace) -> dict[str, object]:
    if args.poll_seconds < 0:
        raise ValueError("--poll-seconds must be non-negative")
    if args.cycles is not None:
        return {
            **_run_project_supervisor_loop(args),
            "mode": "bounded",
            "interrupted": False,
        }
    cycles: list[dict[str, object]] = []
    interrupted = False
    try:
        while True:
            cycles.append(_run_project_supervisor_tick(args))
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        interrupted = True
    return {
        "project_id": args.project_id,
        "mode": "continuous",
        "interrupted": interrupted,
        "cycles": cycles,
        "cycle_count": len(cycles),
        "lifecycle_result_count": sum(
            int(cycle["lifecycle"]["result_count"]) for cycle in cycles  # type: ignore[index]
        ),
        "sweep_finding_count": sum(
            int(cycle["sweep"]["finding_count"]) for cycle in cycles  # type: ignore[index]
        ),
        "published_sweep_message_count": sum(
            int(cycle["sweep"]["published_message_count"]) for cycle in cycles  # type: ignore[index]
        ),
    }


def _validate_supervisor_args(args: argparse.Namespace) -> None:
    if args.stale_after_seconds < 1:
        raise ValueError("--stale-after-seconds must be positive")
    if args.idle_after_seconds < 1:
        raise ValueError("--idle-after-seconds must be positive")
    if args.min_warm_instances_per_role < 0:
        raise ValueError("--min-warm-instances-per-role must be non-negative")
    if args.timeout_seconds < 1:
        raise ValueError("--timeout-seconds must be positive")


def _run_supervisor_lifecycle(db: V3Database, args: argparse.Namespace) -> dict[str, object]:
    snapshot = db.status_snapshot(project_id=args.project_id)
    decisions = plan_lifecycle_actions(
        snapshot.agents,
        policy=HibernationPolicy(
            idle_after_seconds=args.idle_after_seconds,
            min_warm_instances_per_role=args.min_warm_instances_per_role,
        ),
    )
    results = ()
    if args.compose_file:
        results = ComposeLifecycleExecutor(
            ComposeLifecycleConfig(
                compose_files=tuple(args.compose_file),
                working_directory=args.working_directory,
                timeout_seconds=args.timeout_seconds,
            )
        ).apply(decisions, execute=args.execute)
        for result in results:
            db.record_agent_lifecycle_result(
                role_instance_id=result.decision.role_instance_id,
                action=result.decision.action,
                reason=result.decision.reason,
                service_name=result.service_name,
                command=result.command,
                working_directory=str(result.working_directory) if result.working_directory else None,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                executed=result.executed,
            )
    return {
        "execute": args.execute,
        "compose_configured": bool(args.compose_file),
        "decisions": [decision.__dict__ for decision in decisions],
        "results": [_lifecycle_result_dict(result) for result in results],
        "decision_count": len(decisions),
        "result_count": len(results),
    }


def _run_supervisor_sweep(
    db: V3Database,
    args: argparse.Namespace,
    *,
    project_config: V3ProjectConfig,
) -> dict[str, object]:
    service = ProjectSweepService(db)
    findings = service.sweep(stale_after_seconds=args.stale_after_seconds)
    published_message_ids: tuple[str, ...] = ()
    if args.publish_sweep_to_project_manager:
        broker = build_broker_adapter(
            adapter=project_config.broker.adapter,
            servers=project_config.broker.servers,
        )
        published_message_ids = service.publish_findings(
            broker,
            stream=project_config.broker.stream,
            findings=findings,
            project_manager_role_id=args.project_manager_role_id,
        )
    return {
        "findings": [finding.to_dict() for finding in findings],
        "published_message_ids": list(published_message_ids),
        "finding_count": len(findings),
        "published_message_count": len(published_message_ids),
    }


def _broker_inspection_payload(
    broker: BrokerAdapter,
    *,
    stream: str,
    consumer: str | None = None,
    limit: int = 20,
) -> dict[str, object]:
    return {
        "stream": stream,
        "consumer": consumer,
        "pending": [_broker_message_dict(message) for message in broker.pending(stream, consumer, limit=limit)],
        "dead_letters": [_broker_message_dict(message) for message in broker.dead_letters(stream, limit=limit)],
    }


def _publish_approval_response(args: argparse.Namespace, *, approval: dict[str, object] | None) -> str | None:
    if approval is None or args.project_config is None:
        return None
    project_config = load_project_config(args.project_config)
    broker = build_broker_adapter(
        adapter=project_config.broker.adapter,
        servers=project_config.broker.servers,
    )
    role_ids = tuple(role.role_id for role in project_config.roles)
    _ensure_agent_stream(broker, stream=project_config.broker.stream, role_ids=role_ids)
    requested_by_role = str(approval["requested_by_role"])
    message = broker.publish(
        project_config.broker.stream,
        f"agent.{requested_by_role}",
        {
            "message_type": "approval.response_recorded",
            "approval_id": str(approval["approval_id"]),
            "work_item_id": str(approval["work_item_id"]),
            "status": str(approval["status"]),
            "response": str(approval.get("response") or ""),
            "responder_ref": args.responder_ref,
        },
    )
    return message.message_id


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


def _project_uses_onedrive_document_library(args: argparse.Namespace) -> bool:
    project_config = getattr(args, "project_config", None)
    if project_config is None:
        return False
    adapter = load_project_config(project_config).document_library.adapter.casefold().replace("_", "-")
    return adapter in {"onedrive", "sharepoint"}


def _deployment_targets(args: argparse.Namespace) -> dict[str, DeploymentTarget]:
    project_config = getattr(args, "project_config", None)
    if project_config is None:
        return {}
    return deployment_targets_from_project_config(load_project_config(project_config))


def _dogfood_deployment_target_id(args: argparse.Namespace, deployment_targets: dict[str, DeploymentTarget]) -> str:
    explicit = getattr(args, "deployment_target_id", None)
    if explicit:
        return str(explicit)
    if deployment_targets:
        return next(iter(deployment_targets))
    return "local-smoke"


def _dogfood_sponsor_contact(args: argparse.Namespace) -> DogfoodSponsorContact | None:
    project_config = getattr(args, "project_config", None)
    if project_config is None:
        return None
    config = load_project_config(project_config)
    if not config.stakeholder_contacts:
        return None
    sponsor = next(
        (contact for contact in config.stakeholder_contacts if contact.contact_id == "sponsor"),
        None,
    )
    if sponsor is None:
        return None
    return DogfoodSponsorContact(
        connector=sponsor.connector,
        target_ref=sponsor.target_ref,
        thread_ref=sponsor.thread_ref,
        responder_ref=sponsor.contact_id,
    )


def _tool_broker(args: argparse.Namespace) -> tuple[BrokerAdapter | None, str | None]:
    project_config = getattr(args, "project_config", None)
    if project_config is None:
        return None, None
    config = load_project_config(project_config)
    broker = build_broker_adapter(adapter=config.broker.adapter, servers=config.broker.servers)
    _ensure_agent_stream(broker, stream=config.broker.stream, role_ids=tuple(role.role_id for role in config.roles))
    return broker, config.broker.stream


def _stakeholder_bridge(
    args: argparse.Namespace,
    *,
    broker: BrokerAdapter | None = None,
) -> StakeholderBridge | None:
    project_config_path = getattr(args, "project_config", None)
    if project_config_path is None:
        return None
    config = load_project_config(project_config_path)
    adapter = (config.teams_connector.adapter or "").casefold().replace("_", "-")
    if adapter in {"", "none"}:
        return None
    if adapter in {"local", "local-teams", "in-memory"}:
        if broker is None:
            broker = build_broker_adapter(adapter=config.broker.adapter, servers=config.broker.servers)
            _ensure_agent_stream(
                broker,
                stream=config.broker.stream,
                role_ids=tuple(role.role_id for role in config.roles),
            )
        return LocalTeamsBridge(
            broker,
            stream=config.broker.stream,
            role_ids=tuple(role.role_id for role in config.roles),
        )
    if adapter in {"graph", "microsoft-graph", "teams-graph", "teams-bot-connector"}:
        access_token = os.environ.get("AGENTIC_MESH_TEAMS_TOKEN")
        if not access_token:
            raise ValueError("AGENTIC_MESH_TEAMS_TOKEN is required for Graph-backed Teams outbound messaging")
        return GraphTeamsBridge(
            transport=UrlLibGraphTeamsTransport(access_token=access_token),
            graph_base_url=config.teams_connector.graph_base_url,
            sender_user_ref=os.environ.get("AGENTIC_MESH_TEAMS_SENDER_USER_ID"),
        )
    raise ValueError(f"unsupported Teams connector adapter: {config.teams_connector.adapter}")


def _teams_activity_router(args: argparse.Namespace) -> TeamsActivityRouter | None:
    project_config_path = getattr(args, "project_config", None)
    if project_config_path is None:
        return None
    config = load_project_config(project_config_path)
    broker = build_broker_adapter(adapter=config.broker.adapter, servers=config.broker.servers)
    role_ids = tuple(role.role_id for role in config.roles)
    _ensure_agent_stream(broker, stream=config.broker.stream, role_ids=role_ids)
    db_path = getattr(args, "db", None)
    return TeamsActivityRouter(
        LocalTeamsBridge(broker, stream=config.broker.stream, role_ids=role_ids),
        role_identities=teams_role_identities_from_project_config(config),
        conversation_recorder=DatabaseConversationRecorder(db_path) if db_path is not None else None,
        approval_response_recorder=(
            DatabaseApprovalResponseRecorder(db_path, broker=broker, stream=config.broker.stream)
            if db_path is not None
            else None
        ),
        question_response_recorder=(
            DatabaseStakeholderQuestionResponseRecorder(db_path, broker=broker, stream=config.broker.stream)
            if db_path is not None
            else None
        ),
    )


def _materialize_agent_configs(args: argparse.Namespace) -> dict[str, object]:
    project_config_path = getattr(args, "project_config", None)
    if project_config_path is None:
        raise ValueError("--project-config is required for materialize-agent-configs")
    config = load_project_config(project_config_path)
    topology = V3Topology(
        source_repo=args.source_repo,
        deployed_runtime=args.deployed_runtime,
        runtime_state=args.runtime_state_dir,
        organisation_config_repo=args.organisation_config_repo,
        project_config_repo=args.project_config_repo,
        document_library_root=args.document_library_root,
        target_repositories=_target_repository_paths(config),
        local_dev_override=args.local_dev_override,
    )
    errors = validate_topology(topology)
    if errors:
        raise ValueError("invalid V3 topology: " + "; ".join(errors))
    materialized = materialize_project_agent_configs(
        project_config=config,
        image=args.image,
        source_repo=args.source_repo,
        organisation_config_repo=args.organisation_config_repo,
        project_config_repo=args.project_config_repo,
        agent_config_root=args.agent_config_root,
        runtime_state_dir=args.runtime_state_dir,
        document_library_root=args.document_library_root,
        role_templates_dir=args.role_templates_dir,
        system_instructions=_read_system_instructions(args.system_instructions_file),
        organisation_instructions=_read_text_or_default(
            args.organisation_instructions_file,
            "No organisation-specific instructions configured.",
        ),
        raci=_raci_matrix(args, project_config=config),
        tool_instructions=_read_tool_instructions(args.tool_instructions_file),
    )
    compose_output = None
    if args.compose_output is not None:
        args.compose_output.parent.mkdir(parents=True, exist_ok=True)
        args.compose_output.write_text(
            render_role_services_compose(
                [item.container_spec for item in materialized],
                network_name=args.compose_network,
                include_nats=bool(args.compose_include_nats),
                nats_service_name=args.compose_nats_service_name,
                nats_image=args.compose_nats_image,
                nats_ports=tuple(args.compose_nats_ports or ("4222:4222", "8222:8222")),
                include_supervisor=bool(args.compose_include_supervisor),
                supervisor_service_name=args.compose_supervisor_service_name,
                supervisor_image=args.compose_supervisor_image,
                supervisor_poll_seconds=args.compose_supervisor_poll_seconds,
                supervisor_compose_file=args.compose_supervisor_compose_file,
                supervisor_execute=bool(args.compose_supervisor_execute),
                supervisor_mount_docker_socket=bool(args.compose_supervisor_mount_docker_socket),
            ),
            encoding="utf-8",
        )
        compose_output = str(args.compose_output)
    return {
        "project_id": config.project_id,
        "role_instances": [item.container_spec.role_instance_id for item in materialized],
        "written_files": [str(path) for item in materialized for path in item.written_files],
        "compose_output": compose_output,
    }


def _target_repository_paths(project_config: V3ProjectConfig | None) -> dict[str, Path]:
    if project_config is None:
        return {}
    return {
        repository.repository_id: repository.path
        for repository in project_config.target_repositories
    }


def _read_text_or_default(path: Path | None, default: str) -> str:
    if path is None:
        return default
    return path.read_text(encoding="utf-8")


def _read_system_instructions(path: Path | None) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")
    return "\n\n".join(
        _read_existing_prompt(path)
        for path in (
            Path("config/prompts/worker/system-security.xml"),
            Path("config/prompts/worker/instructions.xml"),
        )
    ).strip()


def _read_tool_instructions(path: Path | None) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")
    return _read_existing_prompt(Path("config/prompts/worker/safe-outputs.xml"))


def _read_existing_prompt(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"prompt component not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _raci_matrix(args: argparse.Namespace, *, project_config: V3ProjectConfig | None = None) -> RaciMatrix:
    flow_config = getattr(args, "flow_config", None)
    if flow_config is not None:
        return load_raci_matrix_from_flow(flow_config)
    project_config_path = getattr(args, "project_config", None)
    if project_config_path is not None and project_config is not None:
        resolved = resolve_project_flow_config_path(project_config_path, project_config)
        if resolved is not None:
            return load_raci_matrix_from_flow(resolved)
    return DEFAULT_SDLC_RACI


def _run_agent_once(args: argparse.Namespace):
    project_config_path = getattr(args, "project_config", None)
    if project_config_path is None:
        raise ValueError("--project-config is required for run-agent-once")
    if args.max_delivery_attempts < 1:
        raise ValueError("--max-delivery-attempts must be positive")
    config = load_project_config(project_config_path)
    broker = build_broker_adapter(adapter=config.broker.adapter, servers=config.broker.servers)
    _ensure_agent_stream(broker, stream=config.broker.stream, role_ids=tuple(role.role_id for role in config.roles))
    db = V3Database(args.db)
    try:
        db.migrate()
        service = _build_role_agent_service(
            args,
            project_config=config,
            broker=broker,
            memory=DatabaseRoleMemory(db),
            conversation_context=DatabaseConversationContext(db),
            work_item_governance_context=DatabaseWorkItemGovernanceContextProvider(db),
            status_reporter=DatabaseAgentStatusReporter(db),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
        )
        return service.run_until_idle(max_messages=args.max_messages)
    finally:
        db.close()


def _run_agent_service(args: argparse.Namespace):
    project_config_path = getattr(args, "project_config", None)
    if project_config_path is None:
        raise ValueError("--project-config is required for run-agent-service")
    if args.max_ticks is not None and args.max_ticks < 1:
        raise ValueError("--max-ticks must be positive")
    if args.poll_interval_seconds < 0:
        raise ValueError("--poll-interval-seconds must be non-negative")
    if args.idle_exit_seconds is not None and args.idle_exit_seconds < 0:
        raise ValueError("--idle-exit-seconds must be non-negative")
    if args.max_delivery_attempts < 1:
        raise ValueError("--max-delivery-attempts must be positive")
    config = load_project_config(project_config_path)
    broker = build_broker_adapter(adapter=config.broker.adapter, servers=config.broker.servers)
    _ensure_agent_stream(broker, stream=config.broker.stream, role_ids=tuple(role.role_id for role in config.roles))
    db = V3Database(args.db)
    results = []
    try:
        db.migrate()
        service = _build_role_agent_service(
            args,
            project_config=config,
            broker=broker,
            memory=DatabaseRoleMemory(db),
            conversation_context=DatabaseConversationContext(db),
            work_item_governance_context=DatabaseWorkItemGovernanceContextProvider(db),
            status_reporter=DatabaseAgentStatusReporter(db),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
        )
        idle_since: float | None = None
        ticks = 0
        while True:
            tick_results = service.run_until_idle(max_messages=args.max_messages)
            if tick_results:
                results.extend(tick_results)
                idle_since = None
            else:
                now = time.monotonic()
                idle_since = now if idle_since is None else idle_since
                if args.idle_exit_seconds is not None and now - idle_since >= args.idle_exit_seconds:
                    break
            ticks += 1
            if args.max_ticks is not None and ticks >= args.max_ticks:
                break
            time.sleep(args.poll_interval_seconds)
        return tuple(results)
    finally:
        db.close()


def _build_role_agent_service(
    args: argparse.Namespace,
    *,
    project_config: V3ProjectConfig,
    broker: BrokerAdapter,
    memory: AgentMemory,
    conversation_context: DatabaseConversationContext,
    work_item_governance_context: WorkItemGovernanceContextProvider,
    status_reporter: AgentStatusReporter | None = None,
    terminal_tool_call_audit: TerminalToolCallAudit | None = None,
) -> RoleAgentService:
    service_config = build_role_instance_config(
        project_id=project_config.project_id,
        role_id=args.role_id,
        instance_id=str(args.instance_id),
        agent_config_dir=args.agent_config_dir,
        runtime_state_dir=args.runtime_state_dir,
        inbox_stream=project_config.broker.stream,
    )
    kwargs = {}
    if status_reporter is not None:
        kwargs["status_reporter"] = status_reporter
    if terminal_tool_call_audit is not None:
        kwargs["terminal_tool_call_audit"] = terminal_tool_call_audit
    return RoleAgentService(
        config=service_config,
        broker=broker,
        worker=_worker_from_args(args, project_config=project_config),
        memory=memory,
        conversation_context=conversation_context,
        work_item_governance_context=work_item_governance_context,
        max_delivery_attempts=args.max_delivery_attempts,
        **kwargs,
    )


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
