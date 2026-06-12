from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any
from typing import Protocol
from uuid import uuid4

from agentic_mesh_v2.db import V2Database


@dataclass(frozen=True)
class ComposeRoleLifecycleConfig:
    compose_files: tuple[Path, ...]
    service_name_template: str
    working_directory: Path | None = None

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "ComposeRoleLifecycleConfig":
        adapter = mapping.get("adapter")
        if adapter != "docker-compose":
            raise ValueError("container lifecycle adapter must be `docker-compose`")
        compose_files = mapping.get("compose_files")
        if not isinstance(compose_files, list) or not compose_files:
            raise ValueError("docker-compose container lifecycle requires compose_files")
        parsed_compose_files: list[Path] = []
        for index, item in enumerate(compose_files):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"compose_files item {index} must be a non-empty string")
            parsed_compose_files.append(Path(item))
        service_name_template = mapping.get("service_name_template")
        if not isinstance(service_name_template, str) or not service_name_template.strip():
            raise ValueError("docker-compose container lifecycle requires service_name_template")
        _validate_template(service_name_template)
        working_directory = mapping.get("working_directory")
        parsed_working_directory: Path | None = None
        if working_directory is not None:
            if not isinstance(working_directory, str) or not working_directory.strip():
                raise ValueError("working_directory must be a non-empty string when provided")
            parsed_working_directory = Path(working_directory)
        return cls(
            compose_files=tuple(parsed_compose_files),
            service_name_template=service_name_template,
            working_directory=parsed_working_directory,
        )

    def service_name(self, *, project_id: str, role_id: str, role_instance_id: str) -> str:
        index = _role_instance_index(role_instance_id)
        return self.service_name_template.format(
            project_id=project_id,
            role_id=role_id,
            role_instance_id=role_instance_id,
            index=index,
        )


@dataclass(frozen=True)
class ContainerLifecycleAction:
    role_id: str
    role_instance_id: str
    action: str
    service_name: str
    command: tuple[str, ...]
    working_directory: Path | None
    reason: str


@dataclass(frozen=True)
class ContainerCommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ContainerCommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path | None,
        timeout_seconds: int,
    ) -> ContainerCommandResult:
        ...


class ContainerLifecycleExecutor:
    def __init__(
        self,
        db: V2Database,
        *,
        runner: ContainerCommandRunner | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        if timeout_seconds < 1:
            raise ValueError("container lifecycle timeout_seconds must be at least 1")
        self.db = db
        self.runner = runner or _run_container_command
        self.timeout_seconds = timeout_seconds

    def record_plan(self, action: ContainerLifecycleAction) -> str:
        action_id = _action_id(action)
        action_fingerprint = _action_fingerprint(action)
        self.db.record_role_container_lifecycle_action(
            action_id=action_id,
            action_fingerprint=action_fingerprint,
            role_id=action.role_id,
            role_instance_id=action.role_instance_id,
            action=action.action,
            service_name=action.service_name,
            command=list(action.command),
            working_directory=str(action.working_directory) if action.working_directory is not None else None,
            status="planned",
            reason=action.reason,
        )
        return action_id

    def execute(self, action: ContainerLifecycleAction) -> tuple[str, ContainerCommandResult]:
        action_id = self.record_plan(action)
        result = self.runner(
            list(action.command),
            cwd=action.working_directory,
            timeout_seconds=self.timeout_seconds,
        )
        status = "succeeded" if result.exit_code == 0 else "failed"
        self.db.complete_role_container_lifecycle_action(
            action_id=action_id,
            status=status,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if result.exit_code != 0:
            self.db.create_runtime_attention_item(
                attention_id=f"attention-{action_id}",
                source_type="role_container_lifecycle",
                source_ref=action_id,
                owner="platform-engineer",
                reason_class="container_lifecycle_failed",
                next_action=(
                    "Inspect the failed role container lifecycle action, correct the runtime host "
                    "or deployment target, then retry the lifecycle action."
                ),
                retryable=True,
            )
        if result.exit_code == 0 and action.action == "start":
            self.db.update_role_instance_status(
                role_id=action.role_id,
                role_instance_id=action.role_instance_id,
                status="idle",
                detail="Role instance hydrated after container lifecycle start.",
            )
        return action_id, result


def plan_compose_lifecycle_action(
    *,
    config: ComposeRoleLifecycleConfig,
    project_id: str,
    role_id: str,
    role_instance_id: str,
    status: str,
    reason: str,
) -> ContainerLifecycleAction | None:
    if status == "hibernated":
        action = "stop"
    elif status == "hydrating":
        action = "start"
    else:
        return None
    service_name = config.service_name(
        project_id=project_id,
        role_id=role_id,
        role_instance_id=role_instance_id,
    )
    command = _docker_compose_command(config=config, action=action, service_name=service_name)
    return ContainerLifecycleAction(
        role_id=role_id,
        role_instance_id=role_instance_id,
        action=action,
        service_name=service_name,
        command=tuple(command),
        working_directory=config.working_directory,
        reason=reason,
    )


def _docker_compose_command(
    *,
    config: ComposeRoleLifecycleConfig,
    action: str,
    service_name: str,
) -> list[str]:
    prefix = [
        "docker",
        "compose",
        *[
            part
            for compose_file in config.compose_files
            for part in ("-f", str(compose_file))
        ],
    ]
    if action == "stop":
        return [*prefix, "stop", service_name]
    if action == "start":
        return [*prefix, "up", "-d", service_name]
    raise ValueError("container lifecycle action must be stop or start")


def _role_instance_index(role_instance_id: str) -> str:
    candidate = role_instance_id.rsplit(".", 1)[-1]
    if not candidate.isdigit():
        raise ValueError("role_instance_id must end with a numeric instance index")
    return candidate


def _validate_template(template: str) -> None:
    allowed = {"project_id", "role_id", "role_instance_id", "index"}
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name is None:
            continue
        if field_name not in allowed:
            raise ValueError(f"unsupported service_name_template field `{field_name}`")


def _action_id(action: ContainerLifecycleAction) -> str:
    return f"role-container-action-{uuid4().hex}"


def _action_fingerprint(action: ContainerLifecycleAction) -> str:
    value = "|".join(
        [
            action.role_instance_id,
            action.action,
            action.service_name,
            " ".join(action.command),
            action.reason,
        ]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _run_container_command(
    command: list[str],
    *,
    cwd: Path | None,
    timeout_seconds: int,
) -> ContainerCommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return ContainerCommandResult(
            exit_code=124,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\nTimed out after {timeout_seconds} seconds.",
        )
    except OSError as exc:
        return ContainerCommandResult(exit_code=127, stderr=str(exc))
    return ContainerCommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
