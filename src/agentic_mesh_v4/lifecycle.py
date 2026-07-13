from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ComposeLifecycle:
    compose_files: tuple[Path, ...]
    project_name: str | None = None
    env_file: Path | None = None
    working_directory: Path | None = None

    def wake_service(self, service_name: str) -> None:
        env = self._compose_env()
        self._validate_required_file_binds(service_name=service_name, env=env)
        command = self._base_command()
        command.extend(["up", "-d", "--no-deps", service_name])
        subprocess.run(
            command,
            cwd=self.working_directory,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    def _validate_required_file_binds(
        self,
        *,
        service_name: str,
        env: dict[str, str],
    ) -> None:
        """Reject absent or non-file binds before Docker can create a service."""

        command = self._base_command(all_profiles=True)
        command.extend(["config", "--format", "json"])
        result = subprocess.run(
            command,
            cwd=self.working_directory,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        compose = json.loads(result.stdout)
        services = compose.get("services", {})
        service = services.get(service_name) if isinstance(services, dict) else None
        if not isinstance(service, dict):
            raise RuntimeError(
                f"Compose service is missing from effective configuration: {service_name}"
            )
        image = service.get("image")
        volumes = service.get("volumes", [])
        if not isinstance(volumes, list):
            return
        for volume in volumes:
            if not _is_required_file_bind(volume):
                continue
            if not isinstance(image, str) or not image:
                raise RuntimeError(
                    f"Compose service {service_name} has a required file bind but no probe image."
                )
            source = str(volume["source"])
            target = str(volume["target"])
            probe = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--entrypoint",
                    "/bin/sh",
                    "--mount",
                    (
                        "type=bind,"
                        f"source={source},"
                        "target=/agentic-mesh-required-file,"
                        "readonly"
                    ),
                    image,
                    "-ec",
                    "test -f /agentic-mesh-required-file",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if probe.returncode != 0:
                detail = (probe.stderr or probe.stdout).strip()
                suffix = f" Docker reported: {detail}" if detail else ""
                raise RuntimeError(
                    "Required regular-file bind failed preflight for "
                    f"{service_name}: {source} -> {target}. "
                    "The source must exist as a regular file before service creation; "
                    f"Docker was not allowed to create it.{suffix}"
                )

    def hibernate_service(self, service_name: str) -> None:
        command = self._base_command()
        command.extend(["stop", service_name])
        subprocess.run(
            command,
            cwd=self.working_directory,
            env=self._compose_env(),
            check=True,
            capture_output=True,
            text=True,
        )

    def is_service_running(self, service_name: str) -> bool:
        command = self._base_command()
        command.extend(["ps", "--status", "running", "--services"])
        result = subprocess.run(
            command,
            cwd=self.working_directory,
            env=self._compose_env(),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        return service_name in {line.strip() for line in result.stdout.splitlines()}

    def exec_service(
        self, service_name: str, command_args: list[str]
    ) -> subprocess.CompletedProcess[str]:
        command = self._base_command()
        command.extend(["exec", "-T", service_name, *command_args])
        return subprocess.run(
            command,
            cwd=self.working_directory,
            env=self._compose_env(),
            check=False,
            capture_output=True,
            text=True,
        )

    def _base_command(self, *, all_profiles: bool = False) -> list[str]:
        command = ["docker", "compose"]
        if all_profiles:
            command.extend(["--profile", "*"])
        if self.env_file is not None:
            command.extend(["--env-file", str(self.env_file)])
        if self.project_name:
            command.extend(["--project-name", self.project_name])
        for compose_file in self.compose_files:
            command.extend(["-f", str(compose_file)])
        return command

    def _compose_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.env_file is None or not self.env_file.exists():
            return env
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if key:
                env[key] = value
        return env


def _is_required_file_bind(volume: object) -> bool:
    if not isinstance(volume, dict) or volume.get("type") != "bind":
        return False
    bind = volume.get("bind")
    return isinstance(bind, dict) and bind.get("create_host_path") is False
