from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

import jsonschema
import psycopg
from psycopg import sql
import pytest
import yaml

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.project_manifest import ProjectManifestAuthorizationError
from agentic_mesh_v5.project_manifest import ProjectManifestConflict
from agentic_mesh_v5.project_manifest import ProjectManifestError
from agentic_mesh_v5.project_manifest import ProjectManifestStore
from agentic_mesh_v5.project_manifest import ResourceGrant
from agentic_mesh_v5.project_manifest import StaticResourceAuthorizer
from agentic_mesh_v5.project_manifest import load_project_manifest


ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "config" / "v5" / "project-manifest.schema.json"


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


def _manifest(project_id: str = "alpha", display_name: str = "Alpha") -> dict:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "display_name": display_name,
        "packages": {
            "organization_release_digest": "a" * 64,
            "overrides": [f"project-override/{project_id}@1.0.0"],
        },
        "repositories": {
            "primary": {
                "url": f"https://github.com/example/{project_id}.git",
                "default_branch": "develop",
                "credential": "git",
            },
            "supporting": {
                "url": f"https://github.com/example/{project_id}-docs.git",
                "default_branch": "main",
                "credential": "git",
            },
        },
        "documents": {
            "project-library": {
                "adapter": "onedrive",
                "drive_id": f"drive-{project_id}",
                "root": f"/projects/{project_id}",
                "credential": "graph",
            }
        },
        "teams": {
            "tenant_id": "tenant-seerstone",
            "team_id": f"team-{project_id}",
            "credential": "graph",
            "channels": {
                "project": f"channel-{project_id}",
                "approvals": f"approvals-{project_id}",
            },
        },
        "ado": {
            "organization": "https://dev.azure.com/seerstone",
            "project": project_id,
            "credential": "ado",
        },
        "credentials": {
            "git": {
                "scope": "project",
                "provider": "git",
                "reference": f"secret://projects/{project_id}/git",
            },
            "graph": {
                "scope": "project",
                "provider": "graph",
                "reference": f"secret://projects/{project_id}/graph",
            },
            "ado": {
                "scope": "project",
                "provider": "ado",
                "reference": f"secret://projects/{project_id}/ado",
            },
        },
        "roles": {
            "project-manager": {
                "package": "role/project-manager@1.0.0",
                "tool_profile": "tool-profile/general@1.0.0",
                "instances": {"minimum": 1, "maximum": 1},
            },
            "engineering": {
                "package": "role/engineering@1.0.0",
                "tool_profile": "tool-profile/development@1.0.0",
                "instances": {"minimum": 0, "maximum": 2},
            },
        },
        "limits": {"max_total_instances": 3},
    }


