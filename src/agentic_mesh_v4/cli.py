from __future__ import annotations

import argparse
import json
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
    dispatch_loop.add_argument("--wake", action="store_true")
    dispatch_loop.add_argument("--once", action="store_true")

    schema = subparsers.add_parser("generate-protocol-schema")
    schema.add_argument("--output", type=Path, required=True)
    schema.add_argument("--codex-bin", default="codex")

    subparsers.add_parser("status-json")

    sync_documents = subparsers.add_parser("sync-documents")
    sync_documents.add_argument("--local-root", type=Path, required=True)
    sync_documents.add_argument("--drive-id")
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

            result = V4Runtime(db=db, project_config=project_config, client_factory=factory).dispatch_once(
                role_id=role.role_id
            )
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
            while True:
                processed = _dispatch_available_messages(
                    db=db,
                    project_config=project_config,
                    agent_config_root=args.agent_config_root,
                    lifecycle=lifecycle,
                )
                if args.once:
                    _print_json({"processed": processed})
                    return
                if processed == 0:
                    time.sleep(args.poll_interval_seconds)
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

    runtime = V4Runtime(db=db, project_config=project_config, client_factory=factory)
    runtime.register_roles()
    for role in project_config.roles:
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
