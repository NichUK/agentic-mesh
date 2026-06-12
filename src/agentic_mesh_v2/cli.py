from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.demo import run_demo_slice
from agentic_mesh_v2.server import serve


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

    args = parser.parse_args(argv)
    db_path = Path(args.db)

    if args.command == "serve":
        serve(host=args.host, port=args.port, db_path=db_path)
        return 0

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
    finally:
        db.close()

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
