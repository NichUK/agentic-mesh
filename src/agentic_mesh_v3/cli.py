from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.demo import run_demo_slice
from agentic_mesh_v3.dogfood import run_local_e2e_dogfood_slice
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.documents import build_document_library_adapter
from agentic_mesh_v3.documents import LocalDocumentLibraryAdapter
from agentic_mesh_v3.observability import TelemetrySettings
from agentic_mesh_v3.observability import configure_observability
from agentic_mesh_v3.project_config import load_project_config
from agentic_mesh_v3.server import serve
from agentic_mesh_v3.tool_mcp import run_v3_mcp_stdio
from agentic_mesh_v3.tools import V3ToolService
from agentic_mesh_v3.topology import V3Topology
from agentic_mesh_v3.topology import validate_topology


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-mesh-v3")
    parser.add_argument("--db", type=Path, default=Path(".tmp/v3/agentic-mesh-v3.sqlite3"))
    parser.add_argument("--project-id", default="agentic-mesh-dev")
    parser.add_argument("--project-config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")
    subparsers.add_parser("status-json")
    demo_parser = subparsers.add_parser("demo-slice")
    demo_parser.add_argument("--document-library-root", type=Path)

    dogfood_parser = subparsers.add_parser("local-e2e-dogfood")
    dogfood_parser.add_argument("--document-library-root", type=Path, required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8080)
    serve_parser.add_argument("--document-library-root", type=Path)

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
    if args.command == "serve":
        serve(
            db_path=args.db,
            project_id=args.project_id,
            host=args.host,
            port=args.port,
            document_library=_document_library_adapter(args),
        )
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


if __name__ == "__main__":
    raise SystemExit(main())
