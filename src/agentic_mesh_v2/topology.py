from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class TopologyError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectRepo:
    repo_id: str
    path: Path
    document_library_root: Path
    target_repositories: tuple[Path, ...] = ()


@dataclass(frozen=True)
class RuntimeTopology:
    source_repo: Path
    deployed_runtime: Path
    runtime_state: Path
    project_repos: tuple[ProjectRepo, ...]
    image_identity: str | None = None
    allow_local_dev_overlap: bool = False
    local_dev_reason: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def validate(self) -> "RuntimeTopology":
        source = _resolve(self.source_repo)
        deployed = _resolve(self.deployed_runtime)
        state = _resolve(self.runtime_state)
        projects = tuple(
            ProjectRepo(
                repo_id=project.repo_id,
                path=_resolve(project.path),
                document_library_root=_resolve(project.document_library_root),
                target_repositories=tuple(
                    _resolve(target) for target in project.target_repositories
                ),
            )
            for project in self.project_repos
        )

        if self.allow_local_dev_overlap:
            if not self.local_dev_reason or not self.local_dev_reason.strip():
                raise TopologyError(
                    "local-dev topology overlap requires an explicit reason"
                )
            return RuntimeTopology(
                source_repo=source,
                deployed_runtime=deployed,
                runtime_state=state,
                project_repos=projects,
                image_identity=self.image_identity,
                allow_local_dev_overlap=True,
                local_dev_reason=self.local_dev_reason,
                warnings=(
                    "local-dev overlap enabled; do not use this topology for dogfood or production",
                ),
            )

        _reject_same("source_repo", source, "deployed_runtime", deployed)
        _reject_same("source_repo", source, "runtime_state", state)
        _reject_same("deployed_runtime", deployed, "runtime_state", state)
        if _contains(source, state) or _contains(deployed, state):
            raise TopologyError(
                "runtime_state must be an external mounted state root, not inside "
                "the source repo or deployed runtime tree"
            )

        repo_ids: set[str] = set()
        for project in projects:
            if project.repo_id in repo_ids:
                raise TopologyError(f"duplicate project repo id `{project.repo_id}`")
            repo_ids.add(project.repo_id)
            _reject_same("source_repo", source, f"project_repo[{project.repo_id}]", project.path)
            _reject_same(
                "deployed_runtime",
                deployed,
                f"project_repo[{project.repo_id}]",
                project.path,
            )
            _reject_same(
                "runtime_state",
                state,
                f"project_repo[{project.repo_id}]",
                project.path,
            )
            if _contains(source, project.path) or _contains(deployed, project.path):
                raise TopologyError(
                    f"project repo `{project.repo_id}` must not live inside the "
                    "source repo or deployed runtime tree"
                )
            if not _contains(project.path, project.document_library_root):
                raise TopologyError(
                    f"document library for `{project.repo_id}` must be inside its project repo"
                )
            for target in project.target_repositories:
                _reject_same(
                    "deployed_runtime",
                    deployed,
                    f"target_repository[{project.repo_id}]",
                    target,
                )
                if _contains(deployed, target):
                    raise TopologyError(
                        "target repositories must not point at the deployed runtime tree"
                    )

        return RuntimeTopology(
            source_repo=source,
            deployed_runtime=deployed,
            runtime_state=state,
            project_repos=projects,
            image_identity=self.image_identity,
            allow_local_dev_overlap=False,
            local_dev_reason=None,
        )


def _resolve(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def _reject_same(left_name: str, left: Path, right_name: str, right: Path) -> None:
    if left == right:
        raise TopologyError(f"{left_name} and {right_name} resolve to the same path")


def _contains(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents
