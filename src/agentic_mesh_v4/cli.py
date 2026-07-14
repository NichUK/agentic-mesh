from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agentic_mesh_v4.agent_config import materialize_agent_configs
from agentic_mesh_v4.artifact_preflight import ComposeProbeRunner
from agentic_mesh_v4.artifact_preflight import RoleArtifactPreflight
from agentic_mesh_v4.auto_dispatch import requires_dispatch_path
from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import WebSocketTransport
from agentic_mesh_v4.codex_protocol import app_server_healthz
from agentic_mesh_v4.compose import render_compose
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.decision_records import DecisionRequest
from agentic_mesh_v4.decision_records import cancel_decision
from agentic_mesh_v4.decision_records import deliver_pending_decision_cards
from agentic_mesh_v4.decision_records import link_decision
from agentic_mesh_v4.decision_records import record_card_delivery_attempt
from agentic_mesh_v4.decision_records import recalculate_sla_states
from agentic_mesh_v4.decision_records import reconcile_resolved_decision_notifications
from agentic_mesh_v4.decision_records import render_decision_card
from agentic_mesh_v4.decision_records import request_decision
from agentic_mesh_v4.decision_records import retry_failed_card_updates
from agentic_mesh_v4.decision_records import resolve_decision
from agentic_mesh_v4.documents import DocumentWriteRequest
from agentic_mesh_v4.documents import write_artifact
from agentic_mesh_v4.enterprise_architecture import materialize_enterprise_architecture_portfolio
from agentic_mesh_v4.flow import ARCHITECTURE_DOMAINS
from agentic_mesh_v4.handoff_lifecycle import accept_handoff
from agentic_mesh_v4.handoff_lifecycle import block_handoff
from agentic_mesh_v4.handoff_lifecycle import cancel_handoff
from agentic_mesh_v4.handoff_lifecycle import complete_handoff
from agentic_mesh_v4.handoff_lifecycle import create_handoff
from agentic_mesh_v4.handoff_lifecycle import supersede_handoff
from agentic_mesh_v4.lifecycle import ComposeLifecycle
from agentic_mesh_v4.onedrive_sync import sync_local_documents_to_onedrive
from agentic_mesh_v4.runtime import V4Runtime
from agentic_mesh_v4.server import serve
from agentic_mesh_v4.teams_delivery import TeamsReplySender
from agentic_mesh_v4.topology import validate_runtime_topology
from agentic_mesh_v4.watchdog import WatchdogThresholds
from agentic_mesh_v4.watchdog import run_watchdog_sweep


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-mesh-v4")
    parser.add_argument("--db", default=os.environ.get("AGENTIC_MESH_DATABASE_URL"))
    parser.add_argument("--project-config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")

    reset_state = subparsers.add_parser("reset-state")
    reset_state.add_argument("--confirm", required=True)
    reset_state.add_argument("--document-root", type=Path)

    materialize = subparsers.add_parser("materialize-agent-configs")
    materialize.add_argument("--agent-config-root", type=Path, required=True)
    materialize.add_argument("--role-templates-dir", type=Path, default=Path("config/roles"))

    materialize_ea = subparsers.add_parser("materialize-enterprise-architecture")
    materialize_ea.add_argument("--document-root", type=Path)
    materialize_ea.add_argument("--legacy-root", type=Path)

    compose = subparsers.add_parser("render-compose")
    compose.add_argument("--output", type=Path, required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8100)
    serve_parser.add_argument("--document-root", type=Path, default=Path("documents"))

    enqueue = subparsers.add_parser("enqueue-message")
    enqueue.add_argument("--target-role", default="project-manager")
    enqueue.add_argument("--text", required=True)
    enqueue.add_argument("--source", default="api")
    enqueue.add_argument("--steering", action="store_true")

    dispatch = subparsers.add_parser("dispatch-once")
    dispatch.add_argument("--role-id", required=True)
    dispatch.add_argument("--token-file", type=Path)

    dispatch_loop = subparsers.add_parser("dispatch-loop")
    dispatch_loop.add_argument("--agent-config-root", type=Path, required=True)
    dispatch_loop.add_argument("--poll-interval-seconds", type=float, default=2.0)
    dispatch_loop.add_argument("--compose-file", type=Path, action="append", default=[])
    dispatch_loop.add_argument("--compose-project-name")
    dispatch_loop.add_argument("--compose-env-file", type=Path)
    dispatch_loop.add_argument("--compose-working-directory", type=Path)
    dispatch_loop.add_argument("--active-turn-stale-seconds", type=float, default=900.0)
    dispatch_loop.add_argument("--dispatch-workers", type=int, default=8)
    dispatch_loop.add_argument("--wake", action="store_true")
    dispatch_loop.add_argument("--once", action="store_true")

    watchdog_loop = subparsers.add_parser("watchdog-loop")
    watchdog_loop.add_argument("--compose-file", type=Path, action="append", default=[])
    watchdog_loop.add_argument("--compose-project-name")
    watchdog_loop.add_argument("--compose-env-file", type=Path)
    watchdog_loop.add_argument("--compose-working-directory", type=Path)
    watchdog_loop.add_argument("--service", action="append", default=[])
    watchdog_loop.add_argument("--poll-interval-seconds", type=float, default=30.0)
    watchdog_loop.add_argument("--once", action="store_true")

    watchdog_sweep = subparsers.add_parser("watchdog-sweep")
    watchdog_sweep.add_argument("--initiator-role", required=True)
    watchdog_sweep.add_argument("--mode", choices=("manual", "scheduled"), default="manual")
    watchdog_sweep.add_argument("--queued-seconds", type=int)
    watchdog_sweep.add_argument("--delivering-seconds", type=int)
    watchdog_sweep.add_argument("--active-turn-seconds", type=int)
    watchdog_sweep.add_argument("--failed-seconds", type=int)
    watchdog_sweep.add_argument("--open-handoff-seconds", type=int)

    schema = subparsers.add_parser("generate-protocol-schema")
    schema.add_argument("--output", type=Path, required=True)
    schema.add_argument("--codex-bin", default="codex")

    subparsers.add_parser("status-json")

    sync_documents = subparsers.add_parser("sync-documents")
    sync_documents.add_argument("--local-root", type=Path, required=True)
    sync_documents.add_argument("--drive-id")

    safe_output = subparsers.add_parser("safe-output")
    safe_output_subparsers = safe_output.add_subparsers(dest="safe_output_command", required=True)

    work_item_update = safe_output_subparsers.add_parser("work-item-update")
    work_item_update.add_argument("--role-id", required=True)
    work_item_update.add_argument("--work-item-id", required=True)
    work_item_update.add_argument("--title")
    work_item_update.add_argument("--state")
    work_item_update.add_argument("--owner-role")
    work_item_update.add_argument("--next-action")
    work_item_update.add_argument("--message-id")
    work_item_update.add_argument("--turn-id")

    architecture_impact = safe_output_subparsers.add_parser("architecture-impact")
    architecture_impact.add_argument("--role-id", required=True)
    architecture_impact.add_argument("--work-item-id", required=True)
    architecture_impact.add_argument("--classification", choices=("none", "material", "uncertain"), required=True)
    architecture_impact.add_argument("--rationale", required=True)
    architecture_impact.add_argument(
        "--affected-domain",
        action="append",
        choices=tuple(sorted(ARCHITECTURE_DOMAINS)),
        default=[],
    )
    architecture_impact.add_argument("--decision-ref")
    architecture_impact.add_argument("--message-id")
    architecture_impact.add_argument("--turn-id")

    architecture_conformance = safe_output_subparsers.add_parser("architecture-conformance")
    architecture_conformance.add_argument("--role-id", required=True)
    architecture_conformance.add_argument("--work-item-id", required=True)
    architecture_conformance.add_argument(
        "--status",
        choices=("approved", "changes_requested", "exception"),
        required=True,
    )
    architecture_conformance.add_argument("--rationale", required=True)
    architecture_conformance.add_argument("--decision-ref")
    architecture_conformance.add_argument("--message-id")
    architecture_conformance.add_argument("--turn-id")

    artifact_link = safe_output_subparsers.add_parser("artifact-link")
    artifact_link.add_argument("--role-id", required=True)
    artifact_link.add_argument("--work-item-id", required=True)
    artifact_link.add_argument("--path", required=True)
    artifact_link.add_argument("--title", required=True)
    artifact_link.add_argument("--message-id")
    artifact_link.add_argument("--turn-id")

    document_write = safe_output_subparsers.add_parser("document-write-artifact")
    document_write.add_argument("--role-id", required=True)
    document_write.add_argument("--work-item-id", required=True)
    document_write.add_argument("--path", required=True)
    document_write.add_argument("--title", required=True)
    document_write.add_argument("--content", required=True)
    document_write.add_argument("--document-type")
    document_write.add_argument("--base-sha256")
    document_write.add_argument("--base-revision-id")
    document_write.add_argument("--base-etag")
    document_write.add_argument("--comment-metadata-status")
    document_write.add_argument("--comment-metadata-detail")
    document_write.add_argument("--source-ref")
    document_write.add_argument("--document-root", type=Path)
    document_write.add_argument("--message-id")
    document_write.add_argument("--turn-id")

    handoff = safe_output_subparsers.add_parser("handoff")
    handoff.add_argument("--from-role", required=True)
    handoff.add_argument("--to-role", required=True)
    handoff.add_argument("--work-item-id", required=True)
    handoff.add_argument("--state", required=True)
    handoff.add_argument("--next-action", required=True)
    handoff.add_argument("--reason", required=True)
    handoff.add_argument("--message")
    handoff.add_argument("--message-id")
    handoff.add_argument("--turn-id")

    handoff_accept = safe_output_subparsers.add_parser("handoff-accept")
    handoff_accept.add_argument("--handoff-id", required=True)
    handoff_accept.add_argument("--role-id", required=True)
    handoff_accept.add_argument("--reason", required=True)
    handoff_accept.add_argument("--message-id")
    handoff_accept.add_argument("--turn-id")

    handoff_complete = safe_output_subparsers.add_parser("handoff-complete")
    handoff_complete.add_argument("--handoff-id", required=True)
    handoff_complete.add_argument("--role-id", required=True)
    handoff_complete.add_argument("--next-state", required=True)
    handoff_complete.add_argument("--next-owner-role", required=True)
    handoff_complete.add_argument("--next-action", required=True)
    handoff_complete.add_argument("--reason", required=True)
    handoff_complete.add_argument("--message-id")
    handoff_complete.add_argument("--turn-id")

    handoff_supersede = safe_output_subparsers.add_parser("handoff-supersede")
    handoff_supersede.add_argument("--handoff-id", required=True)
    handoff_supersede.add_argument("--role-id", required=True)
    handoff_supersede.add_argument("--superseded-by-handoff-id", required=True)
    handoff_supersede.add_argument("--reason", required=True)
    handoff_supersede.add_argument("--message-id")
    handoff_supersede.add_argument("--turn-id")

    handoff_cancel = safe_output_subparsers.add_parser("handoff-cancel")
    handoff_cancel.add_argument("--handoff-id", required=True)
    handoff_cancel.add_argument("--role-id", required=True)
    handoff_cancel.add_argument("--reason", required=True)
    handoff_cancel.add_argument("--owner-role", required=True)
    handoff_cancel.add_argument("--next-action", required=True)
    handoff_cancel.add_argument("--message-id")
    handoff_cancel.add_argument("--turn-id")

    handoff_block = safe_output_subparsers.add_parser("handoff-block")
    handoff_block.add_argument("--handoff-id", required=True)
    handoff_block.add_argument("--role-id", required=True)
    handoff_block.add_argument("--reason", required=True)
    handoff_block.add_argument("--next-action", required=True)
    handoff_block.add_argument("--message-id")
    handoff_block.add_argument("--turn-id")

    memory = safe_output_subparsers.add_parser("memory-record")
    memory.add_argument("--role-id", required=True)
    memory.add_argument("--summary", required=True)
    memory.add_argument("--source-ref", required=True)
    memory.add_argument("--scope", choices=("role", "institutional", "both"), default="role")
    memory.add_argument("--tags", action="append", default=[])
    memory.add_argument("--status", default="active")
    memory.add_argument("--message-id")
    memory.add_argument("--turn-id")

    decision_request = safe_output_subparsers.add_parser("decision-request")
    decision_request.add_argument("--role-id", required=True)
    decision_request.add_argument("--work-item-id", required=True)
    decision_request.add_argument("--owner-role", required=True)
    decision_request.add_argument("--authority-label", required=True)
    decision_request.add_argument("--authorized-responders-json", required=True)
    decision_request.add_argument("--decision-type", required=True)
    decision_request.add_argument("--title", required=True)
    decision_request.add_argument("--question", required=True)
    decision_request.add_argument("--options-json", required=True)
    decision_request.add_argument("--recommended-option")
    decision_request.add_argument("--tradeoffs-json", default="{}")
    decision_request.add_argument("--source-refs-json", default="[]")
    decision_request.add_argument("--affected-refs-json", default="[]")
    decision_request.add_argument("--link-effects-json", default="[]")
    decision_request.add_argument("--sla-due-at")
    decision_request.add_argument("--conversation-ref")
    decision_request.add_argument("--teams-activity-json")
    decision_request.add_argument("--detail-url")
    decision_request.add_argument("--message-id")
    decision_request.add_argument("--turn-id")

    decision_resolve = safe_output_subparsers.add_parser("decision-resolve")
    decision_resolve.add_argument("--role-id", required=True)
    decision_resolve.add_argument("--decision-id", required=True)
    decision_resolve.add_argument("--responder-ref", required=True)
    decision_resolve.add_argument("--selected-option", required=True)
    decision_resolve.add_argument("--rationale", default="")
    decision_resolve.add_argument("--idempotency-key")
    decision_resolve.add_argument("--delivery-id")
    decision_resolve.add_argument("--message-id")
    decision_resolve.add_argument("--turn-id")

    decision_cancel = safe_output_subparsers.add_parser("decision-cancel")
    decision_cancel.add_argument("--role-id", required=True)
    decision_cancel.add_argument("--decision-id", required=True)
    decision_cancel.add_argument("--responder-ref", required=True)
    decision_cancel.add_argument("--rationale", default="")
    decision_cancel.add_argument("--message-id")
    decision_cancel.add_argument("--turn-id")

    decision_link = safe_output_subparsers.add_parser("decision-link")
    decision_link.add_argument("--role-id", required=True)
    decision_link.add_argument("--decision-id", required=True)
    decision_link.add_argument("--target-type", required=True)
    decision_link.add_argument("--target-id", required=True)
    decision_link.add_argument("--link-type", required=True)
    decision_link.add_argument("--effect-summary", required=True)
    decision_link.add_argument("--effect-payload-json", default="{}")
    decision_link.add_argument("--message-id")
    decision_link.add_argument("--turn-id")

    decision_sla_sweep = safe_output_subparsers.add_parser("decision-sla-sweep")
    decision_sla_sweep.add_argument("--role-id", required=True)
    decision_sla_sweep.add_argument("--now")
    decision_sla_sweep.add_argument("--due-soon-seconds", type=int, default=3600)
    decision_sla_sweep.add_argument("--escalate-after-seconds", type=int, default=86400)
    decision_sla_sweep.add_argument("--message-id")
    decision_sla_sweep.add_argument("--turn-id")

    decision_delivery_sweep = safe_output_subparsers.add_parser("decision-delivery-sweep")
    decision_delivery_sweep.add_argument("--role-id", required=True)
    decision_delivery_sweep.add_argument("--limit", type=int, default=20)
    decision_delivery_sweep.add_argument("--detail-url")
    decision_delivery_sweep.add_argument("--message-id")
    decision_delivery_sweep.add_argument("--turn-id")

    decision_update_retry = safe_output_subparsers.add_parser("decision-update-retry")
    decision_update_retry.add_argument("--role-id", required=True)
    decision_update_retry.add_argument("--limit", type=int, default=20)
    decision_update_retry.add_argument("--detail-url")
    decision_update_retry.add_argument("--message-id")
    decision_update_retry.add_argument("--turn-id")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if _forward_safe_output_to_role_proxy(args=args, argv=argv):
        return
    if args.command in {"serve", "dispatch-loop", "watchdog-loop"}:
        validate_runtime_topology()
    project_config = load_project_config(args.project_config)
    if args.command == "materialize-agent-configs":
        written = materialize_agent_configs(
            project_config=project_config,
            output_root=args.agent_config_root,
            role_templates_dir=args.role_templates_dir,
        )
        _ensure_ws_tokens(args.agent_config_root, project_config)
        _print_json({"written": len(written)})
        return
    if args.command == "render-compose":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_compose(project_config), encoding="utf-8")
        _print_json({"output": str(args.output)})
        return
    if args.command == "materialize-enterprise-architecture":
        result = materialize_enterprise_architecture_portfolio(
            project_config=project_config,
            document_root=args.document_root or Path(project_config.document_root),
            legacy_root=args.legacy_root,
        )
        _print_json(
            {
                "created": result.created,
                "preserved": result.preserved,
                "migrated": result.migrated,
                "manifest_path": result.manifest_path,
            }
        )
        return
    db = V4Database(args.db)
    try:
        db.migrate()
        runtime = V4Runtime(db=db, project_config=project_config)
        runtime.register_roles()
        if args.command == "init-db":
            _print_json({"status": "ok", "roles": len(project_config.roles)})
            return
        if args.command == "reset-state":
            if args.confirm != "CLEAN-SLATE":
                raise ValueError("reset-state requires --confirm CLEAN-SLATE")
            db.reset_runtime_state()
            runtime.register_roles()
            deleted_paths = _reset_document_state(args.document_root)
            _print_json({"status": "reset", "registered_roles": len(project_config.roles), "deleted_paths": deleted_paths})
            return
        if args.command == "serve":
            serve(
                db_path=args.db,
                project_config=project_config,
                document_root=args.document_root,
                host=args.host,
                port=args.port,
            )
            return
        if args.command == "enqueue-message":
            message_id = runtime.enqueue_conversation(
                target_role=args.target_role,
                text=args.text,
                source=args.source,
                steering=args.steering,
            )
            _print_json({"message_id": message_id})
            return
        if args.command == "dispatch-once":
            role = project_config.role(args.role_id)
            token = args.token_file.read_text(encoding="utf-8").strip() if args.token_file else None

            def factory(role_id: str) -> CodexAppServerClient:
                role_config = project_config.role(role_id)
                return CodexAppServerClient(
                    WebSocketTransport(
                        f"ws://{role_config.service_name}:{role_config.codex_port}",
                        bearer_token=token,
                    )
                )

            result = V4Runtime(
                db=db,
                project_config=project_config,
                client_factory=factory,
                document_syncer=_document_syncer(args.project_config),
                agent_config_root=args.agent_config_root,
            ).dispatch_once(role_id=role.role_id)
            _print_json(_dispatch_result(result))
            return
        if args.command == "dispatch-loop":
            lifecycle = None
            if args.wake:
                lifecycle = ComposeLifecycle(
                    compose_files=tuple(args.compose_file),
                    project_name=args.compose_project_name,
                    env_file=args.compose_env_file,
                    working_directory=args.compose_working_directory,
                )
            if args.once:
                processed = _dispatch_available_messages(
                    db=db,
                    project_config=project_config,
                    project_config_path=args.project_config,
                    agent_config_root=args.agent_config_root,
                    lifecycle=lifecycle,
                )
                _print_json({"processed": processed})
                return
            _dispatch_loop_concurrent(
                db=db,
                project_config=project_config,
                project_config_path=args.project_config,
                agent_config_root=args.agent_config_root,
                lifecycle=lifecycle,
                active_turn_stale_seconds=args.active_turn_stale_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
                max_workers=args.dispatch_workers,
            )
            return
        if args.command == "watchdog-loop":
            lifecycle = ComposeLifecycle(
                compose_files=tuple(args.compose_file),
                project_name=args.compose_project_name,
                env_file=args.compose_env_file,
                working_directory=args.compose_working_directory,
            )
            services = tuple(args.service or ("runtime", "dispatcher"))
            while True:
                checked = _watchdog_services(lifecycle=lifecycle, services=services)
                decision_notifications = reconcile_resolved_decision_notifications(db=db)
                if args.once:
                    _print_json({"services": checked, "decision_notifications": decision_notifications})
                    return
                time.sleep(args.poll_interval_seconds)
            return
        if args.command == "watchdog-sweep":
            project_config.role(args.initiator_role)
            summary = run_watchdog_sweep(
                db=db,
                initiator_role=args.initiator_role,
                mode=args.mode,
                thresholds=_watchdog_thresholds_from_args(args),
            )
            _print_json(_watchdog_sweep_output(db=db, summary=summary))
            return
        if args.command == "generate-protocol-schema":
            args.output.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [args.codex_bin, "app-server", "generate-json-schema", "--experimental", "--out", str(args.output)],
                check=True,
            )
            _print_json({"output": str(args.output)})
            return
        if args.command == "status-json":
            _print_json(db.snapshot())
            return
        if args.command == "sync-documents":
            result = sync_local_documents_to_onedrive(
                project_config=args.project_config,
                local_root=args.local_root,
                drive_id=args.drive_id,
            )
            _print_json(
                {
                    "uploaded": result.uploaded,
                    "folders_created": result.folders_created,
                    "root_path": result.root_path,
                }
            )
            return
        if args.command == "safe-output":
            _handle_safe_output(args=args, db=db, project_config=project_config)
            return
    finally:
        db.close()


