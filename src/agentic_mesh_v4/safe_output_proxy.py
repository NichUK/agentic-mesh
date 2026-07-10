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


MAX_REQUEST_BYTES = 8 * 1024 * 1024


def validated_cli_argv(*, request_argv: list[str], role_id: str, project_config: Path) -> list[str]:
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
    ) -> None:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        self.socket_path = socket_path
        self.role_id = role_id
        self.project_config = project_config
        self.executor = executor
        super().__init__(str(socket_path), _SafeOutputRequestHandler)
        socket_path.chmod(0o600)

    def server_close(self) -> None:
        super().server_close()
        self.socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentic-mesh-v4-safe-output-proxy")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--role-id", required=True)
    parser.add_argument("--project-config", type=Path, required=True)
    args = parser.parse_args()
    with SafeOutputProxyServer(
        socket_path=args.socket,
        role_id=args.role_id,
        project_config=args.project_config,
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
