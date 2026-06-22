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
            env=os.environ.copy(),
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
            env=os.environ.copy(),
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
            env=os.environ.copy(),
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
