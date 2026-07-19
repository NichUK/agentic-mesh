from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest
import yaml

from agentic_mesh_v5.config_activation import ConfigActivationStore
from agentic_mesh_v5.cli import main
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.package_resolver import resolve_packages
from agentic_mesh_v5.project_manifest import ProjectManifestStore
from agentic_mesh_v5.project_manifest import load_project_manifest
from agentic_mesh_v5.project_registration import ProjectRegistrationConflict
from agentic_mesh_v5.project_registration import ProjectRegistrationCoordinator


IMAGE = "ghcr.io/example/agentic-mesh-v5@sha256:" + "b" * 64
ROOT = Path(__file__).parents[1]
EXTERNAL_CONFIG = Path(
    os.environ.get(
        "AGENTIC_MESH_CONFIG_REPOSITORY",
        ROOT.parent / "agentic-mesh-config",
    )
)
SDLC_RELEASE_REFERENCES = (
    "system/core@0.1.0",
    "policy/behavioral-guardrails@0.1.0",
    "flow/sdlc@0.1.0",
    "role/business-analyst@0.1.0",
    "role/delivery-manager@0.1.0",
    "role/engineering@0.1.0",
    "role/enterprise-architect@0.1.0",
    "role/platform-engineer@0.1.0",
    "role/product-manager@0.1.0",
    "role/project-manager@0.1.0",
    "role/prompt-engineer@0.1.0",
    "role/qa-engineer@0.1.0",
    "role/release-manager@0.1.0",
    "role/research-analyst@0.1.0",
    "role/security-architect@0.1.0",
    "role/solution-architect@0.1.0",
    "role/technical-writer@0.1.0",
    "role/ux-designer@0.1.0",
    "tool-profile/general@0.2.0",
    "tool-profile/development@0.1.0",
    "tool-profile/qa@0.1.0",
    "tool-profile/operations@0.1.0",
    "tool-profile/ux@0.1.0",
)


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for Postgres tests")
    database_name = f"mesh_v5_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
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


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_package(
    root: Path, reference: str, settings: dict[str, object]
) -> None:
    path, version = reference.split("@", 1)
    kind, package_id = path.split("/", 1)
    package = root / "packages" / kind / package_id / version
    package.mkdir(parents=True)
    content_name = f"{kind}.json"
    (package / content_name).write_text(
        json.dumps(settings, sort_keys=True) + "\n", encoding="utf-8"
    )
    (package / "package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": package_id,
                "kind": kind,
                "version": version,
                "content": [content_name],
                "dependencies": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _configuration(root: Path) -> tuple[Path, str, str]:
    root.mkdir()
    (root / "schemas").mkdir()
    (root / "schemas" / "package.schema.json").write_text(
        "{}\n", encoding="utf-8"
    )
    _write_package(root, "system/core@1.0.0", {"runtime": {"version": 5}})
    _write_package(
        root,
        "project-override/agentic-mesh-v5@1.0.0",
        {
            "project_id": "agentic-mesh-v5",
            "instructions": ["Use isolated worktrees."],
        },
    )
    _write_package(
        root,
        "role/project-manager@1.0.0",
        {
            "role": {
                "schema_version": 1,
                "role_id": "project-manager",
                "display_name": "Project Manager",
                "role_class": "general",
                "purpose": "Keep bounded work moving to a terminal outcome.",
                "accountabilities": ["Own project continuation."],
                "decision_rights": {
                    "owns": ["Project routing."],
                    "must_not": ["Bypass sponsor gates."],
                },
                "consults": [],
                "handoff_targets": [],
                "memory_scope": "project-role",
                "instructions": ["Use the smallest reliable solution."],
                "documentation": ["Project record."],
            }
        },
    )
    _write_package(
        root,
        "flow/sdlc@1.0.0",
        {
            "flow": {
                "schema_version": 1,
                "flow_id": "sdlc",
                "leader_role": "project-manager",
                "entry_state": "delivery",
                "terminal_states": ["delivery"],
                "states": {
                    "delivery": {
                        "owner_role": "project-manager",
                        "purpose": "Deliver the bounded change.",
                        "artifact": "work/{work_item_id}/delivery.md",
                        "consults": [],
                        "gates": [],
                        "routes": [],
                        "terminal": True,
                    }
                },
            }
        },
    )
    _write_package(
        root,
        "tool-profile/general@1.0.0",
        {
            "tool_profile": {
                "schema_version": 1,
                "profile_id": "general",
                "image": {
                    "repository": "agentic-mesh/worker-general",
                    "tag": "1.0.0",
                    "platform": "linux/amd64",
                },
                "capabilities": [
                    {"id": "filesystem.read", "required": True},
                    {"id": "structured-output", "required": True},
                ],
                "mounts": [
                    {
                        "id": "configuration",
                        "source": "organization-config",
                        "target": "/mesh/config",
                        "access": "read-only",
                        "required": True,
                    }
                ],
                "credentials": [
                    {
                        "id": "codex-auth",
                        "kind": "oauth-cache",
                        "delivery": "mount",
                        "target": "/mesh/credentials/codex",
                        "required": True,
                    }
                ],
                "health": {
                    "command": ["agentic-mesh-worker-healthcheck"],
                    "interval_seconds": 30,
                    "timeout_seconds": 5,
                    "failure_threshold": 3,
                },
                "resources": {
                    "cpu_millis": 1000,
                    "memory_mb": 2048,
                    "ephemeral_storage_mb": 4096,
                },
            }
        },
    )
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "mesh@example.com")
    _git(root, "config", "user.name", "Agentic Mesh")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create external configuration")
    revision = _git(root, "rev-parse", "HEAD")
    store = ConfigActivationStore(root)
    release = store.create_release(["system/core@1.0.0"], actor="sponsor")
    store.activate(release.digest, actor="sponsor", expected_active=None)
    return root, revision, release.digest


