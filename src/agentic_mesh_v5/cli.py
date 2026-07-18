from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Sequence

from agentic_mesh_v5 import __version__
from agentic_mesh_v5.api_auth import AuthenticationConfigurationError
from agentic_mesh_v5.api_auth import TokenAuthorizer
from agentic_mesh_v5.api_client import ApiCallError
from agentic_mesh_v5.api_client import ApiClientConfigurationError
from agentic_mesh_v5.api_client import ControlApiClient
from agentic_mesh_v5.api_client import load_opaque_token
from agentic_mesh_v5.boundary import find_runtime_boundary_violations
from agentic_mesh_v5.config_activation import ConfigActivationError
from agentic_mesh_v5.config_activation import ConfigActivationStore
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import database_url_from_environment
from agentic_mesh_v5.database_operations import DatabaseBackupService
from agentic_mesh_v5.database_operations import MaintenanceStore
from agentic_mesh_v5.package_resolver import PackageResolutionError
from agentic_mesh_v5.package_resolver import resolve_packages
from agentic_mesh_v5.recovery_supervisor import CommandRecoveryLauncher
from agentic_mesh_v5.recovery_supervisor import RecoverySupervisor
from agentic_mesh_v5.recovery_supervisor import RecoverySupervisorError
from agentic_mesh_v5.tool_profiles import ToolProfileError
from agentic_mesh_v5.tool_profiles import ToolProfileRegistry


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
    subparsers.add_parser(
        "database-maintenance-status", help="show the durable V5 write-pause state"
    )
    database_pause = subparsers.add_parser(
        "database-pause", help="wait for active writers and pause V5 mutations"
    )
    database_pause.add_argument("--actor", required=True)
    database_pause.add_argument("--reason", required=True)
    database_resume = subparsers.add_parser(
        "database-resume", help="resume V5 mutations after maintenance"
    )
    database_resume.add_argument("--actor", required=True)
    database_resume.add_argument("--reason", required=True)
    database_backup = subparsers.add_parser(
        "database-backup", help="create an atomic verified V5 Postgres archive"
    )
    database_backup.add_argument("--output", type=Path, required=True)
    database_backup.add_argument("--actor", required=True)
    database_backup.add_argument("--reason", required=True)
    database_restore = subparsers.add_parser(
        "database-restore", help="restore a verified archive into an empty database"
    )
    database_restore.add_argument("--archive", type=Path, required=True)
    api_serve = subparsers.add_parser(
        "api-serve", help="serve the versioned V5 control API"
    )
    api_serve.add_argument("--host", default="127.0.0.1")
    api_serve.add_argument("--port", type=int, default=8080)
    subparsers.add_parser(
        "control-status", help="read control API health through external authentication"
    )
    control_call = subparsers.add_parser(
        "control-call", help="call a versioned control API operation"
    )
    control_call.add_argument("method", choices=("GET", "POST", "PUT"))
    control_call.add_argument("path")
    control_call.add_argument("--body-file", type=Path)
    sponsor_decision = subparsers.add_parser(
        "sponsor-decision",
        help="approve or reject a sponsor gate through the authenticated control API",
    )
    sponsor_decision.add_argument("--project", required=True)
    sponsor_decision.add_argument("--gate", required=True)
    sponsor_decision.add_argument("--decision", choices=("approve", "reject"), required=True)
    sponsor_decision.add_argument("--rationale", required=True)
    sponsor_decision.add_argument("--evidence-file", type=Path)
    recovery_run = subparsers.add_parser(
        "recovery-run-once",
        help="run one independent recovery directly against Postgres",
    )
    recovery_run.add_argument("--config-root", type=Path, required=True)
    recovery_run.add_argument("--tool-profile", required=True)
    recovery_run.add_argument("--launcher-command-file", type=Path, required=True)
    recovery_run.add_argument("--token-file", type=Path, required=True)
    recovery_run.add_argument("--owner", required=True)
    recovery_run.add_argument("--time-limit-minutes", type=int, default=120)
    recovery_run.add_argument("--usage-limit", type=int, default=200_000)
    recovery_run.add_argument("--lease-seconds", type=int, default=60)
    return parser


