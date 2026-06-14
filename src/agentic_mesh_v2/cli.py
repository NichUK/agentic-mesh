from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from agentic_mesh_v2.container_lifecycle import ComposeRoleLifecycleConfig
from agentic_mesh_v2.container_lifecycle import ContainerLifecycleAction
from agentic_mesh_v2.container_lifecycle import ContainerLifecycleExecutor
from agentic_mesh_v2.container_lifecycle import plan_compose_lifecycle_action
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import BotFrameworkDeliveryClient
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.demo import run_demo_slice
from agentic_mesh_v2.hibernation import HibernationPolicy
from agentic_mesh_v2.hibernation import HibernationService
from agentic_mesh_v2.observability import configure_observability
from agentic_mesh_v2.observability import span
from agentic_mesh_v2.project_config import list_project_role_service_configs
from agentic_mesh_v2.project_config import load_document_library_config
from agentic_mesh_v2.project_config import load_project_id
from agentic_mesh_v2.project_config import load_role_container_lifecycle_config
from agentic_mesh_v2.project_config import load_role_hibernation_config
from agentic_mesh_v2.project_config import load_role_memory_config
from agentic_mesh_v2.project_config import load_role_memory_context
from agentic_mesh_v2.project_config import load_role_worker_config
from agentic_mesh_v2.project_config import load_teams_connector_config
from agentic_mesh_v2.project_install import InstallOptions
from agentic_mesh_v2.project_install import AzureCliGraphClient
from agentic_mesh_v2.project_install import GraphRequestError
from agentic_mesh_v2.project_install import TokenGraphClient
from agentic_mesh_v2.project_install import run_project_install
from agentic_mesh_v2.prompt_builder import build_prompt_assembler_for_project
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.safe_outputs import SafeOutputError
from agentic_mesh_v2.safe_outputs import SafeOutputService
from agentic_mesh_v2.safe_output_mcp import run_safe_output_mcp_stdio
from agentic_mesh_v2.safe_output_transport import parse_safe_output_payload_json
from agentic_mesh_v2.safe_output_transport import record_safe_output_for_run
from agentic_mesh_v2.server import serve
from agentic_mesh_v2.teams_ingress import serve_teams_ingress
from agentic_mesh_v2.teams_sync import sync_project_channel_messages
from agentic_mesh_v2.topology import ProjectRepo
from agentic_mesh_v2.topology import RuntimeTopology
from agentic_mesh_v2.worker_adapters import build_worker_adapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-mesh-v2")
    parser.add_argument(
        "--db",
        default="/mesh/project/state/v2/agentic-mesh-v2.sqlite3",
        help="Path to the v2 SQLite runtime database.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create or migrate the v2 runtime database.")

    serve_parser = subparsers.add_parser("serve", help="Run the v2 status/reporting server.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8080)
    serve_parser.add_argument(
        "--project-file",
        type=Path,
        help="Optional project.yaml used to render copyable project supervisor commands.",
    )

    teams_ingress_parser = subparsers.add_parser(
        "serve-teams-ingress",
        help="Run the v2 Microsoft Teams/Bot Framework ingress endpoint.",
    )
    teams_ingress_parser.add_argument("--host", default="127.0.0.1")
    teams_ingress_parser.add_argument("--port", type=int, default=3978)
    teams_ingress_parser.add_argument("--project-file", type=Path, required=True)
    teams_ingress_parser.add_argument("--path", default="/api/messages")
    teams_ingress_parser.add_argument("--external-base-url")

    teams_sync_parser = subparsers.add_parser(
        "sync-teams-conversations",
        help="Poll configured Teams project channels through Graph and replay missed messages/replies.",
    )
    teams_sync_parser.add_argument("--project-file", type=Path, required=True)
    teams_sync_parser.add_argument(
        "--graph-token-file",
        type=Path,
        help="JSON file containing a Graph access_token. Defaults to Azure CLI Graph auth when omitted.",
    )
    teams_sync_parser.add_argument("--max-messages", type=int, default=25)
    teams_sync_parser.add_argument("--skip-replies", action="store_true")
    teams_sync_mode = teams_sync_parser.add_mutually_exclusive_group()
    teams_sync_mode.add_argument("--cycles", type=int, default=1)
    teams_sync_mode.add_argument("--continuous", action="store_true")
    teams_sync_parser.add_argument("--poll-seconds", type=float, default=30.0)

    subparsers.add_parser("demo-slice", help="Create one complete v2 end-to-end demo slice.")
    subparsers.add_parser("status-json", help="Print the v2 runtime status snapshot as JSON.")

    safe_output_parser = subparsers.add_parser(
        "record-safe-output",
        help="Record one safe-output tool call for an active agent run.",
    )
    safe_output_parser.add_argument("--run-id", required=True)
    safe_output_parser.add_argument("--role-id", required=True)
    safe_output_parser.add_argument("--tool-name", required=True)
    safe_output_parser.add_argument(
        "--payload-json",
        required=True,
        help="Safe-output payload object as JSON.",
    )
    safe_output_parser.add_argument(
        "--terminal",
        action="store_true",
        help="Mark this call terminal in addition to terminal-tool defaults.",
    )
    safe_output_parser.add_argument(
        "--project-file",
        type=Path,
        help="Optional project.yaml used to resolve configured safe-output effects such as document publication.",
    )

    safe_output_mcp_parser = subparsers.add_parser(
        "run-safe-output-mcp-stdio",
        help="Run the v2 safe-output MCP-compatible JSON-RPC stdio server.",
    )
    safe_output_mcp_parser.add_argument(
        "--project-file",
        type=Path,
        help="Optional project.yaml used to resolve configured safe-output effects such as document publication.",
    )

    recover_parser = subparsers.add_parser(
        "recover-stale-assignments",
        help="Recover stale claimed role assignments back to queued state.",
    )
    recover_parser.add_argument(
        "--role-id",
        help="Optional role id to recover. Omit only for operator/global recovery.",
    )
    recover_parser.add_argument("--limit", type=int, default=50)
    recover_parser.add_argument(
        "--reason",
        default="Recovered by operator stale-assignment recovery command.",
        help="Audit reason to record on recovered assignments.",
    )

    tick_parser = subparsers.add_parser(
        "run-role-service-tick",
        help="Run one bounded role-service maintenance tick.",
    )
    tick_parser.add_argument("--role-id", required=True)
    tick_parser.add_argument("--role-instance-id", required=True)
    tick_parser.add_argument(
        "--project-file",
        type=Path,
        help="Optional project.yaml to load roles.<role>.worker when --worker is omitted.",
    )
    tick_parser.add_argument(
        "--worker",
        choices=["safe-output-file", "safe-output-subprocess", "codex-cli"],
        help="Worker adapter to use for claimed assignments.",
    )
    tick_parser.add_argument("--safe-output-file", type=Path)
    tick_parser.add_argument(
        "--worker-command-json",
        help="JSON array command for safe-output-subprocess or codex-cli workers.",
    )
    tick_parser.add_argument("--worker-timeout-seconds", type=int)
    tick_parser.add_argument("--max-recoveries", type=int, default=50)
    tick_parser.add_argument("--max-assignments", type=int, default=10)
    tick_parser.add_argument("--assignment-lease-seconds", type=int, default=300)

    role_loop_parser = subparsers.add_parser(
        "run-role-service-loop",
        help="Run a repeated role-service loop for one configured role instance.",
    )
    role_loop_parser.add_argument("--role-id", required=True)
    role_loop_parser.add_argument("--role-instance-id", required=True)
    role_loop_parser.add_argument(
        "--project-file",
        type=Path,
        help="Optional project.yaml to load roles.<role>.worker when --worker is omitted.",
    )
    role_loop_parser.add_argument(
        "--worker",
        choices=["safe-output-file", "safe-output-subprocess", "codex-cli"],
        help="Worker adapter to use for claimed assignments.",
    )
    role_loop_parser.add_argument("--safe-output-file", type=Path)
    role_loop_parser.add_argument(
        "--worker-command-json",
        help="JSON array command for safe-output-subprocess or codex-cli workers.",
    )
    role_loop_parser.add_argument("--worker-timeout-seconds", type=int)
    role_loop_parser.add_argument("--max-recoveries", type=int, default=50)
    role_loop_parser.add_argument("--max-assignments", type=int, default=10)
    role_loop_parser.add_argument("--assignment-lease-seconds", type=int, default=300)
    role_loop_mode = role_loop_parser.add_mutually_exclusive_group(required=True)
    role_loop_mode.add_argument(
        "--cycles",
        type=int,
        help="Run a bounded number of role-service cycles and exit.",
    )
    role_loop_mode.add_argument(
        "--continuous",
        action="store_true",
        help="Run continuously until interrupted by the host/container supervisor.",
    )
    role_loop_parser.add_argument("--poll-seconds", type=float, default=5.0)

    project_tick_parser = subparsers.add_parser(
        "run-project-role-services-once",
        help="Run one bounded role-service tick for each configured project role instance.",
    )
    project_tick_parser.add_argument("--project-file", type=Path, required=True)
    project_tick_parser.add_argument("--max-recoveries", type=int, default=50)
    project_tick_parser.add_argument("--max-assignments", type=int, default=10)
    project_tick_parser.add_argument("--assignment-lease-seconds", type=int, default=300)
    project_tick_parser.add_argument(
        "--skip-unsupported",
        action="store_true",
        help="Skip role instances whose worker adapter is not implemented yet.",
    )

    project_loop_parser = subparsers.add_parser(
        "run-project-role-services-loop",
        help="Run a bounded repeated role-service loop for configured project role instances.",
    )
    project_loop_parser.add_argument("--project-file", type=Path, required=True)
    project_loop_parser.add_argument("--cycles", type=int, required=True)
    project_loop_parser.add_argument("--poll-seconds", type=float, default=5.0)
    project_loop_parser.add_argument("--max-recoveries", type=int, default=50)
    project_loop_parser.add_argument("--max-assignments", type=int, default=10)
    project_loop_parser.add_argument("--assignment-lease-seconds", type=int, default=300)
    project_loop_parser.add_argument(
        "--skip-unsupported",
        action="store_true",
        help="Skip role instances whose worker adapter is not implemented yet.",
    )

    project_hibernation_parser = subparsers.add_parser(
        "run-project-hibernation-maintenance",
        help="Run one bounded hibernation/hydration maintenance tick for configured project role instances.",
    )
    project_hibernation_parser.add_argument("--project-file", type=Path, required=True)
    project_hibernation_parser.add_argument(
        "--hibernate-reason",
        default="Project hibernation maintenance found an idle safe role instance.",
    )
    project_hibernation_parser.add_argument(
        "--hydrate-reason",
        default="Project hibernation maintenance found queued role work.",
    )

    container_lifecycle_parser = subparsers.add_parser(
        "plan-project-container-lifecycle",
        help="Plan container lifecycle commands for hibernated or hydrating project role instances.",
    )
    container_lifecycle_parser.add_argument("--project-file", type=Path, required=True)

    run_container_lifecycle_parser = subparsers.add_parser(
        "run-project-container-lifecycle",
        help="Record or execute container lifecycle commands for hibernated or hydrating project role instances.",
    )
    run_container_lifecycle_parser.add_argument("--project-file", type=Path, required=True)
    run_container_lifecycle_parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run planned lifecycle commands. Without this flag, actions are recorded as planned only.",
    )
    run_container_lifecycle_parser.add_argument("--timeout-seconds", type=int, default=300)

    retry_container_lifecycle_parser = subparsers.add_parser(
        "retry-container-lifecycle-action",
        help="Record or execute a retry of a failed role container lifecycle action.",
    )
    retry_container_lifecycle_parser.add_argument("--action-id", required=True)
    retry_container_lifecycle_parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run the retry command. Without this flag, the retry is recorded as planned only.",
    )
    retry_container_lifecycle_parser.add_argument("--timeout-seconds", type=int, default=300)

    supervisor_tick_parser = subparsers.add_parser(
        "run-project-supervisor-tick",
        help="Run one bounded project supervisor tick: hibernation maintenance plus container lifecycle action handling.",
    )
    supervisor_tick_parser.add_argument("--project-file", type=Path, required=True)
    supervisor_tick_parser.add_argument(
        "--hibernate-reason",
        default="Project supervisor tick found an idle safe role instance.",
    )
    supervisor_tick_parser.add_argument(
        "--hydrate-reason",
        default="Project supervisor tick found queued role work.",
    )
    supervisor_tick_parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run planned container lifecycle commands. Without this flag, actions are recorded as planned only.",
    )
    supervisor_tick_parser.add_argument("--timeout-seconds", type=int, default=300)

    supervisor_loop_parser = subparsers.add_parser(
        "run-project-supervisor-loop",
        help="Run a bounded repeated project supervisor loop.",
    )
    supervisor_loop_parser.add_argument("--project-file", type=Path, required=True)
    supervisor_loop_parser.add_argument("--cycles", type=int, required=True)
    supervisor_loop_parser.add_argument("--poll-seconds", type=float, default=5.0)
    supervisor_loop_parser.add_argument(
        "--hibernate-reason",
        default="Project supervisor loop found an idle safe role instance.",
    )
    supervisor_loop_parser.add_argument(
        "--hydrate-reason",
        default="Project supervisor loop found queued role work.",
    )
    supervisor_loop_parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run planned container lifecycle commands. Without this flag, actions are recorded as planned only.",
    )
    supervisor_loop_parser.add_argument("--timeout-seconds", type=int, default=300)

    supervisor_service_parser = subparsers.add_parser(
        "run-project-supervisor-service",
        help="Run the project supervisor as an explicit service loop.",
    )
    supervisor_service_parser.add_argument("--project-file", type=Path, required=True)
    supervisor_service_mode = supervisor_service_parser.add_mutually_exclusive_group(required=True)
    supervisor_service_mode.add_argument(
        "--cycles",
        type=int,
        help="Run a bounded number of service cycles and exit.",
    )
    supervisor_service_mode.add_argument(
        "--continuous",
        action="store_true",
        help="Run continuously until interrupted by the host/container supervisor.",
    )
    supervisor_service_parser.add_argument("--poll-seconds", type=float, default=5.0)
    supervisor_service_parser.add_argument(
        "--hibernate-reason",
        default="Project supervisor service found an idle safe role instance.",
    )
    supervisor_service_parser.add_argument(
        "--hydrate-reason",
        default="Project supervisor service found queued role work.",
    )
    supervisor_service_parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run planned container lifecycle commands. Without this flag, actions are recorded as planned only.",
    )
    supervisor_service_parser.add_argument("--timeout-seconds", type=int, default=300)

    topology_parser = subparsers.add_parser(
        "validate-topology",
        help="Validate v2 source/runtime/project repository boundaries.",
    )
    topology_parser.add_argument("--source-repo", required=True)
    topology_parser.add_argument("--deployed-runtime", required=True)
    topology_parser.add_argument("--runtime-state", required=True)
    topology_parser.add_argument(
        "--project-repo",
        action="append",
        default=[],
        metavar="ID=PATH|DOCROOT",
        help="Project repo and document library root. May be repeated.",
    )
    topology_parser.add_argument("--image-identity")
    topology_parser.add_argument("--allow-local-dev-overlap", action="store_true")
    topology_parser.add_argument("--local-dev-reason")

    install_parser = subparsers.add_parser(
        "install-project",
        help="Reconcile project connector resources such as Teams, Entra apps, channels, and role app installs.",
    )
    install_parser.add_argument("--project-file", type=Path, required=True)
    install_parser.add_argument("--organization-file", type=Path)
    install_parser.add_argument(
        "--graph-token-file",
        type=Path,
        help="JSON file containing a Graph access_token. Use when Azure CLI cannot request the needed Teams scopes.",
    )
    install_parser.add_argument(
        "--teams-app-package-root",
        type=Path,
        help="Folder containing published-apps.json for project Teams app package ids.",
    )
    install_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply allowed tenant mutations. Without this flag, only a plan/audit is produced.",
    )
    install_parser.add_argument("--allow-create-team", action="store_true")
    install_parser.add_argument("--allow-create-channel", action="store_true")
    install_parser.add_argument("--allow-register-apps", action="store_true")
    install_parser.add_argument("--allow-install-apps", action="store_true")
    install_parser.add_argument("--allow-uninstall-stale", action="store_true")
    install_parser.add_argument("--allow-secret-rotation", action="store_true")

    args = parser.parse_args(argv)
    db_path = Path(args.db)
    configure_observability("agentic-mesh-v2-cli")

    if args.command == "validate-topology":
        with span("v2.cli.validate_topology", command=args.command):
            topology = RuntimeTopology(
                source_repo=Path(args.source_repo),
                deployed_runtime=Path(args.deployed_runtime),
                runtime_state=Path(args.runtime_state),
                project_repos=tuple(_parse_project_repo(value) for value in args.project_repo),
                image_identity=args.image_identity,
                allow_local_dev_overlap=args.allow_local_dev_overlap,
                local_dev_reason=args.local_dev_reason,
            ).validate()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "source_repo": str(topology.source_repo),
                    "deployed_runtime": str(topology.deployed_runtime),
                    "runtime_state": str(topology.runtime_state),
                    "project_repos": [
                        {
                            "repo_id": project.repo_id,
                            "path": str(project.path),
                            "document_library_root": str(project.document_library_root),
                        }
                        for project in topology.project_repos
                    ],
                    "warnings": list(topology.warnings),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "install-project":
        with span("v2.cli.install_project", command=args.command):
            result = run_project_install(
                project_file=args.project_file,
                organization_file=args.organization_file,
                options=InstallOptions(
                    apply=bool(args.apply),
                    allow_create_team=bool(args.allow_create_team),
                    allow_create_channel=bool(args.allow_create_channel),
                    allow_register_apps=bool(args.allow_register_apps),
                    allow_install_apps=bool(args.allow_install_apps),
                    allow_uninstall_stale=bool(args.allow_uninstall_stale),
                    allow_secret_rotation=bool(args.allow_secret_rotation),
                ),
                graph_token_file=args.graph_token_file,
                teams_app_package_root=args.teams_app_package_root,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] != "blocked" else 2

    if args.command == "serve":
        serve(host=args.host, port=args.port, db_path=db_path, project_file=args.project_file)
        return 0
    if args.command == "serve-teams-ingress":
        serve_teams_ingress(
            host=args.host,
            port=args.port,
            db_path=db_path,
            project_file=args.project_file,
            ingress_path=args.path,
            external_base_url=args.external_base_url,
        )
        return 0

    with span("v2.cli.command", command=args.command):
        db = V2Database(db_path)
        try:
            db.migrate()
            if args.command == "init-db":
                print(json.dumps({"status": "ok", "database": str(db_path)}, sort_keys=True))
                return 0
            if args.command == "demo-slice":
                work_id = run_demo_slice(db)
                print(json.dumps({"status": "ok", "work_item_id": work_id}, sort_keys=True))
                return 0
            if args.command == "status-json":
                print(json.dumps(db.status_snapshot(), indent=2, sort_keys=True))
                return 0
            if args.command == "sync-teams-conversations":
                result = _sync_teams_conversations(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0 if result["status"] != "blocked" else 2
            if args.command == "record-safe-output":
                try:
                    result = _record_safe_output_cli(db, args)
                except (ValueError, SafeOutputError) as exc:
                    print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
                    return 1
                else:
                    print(json.dumps(result, sort_keys=True))
                    return 0
            if args.command == "run-safe-output-mcp-stdio":
                run_safe_output_mcp_stdio(db, service=_safe_output_service_or_none(db, args.project_file))
                return 0
            if args.command == "recover-stale-assignments":
                recovered = db.recover_stale_role_assignments(
                    role_id=args.role_id,
                    limit=args.limit,
                    reason=args.reason,
                )
                print(
                    json.dumps(
                        {
                            "status": "ok",
                            "role_id": args.role_id,
                            "recovered_count": len(recovered),
                            "assignment_ids": [row["assignment_id"] for row in recovered],
                        },
                        sort_keys=True,
                    )
                )
                return 0
            if args.command == "run-role-service-tick":
                print(json.dumps(_run_role_service_tick(db, args), sort_keys=True))
                return 0
            if args.command == "run-role-service-loop":
                print(json.dumps(_run_role_service_loop(db, args), sort_keys=True))
                return 0
            if args.command == "run-project-role-services-once":
                result = _run_project_role_services_once(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0
            if args.command == "run-project-role-services-loop":
                result = _run_project_role_services_loop(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0
            if args.command == "run-project-hibernation-maintenance":
                result = _run_project_hibernation_maintenance(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0
            if args.command == "plan-project-container-lifecycle":
                result = _plan_project_container_lifecycle(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0
            if args.command == "run-project-container-lifecycle":
                result = _run_project_container_lifecycle(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0
            if args.command == "retry-container-lifecycle-action":
                result = _retry_container_lifecycle_action(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0
            if args.command == "run-project-supervisor-tick":
                result = _run_project_supervisor_tick(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0
            if args.command == "run-project-supervisor-loop":
                result = _run_project_supervisor_loop(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0
            if args.command == "run-project-supervisor-service":
                result = _run_project_supervisor_service(db, args)
                print(json.dumps(result, sort_keys=True))
                return 0
        finally:
            db.close()

    raise AssertionError(f"unhandled command: {args.command}")


def _record_safe_output_cli(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    return record_safe_output_for_run(
        db,
        run_id=args.run_id,
        role_id=args.role_id,
        tool_name=args.tool_name,
        payload=parse_safe_output_payload_json(args.payload_json),
        terminal=bool(args.terminal),
        service=_safe_output_service(db, args.project_file, process_effects=False),
    )


def _sync_teams_conversations(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    if args.max_messages < 1:
        raise ValueError("--max-messages must be at least 1")
    if args.poll_seconds < 0:
        raise ValueError("--poll-seconds must be zero or greater")
    if args.cycles is not None and args.cycles < 1:
        raise ValueError("--cycles must be at least 1")

    config = load_teams_connector_config(args.project_file)
    graph_client = TokenGraphClient.from_file(args.graph_token_file) if args.graph_token_file else AzureCliGraphClient()
    continuous = bool(args.continuous)
    cycles_requested = None if continuous else int(args.cycles or 1)
    cycles: list[dict[str, object]] = []
    totals = {
        "channels_checked": 0,
        "messages_seen": 0,
        "messages_replayed": 0,
        "duplicates": 0,
        "skipped_bot_messages": 0,
    }
    index = 0
    status = "ok"
    try:
        while cycles_requested is None or index < cycles_requested:
            index += 1
            try:
                result = sync_project_channel_messages(
                    db=db,
                    config=config,
                    graph_client=graph_client,
                    max_messages=args.max_messages,
                    include_replies=not bool(args.skip_replies),
                )
            except GraphRequestError as exc:
                attention_id = f"attention-teams-graph-sync-{config.connector_id}"
                db.create_connector_attention_item(
                    attention_id=attention_id,
                    connector_id=config.connector_id,
                    owner="operator",
                    reason_class="teams_graph_sync_failed",
                    next_action=(
                        "Grant the Teams Graph read scope required for connector-owned message sync, "
                        "then rerun sync-teams-conversations."
                    ),
                    retryable=True,
                    source_ref="sync-teams-conversations",
                )
                return {
                    "status": "blocked",
                    "service_mode": "continuous" if continuous else "bounded",
                    "project_file": str(args.project_file),
                    "cycles_requested": cycles_requested,
                    "cycles_completed": len(cycles),
                    "include_replies": not bool(args.skip_replies),
                    "attention_id": attention_id,
                    "error": exc.message,
                    "totals": totals,
                    "cycles": cycles,
                }
            cycle = {
                "cycle": index,
                "channels_checked": result.channels_checked,
                "messages_seen": result.messages_seen,
                "messages_replayed": result.messages_replayed,
                "duplicates": result.duplicates,
                "skipped_bot_messages": result.skipped_bot_messages,
            }
            cycles.append(cycle)
            for key in totals:
                totals[key] += int(cycle[key])
            if (cycles_requested is None or index < cycles_requested) and args.poll_seconds:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        status = "interrupted"
    return {
        "status": status,
        "service_mode": "continuous" if continuous else "bounded",
        "project_file": str(args.project_file),
        "cycles_requested": cycles_requested,
        "cycles_completed": len(cycles),
        "include_replies": not bool(args.skip_replies),
        "totals": totals,
        "cycles": cycles,
    }


def _parse_project_repo(value: str) -> ProjectRepo:
    if "=" not in value or "|" not in value:
        raise argparse.ArgumentTypeError(
            "--project-repo must use ID=PATH|DOCROOT"
        )
    repo_id, rest = value.split("=", 1)
    path, docroot = rest.split("|", 1)
    if not repo_id.strip() or not path.strip() or not docroot.strip():
        raise argparse.ArgumentTypeError(
            "--project-repo must include non-empty ID, PATH, and DOCROOT"
        )
    return ProjectRepo(
        repo_id=repo_id,
        path=Path(path),
        document_library_root=Path(docroot),
    )


def _parse_worker_command(value: str | None) -> tuple[str, ...]:
    if not value:
        raise ValueError("--worker-command-json is required for safe-output-subprocess worker")
    raw = json.loads(value)
    if not isinstance(raw, list) or not raw:
        raise ValueError("--worker-command-json must be a non-empty JSON array")
    command: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"--worker-command-json item {index} must be a non-empty string")
        command.append(item)
    return tuple(command)


def _worker_config_from_args(args: argparse.Namespace) -> dict[str, object]:
    if args.worker is None:
        if args.project_file is None:
            raise ValueError("--worker or --project-file is required for role-service execution")
        return load_role_worker_config(args.project_file, role_id=args.role_id)
    if args.worker == "safe-output-file":
        if args.safe_output_file is None:
            raise ValueError("--safe-output-file is required for safe-output-file worker")
        return {"adapter": "safe-output-file", "path": str(args.safe_output_file)}
    if args.worker == "safe-output-subprocess":
        config: dict[str, object] = {
            "adapter": "safe-output-subprocess",
            "command": list(_parse_worker_command(args.worker_command_json)),
        }
        if args.worker_timeout_seconds is not None:
            config["timeout_seconds"] = args.worker_timeout_seconds
        return config
    if args.worker == "codex-cli":
        config: dict[str, object] = {"adapter": "codex-cli"}
        if args.worker_command_json:
            config["command"] = list(_parse_worker_command(args.worker_command_json))
        if args.worker_timeout_seconds is not None:
            config["timeout_seconds"] = args.worker_timeout_seconds
        return config
    raise AssertionError(f"unhandled worker adapter: {args.worker}")


def _memory_context_loader(project_file: Path | None):
    if project_file is None:
        return None

    def load(role_id: str) -> tuple[str, ...]:
        return load_role_memory_context(project_file, role_id=role_id)

    return load


def _safe_output_service(
    db: V2Database,
    project_file: Path | None,
    *,
    process_effects: bool = True,
) -> SafeOutputService:
    if project_file is None:
        return SafeOutputService(db, process_effects=process_effects)
    document_library = load_document_library_config(project_file)
    project_id = load_project_id(project_file)
    memory_resolver = _role_memory_path_resolver(project_file)
    service_kwargs = {
        "project_id": project_id,
        "role_memory_path_resolver": memory_resolver,
    }
    if document_library is not None:
        service_kwargs["document_library_root"] = document_library.root
    try:
        teams_config = load_teams_connector_config(project_file)
    except ValueError:
        teams_config = None
    if teams_config is None:
        return SafeOutputService(db, process_effects=process_effects, **service_kwargs)
    secret_root = _project_secret_root(project_file)
    delivery_client = BotFrameworkDeliveryClient(config=teams_config, secret_root=secret_root) if secret_root.exists() else None
    adapter = LocalTeamsTestAdapter(db, teams_config, delivery_client=delivery_client)
    adapter.install()
    return ConnectorSafeOutputService(
        db,
        adapter=adapter,
        process_effects=process_effects,
        **service_kwargs,
    )


def _safe_output_service_or_none(db: V2Database, project_file: Path | None) -> SafeOutputService | None:
    if project_file is None:
        return None
    return _safe_output_service(db, project_file)


def _project_secret_root(project_file: Path) -> Path:
    return project_file.parent.parent / "state" / "secrets"


def _role_memory_path_resolver(project_file: Path):
    def resolve(role_id: str) -> Path | None:
        config = load_role_memory_config(project_file, role_id=role_id)
        if not config.enabled:
            return None
        return config.memory_path

    return resolve


def _run_role_service_tick(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    worker = build_worker_adapter(_worker_config_from_args(args))
    prompt_assembler = build_prompt_assembler_for_project(args.project_file) if args.project_file is not None else None
    service = RoleService(
        db=db,
        role_id=args.role_id,
        role_instance_id=args.role_instance_id,
        worker=worker,
        safe_outputs=_safe_output_service(db, args.project_file),
        safe_output_project_file=args.project_file,
        prompt_assembler=prompt_assembler,
        memory_context_loader=_memory_context_loader(args.project_file),
        assignment_lease_seconds=args.assignment_lease_seconds,
    )
    receipt = service.run_service_tick(
        max_recoveries=args.max_recoveries,
        max_assignments=args.max_assignments,
    )
    return {
        "status": receipt.status,
        "role_id": args.role_id,
        "role_instance_id": args.role_instance_id,
        "recovered_count": receipt.recovered_count,
        "processed_count": receipt.processed_count,
        "runs": [
            {
                "assignment_id": run.assignment_id,
                "run_id": run.run_id,
                "status": run.status,
                "terminal_tool": run.terminal_tool,
                "safe_output_count": run.safe_output_count,
            }
            for run in receipt.receipts
        ],
    }


def _run_role_service_loop(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    if args.cycles is not None and args.cycles < 1:
        raise ValueError("--cycles must be at least 1")
    if args.poll_seconds < 0:
        raise ValueError("--poll-seconds must be zero or greater")

    continuous = bool(args.continuous)
    cycles_requested = None if continuous else int(args.cycles)
    cycles: list[dict[str, object]] = []
    totals = {"processed_count": 0, "recovered_count": 0}
    status = "ok"
    index = 0
    try:
        while cycles_requested is None or index < cycles_requested:
            index += 1
            cycle = _run_role_service_tick(db, args)
            cycle["cycle"] = index
            cycles.append(cycle)
            totals["processed_count"] += int(cycle["processed_count"])
            totals["recovered_count"] += int(cycle["recovered_count"])
            if (cycles_requested is None or index < cycles_requested) and args.poll_seconds:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        status = "interrupted"

    return {
        "status": status,
        "service_mode": "continuous" if continuous else "bounded",
        "role_id": args.role_id,
        "role_instance_id": args.role_instance_id,
        "project_file": str(args.project_file) if args.project_file is not None else None,
        "cycles_requested": cycles_requested,
        "cycles_completed": len(cycles),
        **totals,
        "cycles": cycles,
    }


def _run_project_role_services_once(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    totals = {"processed_count": 0, "recovered_count": 0, "skipped_count": 0}
    for config in list_project_role_service_configs(args.project_file):
        try:
            worker = build_worker_adapter(config.worker_config)
        except ValueError as exc:
            if not args.skip_unsupported:
                raise
            totals["skipped_count"] += 1
            entries.append(
                {
                    "role_id": config.role_id,
                    "role_instance_id": config.role_instance_id,
                    "status": "skipped",
                    "reason": str(exc),
                }
            )
            continue
        service = RoleService(
            db=db,
            role_id=config.role_id,
            role_instance_id=config.role_instance_id,
            worker=worker,
            safe_outputs=_safe_output_service(db, args.project_file),
            safe_output_project_file=args.project_file,
            prompt_assembler=build_prompt_assembler_for_project(args.project_file),
            memory_context_loader=_memory_context_loader(args.project_file),
            assignment_lease_seconds=args.assignment_lease_seconds,
        )
        receipt = service.run_service_tick(
            max_recoveries=args.max_recoveries,
            max_assignments=args.max_assignments,
        )
        totals["processed_count"] += receipt.processed_count
        totals["recovered_count"] += receipt.recovered_count
        entries.append(
            {
                "role_id": config.role_id,
                "role_instance_id": config.role_instance_id,
                "status": receipt.status,
                "processed_count": receipt.processed_count,
                "recovered_count": receipt.recovered_count,
                "runs": [
                    {
                        "assignment_id": run.assignment_id,
                        "run_id": run.run_id,
                        "status": run.status,
                        "terminal_tool": run.terminal_tool,
                        "safe_output_count": run.safe_output_count,
                    }
                    for run in receipt.receipts
                ],
            }
        )
    return {
        "status": "ok",
        "project_file": str(args.project_file),
        **totals,
        "role_instances": entries,
    }


def _run_project_role_services_loop(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    if args.cycles < 1:
        raise ValueError("--cycles must be at least 1")
    if args.poll_seconds < 0:
        raise ValueError("--poll-seconds must be zero or greater")

    cycles: list[dict[str, object]] = []
    totals = {"processed_count": 0, "recovered_count": 0, "skipped_count": 0}
    for index in range(1, args.cycles + 1):
        cycle = _run_project_role_services_once(db, args)
        cycle["cycle"] = index
        cycles.append(cycle)
        totals["processed_count"] += int(cycle["processed_count"])
        totals["recovered_count"] += int(cycle["recovered_count"])
        totals["skipped_count"] += int(cycle["skipped_count"])
        if index < args.cycles and args.poll_seconds:
            time.sleep(args.poll_seconds)

    return {
        "status": "ok",
        "project_file": str(args.project_file),
        "cycles_requested": args.cycles,
        **totals,
        "cycles": cycles,
    }


def _run_project_hibernation_maintenance(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    totals = {"hibernated_count": 0, "hydrating_count": 0, "kept_awake_count": 0}
    hydrated_instances: set[str] = set()
    hydrated_roles: set[str] = set()
    configs = list_project_role_service_configs(args.project_file)
    for config in configs:
        policy = HibernationPolicy.from_mapping(
            load_role_hibernation_config(args.project_file, role_id=config.role_id)
        )
        service = HibernationService(db, policy)
        if config.role_id not in hydrated_roles:
            hydration_decisions = service.hydrate_for_pending_work(
                role_id=config.role_id,
                reason=args.hydrate_reason,
            )
            hydrated_roles.add(config.role_id)
            for hydration in hydration_decisions:
                hydrated_instances.add(hydration.role_instance_id)
                totals["hydrating_count"] += 1
                entries.append(
                    {
                        "role_id": hydration.role_id,
                        "role_instance_id": hydration.role_instance_id,
                        "status": "hydrating",
                        "reason": hydration.reason,
                    }
                )
        if config.role_instance_id in hydrated_instances:
            continue
        decision = service.mark_hibernating(
            role_id=config.role_id,
            role_instance_id=config.role_instance_id,
            reason=args.hibernate_reason,
        )
        if decision.can_hibernate:
            service.mark_hibernated(
                role_id=config.role_id,
                role_instance_id=config.role_instance_id,
                reason=args.hibernate_reason,
            )
            totals["hibernated_count"] += 1
            entries.append(
                {
                    "role_id": config.role_id,
                    "role_instance_id": config.role_instance_id,
                    "status": "hibernated",
                    "reason": args.hibernate_reason,
                    "idle_seconds": decision.idle_seconds,
                }
            )
        else:
            totals["kept_awake_count"] += 1
            entries.append(
                {
                    "role_id": config.role_id,
                    "role_instance_id": config.role_instance_id,
                    "status": "kept_awake",
                    "reason": decision.reason,
                    "idle_seconds": decision.idle_seconds,
                }
            )
    return {
        "status": "ok",
        "project_file": str(args.project_file),
        **totals,
        "role_instances": entries,
    }


def _plan_project_container_lifecycle(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    actions, skipped = _project_container_lifecycle_actions(db, args.project_file)
    return {
        "status": "ok",
        "project_file": str(args.project_file),
        "action_count": len(actions),
        "skipped_count": len(skipped),
        "actions": [_action_to_dict(action) for action in actions],
        "skipped": skipped,
    }


def _run_project_container_lifecycle(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    actions, skipped = _project_container_lifecycle_actions(db, args.project_file)
    executor = ContainerLifecycleExecutor(db, timeout_seconds=args.timeout_seconds)
    receipts: list[dict[str, object]] = []
    executed_count = 0
    failed_count = 0
    planned_count = 0
    existing_planned_count = 0
    for action in actions:
        if args.execute:
            action_id, result = executor.execute(action)
            executed_count += 1
            if result.exit_code != 0:
                failed_count += 1
            receipts.append(
                {
                    **_action_to_dict(action),
                    "action_id": action_id,
                    "status": "succeeded" if result.exit_code == 0 else "failed",
                    "exit_code": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
        else:
            action_id, created = executor.record_plan_if_absent(action)
            status = "planned" if created else "already_planned"
            if created:
                planned_count += 1
            else:
                existing_planned_count += 1
            receipts.append(
                {
                    **_action_to_dict(action),
                    "action_id": action_id,
                    "status": status,
                }
            )
    return {
        "status": "ok",
        "project_file": str(args.project_file),
        "execute": bool(args.execute),
        "action_count": len(actions),
        "planned_count": planned_count,
        "existing_planned_count": existing_planned_count,
        "executed_count": executed_count,
        "failed_count": failed_count,
        "skipped_count": len(skipped),
        "actions": receipts,
        "skipped": skipped,
    }


def _retry_container_lifecycle_action(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    executor = ContainerLifecycleExecutor(db, timeout_seconds=args.timeout_seconds)
    if args.execute:
        action_id, action, result = executor.execute_retry(args.action_id)
        return {
            "status": "succeeded" if result.exit_code == 0 else "failed",
            "retry_of_action_id": args.action_id,
            "execute": True,
            "action": {
                **_action_to_dict(action),
                "action_id": action_id,
                "status": "succeeded" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        }
    action_id, action, created = executor.record_retry_plan(args.action_id)
    return {
        "status": "planned" if created else "already_planned",
        "retry_of_action_id": args.action_id,
        "execute": False,
        "action": {
            **_action_to_dict(action),
            "action_id": action_id,
            "status": "planned" if created else "already_planned",
        },
    }


def _run_project_supervisor_tick(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    hibernation = _run_project_hibernation_maintenance(db, args)
    lifecycle = _run_project_container_lifecycle(db, args)
    return {
        "status": "ok",
        "project_file": str(args.project_file),
        "execute": bool(args.execute),
        "hibernation": hibernation,
        "container_lifecycle": lifecycle,
    }


def _run_project_supervisor_loop(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    if args.cycles < 1:
        raise ValueError("--cycles must be at least 1")
    if args.poll_seconds < 0:
        raise ValueError("--poll-seconds must be zero or greater")

    cycles: list[dict[str, object]] = []
    hibernation_totals = {"hibernated_count": 0, "hydrating_count": 0, "kept_awake_count": 0}
    lifecycle_totals = {
        "action_count": 0,
        "planned_count": 0,
        "existing_planned_count": 0,
        "executed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
    }
    for index in range(1, args.cycles + 1):
        cycle = _run_project_supervisor_tick(db, args)
        cycle["cycle"] = index
        cycles.append(cycle)
        hibernation = cycle["hibernation"]
        lifecycle = cycle["container_lifecycle"]
        if isinstance(hibernation, dict):
            for key in hibernation_totals:
                hibernation_totals[key] += int(hibernation.get(key, 0))
        if isinstance(lifecycle, dict):
            for key in lifecycle_totals:
                lifecycle_totals[key] += int(lifecycle.get(key, 0))
        if index < args.cycles and args.poll_seconds:
            time.sleep(args.poll_seconds)

    return {
        "status": "ok",
        "project_file": str(args.project_file),
        "cycles_requested": args.cycles,
        "execute": bool(args.execute),
        "hibernation_totals": hibernation_totals,
        "container_lifecycle_totals": lifecycle_totals,
        "cycles": cycles,
    }


def _run_project_supervisor_service(db: V2Database, args: argparse.Namespace) -> dict[str, object]:
    if args.cycles is not None and args.cycles < 1:
        raise ValueError("--cycles must be at least 1")
    if args.poll_seconds < 0:
        raise ValueError("--poll-seconds must be zero or greater")

    continuous = bool(args.continuous)
    cycles_requested = None if continuous else int(args.cycles)
    cycles: list[dict[str, object]] = []
    hibernation_totals = {"hibernated_count": 0, "hydrating_count": 0, "kept_awake_count": 0}
    lifecycle_totals = {
        "action_count": 0,
        "planned_count": 0,
        "existing_planned_count": 0,
        "executed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
    }
    status = "ok"
    index = 0
    try:
        while cycles_requested is None or index < cycles_requested:
            index += 1
            cycle = _run_project_supervisor_tick(db, args)
            cycle["cycle"] = index
            cycles.append(cycle)
            hibernation = cycle["hibernation"]
            lifecycle = cycle["container_lifecycle"]
            if isinstance(hibernation, dict):
                for key in hibernation_totals:
                    hibernation_totals[key] += int(hibernation.get(key, 0))
            if isinstance(lifecycle, dict):
                for key in lifecycle_totals:
                    lifecycle_totals[key] += int(lifecycle.get(key, 0))
            if (cycles_requested is None or index < cycles_requested) and args.poll_seconds:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        status = "interrupted"

    return {
        "status": status,
        "service_mode": "continuous" if continuous else "bounded",
        "project_file": str(args.project_file),
        "cycles_requested": cycles_requested,
        "cycles_completed": len(cycles),
        "execute": bool(args.execute),
        "hibernation_totals": hibernation_totals,
        "container_lifecycle_totals": lifecycle_totals,
        "cycles": cycles,
    }


def _project_container_lifecycle_actions(
    db: V2Database,
    project_file: Path,
) -> tuple[list[ContainerLifecycleAction], list[dict[str, str]]]:
    statuses = {
        row["role_instance_id"]: row
        for row in db.list_role_instance_statuses()
    }
    actions: list[ContainerLifecycleAction] = []
    skipped: list[dict[str, str]] = []
    for config in list_project_role_service_configs(project_file):
        row = statuses.get(config.role_instance_id)
        if row is None:
            skipped.append(
                {
                    "role_id": config.role_id,
                    "role_instance_id": config.role_instance_id,
                    "reason": "Role instance has no runtime status.",
                }
            )
            continue
        raw_lifecycle = load_role_container_lifecycle_config(project_file, role_id=config.role_id)
        if not raw_lifecycle:
            skipped.append(
                {
                    "role_id": config.role_id,
                    "role_instance_id": config.role_instance_id,
                    "reason": "Role has no container_lifecycle config.",
                }
            )
            continue
        lifecycle_config = ComposeRoleLifecycleConfig.from_mapping(raw_lifecycle)
        action = plan_compose_lifecycle_action(
            config=lifecycle_config,
            project_id=config.project_id,
            role_id=config.role_id,
            role_instance_id=config.role_instance_id,
            status=str(row["status"]),
            reason=str(row.get("hibernation_reason") or row.get("wake_reason") or row.get("detail") or ""),
        )
        if action is None:
            skipped.append(
                {
                    "role_id": config.role_id,
                    "role_instance_id": config.role_instance_id,
                    "reason": f"Role instance status `{row['status']}` has no container lifecycle action.",
                }
            )
            continue
        actions.append(action)
    return actions, skipped


def _action_to_dict(action: ContainerLifecycleAction) -> dict[str, object]:
    return {
        "role_id": action.role_id,
        "role_instance_id": action.role_instance_id,
        "action": action.action,
        "service_name": action.service_name,
        "command": list(action.command),
        "working_directory": str(action.working_directory) if action.working_directory is not None else None,
        "reason": action.reason,
    }


if __name__ == "__main__":
    raise SystemExit(main())
