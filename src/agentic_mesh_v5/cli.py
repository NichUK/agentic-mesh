from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from agentic_mesh_v5 import __version__
from agentic_mesh_v5.boundary import find_runtime_boundary_violations
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