def _write(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def _write_control(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    for key in ("status", "action_id", "http_status", "method", "path"):
        if key in payload:
            print(f"{key}: {payload[key]}")
    if "error" in payload:
        print("error:")
        print(json.dumps(payload["error"], indent=2, sort_keys=True))
    if "result" in payload:
        print("result:")
        print(json.dumps(payload["result"], indent=2, sort_keys=True))


def _path_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is None
    ):
        raise ApiClientConfigurationError(f"{label} identifier is invalid")
    return value


def _load_evidence(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    if not path.exists() or not path.is_file():
        raise ApiClientConfigurationError(
            "sponsor evidence path must be an existing file"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApiClientConfigurationError(
            "sponsor evidence file must be valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ApiClientConfigurationError("sponsor evidence must be a JSON object")
    return value


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
                "status": (
                    "database-pending"
                    if result.pending_versions
                    else "database-current"
                ),
                **result.to_dict(),
            },
            as_json=args.json,
        )
        return 0

    if args.command in {
        "database-maintenance-status",
        "database-pause",
        "database-resume",
        "database-backup",
        "database-restore",
    }:
        try:
            database_url = database_url_from_environment()
            if args.command == "database-maintenance-status":
                maintenance = MaintenanceStore(database_url).status().to_dict()
                operation_status = (
                    "database-active"
                    if maintenance["status"] == "active"
                    else "database-paused"
                )
                result = {
                    "maintenance_status": maintenance.pop("status"),
                    **maintenance,
                }
            elif args.command == "database-pause":
                maintenance = MaintenanceStore(database_url).pause(
                    actor=args.actor, reason=args.reason
                ).to_dict()
                operation_status = "database-paused"
                result = {
                    "maintenance_status": maintenance.pop("status"),
                    **maintenance,
                }
            elif args.command == "database-resume":
                maintenance = MaintenanceStore(database_url).resume(
                    actor=args.actor, reason=args.reason
                ).to_dict()
                operation_status = "database-active"
                result = {
                    "maintenance_status": maintenance.pop("status"),
                    **maintenance,
                }
            elif args.command == "database-backup":
                result = DatabaseBackupService(database_url).backup(
                    args.output, actor=args.actor, reason=args.reason
                ).to_dict()
                operation_status = "database-backup-created"
            else:
                result = DatabaseBackupService(database_url).restore(
                    args.archive
                ).to_dict()
                operation_status = "database-restored-paused"
        except (DatabaseError, ValueError) as exc:
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
                "status": operation_status,
                **result,
            },
            as_json=args.json,
        )
        return 0

    if args.command == "api-serve":
        try:
            from agentic_mesh_v5.api import app_from_environment
            import uvicorn

            app = app_from_environment()
        except (AuthenticationConfigurationError, DatabaseError) as exc:
            _write(
                {
                    "runtime": "agentic-mesh-v5",
                    "status": "rejected",
                    "error": str(exc),
                },
                as_json=args.json,
            )
            return 2
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    if args.command in {"control-status", "control-call", "sponsor-decision"}:
        try:
            client = ControlApiClient.from_environment()
            if args.command == "sponsor-decision":
                evidence = _load_evidence(args.evidence_file)
                result = client.call(
                    method="POST",
                    path=(
                        f"/api/v1/projects/{_path_id(args.project, 'project')}/"
                        f"gates/{_path_id(args.gate, 'gate')}/decision"
                    ),
                    body={
                        "decision": (
                            "approved" if args.decision == "approve" else "rejected"
                        ),
                        "rationale": args.rationale,
                        "evidence": evidence,
                    },
                )
            else:
                result = client.call(
                    method="GET" if args.command == "control-status" else args.method,
                    path=(
                        "/api/v1/health"
                        if args.command == "control-status"
                        else args.path
                    ),
                    body_file=(
                        None if args.command == "control-status" else args.body_file
                    ),
                )
        except ApiClientConfigurationError as exc:
            _write_control(
                {
                    "runtime": "agentic-mesh-v5",
                    "status": "rejected",
                    "error": {"code": "client_configuration", "detail": str(exc)},
                },
                as_json=args.json,
            )
            return 2
        except ApiCallError as exc:
            _write_control(exc.to_dict(), as_json=args.json)
            return exc.exit_code
        _write_control(result.to_dict(), as_json=args.json)
        return 0

    if args.command == "recovery-run-once":
        try:
            token = load_opaque_token(args.token_file)
            identity = TokenAuthorizer.from_environment().resolve(token)
            if identity is None:
                raise RecoverySupervisorError("independent recovery token is invalid")
            supervisor = RecoverySupervisor(
                database_url=database_url_from_environment(),
                principal=identity,
                owner_id=args.owner,
                registry=ToolProfileRegistry(args.config_root),
                tool_profile_reference=args.tool_profile,
                launcher=CommandRecoveryLauncher.from_file(
                    args.launcher_command_file
                ),
                time_limit_minutes=args.time_limit_minutes,
                usage_limit=args.usage_limit,
                lease_seconds=args.lease_seconds,
            )
            result = supervisor.execute_once()
        except (
            ApiClientConfigurationError,
            AuthenticationConfigurationError,
            DatabaseError,
            RecoverySupervisorError,
            ToolProfileError,
            ValueError,
        ) as exc:
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
            {"runtime": "agentic-mesh-v5", **result.to_safe_dict()},
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