def _write(tmp_path: Path, value: dict, name: str = "project.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_manifest_schema_and_loader_are_deterministic(tmp_path: Path):
    value = _manifest()
    jsonschema.validate(value, json.loads(SCHEMA.read_text(encoding="utf-8")))
    first = load_project_manifest(_write(tmp_path, value))
    reordered = {key: value[key] for key in reversed(value)}
    second = load_project_manifest(_write(tmp_path, reordered, "reordered.yaml"))

    assert first.digest == second.digest
    assert first.snapshot == second.snapshot
    assert [item.kind for item in first.resources] == sorted(
        item.kind for item in first.resources
    )
    assert len(first.resources) == 9
    encoded = json.dumps(first.snapshot)
    assert "secret://projects/alpha/git" in encoded
    assert "super-secret-value" not in encoded


def test_manifest_rejects_invalid_and_sensitive_boundaries(tmp_path: Path):
    cases: list[tuple[str, dict]] = []
    extra = _manifest()
    extra["unexpected"] = True
    cases.append(("unknown", extra))
    credential_url = _manifest()
    credential_url["repositories"]["primary"]["url"] = (
        "https://user:password@github.com/example/alpha.git"
    )
    cases.append(("url", credential_url))
    interpolation = _manifest()
    interpolation["display_name"] = "${PROJECT_NAME}"
    cases.append(("interpolation", interpolation))
    unknown_credential = _manifest()
    unknown_credential["ado"]["credential"] = "missing"
    cases.append(("credential", unknown_credential))
    wrong_package = _manifest()
    wrong_package["roles"]["engineering"]["tool_profile"] = (
        "role/development@1.0.0"
    )
    cases.append(("package", wrong_package))
    invalid_instances = _manifest()
    invalid_instances["roles"]["engineering"]["instances"] = {
        "minimum": 3,
        "maximum": 2,
    }
    cases.append(("instances", invalid_instances))
    raw_secret = _manifest()
    raw_secret["credentials"]["git"]["reference"] = "super-secret-value"
    cases.append(("secret", raw_secret))
    foreign = _manifest()
    foreign["repositories"]["primary"]["owner_project_id"] = "beta"
    cases.append(("foreign", foreign))
    duplicate = _manifest()
    duplicate["repositories"]["supporting"]["url"] = duplicate[
        "repositories"
    ]["primary"]["url"]
    cases.append(("duplicate", duplicate))
    system_credential = _manifest()
    system_credential["credentials"]["git"]["scope"] = "system"
    cases.append(("system", system_credential))

    for index, (label, value) in enumerate(cases):
        with pytest.raises(ProjectManifestError):
            load_project_manifest(_write(tmp_path, value, f"{index}-{label}.yaml"))


def test_store_keeps_git_snapshot_history_and_enforces_project_claims(
    tmp_path: Path, postgres_database: str
):
    status = MigrationRunner(postgres_database).migrate()
    assert status.current_version == 23
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('alpha', 'Alpha'), ('beta', 'Beta'), ('gamma', 'Gamma'),
                   ('delta', 'Delta')
            """
        )

    alpha = load_project_manifest(_write(tmp_path, _manifest(), "alpha.yaml"))
    store = ProjectManifestStore(postgres_database)
    tampered = load_project_manifest(_write(tmp_path, _manifest(), "tampered.yaml"))
    assert isinstance(tampered.snapshot, dict)
    tampered.snapshot["display_name"] = "Tampered"
    with pytest.raises(ProjectManifestError, match="normalized snapshot"):
        store.activate(tampered, source_revision="a" * 40, actor_id="project-admin")
    active = store.activate(
        alpha,
        source_revision="a" * 40,
        actor_id="project-admin",
        expected_active=None,
    )
    assert active.manifest_digest == alpha.digest
    assert active.source_path == "agentic-mesh/project.yaml"
    assert store.get_active("alpha") == active
    assert len(store.list_history("alpha")) == 1

    replay = store.activate(
        alpha,
        source_revision="a" * 40,
        actor_id="project-admin",
        expected_active=alpha.digest,
    )
    assert replay == active
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.audit_records
            WHERE action = 'project.manifest_activated' AND project_id = 'alpha'
            """
        ).fetchone()[0] == 1

    changed_value = _manifest()
    changed_value["repositories"]["supporting"]["url"] = (
        "https://github.com/example/alpha-evidence.git"
    )
    changed = load_project_manifest(_write(tmp_path, changed_value, "changed.yaml"))
    advanced = store.activate(
        changed,
        source_revision="b" * 40,
        actor_id="project-admin",
        expected_active=alpha.digest,
    )
    assert advanced.manifest_digest == changed.digest
    assert len(store.list_history("alpha")) == 2
    with pytest.raises(ProjectManifestConflict, match="active manifest changed"):
        store.activate(
            alpha,
            source_revision="a" * 40,
            actor_id="project-admin",
            expected_active=alpha.digest,
        )
    with pytest.raises(ProjectManifestError):
        store.activate(
            changed,
            source_revision="b" * 40,
            actor_id="project-admin",
            source_path="other/project.yaml",
        )
    with psycopg.connect(postgres_database) as connection:
        with pytest.raises(psycopg.Error, match="immutable"):
            connection.execute(
                """
                UPDATE agentic_mesh_v5.project_manifest_snapshots
                SET source_revision = %s WHERE project_id = 'alpha'
                """,
                ("c" * 40,),
            )

    beta_value = _manifest("beta", "Beta")
    shared_url = changed.snapshot["repositories"]["primary"]["url"]
    beta_value["repositories"]["primary"]["url"] = shared_url
    beta = load_project_manifest(_write(tmp_path, beta_value, "beta.yaml"))
    with pytest.raises(ProjectManifestAuthorizationError, match="belongs to project alpha"):
        ProjectManifestStore(postgres_database).activate(
            beta, source_revision="d" * 40, actor_id="project-admin"
        )

    authorization_ref = "grant://projects/alpha/share-primary-repository"
    beta_value["repositories"]["primary"].update(
        {"owner_project_id": "alpha", "authorization_ref": authorization_ref}
    )
    beta = load_project_manifest(_write(tmp_path, beta_value, "beta-granted.yaml"))
    resource = next(
        item
        for item in beta.resources
        if item.kind == "repository" and item.key == shared_url
    )
    authorizer = StaticResourceAuthorizer(
        (
            ResourceGrant(
                project_id="beta",
                owner_project_id="alpha",
                resource_kind="repository",
                resource_key=resource.key,
                authorization_ref=authorization_ref,
            ),
        )
    )
    beta_active = ProjectManifestStore(
        postgres_database, authorizer=authorizer
    ).activate(beta, source_revision="e" * 40, actor_id="project-admin")
    assert beta_active.manifest_digest == beta.digest
    assert len(ProjectManifestStore(postgres_database).list_history("beta")) == 1

    gamma_value = _manifest("gamma", "Gamma")
    gamma_reference = "grant://projects/alpha/share-primary-repository-with-gamma"
    gamma_value["repositories"]["primary"].update(
        {
            "url": shared_url,
            "owner_project_id": "alpha",
            "authorization_ref": gamma_reference,
        }
    )
    gamma = load_project_manifest(_write(tmp_path, gamma_value, "gamma-granted.yaml"))
    gamma_grant = ResourceGrant(
        "gamma", "alpha", "repository", shared_url, gamma_reference
    )
    gamma_active = ProjectManifestStore(
        postgres_database, authorizer=StaticResourceAuthorizer((gamma_grant,))
    ).activate(gamma, source_revision="f" * 40, actor_id="project-admin")
    assert gamma_active.manifest_digest == gamma.digest

    delta_value = _manifest("delta", "Delta")
    delta_reference = "grant://projects/gamma/share-alpha-repository"
    delta_value["repositories"]["primary"].update(
        {
            "url": shared_url,
            "owner_project_id": "gamma",
            "authorization_ref": delta_reference,
        }
    )
    delta = load_project_manifest(
        _write(tmp_path, delta_value, "delta-invalid-owner.yaml")
    )
    delta_grant = ResourceGrant(
        "delta", "gamma", "repository", shared_url, delta_reference
    )
    with pytest.raises(ProjectManifestAuthorizationError, match="belongs to project alpha"):
        ProjectManifestStore(
            postgres_database, authorizer=StaticResourceAuthorizer((delta_grant,))
        ).activate(delta, source_revision="1" * 40, actor_id="project-admin")


def test_foreign_declared_owner_requires_exact_external_grant(
    tmp_path: Path, postgres_database: str
):
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('alpha', 'Alpha'), ('beta', 'Beta')
            """
        )
    value = _manifest("beta", "Beta")
    reference = "grant://projects/alpha/beta-documents"
    value["documents"]["project-library"].update(
        {"owner_project_id": "alpha", "authorization_ref": reference}
    )
    manifest = load_project_manifest(_write(tmp_path, value))
    with pytest.raises(ProjectManifestAuthorizationError, match="not authorized"):
        ProjectManifestStore(postgres_database).activate(
            manifest, source_revision="f" * 40, actor_id="project-admin"
        )
    document = next(item for item in manifest.resources if item.kind == "document-root")
    grant = ResourceGrant(
        "beta", "alpha", "document-root", document.key, reference
    )
    result = ProjectManifestStore(
        postgres_database, authorizer=StaticResourceAuthorizer((grant,))
    ).activate(manifest, source_revision="f" * 40, actor_id="project-admin")
    assert result.project_id == "beta"
