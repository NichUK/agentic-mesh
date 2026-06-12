from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any


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