def _source(root: Path, project_id: str) -> tuple[Path, str, str]:
    root.mkdir()
    (root / "candidate.py").write_text(
        'def version():\n    return "v1"\n', encoding="utf-8"
    )
    (root / "test_candidate.py").write_text(
        "from candidate import version\n\n\n"
        "def test_version():\n"
        '    assert version() == "v1"\n',
        encoding="utf-8",
    )
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "mesh@example.com")
    _git(root, "config", "user.name", "Agentic Mesh")
    repository_url = f"https://github.com/example/{project_id}.git"
    _git(root, "remote", "add", "origin", repository_url)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create V5 source fixture")
    return root, repository_url, _git(root, "rev-parse", "HEAD")


def _manifest(
    root: Path,
    *,
    project_id: str,
    release_digest: str,
    repository_url: str,
    overrides: list[str] | None = None,
):
    value = {
        "schema_version": 1,
        "project_id": project_id,
        "display_name": (
            "Agentic Mesh V5" if project_id == "agentic-mesh-v5" else project_id
        ),
        "packages": {
            "organization_release_digest": release_digest,
            "overrides": (
                ["project-override/agentic-mesh-v5@1.0.0"]
                if overrides is None
                else overrides
            ),
        },
        "repositories": {
            "primary": {
                "url": repository_url,
                "default_branch": "develop",
                "credential": "git",
            }
        },
        "documents": {},
        "teams": {
            "tenant_id": "tenant-seerstone",
            "team_id": f"team-{project_id}",
            "credential": "graph",
            "channels": {"project": "project", "approvals": "approvals"},
        },
        "ado": {
            "organization": "https://dev.azure.com/seerstone",
            "project": "agentic-mesh",
            "credential": "ado",
        },
        "credentials": {
            name: {
                "scope": "project",
                "provider": name,
                "reference": f"secret://projects/{project_id}/{name}",
            }
            for name in ("git", "graph", "ado")
        },
        "roles": {
            "project-manager": {
                "package": "role/project-manager@1.0.0",
                "tool_profile": "tool-profile/general@1.0.0",
                "instances": {"minimum": 1, "maximum": 1},
            }
        },
        "limits": {"max_total_instances": 1},
    }
    path = root / f"{project_id}.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return load_project_manifest(path)


