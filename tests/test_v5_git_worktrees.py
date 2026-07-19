from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest
import yaml

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.git_worktrees import GitWorkspaceConflict
from agentic_mesh_v5.git_worktrees import GitWorkspaceDirty
from agentic_mesh_v5.git_worktrees import GitWorkspaceError
from agentic_mesh_v5.git_worktrees import GitWorktreeCoordinator
from agentic_mesh_v5.git_worktrees import _branch_name
from agentic_mesh_v5.project_manifest import ProjectManifestStore
from agentic_mesh_v5.project_manifest import load_project_manifest


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for Postgres tests")
    database_name = f"mesh_v5_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    parsed = urlsplit(base_url)
    database_url = urlunsplit(parsed._replace(path=f"/{database_name}"))
    try:
        yield database_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(root: Path, name: str, url: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "Agentic Mesh Tests")
    (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "Initial commit")
    _git(path, "remote", "add", "origin", url)
    return path.resolve()


def _manifest(urls: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "alpha",
        "display_name": "Alpha",
        "packages": {
            "organization_release_digest": "a" * 64,
            "overrides": ["project-override/alpha@1.0.0"],
        },
        "repositories": {
            repository_id: {
                "url": url,
                "default_branch": "main",
                "credential": "git",
            }
            for repository_id, url in urls.items()
        },
        "documents": {},
        "teams": {
            "tenant_id": "tenant-one",
            "team_id": "team-alpha",
            "credential": "graph",
            "channels": {"project": "channel-alpha"},
        },
        "ado": {
            "organization": "https://dev.azure.com/seerstone",
            "project": "alpha",
            "credential": "ado",
        },
        "credentials": {
            name: {
                "scope": "project",
                "provider": name,
                "reference": f"secret://projects/alpha/{name}",
            }
            for name in ("git", "graph", "ado")
        },
        "roles": {
            "engineering": {
                "package": "role/engineering@1.0.0",
                "tool_profile": "tool-profile/development@1.0.0",
                "instances": {"minimum": 1, "maximum": 2},
            }
        },
        "limits": {"max_total_instances": 2},
    }


