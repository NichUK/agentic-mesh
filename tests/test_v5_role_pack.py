from __future__ import annotations

import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest
import yaml

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.fleet import FleetAction
from agentic_mesh_v5.fleet import FleetScaler
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.project_manifest import ProjectManifestStore
from agentic_mesh_v5.project_manifest import load_project_manifest
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.role_pack import RolePackActivator
from agentic_mesh_v5.role_pack import RolePackConflict
from agentic_mesh_v5.routing import RouteDraft
from agentic_mesh_v5.routing import Router
from agentic_mesh_v5.routing import RoutingNotFound


CONFIG = Path(
    os.environ.get(
        "AGENTIC_MESH_CONFIG_REPOSITORY",
        Path(__file__).resolve().parents[2] / "agentic-mesh-config",
    )
)

ROLE_PROFILES = {
    "business-analyst": "general",
    "delivery-manager": "general",
    "engineering": "development",
    "enterprise-architect": "general",
    "platform-engineer": "operations",
    "product-manager": "general",
    "project-manager": "general",
    "prompt-engineer": "general",
    "qa-engineer": "qa",
    "release-manager": "operations",
    "research-analyst": "general",
    "security-architect": "general",
    "solution-architect": "general",
    "technical-writer": "general",
    "ux-designer": "ux",
}

PROFILE_VERSIONS = {
    "general": "0.2.0",
    "development": "0.1.0",
    "operations": "0.1.0",
    "qa": "0.1.0",
    "ux": "0.1.0",
}


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


def _manifest_value(project_id: str, roles: dict[str, dict]) -> dict:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "display_name": project_id.title(),
        "packages": {"organization_release_digest": "a" * 64, "overrides": []},
        "repositories": {
            "primary": {
                "url": f"https://github.com/example/{project_id}.git",
                "default_branch": "develop",
                "credential": "git",
            }
        },
        "documents": {},
        "teams": {
            "tenant_id": "tenant",
            "team_id": f"team-{project_id}",
            "credential": "graph",
            "channels": {"project": "project", "approvals": "approvals"},
        },
        "ado": {
            "organization": "https://dev.azure.com/example",
            "project": project_id,
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
        "roles": roles,
        "limits": {
            "max_total_instances": sum(
                item["instances"]["maximum"] for item in roles.values()
            )
        },
    }


def _activate_manifest(database_url: str, tmp_path: Path, value: dict):
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    manifest = load_project_manifest(path)
    lifecycle = LifecycleStore(database_url)
    lifecycle.create_project(
        project_id=manifest.project_id,
        display_name=manifest.display_name,
        sponsor_ids=("sponsor",),
    )
    ProjectManifestStore(database_url).activate(
        manifest,
        source_revision="a" * 40,
        actor_id="project-manager",
        expected_active=None,
    )
    return manifest


class _Supervisor:
    def __init__(self) -> None:
        self.actions: list[FleetAction] = []

    def apply(self, action: FleetAction) -> None:
        self.actions.append(action)


