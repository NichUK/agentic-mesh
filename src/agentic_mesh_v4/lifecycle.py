from __future__ import annotations

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
        command = self._base_command()
        command.extend(["up", "-d", "--no-deps", service_name])
        subprocess.run(
            command,
            cwd=self.working_directory,
            env=self._compose_env(),
            check=True,
            capture_output=True,
            text=True,
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

    def exec_service(self, service_name: str, command_args: list[str]) -> subprocess.CompletedProcess[str]:
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

    def _base_command(self) -> list[str]:
        command = ["docker", "compose"]
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
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            if key:
                env[key] = value
        return env