def _forward_safe_output_to_role_proxy(*, args, argv: list[str] | None) -> bool:
    socket_path = os.environ.get("AGENTIC_MESH_SAFE_OUTPUT_SOCKET", "").strip()
    if (
        args.command != "safe-output"
        or not socket_path
        or os.environ.get("AGENTIC_MESH_SAFE_OUTPUT_PROXY_BYPASS") == "1"
    ):
        return False
    request_argv = list(argv) if argv is not None else sys.argv[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(json.dumps({"argv": request_argv}).encode("utf-8") + b"\n")
            response_file = client.makefile("rb")
            raw_response = response_file.readline(8 * 1024 * 1024 + 1)
    except OSError as exc:
        raise RuntimeError(f"safe-output role proxy is unavailable at {socket_path}: {exc}") from exc
    if not raw_response or len(raw_response) > 8 * 1024 * 1024:
        raise RuntimeError("safe-output role proxy returned an invalid response")
    response = json.loads(raw_response.decode("utf-8"))
    stdout = str(response.get("stdout", ""))
    stderr = str(response.get("stderr", ""))
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    exit_code = int(response.get("exit_code", 1))
    if exit_code:
        raise SystemExit(exit_code)
    return True


def _ensure_ws_tokens(root: Path, project_config) -> None:
    for role in project_config.roles:
        token_path = root / role.role_id / "1" / "ws-token"
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")


def _role_instance_id(project_config, role_id: str) -> str:
    return project_config.role(role_id).role_instance_id


def _reset_document_state(document_root: Path | None) -> list[str]:
    if document_root is None:
        return []
    root = document_root.resolve()
    if not root.exists():
        return []
    deleted: list[str] = []
    allowed = {
        (root / "work-items").resolve(),
        (root / "delivery").resolve(),
        (root / "debug").resolve(),
    }
    for path in allowed:
        if not str(path).startswith(str(root)):
            raise ValueError(f"refusing to delete outside document root: {path}")
        if not path.exists():
            continue
        if path.is_dir():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink()
        deleted.append(str(path))
    for filename in ("work-items.md", "index.md"):
        path = (root / filename).resolve()
        if path.exists() and str(path).startswith(str(root)):
            path.unlink()
            deleted.append(str(path))
    (root / "work-items").mkdir(parents=True, exist_ok=True)
    (root / "work-items" / "index.md").write_text("# Work Items\n\nNo active work items.\n", encoding="utf-8")
    return deleted


def _dispatch_available_messages(
    *,
    db: V4Database,
    project_config,
    project_config_path: Path,
    agent_config_root: Path,
    lifecycle: ComposeLifecycle | None,
) -> int:
    processed = 0

    def factory(role_id: str) -> CodexAppServerClient:
        role_config = project_config.role(role_id)
        token_file = agent_config_root / role_id / "1" / "ws-token"
        token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else None
        return CodexAppServerClient(
            WebSocketTransport(
                f"ws://{role_config.service_name}:{role_config.codex_port}",
                bearer_token=token,
            )
        )

    runtime = V4Runtime(
        db=db,
        project_config=project_config,
        client_factory=factory,
        document_syncer=_document_syncer(project_config_path),
        agent_config_root=agent_config_root,
        artifact_preflight=(
            RoleArtifactPreflight(
                project_config=project_config,
                compose_text=_compose_text(lifecycle),
                probe_runner=ComposeProbeRunner(lifecycle),
            )
            if lifecycle is not None
            else None
        ),
    )
    runtime.register_roles()
    for role in project_config.roles:
        if lifecycle is not None and db.active_message_for_role(target_role=role.role_id) is not None:
            if not lifecycle.is_service_running(role.service_name):
                recovered = db.requeue_active_messages_for_role(
                    target_role=role.role_id,
                    summary=(
                        f"Recovered orphaned active delivery for {role.role_id}; "
                        f"compose service {role.service_name} is not running."
                    ),
                )
                if recovered:
                    print(
                        json.dumps(
                            {
                                "role_id": role.role_id,
                                "service_name": role.service_name,
                                "state": "recovered_orphaned_active",
                                "messages": recovered,
                            },
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                    )
        if not db.has_queued_messages(role_id=role.role_id):
            continue
        if lifecycle is not None:
            try:
                lifecycle.wake_service(role.service_name)
            except subprocess.CalledProcessError as exc:
                print(
                    json.dumps(
                        {
                            "role_id": role.role_id,
                            "service_name": role.service_name,
                            "state": "wake_failed",
                            "error": exc.stderr or exc.stdout or str(exc),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                continue
        result = runtime.dispatch_once(role_id=role.role_id)
        if result is not None:
            processed += 1
    return processed


def _document_syncer(project_config_path: Path):
    local_root = Path(os.environ.get("AGENTIC_MESH_DOCUMENT_ROOT", "/documents"))

    def sync() -> object:
        return sync_local_documents_to_onedrive(
            project_config=project_config_path,
            local_root=local_root,
        )

    return sync


def _dispatch_loop_concurrent(
    *,
    db: V4Database,
    project_config,
    project_config_path: Path,
    agent_config_root: Path,
    lifecycle: ComposeLifecycle | None,
    active_turn_stale_seconds: float,
    poll_interval_seconds: float,
    max_workers: int,
) -> None:
    active: dict[str, concurrent.futures.Future[_DispatchWorkerResult]] = {}
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor,
        concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-sync") as sync_executor,
    ):
        document_sync = _DocumentSyncCoordinator(
            project_config_path=project_config_path,
            executor=sync_executor,
        )
        # Reconcile any document changes left by an interrupted previous dispatcher.
        document_sync.request()
        while True:
            processed, sync_requested = _collect_completed_dispatches(active)
            if sync_requested:
                document_sync.request()
            sync_activity = document_sync.poll()
            scheduled = _schedule_available_dispatches(
                db=db,
                project_config=project_config,
                project_config_path=project_config_path,
                agent_config_root=agent_config_root,
                lifecycle=lifecycle,
                active_turn_stale_seconds=active_turn_stale_seconds,
                executor=executor,
                active=active,
            )
            if processed == 0 and scheduled == 0 and sync_activity == 0:
                time.sleep(poll_interval_seconds)


@dataclass(frozen=True)
class _DispatchWorkerResult:
    processed: int
    request_document_sync: bool = False


class _DocumentSyncCoordinator:
    """Coalesce document-library syncs without occupying a role delivery slot."""

    def __init__(
        self,
        *,
        project_config_path: Path,
        executor: concurrent.futures.Executor,
    ) -> None:
        self._project_config_path = project_config_path
        self._executor = executor
        self._future: concurrent.futures.Future[object] | None = None
        self._requested = False

    @property
    def running(self) -> bool:
        return self._future is not None and not self._future.done()

    def request(self) -> None:
        self._requested = True

    def poll(self) -> int:
        activity = 0
        if self._future is not None and self._future.done():
            try:
                result = self._future.result()
                print(
                    json.dumps(
                        {
                            "state": "document_sync_completed",
                            "uploaded": getattr(result, "uploaded", None),
                            "folders_created": getattr(result, "folders_created", None),
                            "root_path": getattr(result, "root_path", None),
                        },
                        sort_keys=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - document sync is eventually consistent.
                print(
                    json.dumps(
                        {
                            "state": "document_sync_failed",
                            "error": str(exc),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
            self._future = None
            activity += 1
        if self._future is None and self._requested:
            self._requested = False
            self._future = self._executor.submit(_document_syncer(self._project_config_path))
            activity += 1
        return activity


def _collect_completed_dispatches(
    active: dict[str, concurrent.futures.Future[_DispatchWorkerResult]],
) -> tuple[int, bool]:
    processed = 0
    sync_requested = False
    for role_id, future in list(active.items()):
        if not future.done():
            continue
        try:
            result = future.result()
            processed += result.processed
            sync_requested = sync_requested or result.request_document_sync
        except Exception as exc:  # noqa: BLE001 - dispatcher must survive role delivery failures.
            print(
                json.dumps(
                    {
                        "role_id": role_id,
                        "state": "dispatch_worker_failed",
                        "error": str(exc),
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        del active[role_id]
    return processed, sync_requested


def _schedule_available_dispatches(
    *,
    db: V4Database,
    project_config,
    project_config_path: Path,
    agent_config_root: Path,
    lifecycle: ComposeLifecycle | None,
    active_turn_stale_seconds: float,
    executor: concurrent.futures.Executor,
    active: dict[str, concurrent.futures.Future[_DispatchWorkerResult]],
) -> int:
    scheduled = 0
    for role in project_config.roles:
        if role.role_id in active:
            continue
        active_message = db.active_message_for_role(target_role=role.role_id)
        if active_message is not None:
            active_role_instance_id = str(active_message.get("locked_by") or role.role_instance_id)
            active_turn_id = db.active_turn_id_for_role_instance(
                role_instance_id=active_role_instance_id,
            )
            terminal_event = (
                db.terminal_agent_event_for_message(
                    message_id=str(active_message["message_id"]),
                    turn_id=active_turn_id,
                )
                if active_turn_id
                else None
            )
            recovered_stale = 0
            if terminal_event is None:
                recovered_stale = db.requeue_active_messages_for_role(
                    target_role=role.role_id,
                    stale_after_seconds=active_turn_stale_seconds,
                    summary=(
                        f"Recovered stale active delivery for {role.role_id}; "
                        f"message stayed active longer than {active_turn_stale_seconds:.0f} seconds."
                    ),
                )
            if recovered_stale and lifecycle is not None:
                _hibernate_recovered_role(lifecycle=lifecycle, role=role, recovered=recovered_stale)
            active_message = db.active_message_for_role(target_role=role.role_id)
        if lifecycle is not None and active_message is not None:
            if not lifecycle.is_service_running(role.service_name):
                recovered = db.requeue_active_messages_for_role(
                    target_role=role.role_id,
                    summary=(
                        f"Recovered orphaned active delivery for {role.role_id}; "
                        f"compose service {role.service_name} is not running."
                    ),
                )
                if recovered:
                    print(
                        json.dumps(
                            {
                                "role_id": role.role_id,
                                "service_name": role.service_name,
                                "state": "recovered_orphaned_active",
                                "messages": recovered,
                            },
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                    )
                active_message = db.active_message_for_role(target_role=role.role_id)
        has_queued_message = db.has_queued_messages(role_id=role.role_id)
        if active_message is None and not has_queued_message:
            continue
        if lifecycle is not None:
            try:
                if not _ensure_role_service_ready(lifecycle=lifecycle, role=role):
                    if active_message is not None:
                        recovered = db.requeue_active_messages_for_role(
                            target_role=role.role_id,
                            summary=(
                                f"Recovered active delivery for {role.role_id}; "
                                f"compose service {role.service_name} did not become healthy."
                            ),
                        )
                        if recovered:
                            _hibernate_recovered_role(lifecycle=lifecycle, role=role, recovered=recovered)
                    print(
                        json.dumps(
                            {
                                "role_id": role.role_id,
                                "service_name": role.service_name,
                                "state": "wake_health_timeout",
                                "message_state": "queued" if active_message is None else active_message["state"],
                            },
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                    )
                    continue
            except subprocess.CalledProcessError as exc:
                print(
                    json.dumps(
                        {
                            "role_id": role.role_id,
                            "service_name": role.service_name,
                            "state": "wake_failed",
                            "error": exc.stderr or exc.stdout or str(exc),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                continue
        active[role.role_id] = executor.submit(
            _dispatch_role_message,
            db_path=db.path,
            project_config_path=project_config_path,
            agent_config_root=agent_config_root,
            role_id=role.role_id,
        )
        scheduled += 1
    return scheduled


def _ensure_role_service_ready(
    *,
    lifecycle: ComposeLifecycle,
    role,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.5,
) -> bool:
    if not lifecycle.is_service_running(role.service_name):
        lifecycle.wake_service(role.service_name)
    endpoint = f"ws://{role.service_name}:{role.codex_port}"
    deadline = time.monotonic() + timeout_seconds
    while True:
        if app_server_healthz(endpoint):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_seconds)


def _dispatch_role_message(
    *,
    db_path: str,
    project_config_path: Path,
    agent_config_root: Path,
    role_id: str,
) -> _DispatchWorkerResult:
    worker_db = V4Database(db_path)
    try:
        worker_db.migrate()
        project_config = load_project_config(project_config_path)

        def factory(factory_role_id: str) -> CodexAppServerClient:
            role_config = project_config.role(factory_role_id)
            token_file = agent_config_root / factory_role_id / "1" / "ws-token"
            token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else None
            return CodexAppServerClient(
                WebSocketTransport(
                    f"ws://{role_config.service_name}:{role_config.codex_port}",
                    bearer_token=token,
                )
            )

        result = V4Runtime(
            db=worker_db,
            project_config=project_config,
            client_factory=factory,
            agent_config_root=agent_config_root,
        ).dispatch_once(role_id=role_id)
        return _DispatchWorkerResult(
            processed=1 if result is not None else 0,
            request_document_sync=result is not None and result.state == "completed",
        )
    finally:
        worker_db.close()


def _hibernate_recovered_role(*, lifecycle: ComposeLifecycle, role, recovered: int) -> None:
    try:
        lifecycle.hibernate_service(role.service_name)
    except subprocess.CalledProcessError as exc:
        print(
            json.dumps(
                {
                    "role_id": role.role_id,
                    "service_name": role.service_name,
                    "state": "stale_hibernate_failed",
                    "error": exc.stderr or exc.stdout or str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    print(
        json.dumps(
            {
                "role_id": role.role_id,
                "service_name": role.service_name,
                "state": "recovered_stale_active",
                "messages": recovered,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _watchdog_services(*, lifecycle: ComposeLifecycle, services: tuple[str, ...]) -> list[dict[str, str]]:
    checked: list[dict[str, str]] = []
    for service_name in services:
        try:
            if lifecycle.is_service_running(service_name):
                checked.append({"service": service_name, "state": "running"})
                continue
            lifecycle.wake_service(service_name)
            checked.append({"service": service_name, "state": "restarted"})
        except subprocess.CalledProcessError as exc:
            checked.append(
                {
                    "service": service_name,
                    "state": "restart_failed",
                    "error": exc.stderr or exc.stdout or str(exc),
                }
            )
        except Exception as exc:  # pragma: no cover - defensive guard path
            checked.append({"service": service_name, "state": "check_failed", "error": str(exc)})
    for item in checked:
        if item["state"] != "running":
            print(json.dumps(item, sort_keys=True), file=sys.stderr)
    return checked

def _compose_text(lifecycle: ComposeLifecycle | None) -> str | None:
    if lifecycle is None:
        return None
    for compose_file in lifecycle.compose_files:
        path = Path(compose_file)
        if path.exists():
            return path.read_text(encoding="utf-8")
    return None


def _watchdog_thresholds_from_args(args) -> WatchdogThresholds:
    defaults = WatchdogThresholds()
    return WatchdogThresholds(
        queued_seconds=args.queued_seconds if args.queued_seconds is not None else defaults.queued_seconds,
        delivering_seconds=args.delivering_seconds if args.delivering_seconds is not None else defaults.delivering_seconds,
        active_turn_seconds=args.active_turn_seconds if args.active_turn_seconds is not None else defaults.active_turn_seconds,
        failed_seconds=args.failed_seconds if args.failed_seconds is not None else defaults.failed_seconds,
        open_handoff_seconds=(
            args.open_handoff_seconds
            if args.open_handoff_seconds is not None
            else defaults.open_handoff_seconds
        ),
    )


def _watchdog_sweep_output(*, db: V4Database, summary: dict[str, object]) -> dict[str, object]:
    sweep_run_id = str(summary["sweep_run_id"])
    findings = [
        _watchdog_finding_output(dict(row))
        for row in db.connection.execute(
            """
            SELECT *
            FROM watchdog_findings
            WHERE sweep_run_id=? AND state='open'
            ORDER BY severity DESC, updated_at DESC
            """,
            (sweep_run_id,),
        )
    ]
    severe_findings = [item for item in findings if item["severity"] == "high"]
    suggested_next_actions = []
    for finding in severe_findings:
        next_action = str(finding.get("next_action") or "")
        if next_action and next_action not in suggested_next_actions:
            suggested_next_actions.append(next_action)
    return {
        **summary,
        "automatic_mutation": {
            "enabled": False,
            "reason": "watchdog-sweep records diagnostics only; PM owns manual recovery or handoff until remediation tools are complete.",
        },
        "severe_findings": severe_findings,
        "suggested_next_actions": suggested_next_actions,
    }


def _watchdog_finding_output(row: dict[str, object]) -> dict[str, object]:
    evidence = _json_object(row.get("evidence_json"))
    output = {
        "finding_id": row.get("finding_id"),
        "finding_key": row.get("finding_key"),
        "finding_type": row.get("finding_type"),
        "severity": row.get("severity"),
        "state": row.get("state"),
        "work_item_id": row.get("work_item_id"),
        "handoff_id": row.get("handoff_id"),
        "message_id": row.get("message_id"),
        "target_role": row.get("target_role"),
        "owner_role": row.get("owner_role"),
        "next_action": row.get("next_action"),
        "evidence": evidence,
        "diagnostic": _watchdog_diagnostic(row=row, evidence=evidence),
    }
    return {key: value for key, value in output.items() if value is not None}


def _watchdog_diagnostic(*, row: dict[str, object], evidence: dict[str, object]) -> str:
    parts = [
        f"{row.get('severity')} {row.get('finding_type')}",
        f"finding_key={row.get('finding_key')}",
    ]
    for key in ("work_item_id", "handoff_id", "message_id", "target_role", "owner_role"):
        value = row.get(key)
        if value:
            parts.append(f"{key}={value}")
    if evidence:
        parts.append(f"evidence={json.dumps(evidence, sort_keys=True)}")
    next_action = row.get("next_action")
    if next_action:
        parts.append(f"next_action={next_action}")
    return "; ".join(str(part) for part in parts)


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _handle_safe_output(*, args, db: V4Database, project_config) -> None:
    command = args.safe_output_command
    if command == "work-item-update":
        project_config.role(args.role_id)
        if args.owner_role:
            project_config.role(args.owner_role)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        existing_work = db.connection.execute("SELECT * FROM work_items WHERE work_item_id=?", (args.work_item_id,)).fetchone()
        effective_state = args.state or (str(existing_work["state"]) if existing_work is not None else None)
        effective_next_action = args.next_action or (str(existing_work["next_action"]) if existing_work is not None else None)
        call_id = f"call-{secrets.token_hex(16)}"
        payload = {
            "work_item_id": args.work_item_id,
            "title": args.title,
            "state": args.state,
            "owner_role": args.owner_role,
            "next_action": args.next_action,
        }
        queued_handoff = None
        if (
            args.owner_role
            and args.owner_role != args.role_id
            and effective_state is not None
            and requires_dispatch_path(state=effective_state, next_action=effective_next_action, owner_role=args.owner_role)
        ):
            if existing_work is None:
                db.upsert_work_item(
                    work_item_id=args.work_item_id,
                    title=args.title,
                    state=args.state,
                    owner_role=args.role_id,
                    next_action=args.next_action,
                )
            queued_handoff = create_handoff(
                db=db,
                from_role=args.role_id,
                to_role=args.owner_role,
                work_item_id=args.work_item_id,
                state=effective_state,
                next_action=effective_next_action or f"Continue work on {args.work_item_id}.",
                reason="work-item-update transferred ownership to another role.",
                safe_output_call_id=call_id,
            )
        else:
            db.upsert_work_item(
                work_item_id=args.work_item_id,
                title=args.title,
                state=args.state,
                owner_role=args.owner_role,
                next_action=args.next_action,
            )
        db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="work_item.update",
            payload={
                **{key: value for key, value in payload.items() if value is not None},
                **(
                    {
                        "queued_handoff_id": queued_handoff["handoff_id"],
                        "queued_message_id": queued_handoff["message_id"],
                    }
                    if queued_handoff is not None
                    else {}
                ),
            },
            call_id=call_id,
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=args.work_item_id,
        )
        _print_json(
            {
                "call_id": call_id,
                "work_item_id": args.work_item_id,
                **(
                    {
                        "handoff_id": queued_handoff["handoff_id"],
                        "message_id": queued_handoff["message_id"],
                        "target_role": args.owner_role,
                    }
                    if queued_handoff is not None
                    else {}
                ),
            }
        )
        return
    if command == "architecture-impact":
        project_config.role(args.role_id)
        if args.role_id not in {"business-analyst", "product-manager", "enterprise-architect"}:
            raise ValueError("architecture impact may only be recorded by Business Analysis, Product, or Enterprise Architecture")
        role_instance_id = f"{project_config.project_id}.{args.role_id}.1"
        record_id = db.record_architecture_impact(
            work_item_id=args.work_item_id,
            classification=args.classification,
            rationale=args.rationale,
            affected_domains=args.affected_domain,
            actor_role=args.role_id,
            decision_ref=args.decision_ref,
        )
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="architecture.record_impact",
            payload={
                "work_item_id": args.work_item_id,
                "classification": args.classification,
                "rationale": args.rationale,
                "affected_domains": sorted(set(args.affected_domain)),
                "decision_ref": args.decision_ref,
                "record_id": record_id,
            },
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=args.work_item_id,
        )
        _print_json(
            {
                "call_id": call_id,
                "record_id": record_id,
                "work_item_id": args.work_item_id,
                "architecture_impact": args.classification,
                "conformance_required": args.classification in {"material", "uncertain"},
            }
        )
        return
    if command == "architecture-conformance":
        project_config.role(args.role_id)
        allowed_roles = {"enterprise-architect"}
        if args.status == "exception":
            allowed_roles.add("project-manager")
        if args.role_id not in allowed_roles:
            raise ValueError("architecture conformance is owned by Enterprise Architecture; Project Manager may record sponsor-approved exceptions")
        role_instance_id = f"{project_config.project_id}.{args.role_id}.1"
        record_id = db.record_architecture_conformance(
            work_item_id=args.work_item_id,
            status=args.status,
            rationale=args.rationale,
            actor_role=args.role_id,
            decision_ref=args.decision_ref,
        )
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="architecture.record_conformance",
            payload={
                "work_item_id": args.work_item_id,
                "status": args.status,
                "rationale": args.rationale,
                "decision_ref": args.decision_ref,
                "record_id": record_id,
            },
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=args.work_item_id,
        )
        _print_json(
            {
                "call_id": call_id,
                "record_id": record_id,
                "work_item_id": args.work_item_id,
                "architecture_conformance": args.status,
            }
        )
        return
    if command == "artifact-link":
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        artifact_id = db.record_artifact(work_item_id=args.work_item_id, path=args.path, title=args.title)
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="document.link_artifact",
            payload={
                "work_item_id": args.work_item_id,
                "path": args.path,
                "title": args.title,
                "artifact_id": artifact_id,
            },
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=args.work_item_id,
        )
        _print_json({"artifact_id": artifact_id, "call_id": call_id})
        return
    if command == "document-write-artifact":
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        call_id = f"call-{secrets.token_hex(16)}"
        request = DocumentWriteRequest(
            role_instance_id=role_instance_id,
            work_item_id=args.work_item_id,
            path=args.path,
            title=args.title,
            content=args.content,
            document_type=args.document_type,
            base_sha256=args.base_sha256,
            base_revision_id=args.base_revision_id,
            base_etag=args.base_etag,
            comment_metadata_status=args.comment_metadata_status,
            comment_metadata_detail=args.comment_metadata_detail,
            message_id=args.message_id,
            turn_id=args.turn_id,
            source_ref=args.source_ref,
        )
        result = write_artifact(
            db=db,
            document_root=args.document_root or Path(project_config.document_root),
            request=request,
            safe_output_call_id=call_id,
        )
        db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="document.write_artifact",
            payload={
                "work_item_id": args.work_item_id,
                "path": args.path,
                "title": args.title,
                "document_type": args.document_type,
                "base_sha256": args.base_sha256,
                "base_revision_id": args.base_revision_id,
                "base_etag": args.base_etag,
                "comment_metadata_status": args.comment_metadata_status,
                "comment_metadata_detail": args.comment_metadata_detail,
                "source_ref": args.source_ref,
                "result": result,
            },
            call_id=call_id,
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=args.work_item_id,
        )
        _print_json({"call_id": call_id, **result})
        return
    if command == "handoff":
        project_config.role(args.from_role)
        project_config.role(args.to_role)
        from_role_instance_id = _role_instance_id(project_config, args.from_role)
        call_id = f"call-{secrets.token_hex(16)}"
        lifecycle_result = create_handoff(
            db=db,
            from_role=args.from_role,
            to_role=args.to_role,
            work_item_id=args.work_item_id,
            state=args.state,
            next_action=args.next_action,
            reason=args.reason,
            source_message_id=args.message_id,
            safe_output_call_id=call_id,
            message_text=args.message,
        )
        payload = {
            "work_item_id": args.work_item_id,
            "from_role": args.from_role,
            "to_role": args.to_role,
            "state": args.state,
            "next_action": args.next_action,
            "reason": args.reason,
            "handoff_id": lifecycle_result["handoff_id"],
        }
        db.record_safe_output_call(
            role_instance_id=from_role_instance_id,
            tool_name="handoff.require",
            payload=payload,
            call_id=call_id,
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=args.work_item_id,
        )
        _print_json(
            {
                "call_id": call_id,
                "handoff_id": lifecycle_result["handoff_id"],
                "message_id": lifecycle_result["message_id"],
                "work_item_id": args.work_item_id,
                "target_role": args.to_role,
            }
        )
        return
    if command in {"handoff-accept", "handoff-complete", "handoff-supersede", "handoff-cancel", "handoff-block"}:
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        call_id = f"call-{secrets.token_hex(16)}"
        if command == "handoff-accept":
            result = accept_handoff(db=db, handoff_id=args.handoff_id, actor_role=args.role_id, reason=args.reason, safe_output_call_id=call_id)
            tool_name = "handoff.accept"
        elif command == "handoff-complete":
            project_config.role(args.next_owner_role)
            result = complete_handoff(
                db=db,
                handoff_id=args.handoff_id,
                actor_role=args.role_id,
                next_state=args.next_state,
                next_owner_role=args.next_owner_role,
                next_action=args.next_action,
                reason=args.reason,
                safe_output_call_id=call_id,
            )
            tool_name = "handoff.complete"
        elif command == "handoff-supersede":
            result = supersede_handoff(
                db=db,
                handoff_id=args.handoff_id,
                actor_role=args.role_id,
                superseded_by_handoff_id=args.superseded_by_handoff_id,
                reason=args.reason,
                safe_output_call_id=call_id,
            )
            tool_name = "handoff.supersede"
        elif command == "handoff-cancel":
            project_config.role(args.owner_role)
            result = cancel_handoff(
                db=db,
                handoff_id=args.handoff_id,
                actor_role=args.role_id,
                reason=args.reason,
                owner_role=args.owner_role,
                next_action=args.next_action,
                safe_output_call_id=call_id,
            )
            tool_name = "handoff.cancel"
        else:
            result = block_handoff(
                db=db,
                handoff_id=args.handoff_id,
                actor_role=args.role_id,
                reason=args.reason,
                next_action=args.next_action,
                safe_output_call_id=call_id,
            )
            tool_name = "handoff.block"
        handoff_row = db.connection.execute("SELECT work_item_id FROM handoffs WHERE handoff_id=?", (args.handoff_id,)).fetchone()
        work_item_id = str(handoff_row["work_item_id"]) if handoff_row is not None and handoff_row["work_item_id"] else None
        db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name=tool_name,
            payload={**result, "reason": args.reason},
            call_id=call_id,
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=work_item_id,
        )
        _print_json({"call_id": call_id, **result})
        return
    if command == "memory-record":
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        role_memory_id = None
        project_memory_id = None
        if args.scope in {"role", "both"}:
            role_memory_id = db.record_memory(
                role_instance_id=role_instance_id,
                project_id=project_config.project_id,
                summary=args.summary,
                source_ref=args.source_ref,
                scope="role",
                tags=list(args.tags or []),
                status=args.status,
            )
        if args.scope in {"institutional", "both"}:
            project_memory_id = db.record_project_memory(
                project_id=project_config.project_id,
                summary=args.summary,
                source_ref=args.source_ref,
                created_by_role_instance_id=role_instance_id,
                tags=list(args.tags or []),
                status=args.status,
            )
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="memory.propose_update",
            payload={
                "role_memory_id": role_memory_id,
                "project_memory_id": project_memory_id,
                "scope": args.scope,
                "summary": args.summary,
                "source_ref": args.source_ref,
                "tags": list(args.tags or []),
                "status": args.status,
            },
            message_id=args.message_id,
            turn_id=args.turn_id,
        )
        _print_json({"call_id": call_id, "role_memory_id": role_memory_id, "project_memory_id": project_memory_id})
        return
    if command == "decision-request":
        project_config.role(args.role_id)
        project_config.role(args.owner_role)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        result = request_decision(
            db=db,
            request=DecisionRequest(
                work_item_id=args.work_item_id,
                requester_role=args.role_id,
                owner_role=args.owner_role,
                authority_label=args.authority_label,
                authorized_responders=_json_object_arg(args.authorized_responders_json),
                decision_type=args.decision_type,
                title=args.title,
                question=args.question,
                options=tuple(str(item) for item in _json_list_arg(args.options_json)),
                recommended_option=args.recommended_option,
                tradeoffs={str(key): str(value) for key, value in _json_object_arg(args.tradeoffs_json).items()},
                source_refs=tuple(str(item) for item in _json_list_arg(args.source_refs_json)),
                affected_refs=tuple(item for item in _json_list_arg(args.affected_refs_json) if isinstance(item, dict)),
                link_effects=tuple(item for item in _json_list_arg(args.link_effects_json) if isinstance(item, dict)),
                sla_due_at=args.sla_due_at,
                conversation_ref=args.conversation_ref,
            ),
            deliver=not bool(args.teams_activity_json),
        )
        if args.teams_activity_json:
            decision_row = db.connection.execute(
                "SELECT * FROM decision_records WHERE decision_id=?",
                (result["decision_id"],),
            ).fetchone()
            activity = _json_object_arg(args.teams_activity_json)
            try:
                activity_ref = json.dumps(activity, sort_keys=True)
                activity_id = TeamsReplySender.from_env().send_decision_card(
                    role_id=args.owner_role,
                    activity=activity,
                    card=render_decision_card({key: decision_row[key] for key in decision_row.keys()}, detail_url=args.detail_url),
                )
                delivery_id = record_card_delivery_attempt(
                    db=db,
                    decision_id=str(result["decision_id"]),
                    channel="teams",
                    conversation_ref=args.conversation_ref or activity_ref,
                    activity_id=activity_id,
                    state="delivered",
                )
            except Exception as exc:  # noqa: BLE001 - delivery failure must be durable.
                delivery_id = record_card_delivery_attempt(
                    db=db,
                    decision_id=str(result["decision_id"]),
                    channel="teams",
                    conversation_ref=args.conversation_ref or json.dumps(activity, sort_keys=True),
                    state="delivery_failed",
                    error=str(exc),
                )
            result["delivery_id"] = delivery_id
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="decision.request",
            payload={
                "work_item_id": args.work_item_id,
                "decision_id": result["decision_id"],
                "decision_type": args.decision_type,
                "title": args.title,
            },
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=args.work_item_id,
        )
        _print_json({"call_id": call_id, **result})
        return
    if command == "decision-resolve":
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        result = resolve_decision(
            db=db,
            decision_id=args.decision_id,
            responder_ref=args.responder_ref,
            selected_option=args.selected_option,
            rationale=args.rationale,
            idempotency_key=args.idempotency_key,
            delivery_id=args.delivery_id,
            raw_payload={
                "decision_id": args.decision_id,
                "responder_ref": args.responder_ref,
                "selected_option": args.selected_option,
            },
        )
        decision = db.connection.execute("SELECT work_item_id FROM decision_records WHERE decision_id=?", (args.decision_id,)).fetchone()
        work_item_id = str(decision["work_item_id"]) if decision is not None else None
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="decision.resolve",
            payload=result,
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=work_item_id,
        )
        _print_json({"call_id": call_id, **result})
        return
    if command == "decision-cancel":
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        result = cancel_decision(
            db=db,
            decision_id=args.decision_id,
            responder_ref=args.responder_ref,
            rationale=args.rationale,
        )
        decision = db.connection.execute("SELECT work_item_id FROM decision_records WHERE decision_id=?", (args.decision_id,)).fetchone()
        work_item_id = str(decision["work_item_id"]) if decision is not None else None
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="decision.cancel",
            payload=result,
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=work_item_id,
        )
        _print_json({"call_id": call_id, **result})
        return
    if command == "decision-link":
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        result = link_decision(
            db=db,
            decision_id=args.decision_id,
            target_type=args.target_type,
            target_id=args.target_id,
            link_type=args.link_type,
            effect_summary=args.effect_summary,
            effect_payload=_json_object_arg(args.effect_payload_json),
        )
        decision = db.connection.execute("SELECT work_item_id FROM decision_records WHERE decision_id=?", (args.decision_id,)).fetchone()
        work_item_id = str(decision["work_item_id"]) if decision is not None else None
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="decision.link",
            payload={**result, "target_type": args.target_type, "target_id": args.target_id},
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=work_item_id,
        )
        _print_json({"call_id": call_id, **result})
        return
    if command == "decision-sla-sweep":
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        result = recalculate_sla_states(
            db=db,
            now=args.now,
            due_soon_seconds=args.due_soon_seconds,
            escalate_after_seconds=args.escalate_after_seconds,
        )
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="decision.sla_sweep",
            payload=result,
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=None,
        )
        _print_json({"call_id": call_id, **result})
        return
    if command == "decision-delivery-sweep":
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        result = deliver_pending_decision_cards(
            db=db,
            sender=TeamsReplySender.from_env(),
            limit=args.limit,
            detail_url=args.detail_url,
        )
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="decision.delivery_sweep",
            payload=result,
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=None,
        )
        _print_json({"call_id": call_id, **result})
        return
    if command == "decision-update-retry":
        project_config.role(args.role_id)
        role_instance_id = _role_instance_id(project_config, args.role_id)
        result = retry_failed_card_updates(
            db=db,
            sender=TeamsReplySender.from_env(),
            limit=args.limit,
            detail_url=args.detail_url,
        )
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="decision.update_retry",
            payload=result,
            message_id=args.message_id,
            turn_id=args.turn_id,
            work_item_id=None,
        )
        _print_json({"call_id": call_id, **result})
        return
    raise ValueError(f"unknown safe-output command: {command}")


def _dispatch_result(result) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "message_id": result.message_id,
        "state": result.state,
        "thread_id": result.thread_id,
        "turn_id": result.turn_id,
        "error": result.error,
    }


def _print_json(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True))


def _json_object_arg(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


def _json_list_arg(value: str) -> list[object]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("expected JSON array")
    return parsed


if __name__ == "__main__":
    main()