def test_released_15_role_pack_materializes_routes_and_wakes(
    postgres_database: str, tmp_path: Path
) -> None:
    if not (CONFIG / "packages" / "flow" / "sdlc" / "0.1.0").exists():
        pytest.skip("external configuration repository is not available")
    assert MigrationRunner(postgres_database).migrate().current_version == 24
    roles = {}
    for role_id, profile in ROLE_PROFILES.items():
        roles[role_id] = {
            "package": f"role/{role_id}@0.1.0",
            "tool_profile": (
                f"tool-profile/{profile}@{PROFILE_VERSIONS[profile]}"
            ),
            "instances": {
                "minimum": 1 if role_id == "project-manager" else 0,
                "maximum": 2 if role_id == "engineering" else 1,
            },
        }
    manifest = _activate_manifest(
        postgres_database, tmp_path, _manifest_value("alpha", roles)
    )
    activator = RolePackActivator(postgres_database, CONFIG)
    first = activator.activate(
        manifest,
        flow_reference="flow/sdlc@0.1.0",
        actor_id="project-manager",
    )
    second = activator.activate(
        manifest,
        flow_reference="flow/sdlc@0.1.0",
        actor_id="project-manager",
    )
    assert first.roles == second.roles
    assert activator.get("alpha") == second
    assert len(first.roles) == 15
    assert {item.role_id for item in first.roles} == set(ROLE_PROFILES)
    assert {item.memory_scope for item in first.roles} == {"project-role"}
    assert next(item for item in first.roles if item.role_id == "qa-engineer").tool_profile_id == "qa"

    with psycopg.connect(postgres_database) as connection:
        for index, role_id in enumerate(sorted(ROLE_PROFILES), 1):
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.work_items
                    (project_id, work_item_id, assigned_role_id, title, status)
                VALUES ('alpha', %s, %s, %s, 'active')
                """,
                (f"work-{index}", role_id, f"Work for {role_id}"),
            )
    router = Router(postgres_database)
    for index, role_id in enumerate(sorted(ROLE_PROFILES), 1):
        routed = router.route(
            RouteDraft(
                project_id="alpha",
                work_item_id=f"work-{index}",
                target_role_id=role_id,
                idempotency_key=f"route-{role_id}",
                payload={"role": role_id},
            )
        )
        assert routed.queue_id == role_id

    supervisor = _Supervisor()
    reconciled = FleetScaler(postgres_database, supervisor).reconcile("alpha")
    assert len(reconciled.actions) == 15
    assert {action.role_id for action in supervisor.actions} == set(ROLE_PROFILES)
    queues = RoleQueueStore(postgres_database)
    for role_id in ROLE_PROFILES:
        claim = queues.claim(
            project_id="alpha",
            queue_id=role_id,
            owner_instance_id=f"alpha.{role_id}.1",
            lease_seconds=60,
        )
        assert claim.queue_item.work_item_id.startswith("work-")
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.role_bindings WHERE project_id='alpha'"
        ).fetchone()[0] == 15
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.role_queues WHERE project_id='alpha'"
        ).fetchone()[0] == 15
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.role_instances WHERE project_id='alpha'"
        ).fetchone()[0] == 16
        engineering = connection.execute(
            """
            SELECT count(DISTINCT binding.role_id), count(instance.instance_id)
            FROM agentic_mesh_v5.role_bindings AS binding
            JOIN agentic_mesh_v5.role_instances AS instance
              ON instance.project_id = binding.project_id
             AND instance.role_id = binding.role_id
            WHERE binding.project_id = 'alpha' AND binding.role_id = 'engineering'
            """
        ).fetchone()
    assert engineering == (1, 2)


def _write_package(root: Path, reference: str, settings: dict) -> None:
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


def _synthetic_repository(root: Path) -> Path:
    (root / "schemas").mkdir(parents=True)
    (root / "schemas" / "package.schema.json").write_text("{}\n", encoding="utf-8")
    role_id = "finance-analyst"
    role_settings = {
        "role": {
            "schema_version": 1,
            "role_id": role_id,
            "display_name": "Finance Analyst",
            "role_class": "general",
            "purpose": "Analyse a bounded financial question.",
            "accountabilities": ["Produce sourced analysis."],
            "decision_rights": {
                "owns": ["Analysis method."],
                "must_not": ["Approve expenditure."],
            },
            "consults": [],
            "handoff_targets": [],
            "memory_scope": "project-role",
            "instructions": ["Use the simplest sufficient analysis."],
            "documentation": ["Analysis record."],
        }
    }
    _write_package(
        root,
        f"role/{role_id}@1.0.0",
        role_settings,
    )
    auditor = json.loads(json.dumps(role_settings))
    auditor["role"]["role_id"] = "finance-auditor"
    auditor["role"]["display_name"] = "Finance Auditor"
    auditor["role"]["purpose"] = "Review bounded financial evidence."
    _write_package(root, "role/finance-auditor@1.0.0", auditor)
    _write_package(
        root,
        "flow/finance@1.0.0",
        {
            "flow": {
                "schema_version": 1,
                "flow_id": "finance",
                "leader_role": role_id,
                "entry_state": "analysis",
                "terminal_states": ["analysis"],
                "states": {
                    "analysis": {
                        "owner_role": role_id,
                        "purpose": "Complete the analysis.",
                        "artifact": "work/{work_item_id}/analysis.md",
                        "consults": [],
                        "gates": [],
                        "routes": [],
                        "terminal": True,
                    }
                },
            }
        },
    )
    _write_package(root, "tool-profile/general@1.0.0", _general_profile())
    _write_package(
        root,
        "tool-profile/restricted@1.0.0",
        _general_profile(profile_id="restricted", restricted=True),
    )
    return root


def _general_profile(
    *, profile_id: str = "general", restricted: bool = False
) -> dict:
    profile = {
        "tool_profile": {
            "schema_version": 1,
            "profile_id": profile_id,
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
    }
    if restricted:
        profile["tool_profile"]["launch_policy"] = {
            "normal_routing": False,
            "allowed_launchers": ["recovery-supervisor"],
        }
    return profile


def test_synthetic_future_role_uses_the_same_activation_contract(
    postgres_database: str, tmp_path: Path
) -> None:
    assert MigrationRunner(postgres_database).migrate().current_version == 24
    root = _synthetic_repository(tmp_path / "configuration")
    roles = {
        "finance-analyst": {
            "package": "role/finance-analyst@1.0.0",
            "tool_profile": "tool-profile/general@1.0.0",
            "instances": {"minimum": 0, "maximum": 2},
        },
        "finance-auditor": {
            "package": "role/finance-auditor@1.0.0",
            "tool_profile": "tool-profile/general@1.0.0",
            "instances": {"minimum": 1, "maximum": 1},
        }
    }
    manifest = _activate_manifest(
        postgres_database,
        tmp_path,
        _manifest_value("finance", roles),
    )
    activated = RolePackActivator(postgres_database, root).activate(
        manifest,
        flow_reference="flow/finance@1.0.0",
        actor_id="project-manager",
    )
    assert [item.role_id for item in activated.roles] == [
        "finance-analyst",
        "finance-auditor",
    ]

    smaller_roles = {"finance-analyst": roles["finance-analyst"]}
    smaller_path = tmp_path / "smaller.yaml"
    smaller_path.write_text(
        yaml.safe_dump(_manifest_value("finance", smaller_roles), sort_keys=False),
        encoding="utf-8",
    )
    smaller = load_project_manifest(smaller_path)
    ProjectManifestStore(postgres_database).activate(
        smaller,
        source_revision="b" * 40,
        actor_id="project-manager",
        expected_active=manifest.digest,
    )
    reduced = RolePackActivator(postgres_database, root).activate(
        smaller,
        flow_reference="flow/finance@1.0.0",
        actor_id="project-manager",
    )
    assert [item.role_id for item in reduced.roles] == ["finance-analyst"]
    assert RolePackActivator(postgres_database, root).get("finance") == reduced
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items
                (project_id, work_item_id, assigned_role_id, title)
            VALUES ('finance', 'removed-route', 'finance-analyst', 'Removed route')
            """
        )
        removed = connection.execute(
            """
            SELECT role.status, queue.paused, policy.min_warm_instances,
                   policy.hibernation_enabled
            FROM agentic_mesh_v5.roles AS role
            JOIN agentic_mesh_v5.role_queues AS queue USING (project_id, role_id)
            JOIN agentic_mesh_v5.role_scaling_policies AS policy
              USING (project_id, role_id)
            WHERE role.project_id = 'finance' AND role.role_id = 'finance-auditor'
            """
        ).fetchone()
    assert removed == ("inactive", True, 0, True)
    with pytest.raises(RoutingNotFound):
        Router(postgres_database).route(
            RouteDraft(
                project_id="finance",
                work_item_id="removed-route",
                target_role_id="finance-auditor",
                idempotency_key="removed-auditor",
                payload={},
            )
        )
    assert reduced.roles[0].tool_profile_id == "general"
    assert reduced.roles[0].maximum_instances == 2

    restricted_roles = {
        "finance-analyst": {
            "package": "role/finance-analyst@1.0.0",
            "tool_profile": "tool-profile/restricted@1.0.0",
            "instances": {"minimum": 0, "maximum": 1},
        }
    }
    restricted_manifest = _activate_manifest(
        postgres_database,
        tmp_path,
        _manifest_value("restricted", restricted_roles),
    )
    with pytest.raises(RolePackConflict, match="excluded from normal routing"):
        RolePackActivator(postgres_database, root).activate(
            restricted_manifest,
            flow_reference="flow/finance@1.0.0",
            actor_id="project-manager",
        )
    with psycopg.connect(postgres_database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.roles WHERE project_id='restricted'"
        ).fetchone()[0] == 0