def _seed(
    database_url: str,
    tmp_path: Path,
    urls: dict[str, str],
    work_items: tuple[str, ...],
) -> dict[str, object]:
    assert MigrationRunner(database_url).migrate().current_version == 27
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO agentic_mesh_v5.projects(project_id, display_name) "
            "VALUES ('alpha', 'Alpha')"
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO agentic_mesh_v5.work_items
                    (project_id, work_item_id, title)
                VALUES ('alpha', %s, %s)
                """,
                [(item, f"Work {item}") for item in work_items],
            )
    value = _manifest(urls)
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    manifest = load_project_manifest(path)
    ProjectManifestStore(database_url).activate(
        manifest, source_revision="a" * 40, actor_id="project-admin"
    )
    return value


def test_multi_repository_work_is_isolated_and_cleanup_preserves_user_changes(
    tmp_path: Path, postgres_database: str
):
    urls = {
        "primary": "https://github.com/example/alpha.git",
        "supporting": "https://github.com/example/alpha-supporting.git",
    }
    sources = {
        name: _repository(tmp_path / "sources", name, url)
        for name, url in urls.items()
    }
    _seed(postgres_database, tmp_path, urls, ("work-one", "work-two"))
    tracked = sources["primary"] / "README.md"
    tracked.write_text("# primary\nuser edit\n", encoding="utf-8")
    untracked = sources["primary"] / "user-note.txt"
    untracked.write_text("preserve me\n", encoding="utf-8")
    source_status = _git(sources["primary"], "status", "--porcelain=v1")
    workspace_root = (tmp_path / "workspaces").resolve()
    coordinator = GitWorktreeCoordinator(
        postgres_database, workspace_root=workspace_root
    )

    def prepare(work_item_id: str):
        return coordinator.prepare(
            project_id="alpha",
            work_item_id=work_item_id,
            actor_id=f"engineering-{work_item_id}",
            source_repositories=sources,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(pool.map(prepare, ("work-one", "work-two")))
    assert first.status == second.status == "ready"
    assert first.manifest_digest == second.manifest_digest
    assert {item.repository_id for item in first.repositories} == set(urls)
    first_paths = {item.repository_id: Path(item.worktree_path) for item in first.repositories}
    second_paths = {
        item.repository_id: Path(item.worktree_path) for item in second.repositories
    }
    assert set(first_paths.values()).isdisjoint(second_paths.values())
    assert {item.branch_name for item in first.repositories} == {
        "codex/alpha-work-one"
    }
    assert {item.branch_name for item in second.repositories} == {
        "codex/alpha-work-two"
    }

    replay = coordinator.prepare(
        project_id="alpha",
        work_item_id="work-one",
        actor_id="replacement-engineer",
        source_repositories=sources,
    )
    assert replay == first
    isolated = first_paths["primary"] / "isolated.txt"
    isolated.write_text("only work one\n", encoding="utf-8")
    assert not (second_paths["primary"] / "isolated.txt").exists()
    assert not (sources["primary"] / "isolated.txt").exists()

    with pytest.raises(GitWorkspaceDirty, match="primary"):
        coordinator.cleanup(
            project_id="alpha", work_item_id="work-one", actor_id="engineering"
        )
    blocked = coordinator.get("alpha", "work-one")
    assert blocked is not None and blocked.status == "cleanup_blocked"
    assert isolated.exists()
    isolated.unlink()
    released = coordinator.cleanup(
        project_id="alpha", work_item_id="work-one", actor_id="replacement-engineer"
    )
    for item in second.repositories:
        _git(Path(item.source_path), "worktree", "remove", item.worktree_path)
    second_released = coordinator.cleanup(
        project_id="alpha", work_item_id="work-two", actor_id="engineering"
    )
    assert released.status == second_released.status == "released"
    assert all(item.status == "released" for item in released.repositories)
    assert all(not Path(item.worktree_path).exists() for item in released.repositories)
    assert _git(sources["primary"], "status", "--porcelain=v1") == source_status
    assert tracked.read_text(encoding="utf-8") == "# primary\nuser edit\n"
    assert untracked.read_text(encoding="utf-8") == "preserve me\n"
    for item in (*released.repositories, *second_released.repositories):
        assert _git(
            Path(item.source_path), "show-ref", "--verify", f"refs/heads/{item.branch_name}"
        )


def test_partial_prepare_resumes_pinned_plan_and_rejects_unowned_path(
    tmp_path: Path, postgres_database: str, monkeypatch: pytest.MonkeyPatch
):
    urls = {
        "primary": "https://github.com/example/alpha.git",
        "supporting": "https://github.com/example/alpha-supporting.git",
    }
    sources = {
        name: _repository(tmp_path / "sources", name, url)
        for name, url in urls.items()
    }
    original_manifest = _seed(
        postgres_database, tmp_path, urls, ("crash-work", "occupied-work")
    )
    root = (tmp_path / "workspaces").resolve()
    coordinator = GitWorktreeCoordinator(postgres_database, workspace_root=root)
    original_prepare = coordinator._prepare_repository
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("simulated process crash")
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_prepare_repository", interrupted)
    with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
        coordinator.prepare(
            project_id="alpha",
            work_item_id="crash-work",
            actor_id="engineering-one",
            source_repositories=sources,
        )
    partial = coordinator.get("alpha", "crash-work")
    assert partial is not None and partial.status == "preparing"
    assert {item.status for item in partial.repositories} == {"ready", "planned"}

    prior_supporting = _git(sources["supporting"], "rev-parse", "HEAD")
    (sources["supporting"] / "later.txt").write_text("later\n", encoding="utf-8")
    _git(sources["supporting"], "add", "later.txt")
    _git(sources["supporting"], "commit", "-m", "Advance default branch")
    changed = deepcopy(original_manifest)
    changed["packages"]["overrides"] = ["project-override/alpha@1.1.0"]
    changed_path = tmp_path / "changed-project.yaml"
    changed_path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    changed_manifest = load_project_manifest(changed_path)
    ProjectManifestStore(postgres_database).activate(
        changed_manifest, source_revision="b" * 40, actor_id="project-admin"
    )

    resumed = GitWorktreeCoordinator(
        postgres_database, workspace_root=root
    ).prepare(
        project_id="alpha",
        work_item_id="crash-work",
        actor_id="engineering-two",
        source_repositories=sources,
    )
    assert resumed.status == "ready"
    assert resumed.manifest_digest != changed_manifest.digest
    supporting = next(
        item for item in resumed.repositories if item.repository_id == "supporting"
    )
    assert supporting.base_revision == prior_supporting
    assert supporting.current_revision == prior_supporting

    primary = next(item for item in resumed.repositories if item.repository_id == "primary")
    moved = (root / "moved-primary").resolve()
    _git(sources["primary"], "worktree", "move", primary.worktree_path, str(moved))
    with pytest.raises(GitWorkspaceConflict, match="unexpected path"):
        GitWorktreeCoordinator(postgres_database, workspace_root=root).cleanup(
            project_id="alpha", work_item_id="crash-work", actor_id="engineering-two"
        )
    failed = coordinator.get("alpha", "crash-work")
    assert failed is not None and failed.status == "error"
    assert moved.exists()

    occupied = root / "alpha" / "occupied-work" / "primary"
    occupied.mkdir(parents=True)
    sentinel = occupied / "user-file.txt"
    sentinel.write_text("not ours\n", encoding="utf-8")
    with pytest.raises(GitWorkspaceConflict, match="already exists"):
        GitWorktreeCoordinator(postgres_database, workspace_root=root).prepare(
            project_id="alpha",
            work_item_id="occupied-work",
            actor_id="engineering",
            source_repositories=sources,
        )
    assert sentinel.read_text(encoding="utf-8") == "not ours\n"
    assert GitWorktreeCoordinator(
        postgres_database, workspace_root=root
    ).get("alpha", "occupied-work") is None


def test_repository_substitution_and_live_source_root_fail_closed(
    tmp_path: Path, postgres_database: str, monkeypatch: pytest.MonkeyPatch
):
    urls = {
        "primary": "https://github.com/example/alpha.git",
        "supporting": "https://github.com/example/alpha-supporting.git",
    }
    sources = {
        name: _repository(tmp_path / "sources", name, url)
        for name, url in urls.items()
    }
    _seed(postgres_database, tmp_path, urls, ("boundary-work", "failure-work"))
    with pytest.raises(ValueError, match="must be separate"):
        GitWorktreeCoordinator(
            postgres_database, workspace_root=sources["primary"] / "workspaces"
        ).prepare(
            project_id="alpha",
            work_item_id="boundary-work",
            actor_id="engineering",
            source_repositories=sources,
        )
    swapped = {"primary": sources["supporting"], "supporting": sources["primary"]}
    with pytest.raises(GitWorkspaceConflict, match="origin does not match"):
        GitWorktreeCoordinator(
            postgres_database, workspace_root=(tmp_path / "safe-workspaces").resolve()
        ).prepare(
            project_id="alpha",
            work_item_id="boundary-work",
            actor_id="engineering",
            source_repositories=swapped,
        )
    assert not (tmp_path / "safe-workspaces").exists()

    coordinator = GitWorktreeCoordinator(
        postgres_database, workspace_root=(tmp_path / "failure-workspaces").resolve()
    )
    original_git = coordinator._git

    def denied(path: Path, *arguments: str) -> str:
        if arguments[:2] == ("worktree", "add"):
            raise GitWorkspaceError("Git command failed: worktree (exit 7): denied")
        return original_git(path, *arguments)

    monkeypatch.setattr(coordinator, "_git", denied)
    with pytest.raises(GitWorkspaceError, match="exit 7"):
        coordinator.prepare(
            project_id="alpha",
            work_item_id="failure-work",
            actor_id="engineering",
            source_repositories=sources,
        )
    failed = coordinator.get("alpha", "failure-work")
    assert failed is not None and failed.status == "error"
    repository = next(item for item in failed.repositories if item.repository_id == "primary")
    assert repository.status == "error"
    assert repository.last_error == "Git command failed: worktree (exit 7): denied"


def test_branch_limit_and_git_failure_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with pytest.raises(GitWorkspaceConflict, match="branch length limit"):
        _branch_name("a" * 128, "b" * 128)
    assert _branch_name("alpha", "work-one") == "codex/alpha-work-one"

    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=7, stdout="", stderr="permission denied\ntry later"
        )

    monkeypatch.setattr("agentic_mesh_v5.git_worktrees.subprocess.run", failed)
    with pytest.raises(
        GitWorkspaceError,
        match=r"Git command failed: status \(exit 7\): permission denied try later",
    ):
        GitWorktreeCoordinator._git(tmp_path, "status")
