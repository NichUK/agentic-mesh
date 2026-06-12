from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.demo import run_demo_slice
from agentic_mesh_v2.hibernation import HibernationPolicy
from agentic_mesh_v2.hibernation import HibernationService
from agentic_mesh_v2.observability import configure_observability
from agentic_mesh_v2.observability import span
from agentic_mesh_v2.project_config import list_project_role_service_configs
from agentic_mesh_v2.project_config import load_role_hibernation_config
from agentic_mesh_v2.project_config import load_role_worker_config
from agentic_mesh_v2.role_service import RoleService
from agentic_mesh_v2.server import serve
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
        "--project-file",
        type=Path,
        help="Optional project.yaml to load roles.<role>.worker when --worker is omitted.",
    )
    tick_parser.add_argument(
        "--worker",
        choices=["safe-output-file", "safe-output-subprocess"],
        help="Worker adapter to use for claimed assignments.",
    )
    tick_parser.add_argument("--safe-output-file", type=Path)
    tick_parser.add_argument(
        "--worker-command-json",
        help="JSON array command for the safe-output-subprocess worker.",
    )
    tick_parser.add_argument("--worker-timeout-seconds", type=int, default=300)
    tick_parser.add_argument("--max-recoveries", type=int, default=50)
    tick_parser.add_argument("--max-assignments", type=int, default=10)
    tick_parser.add_argument("--assignment-lease-seconds", type=int, default=300)

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
                worker = build_worker_adapter(_worker_config_from_args(args))
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
            raise ValueError("--worker or --project-file is required for run-role-service-tick")
        return load_role_worker_config(args.project_file, role_id=args.role_id)
    if args.worker == "safe-output-file":
        if args.safe_output_file is None:
            raise ValueError("--safe-output-file is required for safe-output-file worker")
        return {"adapter": "safe-output-file", "path": str(args.safe_output_file)}
    if args.worker == "safe-output-subprocess":
        return {
            "adapter": "safe-output-subprocess",
            "command": list(_parse_worker_command(args.worker_command_json)),
            "timeout_seconds": args.worker_timeout_seconds,
        }
    raise AssertionError(f"unhandled worker adapter: {args.worker}")


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


if __name__ == "__main__":
    raise SystemExit(main())
