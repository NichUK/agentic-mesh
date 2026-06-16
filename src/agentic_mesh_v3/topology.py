from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class V3Topology:
    source_repo: Path
    deployed_runtime: Path
    runtime_state: Path
    organisation_config_repo: Path
    project_config_repo: Path
    document_library_root: Path
    local_dev_override: bool = False

    def resolved(self) -> "V3Topology":
        return V3Topology(
            source_repo=self.source_repo.resolve(strict=False),
            deployed_runtime=self.deployed_runtime.resolve(strict=False),
            runtime_state=self.runtime_state.resolve(strict=False),
            organisation_config_repo=self.organisation_config_repo.resolve(strict=False),
            project_config_repo=self.project_config_repo.resolve(strict=False),
            document_library_root=self.document_library_root.resolve(strict=False),
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
                if paths[left_name] == paths[right_name]:
                    errors.append(f"{left_name} and {right_name} resolve to the same path: {paths[left_name]}")
    if _is_relative_to(resolved.runtime_state, resolved.source_repo):
        errors.append("runtime_state must not live inside the source repo")
    if _is_relative_to(resolved.document_library_root, resolved.deployed_runtime):
        errors.append("document_library_root must not live inside the deployed runtime install")
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