def _registration_values(
    tmp_path: Path, source_revision: str, configuration_revision: str
) -> dict[str, object]:
    running = tmp_path / "running"
    state = tmp_path / "runtime-state"
    workspaces = tmp_path / "workspaces"
    for root in (running, state, workspaces):
        root.mkdir()
    (running / "version.txt").write_text("running-v1\n", encoding="utf-8")
    (state / "state.json").write_text('{"revision": 1}\n', encoding="utf-8")
    return {
        "source_revision": source_revision,
        "configuration_revision": configuration_revision,
        "flow_reference": "flow/sdlc@1.0.0",
        "sponsor_ids": ("sponsor",),
        "running_image_ref": IMAGE,
        "running_install_root": running,
        "runtime_state_root": state,
        "workspace_root": workspaces,
        "deployment_adapter": "compose",
        "actor_id": "project-manager",
    }


def test_checked_in_v5_project_manifest_pins_the_external_sdlc_release() -> None:
    manifest = load_project_manifest(
        ROOT
        / "examples"
        / "projects"
        / "agentic-mesh-v5"
        / "agentic-mesh"
        / "project.yaml"
    )
    assert manifest.project_id == "agentic-mesh-v5"
    assert len(manifest.snapshot["roles"]) == 15
    assert manifest.snapshot["repositories"]["primary"]["url"] == (
        "https://github.com/NichUK/agentic-mesh.git"
    )
    assert manifest.snapshot["ado"]["project"] == "agentic-mesh"
    packages = manifest.snapshot["packages"]
    assert packages["overrides"] == [
        "project-override/agentic-mesh-v5@0.1.0"
    ]
    if not (
        EXTERNAL_CONFIG
        / "packages"
        / "project-override"
        / "agentic-mesh-v5"
        / "0.1.0"
    ).exists():
        return
    release = resolve_packages(EXTERNAL_CONFIG, SDLC_RELEASE_REFERENCES)
    override = resolve_packages(
        EXTERNAL_CONFIG, packages["overrides"]
    )
    assert packages["organization_release_digest"] == release.digest
    assert override.settings["project_id"] == manifest.project_id
    assert "do not over-engineer" in " ".join(
        override.settings["instructions"]
    ).casefold()


