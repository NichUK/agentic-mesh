from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from agentic_mesh_v5 import __version__
from agentic_mesh_v5.boundary import find_runtime_boundary_violations
from agentic_mesh_v5.config_activation import ConfigActivationError
from agentic_mesh_v5.config_activation import ConfigActivationStore
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import database_url_from_environment
from agentic_mesh_v5.package_resolver import PackageResolutionError
from agentic_mesh_v5.package_resolver import resolve_packages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-mesh-v5")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="show bootstrap runtime status")
    boundary = subparsers.add_parser(
        "boundary-check", help="reject imports from superseded runtime packages"
    )
    boundary.add_argument(
        "--package-root",
        type=Path,
        help="source directory to inspect; defaults to the installed V5 package",
    )
    resolve = subparsers.add_parser(
        "resolve-config", help="resolve external configuration packages"
    )
    resolve.add_argument("--config-root", type=Path, required=True)
    resolve.add_argument("--package", action="append", required=True, dest="packages")
    release_create = subparsers.add_parser(
        "release-create", help="validate configuration and create an immutable release"
    )
    release_create.add_argument("--config-root", type=Path, required=True)
    release_create.add_argument("--package", action="append", required=True, dest="packages")
    release_create.add_argument("--actor", required=True)
    release_activate = subparsers.add_parser(
        "release-activate", help="atomically activate an immutable configuration release"
    )
    release_activate.add_argument("--config-root", type=Path, required=True)
    release_activate.add_argument("--digest", required=True)
    release_activate.add_argument("--actor", required=True)
    release_activate.add_argument("--reason", default="")
    expectation = release_activate.add_mutually_exclusive_group()
    expectation.add_argument("--expected-active")
    expectation.add_argument("--expect-empty", action="store_true")
    release_rollback = subparsers.add_parser(
        "release-rollback", help="roll back to an earlier immutable release"
    )
    release_rollback.add_argument("--config-root", type=Path, required=True)
    release_rollback.add_argument("--digest", required=True)
    release_rollback.add_argument("--actor", required=True)
    release_rollback.add_argument("--reason", required=True)
    release_status = subparsers.add_parser(
        "release-status", help="show the active configuration release and audit history"
    )
    release_status.add_argument("--config-root", type=Path, required=True)
    subparsers.add_parser(
        "database-migrate", help="apply pending V5 Postgres migrations"
    )
    subparsers.add_parser(
        "database-status", help="show V5 Postgres migration status"
    )
    return parser


def _write(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        _write(
            {
                "runtime": "agentic-mesh-v5",
                "status": "bootstrap-ready",
                "version": __version__,
            },
            as_json=args.json,
        )
        return 0

    if args.command == "resolve-config":
        try:
            resolved = resolve_packages(args.config_root, args.packages)
        except PackageResolutionError as exc:
            _write(
                {"runtime": "agentic-mesh-v5", "status": "rejected", "error": str(exc)},
                as_json=args.json,
            )
            return 2
        _write(
            {"runtime": "agentic-mesh-v5", "status": "resolved", **resolved.to_dict()},
            as_json=args.json,
        )
        return 0

    if args.command in {"database-migrate", "database-status"}:
        try:
            runner = MigrationRunner(database_url_from_environment())
            result = (
                runner.migrate()
                if args.command == "database-migrate"
                else runner.status()
            )
        except DatabaseError as exc:
            _write(
                {
                    "runtime": "agentic-mesh-v5",
                    "status": "rejected",
                    "error": str(exc),
                },
                as_json=args.json,
            )
            return 2
        _write(
            {
                "runtime": "agentic-mesh-v5",
                "status": "database-current",
                **result.to_dict(),
            },
            as_json=args.json,
        )
        return 0

    if args.command.startswith("release-"):
        try:
            store = ConfigActivationStore(args.config_root)
            if args.command == "release-create":
                result = store.create_release(
                    args.packages, actor=args.actor
                ).to_dict()
                status = "release-created"
            elif args.command == "release-activate":
                options: dict[str, object] = {}
                if args.expect_empty:
                    options["expected_active"] = None
                elif args.expected_active is not None:
                    options["expected_active"] = args.expected_active
                result = store.activate(
                    args.digest,
                    actor=args.actor,
                    reason=args.reason,
                    **options,
                ).to_dict()
                status = "release-active"
            elif args.command == "release-rollback":
                result = store.rollback(
                    args.digest, actor=args.actor, reason=args.reason
                ).to_dict()
                status = "release-rolled-back"
            else:
                result = store.get_state().to_dict()
                status = "release-status"
        except ConfigActivationError as exc:
            _write(
                {"runtime": "agentic-mesh-v5", "status": "rejected", "error": str(exc)},
                as_json=args.json,
            )
            return 2
        _write(
            {"runtime": "agentic-mesh-v5", "status": status, **result},
            as_json=args.json,
        )
        return 0

    package_root = args.package_root
    if package_root is not None and (
        not package_root.exists() or not package_root.is_dir()
    ):
        _write(
            {
                "error": "package root must be an existing directory",
                "path": str(package_root),
                "runtime": "agentic-mesh-v5",
                "status": "error",
            },
            as_json=args.json,
        )
        return 2

    violations = find_runtime_boundary_violations(package_root)
    payload = {
        "runtime": "agentic-mesh-v5",
        "status": "clean" if not violations else "rejected",
        "violations": [
            {"line": item.line, "module": item.module, "path": str(item.path)}
            for item in violations
        ],
    }
    _write(payload, as_json=args.json)
    return 0 if not violations else 2
