from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from dataclasses import asdict

from agentic_mesh import telemetry
from agentic_mesh.auth import AuthResolver
from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.config import load_mesh_config
from agentic_mesh.connectors import BotFrameworkTeamsConnectorAdapter
from agentic_mesh.connectors import FileSecretResolver
from agentic_mesh.connectors import GraphTeamsChannelIngressAdapter
from agentic_mesh.connectors import GraphTeamsConnectorAdapter
from agentic_mesh.connectors import LocalTeamsConnectorAdapter
from agentic_mesh.connectors import load_graph_token
from agentic_mesh.journal import EventJournal
from agentic_mesh.lifecycle import LifecycleStore
from agentic_mesh.messaging import build_human_response_received_message
from agentic_mesh.models import Message
from agentic_mesh.models import new_id
from agentic_mesh.runtime import AgentRuntime
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.teams_ingress import ReloadableTeamsBotIngress
from agentic_mesh.teams_ingress import serve_teams_bot_ingress
from agentic_mesh.workers import StubCodexWorkerAdapter


def build_runtime(
    config_root: Path,
    project_file: str,
    workspace_root: Path,
    state_root: Path,
):
    mesh_config = load_mesh_config(config_root, project_file=project_file)
    effective_workspace_root = project_workspace_root(workspace_root, mesh_config)
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    connector_outbox = FileConnectorOutbox(
        state_root,
        mesh_config.project.project_id,
        journal,
    )
    artifact_store = ArtifactStore(
        effective_workspace_root,
        mesh_config.project.project_id,
        journal,
    )
    lifecycle = LifecycleStore(state_root, mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store=message_store,
        artifact_store=artifact_store,
        journal=journal,
        project=mesh_config.project,
        worker=StubCodexWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    return mesh_config, journal, message_store, connector_outbox, lifecycle, runtime


def project_workspace_root(base_workspace_root: Path, mesh_config) -> Path:
    configured_root = Path(mesh_config.project.workspace.root)
    if configured_root.is_absolute():
        return configured_root
    return (base_workspace_root / configured_root).resolve()


def configure_component_telemetry(mesh_config, component: str) -> None:
    telemetry.configure_process_telemetry(
        mesh_config,
        service_name=telemetry.service_name_for_component(mesh_config, component),
        component=component,
    )


def configure_instance_telemetry(mesh_config, instance_id: str) -> None:
    instance = mesh_config.instances[instance_id]
    telemetry.configure_process_telemetry(
        mesh_config,
        service_name=telemetry.service_name_for_instance(instance),
        role_instance=instance,
    )


def cmd_validate(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    print(
        json.dumps(
            {
                "organization_id": mesh_config.organization.organization_id,
                "global_language": mesh_config.organization.global_language,
                "global_locale": mesh_config.organization.global_locale,
                "auth_methods": sorted(mesh_config.auth_methods),
                "response_types": sorted(mesh_config.response_types),
                "project_id": mesh_config.project.project_id,
                "workspace": {
                    "root": mesh_config.project.workspace.root,
                    "default_repository": mesh_config.project.workspace.default_repository,
                    "repositories": {
                        repository_id: asdict(repository)
                        for repository_id, repository in sorted(
                            mesh_config.project.workspace.repositories.items()
                        )
                    },
                },
                "connectors": {
                    connector_id: {
                        "adapter": connector.adapter,
                        "identity_model": connector.identity_model,
                        "team_id": connector.team_id,
                        "team_name": connector.team_name,
                        "ingress": (
                            asdict(connector.ingress)
                            if connector.ingress is not None
                            else None
                        ),
                        "channels": sorted(connector.channels),
                        "role_bots": sorted(connector.role_bots),
                    }
                    for connector_id, connector in sorted(
                        mesh_config.project.connectors.items()
                    )
                },
                "roles": sorted(mesh_config.project.roles),
                "instances": sorted(mesh_config.instances),
                "role_auth": {
                    role_id: role.worker.auth.method if role.worker.auth else None
                    for role_id, role in sorted(mesh_config.project.roles.items())
                },
                "document_accountabilities": sorted(
                    mesh_config.project.document_accountabilities
                ),
                "flow_id": mesh_config.project.flow.flow_id,
                "entry_state": mesh_config.project.flow.entry_state,
                "flow_states": sorted(mesh_config.project.flow.states),
            },
            indent=2,
        )
    )
    return 0


def cmd_enqueue(args) -> int:
    mesh_config, _, message_store, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "cli")
    lifecycle_state = args.lifecycle_state or mesh_config.project.flow.entry_state
    flow_state = mesh_config.project.flow.states[lifecycle_state]
    role_id = args.role or flow_state.owner_role
    work_item_id = args.work_item_id or new_id("work")
    message = Message.create(
        role_id=role_id,
        message_type=args.type,
        payload={
            "title": args.title,
            "summary": args.summary,
            "work_item_id": work_item_id,
            "work_item_type": args.work_item_type,
            "lifecycle_state": lifecycle_state,
        },
        source=args.source,
    )
    message_store.enqueue(message)
    print(json.dumps({"message_id": message.message_id, "correlation_id": message.correlation_id}))
    return 0


def cmd_run_agent(args) -> int:
    mesh_config, _, _, _, lifecycle, runtime = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_instance_telemetry(mesh_config, args.instance)
    instance = mesh_config.instances[args.instance]
    lifecycle.set_state(instance, "active", reason="run_once")
    did_work = runtime.run_once(args.instance, instance)
    lifecycle.set_state(instance, "idle", reason="run_once_complete")
    print(json.dumps({"instance": args.instance, "did_work": did_work}))
    return 0


def cmd_control_plane_tick(args) -> int:
    mesh_config, _, message_store, _, lifecycle, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "control-plane")
    transitions = lifecycle.control_plane_tick(
        mesh_config,
        message_store,
        idle_grace_seconds=args.idle_grace_seconds,
    )
    print(json.dumps({"transitions": transitions}, indent=2))
    return 0


