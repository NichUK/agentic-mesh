from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path


@dataclass(frozen=True)
class V3Topology:
    source_repo: Path
    deployed_runtime: Path
    runtime_state: Path
    organisation_config_repo: Path
    project_config_repo: Path
    document_library_root: Path
    target_repositories: dict[str, Path] = field(default_factory=dict)
    local_dev_override: bool = False

    def resolved(self) -> "V3Topology":
        return V3Topology(
            source_repo=self.source_repo.resolve(strict=False),
            deployed_runtime=self.deployed_runtime.resolve(strict=False),
            runtime_state=self.runtime_state.resolve(strict=False),
            organisation_config_repo=self.organisation_config_repo.resolve(strict=False),
            project_config_repo=self.project_config_repo.resolve(strict=False),
            document_library_root=self.document_library_root.resolve(strict=False),
            target_repositories={
                repository_id: path.resolve(strict=False)
                for repository_id, path in self.target_repositories.items()
            },
            local_dev_override=self.local_dev_override,
        )


def validate_topology(topology: V3Topology) -> list[str]:
    resolved = topology.resolved()
    paths = {
        "source_repo": resolved.source_repo,
        "deployed_runtime": resolved.deployed_runtime,
        "runtime_state": resolved.runtime_state,
        "organisation_config_repo": resolved.organisation_config_repo,
        "project_config_repo": resolved.project_config_repo,
        "document_library_root": resolved.document_library_root,
    }
    errors: list[str] = []
    if not resolved.local_dev_override:
        names = list(paths)
        for index, left_name in enumerate(names):
            for right_name in names[index + 1 :]:
                errors.extend(_path_boundary_errors(left_name, paths[left_name], right_name, paths[right_name]))
        errors.extend(_target_repository_boundary_errors(resolved))
    return errors


def require_valid_topology(topology: V3Topology) -> None:
    errors = validate_topology(topology)
    if errors:
        raise ValueError("; ".join(errors))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _path_boundary_errors(left_name: str, left: Path, right_name: str, right: Path) -> list[str]:
    if left == right:
        return [f"{left_name} and {right_name} resolve to the same path: {left}"]
    if _is_relative_to(left, right):
        return [f"{left_name} must not live inside {right_name}: {left} is inside {right}"]
    if _is_relative_to(right, left):
        return [f"{right_name} must not live inside {left_name}: {right} is inside {left}"]
    return []


def _target_repository_boundary_errors(topology: V3Topology) -> list[str]:
    protected_paths = {
        "deployed_runtime": topology.deployed_runtime,
        "runtime_state": topology.runtime_state,
        "organisation_config_repo": topology.organisation_config_repo,
        "project_config_repo": topology.project_config_repo,
        "document_library_root": topology.document_library_root,
    }
    errors: list[str] = []
    target_paths = {
        f"target_repository.{repository_id}": path
        for repository_id, path in topology.target_repositories.items()
    }
    for target_name, target_path in target_paths.items():
        for protected_name, protected_path in protected_paths.items():
            errors.extend(_path_boundary_errors(target_name, target_path, protected_name, protected_path))
    names = list(target_paths)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            errors.extend(_path_boundary_errors(left_name, target_paths[left_name], right_name, target_paths[right_name]))
    return errors
