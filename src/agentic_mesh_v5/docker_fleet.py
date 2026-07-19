from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Callable, Mapping, Sequence

from agentic_mesh_v5.fleet import FleetAction, FleetSupervisorError


_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_ERROR = "fleet container operation failed"


class DockerContainerFleetSupervisor:
    """Start and stop exact pre-provisioned role containers without a shell."""

    def __init__(
        self,
        mapping_file: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        docker_command: str = "docker",
        stop_seconds: int = 30,
    ) -> None:
        if not isinstance(mapping_file, Path) or not mapping_file.is_absolute():
            raise ValueError("fleet mapping path must be absolute")
        if not mapping_file.is_file():
            raise ValueError("fleet mapping is unavailable")
        if not callable(runner):
            raise ValueError("fleet command runner is invalid")
        if (
            not isinstance(docker_command, str)
            or not docker_command
            or any(item in docker_command for item in ("\0", "\r", "\n"))
        ):
            raise ValueError("docker command is invalid")
        if (
            isinstance(stop_seconds, bool)
            or not isinstance(stop_seconds, int)
            or not 1 <= stop_seconds <= 300
        ):
            raise ValueError("fleet stop timeout is invalid")
        self._mapping = _load_mapping(mapping_file)
        self._runner = runner
        self._docker = docker_command
        self._stop_seconds = stop_seconds

    def apply(self, action: FleetAction) -> None:
        if not isinstance(action, FleetAction):
            raise ValueError("fleet action is invalid")
        selected = self._mapping.get((action.project_id, action.instance_id))
        if selected is None or selected[0] != action.role_id:
            raise FleetSupervisorError("fleet instance is not provisioned")
        container = selected[1]
        running = self._is_running(container)
        if action.action == "wake":
            if running:
                return
            self._run((self._docker, "start", container))
            return
        if action.action == "hibernate":
            if not running:
                return
            self._run(
                (
                    self._docker,
                    "stop",
                    "--time",
                    str(self._stop_seconds),
                    container,
                ),
                timeout=self._stop_seconds + 30,
            )
            return
        raise FleetSupervisorError("fleet action is unsupported")

    def _is_running(self, container: str) -> bool:
        result = self._run(
            (self._docker, "inspect", "--format", "{{.State.Running}}", container)
        )
        value = result.stdout.strip().casefold()
        if value not in {"true", "false"}:
            raise FleetSupervisorError(_SAFE_ERROR)
        return value == "true"

    def _run(
        self, command: Sequence[str], *, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(
                list(command),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception:
            raise FleetSupervisorError(_SAFE_ERROR) from None
        if (
            not isinstance(result, subprocess.CompletedProcess)
            or result.returncode != 0
        ):
            raise FleetSupervisorError(_SAFE_ERROR)
        return result


def _load_mapping(path: Path) -> Mapping[tuple[str, str], tuple[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        projects = payload["projects"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise ValueError("fleet mapping is invalid") from None
    if not isinstance(projects, dict) or not projects:
        raise ValueError("fleet mapping is invalid")
    result: dict[tuple[str, str], tuple[str, str]] = {}
    for project_id, project in projects.items():
        if (
            not isinstance(project_id, str)
            or _ID.fullmatch(project_id) is None
            or not isinstance(project, dict)
        ):
            raise ValueError("fleet mapping is invalid")
        instances = project.get("instances")
        if not isinstance(instances, dict) or not instances:
            raise ValueError("fleet mapping is invalid")
        for instance_id, record in instances.items():
            if (
                not isinstance(instance_id, str)
                or _ID.fullmatch(instance_id) is None
                or not isinstance(record, dict)
            ):
                raise ValueError("fleet mapping is invalid")
            role_id = record.get("role_id")
            container = record.get("container")
            if (
                not isinstance(role_id, str)
                or _ID.fullmatch(role_id) is None
                or not isinstance(container, str)
                or _CONTAINER.fullmatch(container) is None
            ):
                raise ValueError("fleet mapping is invalid")
            key = (project_id, instance_id)
            if key in result:
                raise ValueError("fleet mapping is invalid")
            result[key] = (role_id, container)
    return result
