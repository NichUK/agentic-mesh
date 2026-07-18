from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Mapping, Sequence

import psycopg

from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA


_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class GitWorkspaceError(DatabaseError):
    pass


class GitWorkspaceConflict(GitWorkspaceError):
    pass


class GitWorkspaceNotFound(GitWorkspaceError):
    pass


class GitWorkspaceDirty(GitWorkspaceError):
    pass


@dataclass(frozen=True, slots=True)
class RepositoryWorktree:
    repository_id: str
    repository_url: str
    default_branch: str
    source_path: str
    worktree_path: str
    branch_name: str
    base_revision: str
    current_revision: str | None
    status: str
    last_error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "repository_url": self.repository_url,
            "default_branch": self.default_branch,
            "source_path": self.source_path,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "base_revision": self.base_revision,
            "current_revision": self.current_revision,
            "status": self.status,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class WorkItemWorkspace:
    project_id: str
    work_item_id: str
    manifest_digest: str
    workspace_root: str
    status: str
    created_by: str
    last_error: str | None
    repositories: tuple[RepositoryWorktree, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "work_item_id": self.work_item_id,
            "manifest_digest": self.manifest_digest,
            "workspace_root": self.workspace_root,
            "status": self.status,
            "created_by": self.created_by,
            "last_error": self.last_error,
            "repositories": [item.to_dict() for item in self.repositories],
        }


@dataclass(frozen=True, slots=True)
class _PlanRepository:
    repository_id: str
    repository_url: str
    default_branch: str
    source: Path
    worktree: Path
    branch: str
    base_revision: str


