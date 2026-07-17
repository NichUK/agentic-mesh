from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from agentic_mesh_v5 import __version__
from agentic_mesh_v5.boundary import find_runtime_boundary_violations


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

    violations = find_runtime_boundary_violations(args.package_root)
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
