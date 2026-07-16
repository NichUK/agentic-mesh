from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import socket
import socketserver
from pathlib import Path
from typing import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentic_mesh_v4.shared_fleet import SharedFleetOperationGuard


MAX_REQUEST_BYTES = 8 * 1024 * 1024


def validated_cli_argv(
    *,
    request_argv: list[str],
    role_id: str,
    project_config: Path,
    shared_fleet_guard: "SharedFleetOperationGuard | None" = None,
    binding_project_id: str | None = None,
    binding_generation: int | None = None,
) -> list[str]:
    try:
        safe_output_index = request_argv.index("safe-output")
    except ValueError as exc:
        raise ValueError("the role proxy accepts only safe-output commands") from exc
    safe_output_argv = request_argv[safe_output_index + 1 :]
    if not safe_output_argv:
        raise ValueError("safe-output command is required")
    action = safe_output_argv[0]
    actor_flag = "--from-role" if action == "handoff" else "--role-id"
    actor = _option_value(safe_output_argv, actor_flag)
    if actor is None:
        raise ValueError(f"{action} must declare {actor_flag}")
    if actor != role_id:
        raise PermissionError(f"role {role_id} cannot act as {actor}")
    if any(value == "--db" or value.startswith("--db=") for value in safe_output_argv):
        raise ValueError("database overrides are not accepted by the role proxy")
    if shared_fleet_guard is not None:
        shared_fleet_guard.require(
            project_id=binding_project_id,
            generation=binding_generation,
        )
    return ["--project-config", str(project_config), "safe-output", *safe_output_argv]


def _option_value(argv: list[str], flag: str) -> str | None:
    for index, value in enumerate(argv):
        if value == flag:
            if index + 1 >= len(argv):
                raise ValueError(f"{flag} requires a value")
            return argv[index + 1]
        if value.startswith(f"{flag}="):
            return value.split("=", 1)[1]
    return None


def execute_cli(argv: list[str]) -> tuple[int, str, str]:
    from agentic_mesh_v4.cli import main as cli_main

    stdout = io.StringIO()
    stderr = io.StringIO()
    previous_bypass = os.environ.get("AGENTIC_MESH_SAFE_OUTPUT_PROXY_BYPASS")
    os.environ["AGENTIC_MESH_SAFE_OUTPUT_PROXY_BYPASS"] = "1"
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                cli_main(argv)
            except SystemExit as exc:
                return int(exc.code or 0), stdout.getvalue(), stderr.getvalue()
        return 0, stdout.getvalue(), stderr.getvalue()
    except Exception as exc:  # The proxy must return a structured failure to the role CLI.
        return 1, stdout.getvalue(), f"{type(exc).__name__}: {exc}\n"
    finally:
        if previous_bypass is None:
            os.environ.pop("AGENTIC_MESH_SAFE_OUTPUT_PROXY_BYPASS", None)
        else:
            os.environ["AGENTIC_MESH_SAFE_OUTPUT_PROXY_BYPASS"] = previous_bypass


class _SafeOutputRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            self._write_response({"exit_code": 1, "stdout": "", "stderr": "safe-output request is too large\n"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
            request_argv = payload.get("argv")
            if not isinstance(request_argv, list) or not all(isinstance(value, str) for value in request_argv):
                raise ValueError("argv must be a list of strings")
            server = self.server
            assert isinstance(server, SafeOutputProxyServer)
            argv = validated_cli_argv(
                request_argv=request_argv,
                role_id=server.role_id,
                project_config=server.project_config,
                shared_fleet_guard=server.shared_fleet_guard,
                binding_project_id=server.binding_project_id,
                binding_generation=server.binding_generation,
            )
            exit_code, stdout, stderr = server.executor(argv)
            self._write_response({"exit_code": exit_code, "stdout": stdout, "stderr": stderr})
        except Exception as exc:
            self._write_response(
                {"exit_code": 1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}\n"}
            )

    def _write_response(self, response: dict[str, object]) -> None:
        self.wfile.write(json.dumps(response, sort_keys=True).encode("utf-8") + b"\n")


class _UnixStreamServer(socketserver.TCPServer):
    address_family = getattr(socket, "AF_UNIX", socket.AF_INET)


class SafeOutputProxyServer(_UnixStreamServer):
    def __init__(
        self,
        *,
        socket_path: Path,
        role_id: str,
        project_config: Path,
        executor: Callable[[list[str]], tuple[int, str, str]] = execute_cli,
        shared_fleet_guard: "SharedFleetOperationGuard | None" = None,
        binding_project_id: str | None = None,
        binding_generation: int | None = None,
    ) -> None:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        self.socket_path = socket_path
        self.role_id = role_id
        self.project_config = project_config
        self.executor = executor
        self.shared_fleet_guard = shared_fleet_guard
        self.binding_project_id = binding_project_id
        self.binding_generation = binding_generation
        super().__init__(str(socket_path), _SafeOutputRequestHandler)
        socket_path.chmod(0o600)

    def server_close(self) -> None:
        super().server_close()
        self.socket_path.unlink(missing_ok=True)


def main() -> None:
    from agentic_mesh_v4.config import load_project_config
    from agentic_mesh_v4.shared_fleet import CapturedBindingController
    from agentic_mesh_v4.shared_fleet import FleetBinding
    from agentic_mesh_v4.shared_fleet import SharedFleetOperationGuard
    from agentic_mesh_v4.shared_fleet import require_activation_ready
    from agentic_mesh_v4.shared_fleet import stable_fleet_instance_id

    parser = argparse.ArgumentParser(prog="agentic-mesh-v4-safe-output-proxy")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--role-id", required=True)
    parser.add_argument("--project-config", type=Path, required=True)
    parser.add_argument("--fleet-instance-id")
    parser.add_argument("--binding-project-id")
    parser.add_argument("--binding-generation", type=int)
    args = parser.parse_args()
    binding_values = (
        args.fleet_instance_id,
        args.binding_project_id,
        args.binding_generation,
    )
    if any(value is not None for value in binding_values) and not all(
        value is not None for value in binding_values
    ):
        parser.error("shared-fleet binding arguments must be supplied together")
    guard = None
    if all(value is not None for value in binding_values):
        config = load_project_config(args.project_config)
        if not config.shared_fleet.enabled:
            parser.error("shared-fleet binding requires enabled project configuration")
        require_activation_ready(config, operation="safe-output proxy construction")
        expected_instance_id = stable_fleet_instance_id(
            fleet_id=config.shared_fleet.fleet_id,
            role_id=args.role_id,
        )
        if args.fleet_instance_id != expected_instance_id:
            parser.error("shared-fleet binding identity does not match the proxy role")
        if args.binding_generation < 1:
            parser.error("shared-fleet binding generation must be positive")
        binding = FleetBinding(
            fleet_instance_id=args.fleet_instance_id,
            project_id=args.binding_project_id,
            generation=args.binding_generation,
            state="bound",
        )
        guard = SharedFleetOperationGuard(
            controller=CapturedBindingController(config.shared_fleet.project_assignments),
            binding=binding,
        )
        guard.require(
            project_id=args.binding_project_id,
            generation=args.binding_generation,
        )
    with SafeOutputProxyServer(
        socket_path=args.socket,
        role_id=args.role_id,
        project_config=args.project_config,
        shared_fleet_guard=guard,
        binding_project_id=args.binding_project_id,
        binding_generation=args.binding_generation,
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