def cmd_status(args) -> int:
    mesh_config, journal, message_store, connector_outbox, lifecycle, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    lifecycle.ensure_instances(mesh_config)
    configure_component_telemetry(mesh_config, "cli")
    for role_id in sorted(mesh_config.project.roles):
        telemetry.set_gauge(
            "agentic_mesh.role_queue.pending",
            message_store.pending_count(role_id),
            {"project_id": mesh_config.project.project_id, "role_id": role_id},
        )
    for channel in ["approvals"]:
        telemetry.set_gauge(
            "agentic_mesh.connector_outbox.pending",
            connector_outbox.pending_count(channel),
            {"project_id": mesh_config.project.project_id, "channel": channel},
        )
    status = {
        "organization_id": mesh_config.organization.organization_id,
        "global_language": mesh_config.organization.global_language,
        "project_id": mesh_config.project.project_id,
        "workspace_root": str(project_workspace_root(args.workspace_root, mesh_config)),
        "default_repository": mesh_config.project.workspace.default_repository,
        "instances": {
            instance_id: lifecycle.get_state(instance_id)
            for instance_id in sorted(mesh_config.instances)
        },
        "pending": {
            role_id: message_store.pending_count(role_id)
            for role_id in sorted(mesh_config.project.roles)
        },
        "connector_pending": {
            channel: connector_outbox.pending_count(channel)
            for channel in ["approvals"]
        },
        "journal_path": str(journal.path),
    }
    print(json.dumps(status, indent=2))
    return 0


def cmd_auth_plan(args) -> int:
    mesh_config, _, _, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "cli")
    resolver = AuthResolver(mesh_config.auth_methods)
    plans = [
        asdict(resolver.plan_for_instance(instance))
        for instance in mesh_config.instances.values()
        if args.instance is None or instance.instance_id == args.instance
    ]
    print(json.dumps({"auth_plans": plans}, indent=2))
    return 0


def _parse_response_value(value: str):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def cmd_record_human_response(args) -> int:
    mesh_config, _, message_store, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "cli")
    flow_state = mesh_config.project.flow.states[args.lifecycle_state]
    message = build_human_response_received_message(
        target_role=args.role or flow_state.owner_role,
        work_item_id=args.work_item_id,
        work_item_type=args.work_item_type,
        lifecycle_state=args.lifecycle_state,
        gate_id=args.gate_id,
        response_request_id=args.response_request_id,
        responder=args.responder,
        response_value=_parse_response_value(args.value),
        source=args.source,
        correlation_id=args.correlation_id,
    )
    message_store.enqueue(message)
    print(
        json.dumps(
            {
                "message_id": message.message_id,
                "target_role": message.role_id,
                "correlation_id": message.correlation_id,
            }
        )
    )
    return 0


def cmd_agent_loop(args) -> int:
    while True:
        cmd_run_agent(args)
        time.sleep(args.poll_seconds)


def cmd_control_plane_loop(args) -> int:
    while True:
        cmd_control_plane_tick(args)
        time.sleep(args.poll_seconds)


def cmd_router_loop(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    configure_component_telemetry(mesh_config, "router")
    while True:
        print(json.dumps({"service": "router", "status": "idle", "note": "handoffs route in runtime v0"}))
        time.sleep(args.poll_seconds)


def cmd_teams_connector_once(args) -> int:
    mesh_config, journal, _, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "teams-connector")
    connector = LocalTeamsConnectorAdapter(
        connector_id=args.connector_id,
        project_id=mesh_config.project.project_id,
        state_root=args.state_root,
        outbox=connector_outbox,
        journal=journal,
    )
    did_work = connector.process_once(args.channel)
    print(
        json.dumps(
            {
                "connector_id": args.connector_id,
                "channel": args.channel,
                "did_work": did_work,
            }
        )
    )
    return 0


def cmd_teams_connector_loop(args) -> int:
    while True:
        cmd_teams_connector_once(args)
        time.sleep(args.poll_seconds)


def _connector_channels(mesh_config, connector_id: str, channel: str) -> list[str]:
    connector_config = mesh_config.project.connectors[connector_id]
    if channel == "all":
        return sorted(connector_config.channels)
    return [channel]


def cmd_teams_graph_connector_once(args) -> int:
    mesh_config, journal, _, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "teams-connector")
    connector_config = mesh_config.project.connectors[args.connector]
    token = load_graph_token(args.token, args.token_file)
    connector = GraphTeamsConnectorAdapter(
        connector_id=args.connector_id,
        project_id=mesh_config.project.project_id,
        connector_config=connector_config,
        outbox=connector_outbox,
        journal=journal,
        token=token,
    )
    results = {}
    for channel in _connector_channels(mesh_config, args.connector, args.channel):
        results[channel] = connector.process_once(channel)
    print(json.dumps({"connector_id": args.connector_id, "results": results}))
    return 0


def cmd_teams_graph_connector_loop(args) -> int:
    while True:
        cmd_teams_graph_connector_once(args)
        time.sleep(args.poll_seconds)


def cmd_teams_graph_ingress_once(args) -> int:
    mesh_config, journal, message_store, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "teams-ingress")
    connector_config = mesh_config.project.connectors[args.connector]
    token = load_graph_token(args.token, args.token_file)
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id=args.connector_id,
        project_id=mesh_config.project.project_id,
        state_root=args.state_root,
        connector_config=connector_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token=token,
    )
    results = {}
    exit_code = 0
    for channel in _connector_channels(mesh_config, args.connector, args.channel):
        try:
            results[channel] = ingress.process_once(
                channel,
                max_messages=args.max_messages,
            )
        except Exception as exc:
            journal.append(
                "teams_graph_ingress_failed",
                project_id=mesh_config.project.project_id,
                connector_id=args.connector_id,
                channel=channel,
                error=str(exc),
            )
            results[channel] = {"error": str(exc)}
            exit_code = 1
    print(json.dumps({"connector_id": args.connector_id, "results": results}))
    return exit_code


def cmd_teams_graph_ingress_loop(args) -> int:
    while True:
        cmd_teams_graph_ingress_once(args)
        time.sleep(args.poll_seconds)


def cmd_teams_bot_connector_once(args) -> int:
    mesh_config, journal, _, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "teams-connector")
    connector_config = mesh_config.project.connectors[args.connector]
    connector = BotFrameworkTeamsConnectorAdapter(
        connector_id=args.connector_id,
        project_id=mesh_config.project.project_id,
        connector_config=connector_config,
        outbox=connector_outbox,
        journal=journal,
        secrets=FileSecretResolver(args.secret_root),
        service_url=args.service_url,
    )
    results = {}
    for channel in _connector_channels(mesh_config, args.connector, args.channel):
        results[channel] = connector.process_once(channel)
    print(json.dumps({"connector_id": args.connector_id, "results": results}))
    return 0


def cmd_teams_bot_connector_loop(args) -> int:
    while True:
        cmd_teams_bot_connector_once(args)
        time.sleep(args.poll_seconds)


