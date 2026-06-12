from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.demo import run_demo_slice
from agentic_mesh_v2.observability import configure_observability
from agentic_mesh_v2.observability import span
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.server import serve
from agentic_mesh_v2.topology import ProjectRepo
from agentic_mesh_v2.topology import RuntimeTopology
from agentic_mesh_v2.worker_adapters import SafeOutputFileWorker


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

    subparsers.add_parser("demo-slice", help="Create one complete v2 end-to-end demo slice.")
    subparsers.add_parser("status-json", help="Print the v2 runtime status snapshot as JSON.")

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
        "--worker",
        choices=["safe-output-file"],
        required=True,
        help="Worker adapter to use for claimed assignments.",
    )
    tick_parser.add_argument("--safe-output-file", type=Path, required=True)
    tick_parser.add_argument("--max-recoveries", type=int, default=50)
    tick_parser.add_argument("--max-assignments", type=int, default=10)
    tick_parser.add_argument("--assignment-lease-seconds", type=int, default=300)

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

    if args.command == "serve":
        serve(host=args.host, port=args.port, db_path=db_path)
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
                worker = SafeOutputFileWorker(args.safe_output_file)
                service = RoleService(
                    db=db,
                    role_id=args.role_id,
                    role_instance_id=args.role_instance_id,
                    worker=worker,
                    assignment_lease_seconds=args.assignment_lease_seconds,
                )
                receipt = service.run_service_tick(
                    max_recoveries=args.max_recoveries,
                    max_assignments=args.max_assignments,
                )
                print(
                    json.dumps(
                        {
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
                        },
                        sort_keys=True,
                    )
                )
                return 0
        finally:
            db.close()

    raise AssertionError(f"unhandled command: {args.command}")


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


if __name__ == "__main__":
    raise SystemExit(main())
