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
        command.extend(["up", "-d", service_name])
        subprocess.run(
            command,
            cwd=self.working_directory,
            env=self._compose_environment(),
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
            env=self._compose_environment(),
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
            env=self._compose_environment(),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        return service_name in {line.strip() for line in result.stdout.splitlines()}

    def _base_command(self) -> list[str]:
        command = ["docker", "compose"]
        if self.env_file is not None:
            command.extend(["--env-file", str(self.env_file)])
        if self.project_name:
            command.extend(["--project-name", self.project_name])
        for compose_file in self.compose_files:
            command.extend(["-f", str(compose_file)])
        return command

    def _compose_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self.env_file is not None:
            environment.update(_read_compose_env_file(self.env_file))
        return environment


def _read_compose_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _strip_env_quotes(value.strip())
    return values


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
