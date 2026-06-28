from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from agentic_mesh_v4.agent_config import materialize_agent_configs
from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import WebSocketTransport
from agentic_mesh_v4.compose import render_compose
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.lifecycle import ComposeLifecycle
from agentic_mesh_v4.onedrive_sync import sync_local_documents_to_onedrive
from agentic_mesh_v4.runtime import V4Runtime
from agentic_mesh_v4.server import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-mesh-v4")
    parser.add_argument("--db", type=Path, default=Path(".tmp/agentic-mesh-v4.sqlite3"))
    parser.add_argument("--project-config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")

    materialize = subparsers.add_parser("materialize-agent-configs")
    materialize.add_argument("--agent-config-root", type=Path, required=True)
    materialize.add_argument("--role-templates-dir", type=Path, default=Path("config/roles"))

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

    artifact_link = safe_output_subparsers.add_parser("artifact-link")
    artifact_link.add_argument("--role-id", required=True)
    artifact_link.add_argument("--work-item-id", required=True)
    artifact_link.add_argument("--path", required=True)
    artifact_link.add_argument("--title", required=True)

    handoff = safe_output_subparsers.add_parser("handoff")
    handoff.add_argument("--from-role", required=True)
    handoff.add_argument("--to-role", required=True)
    handoff.add_argument("--work-item-id", required=True)
    handoff.add_argument("--state", required=True)
    handoff.add_argument("--next-action", required=True)
    handoff.add_argument("--reason", required=True)
    handoff.add_argument("--message")

    memory = safe_output_subparsers.add_parser("memory-record")
    memory.add_argument("--role-id", required=True)
    memory.add_argument("--summary", required=True)
    memory.add_argument("--source-ref", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    project_config = load_project_config(args.project_config)
    db = V4Database(args.db)
    try:
        db.migrate()
        runtime = V4Runtime(db=db, project_config=project_config)
        runtime.register_roles()
        if args.command == "init-db":
            _print_json({"status": "ok", "roles": len(project_config.roles)})
            return
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
                    active_turn_stale_seconds=args.active_turn_stale_seconds,
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
                max_workers=max(1, args.dispatch_workers),
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
                if args.once:
                    _print_json({"checked": checked})
                    return
                time.sleep(args.poll_interval_seconds)
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


def _ensure_ws_tokens(root: Path, project_config) -> None:
    for role in project_config.roles:
        token_path = root / role.role_id / "1" / "ws-token"
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")


def _dispatch_available_messages(
    *,
    db: V4Database,
    project_config,
    project_config_path: Path,
    agent_config_root: Path,
    lifecycle: ComposeLifecycle | None,
    active_turn_stale_seconds: float,
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
    )
    runtime.register_roles()
    for role in project_config.roles:
        if db.active_message_for_role(target_role=role.role_id) is not None:
            recovered_stale = db.requeue_active_messages_for_role(
                target_role=role.role_id,
                stale_after_seconds=active_turn_stale_seconds,
                summary=(
                    f"Recovered stale active delivery for {role.role_id}; "
                    f"message stayed active longer than {active_turn_stale_seconds:.0f} seconds."
                ),
            )
            if recovered_stale and lifecycle is not None:
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
                            "messages": recovered_stale,
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
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
    active: dict[str, concurrent.futures.Future[int]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            processed = _collect_completed_dispatches(active)
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
            if processed == 0 and scheduled == 0:
                time.sleep(poll_interval_seconds)


def _collect_completed_dispatches(active: dict[str, concurrent.futures.Future[int]]) -> int:
    processed = 0
    for role_id, future in list(active.items()):
        if not future.done():
            continue
        try:
            processed += future.result()
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
    return processed


def _schedule_available_dispatches(
    *,
    db: V4Database,
    project_config,
    project_config_path: Path,
    agent_config_root: Path,
    lifecycle: ComposeLifecycle | None,
    active_turn_stale_seconds: float,
    executor: concurrent.futures.Executor,
    active: dict[str, concurrent.futures.Future[int]],
) -> int:
    scheduled = 0
    for role in project_config.roles:
        if role.role_id in active:
            continue
        if db.active_message_for_role(target_role=role.role_id) is not None:
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
        active[role.role_id] = executor.submit(
            _dispatch_role_message,
            db_path=db.path,
            project_config_path=project_config_path,
            agent_config_root=agent_config_root,
            role_id=role.role_id,
        )
        scheduled += 1
    return scheduled


def _dispatch_role_message(
    *,
    db_path: Path,
    project_config_path: Path,
    agent_config_root: Path,
    role_id: str,
) -> int:
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
            document_syncer=_document_syncer(project_config_path),
            agent_config_root=agent_config_root,
        ).dispatch_once(role_id=role_id)
        return 1 if result is not None else 0
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


def _document_syncer(project_config_path: Path):
    local_root = Path(os.environ.get("AGENTIC_MESH_DOCUMENT_ROOT", "/documents"))

    def sync() -> object:
        return sync_local_documents_to_onedrive(
            project_config=project_config_path,
            local_root=local_root,
        )

    return sync


def _handle_safe_output(*, args, db: V4Database, project_config) -> None:
    command = args.safe_output_command
    if command == "work-item-update":
        project_config.role(args.role_id)
        if args.owner_role:
            project_config.role(args.owner_role)
        role_instance_id = f"{project_config.project_id}.{args.role_id}.1"
        payload = {
            "work_item_id": args.work_item_id,
            "title": args.title,
            "state": args.state,
            "owner_role": args.owner_role,
            "next_action": args.next_action,
        }
        db.upsert_work_item(
            work_item_id=args.work_item_id,
            title=args.title,
            state=args.state,
            owner_role=args.owner_role,
            next_action=args.next_action,
        )
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="work_item.update",
            payload={key: value for key, value in payload.items() if value is not None},
        )
        _print_json({"call_id": call_id, "work_item_id": args.work_item_id})
        return
    if command == "artifact-link":
        project_config.role(args.role_id)
        role_instance_id = f"{project_config.project_id}.{args.role_id}.1"
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
        )
        _print_json({"artifact_id": artifact_id, "call_id": call_id})
        return
    if command == "handoff":
        project_config.role(args.from_role)
        project_config.role(args.to_role)
        from_role_instance_id = f"{project_config.project_id}.{args.from_role}.1"
        handoff_id = db.record_handoff(
            from_role=args.from_role,
            to_role=args.to_role,
            work_item_id=args.work_item_id,
            reason=args.reason,
        )
        db.upsert_work_item(
            work_item_id=args.work_item_id,
            state=args.state,
            owner_role=args.to_role,
            next_action=args.next_action,
        )
        payload = {
            "work_item_id": args.work_item_id,
            "from_role": args.from_role,
            "to_role": args.to_role,
            "state": args.state,
            "next_action": args.next_action,
            "reason": args.reason,
            "handoff_id": handoff_id,
        }
        call_id = db.record_safe_output_call(
            role_instance_id=from_role_instance_id,
            tool_name="handoff.require",
            payload=payload,
        )
        message_text = args.message or (
            f"Handoff for {args.work_item_id} from {args.from_role} to {args.to_role}. "
            f"Next action: {args.next_action}"
        )
        message_id = db.enqueue_message(
            target_role=args.to_role,
            text=message_text,
            source="safe-output",
            payload=payload,
        )
        _print_json(
            {
                "call_id": call_id,
                "handoff_id": handoff_id,
                "message_id": message_id,
                "work_item_id": args.work_item_id,
                "target_role": args.to_role,
            }
        )
        return
    if command == "memory-record":
        project_config.role(args.role_id)
        role_instance_id = f"{project_config.project_id}.{args.role_id}.1"
        memory_id = db.record_memory(
            role_instance_id=role_instance_id,
            summary=args.summary,
            source_ref=args.source_ref,
        )
        call_id = db.record_safe_output_call(
            role_instance_id=role_instance_id,
            tool_name="memory.propose_update",
            payload={"memory_id": memory_id, "summary": args.summary, "source_ref": args.source_ref},
        )
        _print_json({"call_id": call_id, "memory_id": memory_id})
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


if __name__ == "__main__":
    main()