class GitWorktreeCoordinator:
    def __init__(self, database_url: str, *, workspace_root: Path) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        if not workspace_root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        if any(character in str(workspace_root) for character in ("\n", "\r", "\x00")):
            raise ValueError("workspace_root contains unsupported characters")
        self._database_url = database_url
        self._workspace_root = workspace_root.resolve()

    def prepare(
        self,
        *,
        project_id: str,
        work_item_id: str,
        actor_id: str,
        source_repositories: Mapping[str, Path],
        repository_ids: Sequence[str] | None = None,
    ) -> WorkItemWorkspace:
        project_id = _identifier(project_id, "project_id")
        work_item_id = _identifier(work_item_id, "work_item_id")
        actor_id = _text(actor_id, "actor_id")
        sources = {
            _identifier(key, "repository_id"): _absolute_source(value)
            for key, value in source_repositories.items()
        }
        selected = (
            tuple(sorted(sources))
            if repository_ids is None
            else tuple(sorted(_identifier(item, "repository_id") for item in repository_ids))
        )
        if not selected or len(selected) != len(set(selected)) or set(sources) != set(selected):
            raise ValueError("source_repositories must exactly match selected repository ids")
        self._validate_root_boundaries(sources.values())
        lock_keys = [f"workspace:{project_id}:{work_item_id}"] + [
            f"git-source:{path}" for path in sorted(str(path) for path in sources.values())
        ]
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                self._lock(connection, lock_keys)
                try:
                    current = self._read(connection, project_id, work_item_id)
                    digest, plan = self._plan(
                        connection,
                        project_id,
                        work_item_id,
                        sources,
                        selected,
                        current,
                    )
                    if current is None:
                        self._preflight_new(plan)
                        self._insert_plan(
                            connection,
                            project_id,
                            work_item_id,
                            actor_id,
                            digest,
                            plan,
                        )
                    else:
                        self._validate_existing(current, plan)
                        if current.status == "released":
                            raise GitWorkspaceConflict("workspace is already released")
                    self._set_workspace(connection, project_id, work_item_id, "preparing", None)
                    for repository in plan:
                        try:
                            self._prepare_repository(
                                connection, project_id, work_item_id, repository
                            )
                        except GitWorkspaceConflict:
                            self._set_workspace(
                                connection,
                                project_id,
                                work_item_id,
                                "error",
                                f"repository {repository.repository_id} conflicts with its plan",
                            )
                            raise
                        except GitWorkspaceError as exc:
                            detail = str(exc)
                            self._repository_error(
                                connection,
                                project_id,
                                work_item_id,
                                repository.repository_id,
                                detail,
                            )
                            self._set_workspace(
                                connection,
                                project_id,
                                work_item_id,
                                "error",
                                f"repository {repository.repository_id}: {detail}",
                            )
                            raise
                    self._set_workspace(connection, project_id, work_item_id, "ready", None)
                    return self._required_read(connection, project_id, work_item_id)
                finally:
                    self._unlock(connection, lock_keys)
        except GitWorkspaceError:
            raise
        except Exception as exc:
            raise GitWorkspaceError("Git workspace preparation failed") from exc

    def cleanup(
        self, *, project_id: str, work_item_id: str, actor_id: str
    ) -> WorkItemWorkspace:
        project_id = _identifier(project_id, "project_id")
        work_item_id = _identifier(work_item_id, "work_item_id")
        _text(actor_id, "actor_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                current = self._required_read(connection, project_id, work_item_id)
                lock_keys = [f"workspace:{project_id}:{work_item_id}"] + [
                    f"git-source:{item.source_path}" for item in current.repositories
                ]
                self._lock(connection, lock_keys)
                try:
                    current = self._required_read(connection, project_id, work_item_id)
                    if current.status == "released":
                        return current
                    self._set_workspace(connection, project_id, work_item_id, "releasing", None)
                    blocked: list[str] = []
                    for item in current.repositories:
                        if item.status == "released":
                            continue
                        try:
                            self._release_repository(
                                connection, project_id, work_item_id, item
                            )
                        except GitWorkspaceDirty:
                            blocked.append(item.repository_id)
                        except GitWorkspaceError as exc:
                            detail = str(exc)
                            self._repository_error(
                                connection,
                                project_id,
                                work_item_id,
                                item.repository_id,
                                detail,
                            )
                            self._set_workspace(
                                connection,
                                project_id,
                                work_item_id,
                                "error",
                                f"repository {item.repository_id}: {detail}",
                            )
                            raise
                    if blocked:
                        detail = "dirty worktrees: " + ", ".join(sorted(blocked))
                        self._set_workspace(
                            connection, project_id, work_item_id, "cleanup_blocked", detail
                        )
                        raise GitWorkspaceDirty(detail)
                    self._set_workspace(connection, project_id, work_item_id, "released", None)
                    return self._required_read(connection, project_id, work_item_id)
                finally:
                    self._unlock(connection, lock_keys)
        except GitWorkspaceError:
            raise
        except Exception as exc:
            raise GitWorkspaceError("Git workspace cleanup failed") from exc

    def get(self, project_id: str, work_item_id: str) -> WorkItemWorkspace | None:
        project_id = _identifier(project_id, "project_id")
        work_item_id = _identifier(work_item_id, "work_item_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                return self._read(connection, project_id, work_item_id)
        except Exception as exc:
            raise GitWorkspaceError("Git workspace read failed") from exc

    def _plan(
        self,
        connection,
        project_id: str,
        work_item_id: str,
        sources: Mapping[str, Path],
        selected: Sequence[str],
        current: WorkItemWorkspace | None,
    ) -> tuple[str, tuple[_PlanRepository, ...]]:
        work = connection.execute(
            f"SELECT 1 FROM {SCHEMA}.work_items WHERE project_id = %s AND work_item_id = %s",
            (project_id, work_item_id),
        ).fetchone()
        if work is None:
            raise GitWorkspaceNotFound("work item was not found")
        if current is None:
            manifest_row = connection.execute(
                f"""
                SELECT active.manifest_digest, snapshot.snapshot
                FROM {SCHEMA}.project_manifest_active AS active
                JOIN {SCHEMA}.project_manifest_snapshots AS snapshot
                  ON snapshot.project_id = active.project_id
                 AND snapshot.manifest_digest = active.manifest_digest
                WHERE active.project_id = %s
                """,
                (project_id,),
            ).fetchone()
        else:
            manifest_row = connection.execute(
                f"""
                SELECT manifest_digest, snapshot
                FROM {SCHEMA}.project_manifest_snapshots
                WHERE project_id = %s AND manifest_digest = %s
                """,
                (project_id, current.manifest_digest),
            ).fetchone()
        if manifest_row is None:
            raise GitWorkspaceNotFound("active project manifest was not found")
        repositories = manifest_row[1].get("repositories", {})
        if not isinstance(repositories, dict):
            raise GitWorkspaceConflict("active manifest repositories are invalid")
        branch = _branch_name(project_id, work_item_id)
        root = self._workspace_root / project_id / work_item_id
        existing = (
            {}
            if current is None
            else {item.repository_id: item for item in current.repositories}
        )
        plan: list[_PlanRepository] = []
        for repository_id in selected:
            manifest_repository = repositories.get(repository_id)
            if not isinstance(manifest_repository, dict):
                raise GitWorkspaceConflict(
                    f"repository {repository_id} is not in the active manifest"
                )
            source = sources[repository_id]
            url = self._git(source, "remote", "get-url", "origin").strip()
            if url != manifest_repository.get("url"):
                raise GitWorkspaceConflict(
                    f"repository {repository_id} origin does not match the manifest"
                )
            default_branch = manifest_repository.get("default_branch")
            if not isinstance(default_branch, str):
                raise GitWorkspaceConflict("manifest default branch is invalid")
            prior = existing.get(repository_id)
            base = (
                self._base_revision(source, default_branch)
                if prior is None
                else prior.base_revision
            )
            worktree = (root / repository_id).resolve()
            if not _is_within(worktree, self._workspace_root):
                raise GitWorkspaceConflict("worktree path escapes workspace_root")
            plan.append(
                _PlanRepository(
                    repository_id, url, default_branch, source, worktree, branch, base
                )
            )
        return manifest_row[0], tuple(plan)

    def _preflight_new(self, plan: Sequence[_PlanRepository]) -> None:
        for item in plan:
            if item.worktree.exists():
                raise GitWorkspaceConflict(f"worktree path already exists: {item.repository_id}")
            if self._ref_exists(item.source, f"refs/heads/{item.branch}"):
                raise GitWorkspaceConflict(
                    f"workspace branch already exists: {item.repository_id}"
                )

    def _insert_plan(
        self,
        connection,
        project_id: str,
        work_item_id: str,
        actor_id: str,
        manifest_digest: str,
        plan: Sequence[_PlanRepository],
    ) -> None:
        with connection.transaction():
            connection.execute(
                f"""
                INSERT INTO {SCHEMA}.git_workspaces
                    (project_id, work_item_id, manifest_digest, workspace_root,
                     status, created_by)
                VALUES (%s, %s, %s, %s, 'preparing', %s)
                """,
                (
                    project_id,
                    work_item_id,
                    manifest_digest,
                    str(self._workspace_root),
                    actor_id,
                ),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    f"""
                    INSERT INTO {SCHEMA}.git_workspace_repositories
                        (project_id, work_item_id, repository_id, repository_url,
                         default_branch, source_path, worktree_path, branch_name,
                         base_revision, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'planned')
                    """,
                    [
                        (
                            project_id,
                            work_item_id,
                            item.repository_id,
                            item.repository_url,
                            item.default_branch,
                            str(item.source),
                            str(item.worktree),
                            item.branch,
                            item.base_revision,
                        )
                        for item in plan
                    ],
                )

    def _validate_existing(
        self,
        current: WorkItemWorkspace,
        plan: Sequence[_PlanRepository],
    ) -> None:
        expected = {
            item.repository_id: (
                item.repository_url,
                str(item.source),
                str(item.worktree),
                item.branch,
                item.base_revision,
            )
            for item in plan
        }
        actual = {
            item.repository_id: (
                item.repository_url,
                item.source_path,
                item.worktree_path,
                item.branch_name,
                item.base_revision,
            )
            for item in current.repositories
        }
        if (
            current.workspace_root != str(self._workspace_root)
            or actual != expected
        ):
            raise GitWorkspaceConflict("workspace retry does not match its durable plan")

    def _prepare_repository(
        self,
        connection,
        project_id: str,
        work_item_id: str,
        item: _PlanRepository,
    ) -> None:
        if item.worktree.exists():
            head, branch = self._verify_worktree(item.source, item.worktree)
            if branch != item.branch:
                self._repository_error(
                    connection, project_id, work_item_id, item.repository_id,
                    "existing worktree branch does not match the durable plan",
                )
                raise GitWorkspaceConflict("existing worktree branch does not match")
        else:
            if self._ref_exists(item.source, f"refs/heads/{item.branch}"):
                self._repository_error(
                    connection, project_id, work_item_id, item.repository_id,
                    "planned branch exists without its registered worktree",
                )
                raise GitWorkspaceConflict("planned branch exists without worktree")
            item.worktree.parent.mkdir(parents=True, exist_ok=True)
            self._git(
                item.source,
                "worktree", "add", "--no-track", "-b", item.branch,
                str(item.worktree), item.base_revision,
            )
            head, branch = self._verify_worktree(item.source, item.worktree)
        if branch != item.branch or not _COMMIT.fullmatch(head):
            raise GitWorkspaceConflict("created worktree evidence is invalid")
        connection.execute(
            f"""
            UPDATE {SCHEMA}.git_workspace_repositories
            SET status = 'ready', current_revision = %s, last_error = NULL,
                prepared_at = COALESCE(prepared_at, clock_timestamp())
            WHERE project_id = %s AND work_item_id = %s AND repository_id = %s
            """,
            (head, project_id, work_item_id, item.repository_id),
        )

    def _release_repository(
        self,
        connection,
        project_id: str,
        work_item_id: str,
        item: RepositoryWorktree,
    ) -> None:
        source = Path(item.source_path)
        worktree = Path(item.worktree_path)
        if not worktree.exists():
            registered = self._registered_worktree(source, item.branch_name)
            if registered is not None:
                raise GitWorkspaceConflict(
                    "workspace branch is registered at an unexpected path"
                )
            head = self._branch_revision(source, item.branch_name)
        else:
            head, branch = self._verify_worktree(source, worktree)
            if branch != item.branch_name:
                raise GitWorkspaceConflict("cleanup worktree branch does not match")
            if self._git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
                connection.execute(
                    f"""
                    UPDATE {SCHEMA}.git_workspace_repositories
                    SET status = 'cleanup_blocked', current_revision = %s,
                        last_error = 'worktree contains uncommitted changes'
                    WHERE project_id = %s AND work_item_id = %s AND repository_id = %s
                    """,
                    (head, project_id, work_item_id, item.repository_id),
                )
                raise GitWorkspaceDirty("worktree contains uncommitted changes")
            self._git(source, "worktree", "remove", str(worktree))
        connection.execute(
            f"""
            UPDATE {SCHEMA}.git_workspace_repositories
            SET status = 'released', current_revision = %s, last_error = NULL,
                released_at = COALESCE(released_at, clock_timestamp())
            WHERE project_id = %s AND work_item_id = %s AND repository_id = %s
            """,
            (head, project_id, work_item_id, item.repository_id),
        )

    def _verify_worktree(self, source: Path, worktree: Path) -> tuple[str, str]:
        top = Path(self._git(worktree, "rev-parse", "--show-toplevel")).resolve()
        if top != worktree.resolve():
            raise GitWorkspaceConflict("registered worktree path is not a Git root")
        source_common = self._common_dir(source)
        worktree_common = self._common_dir(worktree)
        if source_common != worktree_common:
            raise GitWorkspaceConflict("worktree belongs to a different repository")
        head = self._git(worktree, "rev-parse", "HEAD").strip().lower()
        branch = self._git(worktree, "symbolic-ref", "--short", "HEAD").strip()
        return head, branch

    def _base_revision(self, source: Path, default_branch: str) -> str:
        for reference in (
            f"refs/remotes/origin/{default_branch}",
            f"refs/heads/{default_branch}",
        ):
            if self._ref_exists(source, reference):
                return self._git(source, "rev-parse", f"{reference}^{{commit}}").strip().lower()
        raise GitWorkspaceConflict("manifest default branch is unavailable locally")

    def _branch_revision(self, source: Path, branch: str) -> str:
        if not self._ref_exists(source, f"refs/heads/{branch}"):
            raise GitWorkspaceConflict("workspace branch evidence is missing")
        return self._git(source, "rev-parse", f"refs/heads/{branch}^{{commit}}").strip().lower()

    def _common_dir(self, path: Path) -> Path:
        value = Path(self._git(path, "rev-parse", "--git-common-dir"))
        return (path / value).resolve() if not value.is_absolute() else value.resolve()

    def _registered_worktree(self, source: Path, branch: str) -> Path | None:
        current: Path | None = None
        for field in self._git(source, "worktree", "list", "--porcelain").splitlines():
            if field.startswith("worktree "):
                current = Path(field.removeprefix("worktree ")).resolve()
            elif field == f"branch refs/heads/{branch}":
                return current
        return None

    def _ref_exists(self, source: Path, reference: str) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(source), "show-ref", "--verify", "--quiet", reference],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode not in {0, 1}:
            raise GitWorkspaceError("Git reference check failed")
        return completed.returncode == 0

    @staticmethod
    def _git(path: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = " ".join(completed.stderr.strip().splitlines())
            if len(detail) > 500:
                detail = detail[:497] + "..."
            suffix = f": {detail}" if detail else ""
            raise GitWorkspaceError(
                f"Git command failed: {arguments[0]} "
                f"(exit {completed.returncode}){suffix}"
            )
        return completed.stdout.strip()

    def _validate_root_boundaries(self, sources: Sequence[Path]) -> None:
        seen: set[Path] = set()
        for source in sources:
            if source in seen:
                raise ValueError("source repository paths must be unique")
            seen.add(source)
            if _is_within(self._workspace_root, source) or _is_within(
                source, self._workspace_root
            ):
                raise ValueError("workspace_root and source repositories must be separate")

    @staticmethod
    def _lock(connection, keys: Sequence[str]) -> None:
        for key in sorted(set(keys)):
            connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,)
            )

    @staticmethod
    def _unlock(connection, keys: Sequence[str]) -> None:
        for key in reversed(sorted(set(keys))):
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,)
            )

    @staticmethod
    def _set_workspace(
        connection,
        project_id: str,
        work_item_id: str,
        status: str,
        error: str | None,
    ) -> None:
        connection.execute(
            f"""
            UPDATE {SCHEMA}.git_workspaces
            SET status = %s, last_error = %s, updated_at = clock_timestamp(),
                released_at = CASE WHEN %s = 'released'
                                   THEN COALESCE(released_at, clock_timestamp())
                                   ELSE released_at END
            WHERE project_id = %s AND work_item_id = %s
            """,
            (status, error, status, project_id, work_item_id),
        )

    @staticmethod
    def _repository_error(
        connection,
        project_id: str,
        work_item_id: str,
        repository_id: str,
        detail: str,
    ) -> None:
        connection.execute(
            f"""
            UPDATE {SCHEMA}.git_workspace_repositories
            SET status = 'error', last_error = %s
            WHERE project_id = %s AND work_item_id = %s AND repository_id = %s
            """,
            (detail, project_id, work_item_id, repository_id),
        )

    def _required_read(
        self, connection, project_id: str, work_item_id: str
    ) -> WorkItemWorkspace:
        result = self._read(connection, project_id, work_item_id)
        if result is None:
            raise GitWorkspaceNotFound("workspace was not found")
        return result

    @staticmethod
    def _read(connection, project_id: str, work_item_id: str) -> WorkItemWorkspace | None:
        row = connection.execute(
            f"""
            SELECT manifest_digest, workspace_root, status, created_by, last_error
            FROM {SCHEMA}.git_workspaces
            WHERE project_id = %s AND work_item_id = %s
            """,
            (project_id, work_item_id),
        ).fetchone()
        if row is None:
            return None
        repositories = connection.execute(
            f"""
            SELECT repository_id, repository_url, default_branch, source_path,
                   worktree_path, branch_name, base_revision, current_revision,
                   status, last_error
            FROM {SCHEMA}.git_workspace_repositories
            WHERE project_id = %s AND work_item_id = %s
            ORDER BY repository_id
            """,
            (project_id, work_item_id),
        ).fetchall()
        return WorkItemWorkspace(
            project_id,
            work_item_id,
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            tuple(RepositoryWorktree(*item) for item in repositories),
        )


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _branch_name(project_id: str, work_item_id: str) -> str:
    component = f"{project_id}-{work_item_id}"
    if len(component.encode("ascii")) > 255:
        raise GitWorkspaceConflict("project and work item exceed Git branch length limit")
    return f"codex/{component}"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field} is invalid")
    return value.strip()


def _absolute_source(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or not value.is_dir():
        raise ValueError("source repository path must be an existing absolute directory")
    if any(character in str(value) for character in ("\n", "\r", "\x00")):
        raise ValueError("source repository path contains unsupported characters")
    return value.resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
