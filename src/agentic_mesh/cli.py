from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from threading import Thread
from typing import Any

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
from agentic_mesh.controller_auth import ControllerAuthService
from agentic_mesh.controller_auth import serve_controller_auth
from agentic_mesh.document_library import build_document_manifest
from agentic_mesh.document_library import document_library_context
from agentic_mesh.document_library import render_flow_mermaid
from agentic_mesh.document_library import resolve_document_library_root
from agentic_mesh.document_library import write_document_manifest
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
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import SourceAnchor
from agentic_mesh.work_queue import WorkQueueError
from agentic_mesh.workers import ConfiguredWorkerAdapter


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
        document_library_root=project_document_library_root(
            effective_workspace_root,
            mesh_config,
        ),
    )
    lifecycle = LifecycleStore(state_root, mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store=message_store,
        artifact_store=artifact_store,
        journal=journal,
        project=mesh_config.project,
        worker=ConfiguredWorkerAdapter(
            project=mesh_config.project,
            auth_methods=mesh_config.auth_methods,
            workspace_root=effective_workspace_root,
            state_root=state_root,
        ),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    return mesh_config, journal, message_store, connector_outbox, lifecycle, runtime


def project_workspace_root(base_workspace_root: Path, mesh_config) -> Path:
    configured_root = Path(mesh_config.project.workspace.root)
    if configured_root.is_absolute():
        return configured_root
    return (base_workspace_root / configured_root).resolve()


def project_document_library_root(effective_workspace_root: Path, mesh_config) -> Path:
    return resolve_document_library_root(
        effective_workspace_root,
        mesh_config.project.document_library,
    )


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
                "document_library": {
                    **document_library_context(
                        project_workspace_root(args.workspace_root, mesh_config),
                        mesh_config.project,
                    ),
                },
                "meshes": sorted(mesh_config.project.meshes),
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
                "auth_credentials": sorted(mesh_config.project.auth_credentials),
                "instances": sorted(mesh_config.instances),
                "role_auth": {
                    role_id: (
                        {
                            "credential_ref": role.worker.auth.credential_ref,
                            "method": role.worker.auth.method,
                        }
                        if role.worker.auth
                        else None
                    )
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
    work_queue = FileWorkQueueStore(args.state_root, mesh_config.project.project_id, journal)
    work_queue_counts = work_queue.status_counts()
    for owner_role, statuses in work_queue_counts.items():
        for queue_status, count in statuses.items():
            telemetry.set_gauge(
                "agentic_mesh.work_queue.depth",
                count,
                {
                    "project_id": mesh_config.project.project_id,
                    "owner_role": owner_role,
                    "queue_status": queue_status,
                },
            )
    status = {
        "organization_id": mesh_config.organization.organization_id,
        "global_language": mesh_config.organization.global_language,
        "project_id": mesh_config.project.project_id,
        "workspace_root": str(project_workspace_root(args.workspace_root, mesh_config)),
        "document_library_root": str(
            project_document_library_root(
                project_workspace_root(args.workspace_root, mesh_config),
                mesh_config,
            )
        ),
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
        "work_queue": work_queue_counts,
        "journal_path": str(journal.path),
    }
    print(json.dumps(status, indent=2))
    return 0


def cmd_document_manifest(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    workspace = project_workspace_root(args.workspace_root, mesh_config)
    configure_component_telemetry(mesh_config, "cli")
    if args.write:
        manifest_path = write_document_manifest(workspace, mesh_config.project)
        print(json.dumps({"manifest_path": str(manifest_path)}, indent=2))
    else:
        print(json.dumps(build_document_manifest(mesh_config.project), indent=2))
    return 0


def cmd_flow_mermaid(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    configure_component_telemetry(mesh_config, "cli")
    output = render_flow_mermaid(mesh_config.project.flow)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(json.dumps({"output": str(args.output)}, indent=2))
    else:
        print(output, end="")
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


def _credential_for_cli(args):
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    credential = mesh_config.project.auth_credentials.get(args.credential)
    if credential is None:
        raise SystemExit(
            f"Unknown auth credential `{args.credential}` in project "
            f"`{mesh_config.project.project_id}`."
        )
    method = mesh_config.auth_methods[credential.method]
    return mesh_config, credential, method


def _state_secret_path(state_root: Path, secret_ref: str) -> Path:
    return state_root / "secrets" / secret_ref


def _state_mount_path(state_root: Path, mount_ref: str) -> Path:
    return state_root / "worker_mounts" / mount_ref


def cmd_auth_store_secret(args) -> int:
    _, credential, method = _credential_for_cli(args)
    if not method.requires_secret_ref or not credential.secret_ref:
        raise SystemExit(
            f"Auth credential `{credential.credential_id}` does not use a secret_ref."
        )

    if args.value_env:
        secret_value = os.getenv(args.value_env, "")
    elif not sys.stdin.isatty():
        secret_value = sys.stdin.read()
    else:
        secret_value = getpass.getpass(
            f"Secret value for {credential.credential_id}: "
        )
    secret_value = secret_value.strip()
    if not secret_value:
        raise SystemExit("No secret value was provided.")

    secret_path = _state_secret_path(args.state_root, credential.secret_ref)
    if secret_path.exists() and not args.overwrite:
        raise SystemExit(
            f"Secret `{credential.secret_ref}` already exists. Use --overwrite to replace it."
        )
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(secret_value, encoding="utf-8")
    try:
        secret_path.chmod(0o600)
    except OSError:
        pass
    print(
        json.dumps(
            {
                "credential": credential.credential_id,
                "method": credential.method,
                "secret_ref": credential.secret_ref,
                "path": str(secret_path),
                "redacted": True,
            },
            indent=2,
        )
    )
    return 0


def cmd_codex_auth_login(args) -> int:
    _, credential, method = _credential_for_cli(args)
    if method.method_id != "codex_oauth_cache" or not credential.mount_ref:
        raise SystemExit(
            "codex-auth-login requires a credential using method "
            "`codex_oauth_cache` with a mount_ref."
        )
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise SystemExit("Codex CLI is not installed or not on PATH.")

    mount_path = _state_mount_path(args.state_root, credential.mount_ref)
    mount_path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(credential.env)
    env["CODEX_HOME"] = str(mount_path)
    command = [codex_bin, "login"]
    if args.device_auth:
        command.append("--device-auth")
    completed = subprocess.run(command, env=env, check=False)
    if completed.returncode == 0:
        print(
            json.dumps(
                {
                    "credential": credential.credential_id,
                    "method": credential.method,
                    "mount_ref": credential.mount_ref,
                    "codex_home": str(mount_path),
                    "redacted": True,
                },
                indent=2,
            )
        )
    return completed.returncode


def cmd_codex_auth_status(args) -> int:
    _, credential, method = _credential_for_cli(args)
    if method.method_id != "codex_oauth_cache" or not credential.mount_ref:
        raise SystemExit(
            "codex-auth-status requires a credential using method "
            "`codex_oauth_cache` with a mount_ref."
        )
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise SystemExit("Codex CLI is not installed or not on PATH.")

    mount_path = _state_mount_path(args.state_root, credential.mount_ref)
    env = os.environ.copy()
    env.update(credential.env)
    env["CODEX_HOME"] = str(mount_path)
    return subprocess.run(
        [codex_bin, "login", "status"],
        env=env,
        check=False,
    ).returncode


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
    if getattr(args, "auth_admin_port", 0):
        service = ControllerAuthService(
            config_root=args.config_root,
            project_file=args.project_file,
            state_root=args.state_root,
            workspace_root=args.workspace_root,
        )
        thread = Thread(
            target=serve_controller_auth,
            kwargs={
                "host": args.auth_admin_host,
                "port": args.auth_admin_port,
                "service": service,
            },
            daemon=True,
        )
        thread.start()
    while True:
        cmd_control_plane_tick(args)
        time.sleep(args.poll_seconds)


def cmd_auth_admin_server(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    configure_component_telemetry(mesh_config, "auth-admin")
    service = ControllerAuthService(
        config_root=args.config_root,
        project_file=args.project_file,
        state_root=args.state_root,
        workspace_root=args.workspace_root,
    )
    serve_controller_auth(host=args.host, port=args.port, service=service)
    return 0


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
    token = load_graph_token(
        args.token,
        args.token_file,
        tenant_id=args.tenant_id,
        client_id=args.client_id,
        client_secret=args.client_secret,
        client_secret_file=args.client_secret_file,
    )
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
    mesh_config, journal, message_store, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "teams-ingress")
    connector_config = mesh_config.project.connectors[args.connector]
    token = load_graph_token(
        args.token,
        args.token_file,
        tenant_id=args.tenant_id,
        client_id=args.client_id,
        client_secret=args.client_secret,
        client_secret_file=args.client_secret_file,
    )
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id=args.connector_id,
        project_id=mesh_config.project.project_id,
        state_root=args.state_root,
        connector_config=connector_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token=token,
        connector_outbox=connector_outbox,
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


def _work_queue_store(args) -> tuple[Any, EventJournal, FileMessageStore, FileWorkQueueStore]:
    mesh_config, journal, message_store, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "cli")
    return (
        mesh_config,
        journal,
        message_store,
        FileWorkQueueStore(args.state_root, mesh_config.project.project_id, journal),
    )


def cmd_work_queue_capture(args) -> int:
    _, _, _, work_queue = _work_queue_store(args)
    anchor = SourceAnchor(
        connector_type=args.connector_type,
        connector_id=args.connector_id,
        source_scope=args.source_scope,
        source_message_id=args.source_message_id,
        actor=args.actor,
        received_at=args.received_at or _now_for_cli(),
        display_label=args.display_label or args.source_scope,
        external_url=args.external_url,
    )
    raw_payload = json.loads(args.raw_payload) if args.raw_payload else None
    item = work_queue.capture(
        title=args.title,
        summary=args.summary,
        owner_role=args.owner_role,
        source_anchor=anchor,
        recommended_work_item_type=args.work_item_type,
        idempotency_key=args.idempotency_key,
        raw_payload=raw_payload,
        retain_raw_payload=args.retain_raw_payload,
        metadata={"source": "cli"},
    )
    print(json.dumps(item.redacted_summary(), indent=2, sort_keys=True))
    return 0


def cmd_work_queue_list(args) -> int:
    _, _, _, work_queue = _work_queue_store(args)
    items = [
        item.redacted_summary()
        for item in work_queue.list_items()
        if (args.status is None or item.status == args.status)
        and (args.owner_role is None or item.owner_role == args.owner_role)
    ]
    print(json.dumps({"items": items}, indent=2, sort_keys=True))
    return 0


def cmd_work_queue_show(args) -> int:
    _, _, _, work_queue = _work_queue_store(args)
    item = work_queue.get(args.queue_item_id)
    if item is None:
        raise SystemExit(f"Unknown queue item `{args.queue_item_id}`")
    if args.support:
        try:
            result = work_queue.support_read(
                args.queue_item_id,
                actor=args.actor or "",
                reason=args.reason or "",
                correlation_id=args.correlation_id or "",
            )
        except WorkQueueError as exc:
            print(
                json.dumps({"error": str(exc)}, indent=2, sort_keys=True),
                file=sys.stderr,
            )
            return 1
    else:
        result = item.redacted_summary()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_work_queue_transition(args) -> int:
    _, _, _, work_queue = _work_queue_store(args)
    try:
        item = work_queue.transition(
            args.queue_item_id,
            args.status,
            actor_role=args.actor_role,
            reason=args.reason,
            correlation_id=args.correlation_id,
        )
    except WorkQueueError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(item.redacted_summary(), indent=2, sort_keys=True))
    return 0


def cmd_work_queue_readiness(args) -> int:
    _, _, _, work_queue = _work_queue_store(args)
    evidence = json.loads(args.evidence)
    try:
        item = work_queue.mark_readiness(
            args.queue_item_id,
            actor_role=args.actor_role,
            evidence=evidence,
            correlation_id=args.correlation_id,
        )
    except WorkQueueError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(item.redacted_summary(), indent=2, sort_keys=True))
    return 0


def cmd_work_queue_promote(args) -> int:
    mesh_config, _, message_store, work_queue = _work_queue_store(args)
    lifecycle_state = args.lifecycle_state or mesh_config.project.flow.entry_state
    target_role = args.target_role or mesh_config.project.flow.states[lifecycle_state].owner_role
    try:
        promotion = work_queue.promote(
            args.queue_item_id,
            actor_role=args.actor_role,
            message_store=message_store,
            target_role=target_role,
            lifecycle_state=lifecycle_state,
            work_item_id=args.work_item_id,
            work_item_type=args.work_item_type,
            message_type=args.message_type,
            idempotency_key=args.idempotency_key,
        )
    except WorkQueueError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(asdict(promotion), indent=2, sort_keys=True))
    return 0