def cmd_teams_bot_listener(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    configure_component_telemetry(mesh_config, "teams-bot-listener")
    ingress = ReloadableTeamsBotIngress(
        config_root=args.config_root,
        project_file=args.project_file,
        state_root=args.state_root,
        connector=args.connector,
        connector_id=args.connector_id,
        secret_root=args.secret_root,
    )
    serve_teams_bot_ingress(host=args.host, port=args.port, ingress=ingress)
    return 0


def parser() -> argparse.ArgumentParser:
    root = Path(os.getenv("AGENTIC_MESH_CONFIG_ROOT", Path.cwd()))
    workspace_root = Path(os.getenv("AGENTIC_MESH_WORKSPACE_ROOT", root))
    state_root = Path(os.getenv("AGENTIC_MESH_STATE_ROOT", workspace_root / "state"))
    project_file = os.getenv(
        "AGENTIC_MESH_PROJECT_FILE",
        "examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
    )
    parser = argparse.ArgumentParser(prog="agentic-mesh")
    parser.add_argument(
        "--root",
        type=Path,
        dest="config_root",
        default=root,
        help="Deprecated alias for --config-root.",
    )
    parser.add_argument("--config-root", type=Path, default=root)
    parser.add_argument("--project-file", default=project_file)
    parser.add_argument("--workspace-root", type=Path, default=workspace_root)
    parser.add_argument("--state-root", type=Path, default=state_root)
    subcommands = parser.add_subparsers(required=True)

    validate = subcommands.add_parser("validate-config")
    validate.set_defaults(func=cmd_validate)

    enqueue = subcommands.add_parser("enqueue")
    enqueue.add_argument("--role")
    enqueue.add_argument("--type", default="sdlc.intake")
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--summary", required=True)
    enqueue.add_argument("--work-item-id")
    enqueue.add_argument("--work-item-type", default="slice")
    enqueue.add_argument("--lifecycle-state")
    enqueue.add_argument("--source", default="local-cli")
    enqueue.set_defaults(func=cmd_enqueue)

    run_agent = subcommands.add_parser("run-agent")
    run_agent.add_argument("--instance", required=True)
    run_agent.set_defaults(func=cmd_run_agent)

    tick = subcommands.add_parser("control-plane-tick")
    tick.add_argument("--idle-grace-seconds", type=int, default=300)
    tick.set_defaults(func=cmd_control_plane_tick)

    status = subcommands.add_parser("status")
    status.set_defaults(func=cmd_status)

    auth_plan = subcommands.add_parser("auth-plan")
    auth_plan.add_argument("--instance")
    auth_plan.set_defaults(func=cmd_auth_plan)

    human_response = subcommands.add_parser("record-human-response")
    human_response.add_argument("--work-item-id", required=True)
    human_response.add_argument("--work-item-type", default="slice")
    human_response.add_argument("--lifecycle-state", required=True)
    human_response.add_argument("--gate-id", required=True)
    human_response.add_argument("--response-request-id", required=True)
    human_response.add_argument("--responder", required=True)
    human_response.add_argument("--value", required=True)
    human_response.add_argument("--role")
    human_response.add_argument("--source", default="local-cli")
    human_response.add_argument("--correlation-id")
    human_response.set_defaults(func=cmd_record_human_response)

    agent_loop = subcommands.add_parser("agent-loop")
    agent_loop.add_argument("--instance", required=True)
    agent_loop.add_argument("--poll-seconds", type=int, default=5)
    agent_loop.set_defaults(func=cmd_agent_loop)

    control_loop = subcommands.add_parser("control-plane-loop")
    control_loop.add_argument("--idle-grace-seconds", type=int, default=300)
    control_loop.add_argument("--poll-seconds", type=int, default=5)
    control_loop.set_defaults(func=cmd_control_plane_loop)

    router_loop = subcommands.add_parser("router-loop")
    router_loop.add_argument("--poll-seconds", type=int, default=10)
    router_loop.set_defaults(func=cmd_router_loop)

    teams_once = subcommands.add_parser("teams-connector-once")
    teams_once.add_argument("--channel", default="approvals")
    teams_once.add_argument("--connector-id", default="local-teams-connector")
    teams_once.set_defaults(func=cmd_teams_connector_once)

    teams_loop = subcommands.add_parser("teams-connector-loop")
    teams_loop.add_argument("--channel", default="approvals")
    teams_loop.add_argument("--connector-id", default="local-teams-connector")
    teams_loop.add_argument("--poll-seconds", type=int, default=5)
    teams_loop.set_defaults(func=cmd_teams_connector_loop)

    teams_graph_once = subcommands.add_parser("teams-graph-connector-once")
    teams_graph_once.add_argument("--connector", default="teams")
    teams_graph_once.add_argument("--channel", default="all")
    teams_graph_once.add_argument("--connector-id", default="teams-graph-connector")
    teams_graph_once.add_argument("--token")
    teams_graph_once.add_argument("--token-file", type=Path)
    teams_graph_once.set_defaults(func=cmd_teams_graph_connector_once)

    teams_graph_loop = subcommands.add_parser("teams-graph-connector-loop")
    teams_graph_loop.add_argument("--connector", default="teams")
    teams_graph_loop.add_argument("--channel", default="all")
    teams_graph_loop.add_argument("--connector-id", default="teams-graph-connector")
    teams_graph_loop.add_argument("--token")
    teams_graph_loop.add_argument("--token-file", type=Path)
    teams_graph_loop.add_argument("--poll-seconds", type=int, default=5)
    teams_graph_loop.set_defaults(func=cmd_teams_graph_connector_loop)

    teams_graph_ingress_once = subcommands.add_parser("teams-graph-ingress-once")
    teams_graph_ingress_once.add_argument("--connector", default="teams")
    teams_graph_ingress_once.add_argument("--channel", default="all-agents")
    teams_graph_ingress_once.add_argument("--connector-id", default="teams-graph-ingress")
    teams_graph_ingress_once.add_argument("--token")
    teams_graph_ingress_once.add_argument("--token-file", type=Path)
    teams_graph_ingress_once.add_argument("--max-messages", type=int, default=25)
    teams_graph_ingress_once.set_defaults(func=cmd_teams_graph_ingress_once)

    teams_graph_ingress_loop = subcommands.add_parser("teams-graph-ingress-loop")
    teams_graph_ingress_loop.add_argument("--connector", default="teams")
    teams_graph_ingress_loop.add_argument("--channel", default="all-agents")
    teams_graph_ingress_loop.add_argument("--connector-id", default="teams-graph-ingress")
    teams_graph_ingress_loop.add_argument("--token")
    teams_graph_ingress_loop.add_argument("--token-file", type=Path)
    teams_graph_ingress_loop.add_argument("--max-messages", type=int, default=25)
    teams_graph_ingress_loop.add_argument("--poll-seconds", type=int, default=10)
    teams_graph_ingress_loop.set_defaults(func=cmd_teams_graph_ingress_loop)

    teams_bot_once = subcommands.add_parser("teams-bot-connector-once")
    teams_bot_once.add_argument("--connector", default="teams")
    teams_bot_once.add_argument("--channel", default="all")
    teams_bot_once.add_argument("--connector-id", default="teams-bot-connector")
    teams_bot_once.add_argument("--secret-root", type=Path, default=state_root / "secrets")
    teams_bot_once.add_argument("--service-url", default="https://smba.trafficmanager.net/teams")
    teams_bot_once.set_defaults(func=cmd_teams_bot_connector_once)

    teams_bot_loop = subcommands.add_parser("teams-bot-connector-loop")
    teams_bot_loop.add_argument("--connector", default="teams")
    teams_bot_loop.add_argument("--channel", default="all")
    teams_bot_loop.add_argument("--connector-id", default="teams-bot-connector")
    teams_bot_loop.add_argument("--secret-root", type=Path, default=state_root / "secrets")
    teams_bot_loop.add_argument("--service-url", default="https://smba.trafficmanager.net/teams")
    teams_bot_loop.add_argument("--poll-seconds", type=int, default=5)
    teams_bot_loop.set_defaults(func=cmd_teams_bot_connector_loop)

    teams_listener = subcommands.add_parser("teams-bot-listener")
    teams_listener.add_argument("--host", default="0.0.0.0")
    teams_listener.add_argument("--port", type=int, default=3978)
    teams_listener.add_argument("--connector", default="teams")
    teams_listener.add_argument("--connector-id", default="teams-bot-listener")
    teams_listener.add_argument("--secret-root", type=Path, default=state_root / "secrets")
    teams_listener.set_defaults(func=cmd_teams_bot_listener)
    return parser


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