def test_v5_registration_is_idempotent_and_candidate_work_is_arms_length(
    postgres_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert MigrationRunner(postgres_database).migrate().current_version == 25
    config, config_revision, release_digest = _configuration(
        tmp_path / "configuration"
    )
    source, repository_url, source_revision = _source(
        tmp_path / "source", "agentic-mesh-v5"
    )
    manifest = _manifest(
        tmp_path,
        project_id="agentic-mesh-v5",
        release_digest=release_digest,
        repository_url=repository_url,
    )
    values = _registration_values(tmp_path, source_revision, config_revision)
    coordinator = ProjectRegistrationCoordinator(
        postgres_database, configuration_root=config
    )

    monkeypatch.setenv("AGENTIC_MESH_V5_DATABASE_URL", postgres_database)
    command = [
        "--json",
        "project-register",
        "--manifest",
        str(tmp_path / "agentic-mesh-v5.yaml"),
        "--config-root",
        str(config),
        "--source-revision",
        source_revision,
        "--configuration-revision",
        config_revision,
        "--flow-package",
        "flow/sdlc@1.0.0",
        "--sponsor",
        "sponsor",
        "--running-image",
        IMAGE,
        "--running-install-root",
        str(values["running_install_root"]),
        "--runtime-state-root",
        str(values["runtime_state_root"]),
        "--workspace-root",
        str(values["workspace_root"]),
        "--deployment-adapter",
        "compose",
        "--actor",
        "project-manager",
    ]
    assert main(command) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "project-registered"
    assert payload["boundary"]["running_image_ref"] == IMAGE

    first = coordinator.register(manifest, **values)
    repeated = coordinator.register(manifest, **values)
    assert repeated == first
    replayed_by_sponsor = coordinator.register(
        manifest, **{**values, "actor_id": "sponsor"}
    )
    assert replayed_by_sponsor.boundary == first.boundary
    assert replayed_by_sponsor.boundary.registered_by == "project-manager"
    assert first.boundary.running_image_ref == IMAGE

    lifecycle = LifecycleStore(postgres_database)
    lifecycle.create_work_item(
        project_id="agentic-mesh-v5",
        work_item_id="candidate-change",
        title="Build and test a candidate V5 change",
        owner_role_id="project-manager",
        actor_id="project-manager",
        correlation_id="corr-candidate-change",
    )
    workspace = coordinator.prepare_workspace(
        project_id="agentic-mesh-v5",
        work_item_id="candidate-change",
        actor_id="project-manager",
        source_repositories={"primary": source},
    )
    worktree = Path(workspace.repositories[0].worktree_path)
    (worktree / "candidate.py").write_text(
        'def version():\n    return "v2"\n', encoding="utf-8"
    )
    (worktree / "test_candidate.py").write_text(
        "from candidate import version\n\n\n"
        "def test_version():\n"
        '    assert version() == "v2"\n',
        encoding="utf-8",
    )
    _git(worktree, "add", "candidate.py", "test_candidate.py")
    _git(worktree, "commit", "-m", "Build candidate V5 change")
    tested = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_candidate.py"],
        cwd=worktree,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tested.returncode == 0, tested.stdout + tested.stderr

    restarted = ProjectRegistrationCoordinator(
        postgres_database, configuration_root=config
    )
    resumed = restarted.prepare_workspace(
        project_id="agentic-mesh-v5",
        work_item_id="candidate-change",
        actor_id="project-manager",
        source_repositories={"primary": source},
    )
    assert resumed.workspace_root == workspace.workspace_root
    assert restarted.get("agentic-mesh-v5") == first.boundary
    assert 'return "v1"' in (source / "candidate.py").read_text(encoding="utf-8")
    assert (Path(first.boundary.running_install_root) / "version.txt").read_text(
        encoding="utf-8"
    ) == "running-v1\n"
    assert (Path(first.boundary.runtime_state_root) / "state.json").read_text(
        encoding="utf-8"
    ) == '{"revision": 1}\n'
    assert restarted.get("agentic-mesh-v5").running_image_ref == IMAGE
    with psycopg.connect(postgres_database) as connection:
        audit_count = connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.audit_records
            WHERE project_id = 'agentic-mesh-v5'
              AND action = 'project.runtime_registered'
            """
        ).fetchone()[0]
    assert audit_count == 1


def test_registration_rejects_conflicts_before_project_activation(
    postgres_database: str, tmp_path: Path
) -> None:
    MigrationRunner(postgres_database).migrate()
    config, config_revision, release_digest = _configuration(
        tmp_path / "configuration"
    )
    source, repository_url, source_revision = _source(
        tmp_path / "source", "agentic-mesh-v5"
    )
    manifest = _manifest(
        tmp_path,
        project_id="agentic-mesh-v5",
        release_digest=release_digest,
        repository_url=repository_url,
    )
    values = _registration_values(tmp_path, source_revision, config_revision)
    coordinator = ProjectRegistrationCoordinator(
        postgres_database, configuration_root=config
    )
    coordinator.register(manifest, **values)

    conflicting = {**values, "running_image_ref": IMAGE.replace("b" * 64, "c" * 64)}
    with pytest.raises(ProjectRegistrationConflict, match="conflicts"):
        coordinator.register(manifest, **conflicting)
    assert ProjectManifestStore(postgres_database).get_active(
        "agentic-mesh-v5"
    ).manifest_digest == manifest.digest

    LifecycleStore(postgres_database).create_work_item(
        project_id="agentic-mesh-v5",
        work_item_id="protected-source",
        title="Reject protected source",
        owner_role_id="project-manager",
        actor_id="project-manager",
        correlation_id="corr-protected-source",
    )
    with pytest.raises(ProjectRegistrationConflict, match="protected"):
        coordinator.prepare_workspace(
            project_id="agentic-mesh-v5",
            work_item_id="protected-source",
            actor_id="project-manager",
            source_repositories={"primary": config},
        )

    other = _manifest(
        tmp_path,
        project_id="dashboard",
        release_digest=release_digest,
        repository_url="https://github.com/example/dashboard.git",
        overrides=[],
    )
    other_values = {
        **values,
        "source_revision": "d" * 40,
        "running_install_root": tmp_path / "dashboard-running",
        "runtime_state_root": tmp_path / "dashboard-state",
        "workspace_root": Path(values["workspace_root"]) / "dashboard",
    }
    Path(other_values["running_install_root"]).mkdir()
    Path(other_values["runtime_state_root"]).mkdir()
    with pytest.raises(ProjectRegistrationConflict, match="another project"):
        coordinator.register(other, **other_values)
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.projects WHERE project_id='dashboard'"
        ).fetchone()[0] == 0