def cmd_work_queue_purge(args) -> int:
    _, _, _, work_queue = _work_queue_store(args)
    try:
        result = work_queue.purge_raw(
            queue_item_id=args.queue_item_id,
            dry_run=not args.execute,
            actor=args.actor,
            reason=args.reason,
            correlation_id=args.correlation_id,
        )
    except WorkQueueError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _now_for_cli() -> str:
    from agentic_mesh.models import utc_now_iso

    return utc_now_iso()


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

    work_queue = subcommands.add_parser("work-queue")
    work_queue_commands = work_queue.add_subparsers(required=True)

    queue_capture = work_queue_commands.add_parser("capture")
    queue_capture.add_argument("--title", required=True)
    queue_capture.add_argument("--summary", required=True)
    queue_capture.add_argument("--owner-role", required=True)
    queue_capture.add_argument("--work-item-type", default="spike")
    queue_capture.add_argument("--connector-type", default="cli")
    queue_capture.add_argument("--connector-id", default="local-cli")
    queue_capture.add_argument("--source-scope", default="cli")
    queue_capture.add_argument("--source-message-id")
    queue_capture.add_argument("--actor")
    queue_capture.add_argument("--received-at")
    queue_capture.add_argument("--display-label")
    queue_capture.add_argument("--external-url")
    queue_capture.add_argument("--idempotency-key")
    queue_capture.add_argument("--raw-payload")
    queue_capture.add_argument("--retain-raw-payload", action="store_true")
    queue_capture.set_defaults(func=cmd_work_queue_capture)

    queue_list = work_queue_commands.add_parser("list")
    queue_list.add_argument("--status")
    queue_list.add_argument("--owner-role")
    queue_list.set_defaults(func=cmd_work_queue_list)

    queue_show = work_queue_commands.add_parser("show")
    queue_show.add_argument("--queue-item-id", required=True)
    queue_show.add_argument("--support", action="store_true")
    queue_show.add_argument("--actor")
    queue_show.add_argument("--reason")
    queue_show.add_argument("--correlation-id")
    queue_show.set_defaults(func=cmd_work_queue_show)

    queue_transition = work_queue_commands.add_parser("transition")
    queue_transition.add_argument("--queue-item-id", required=True)
    queue_transition.add_argument("--status", required=True)
    queue_transition.add_argument("--actor-role", required=True)
    queue_transition.add_argument("--reason")
    queue_transition.add_argument("--correlation-id")
    queue_transition.set_defaults(func=cmd_work_queue_transition)

    queue_readiness = work_queue_commands.add_parser("readiness")
    queue_readiness.add_argument("--queue-item-id", required=True)
    queue_readiness.add_argument("--actor-role", required=True)
    queue_readiness.add_argument("--evidence", required=True)
    queue_readiness.add_argument("--correlation-id")
    queue_readiness.set_defaults(func=cmd_work_queue_readiness)

    queue_promote = work_queue_commands.add_parser("promote")
    queue_promote.add_argument("--queue-item-id", required=True)
    queue_promote.add_argument("--actor-role", default="promotion-service")
    queue_promote.add_argument("--target-role")
    queue_promote.add_argument("--lifecycle-state")
    queue_promote.add_argument("--work-item-id")
    queue_promote.add_argument("--work-item-type")
    queue_promote.add_argument("--message-type", default="sdlc.intake")
    queue_promote.add_argument("--idempotency-key")
    queue_promote.set_defaults(func=cmd_work_queue_promote)

    queue_purge = work_queue_commands.add_parser("purge")
    queue_purge.add_argument("--queue-item-id")
    queue_purge.add_argument("--execute", action="store_true")
    queue_purge.add_argument("--actor")
    queue_purge.add_argument("--reason")
    queue_purge.add_argument("--correlation-id")
    queue_purge.set_defaults(func=cmd_work_queue_purge)

    document_manifest = subcommands.add_parser("document-manifest")
    document_manifest.add_argument("--write", action="store_true")
    document_manifest.set_defaults(func=cmd_document_manifest)

    flow_mermaid = subcommands.add_parser("flow-mermaid")
    flow_mermaid.add_argument("--output", type=Path)
    flow_mermaid.set_defaults(func=cmd_flow_mermaid)

    auth_plan = subcommands.add_parser("auth-plan")
    auth_plan.add_argument("--instance")
    auth_plan.set_defaults(func=cmd_auth_plan)

    auth_store = subcommands.add_parser("auth-store-secret")
    auth_store.add_argument("--credential", required=True)
    auth_store.add_argument(
        "--value-env",
        help="Read the secret value from this environment variable instead of stdin.",
    )
    auth_store.add_argument("--overwrite", action="store_true")
    auth_store.set_defaults(func=cmd_auth_store_secret)

    codex_login = subcommands.add_parser("codex-auth-login")
    codex_login.add_argument("--credential", required=True)
    codex_login.add_argument(
        "--device-auth",
        action="store_true",
        help="Use Codex device authentication for terminals without browser launch.",
    )
    codex_login.set_defaults(func=cmd_codex_auth_login)

    codex_status = subcommands.add_parser("codex-auth-status")
    codex_status.add_argument("--credential", required=True)
    codex_status.set_defaults(func=cmd_codex_auth_status)

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
    control_loop.add_argument("--auth-admin-host", default="127.0.0.1")
    control_loop.add_argument("--auth-admin-port", type=int, default=0)
    control_loop.set_defaults(func=cmd_control_plane_loop)

    auth_admin = subcommands.add_parser("auth-admin-server")
    auth_admin.add_argument("--host", default="127.0.0.1")
    auth_admin.add_argument("--port", type=int, default=8080)
    auth_admin.set_defaults(func=cmd_auth_admin_server)

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
    teams_graph_once.add_argument("--tenant-id")
    teams_graph_once.add_argument("--client-id")
    teams_graph_once.add_argument("--client-secret")
    teams_graph_once.add_argument("--client-secret-file", type=Path)
    teams_graph_once.set_defaults(func=cmd_teams_graph_connector_once)

    teams_graph_loop = subcommands.add_parser("teams-graph-connector-loop")
    teams_graph_loop.add_argument("--connector", default="teams")
    teams_graph_loop.add_argument("--channel", default="all")
    teams_graph_loop.add_argument("--connector-id", default="teams-graph-connector")
    teams_graph_loop.add_argument("--token")
    teams_graph_loop.add_argument("--token-file", type=Path)
    teams_graph_loop.add_argument("--tenant-id")
    teams_graph_loop.add_argument("--client-id")
    teams_graph_loop.add_argument("--client-secret")
    teams_graph_loop.add_argument("--client-secret-file", type=Path)
    teams_graph_loop.add_argument("--poll-seconds", type=int, default=5)
    teams_graph_loop.set_defaults(func=cmd_teams_graph_connector_loop)

    teams_graph_ingress_once = subcommands.add_parser("teams-graph-ingress-once")
    teams_graph_ingress_once.add_argument("--connector", default="teams")
    teams_graph_ingress_once.add_argument("--channel", default="all-agents")
    teams_graph_ingress_once.add_argument("--connector-id", default="teams-graph-ingress")
    teams_graph_ingress_once.add_argument("--token")
    teams_graph_ingress_once.add_argument("--token-file", type=Path)
    teams_graph_ingress_once.add_argument("--tenant-id")
    teams_graph_ingress_once.add_argument("--client-id")
    teams_graph_ingress_once.add_argument("--client-secret")
    teams_graph_ingress_once.add_argument("--client-secret-file", type=Path)
    teams_graph_ingress_once.add_argument("--max-messages", type=int, default=25)
    teams_graph_ingress_once.set_defaults(func=cmd_teams_graph_ingress_once)

    teams_graph_ingress_loop = subcommands.add_parser("teams-graph-ingress-loop")
    teams_graph_ingress_loop.add_argument("--connector", default="teams")
    teams_graph_ingress_loop.add_argument("--channel", default="all-agents")
    teams_graph_ingress_loop.add_argument("--connector-id", default="teams-graph-ingress")
    teams_graph_ingress_loop.add_argument("--token")
    teams_graph_ingress_loop.add_argument("--token-file", type=Path)
    teams_graph_ingress_loop.add_argument("--tenant-id")
    teams_graph_ingress_loop.add_argument("--client-id")
    teams_graph_ingress_loop.add_argument("--client-secret")
    teams_graph_ingress_loop.add_argument("--client-secret-file", type=Path)
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
