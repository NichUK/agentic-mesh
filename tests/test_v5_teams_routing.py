from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.teams_connector import ProjectTeamsConnector
from agentic_mesh_v5.teams_routing import TeamsProjectRouter
from agentic_mesh_v5.teams_routing import TeamsRouteRejected


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


class ProjectAccess:
    def __init__(self) -> None:
        self.allowed = {
            "alice": {"alpha", "beta", "gamma"},
            "bob": {"alpha"},
        }
        self.fail = False

    def allows(self, *, sender_id: str, project_id: str) -> bool:
        if self.fail:
            raise RuntimeError("authorization detail must not leak")
        return project_id in self.allowed.get(sender_id, set())


class UnusedTokenProvider:
    def access_token(self, **_: str) -> str:
        raise AssertionError("routing must not resolve a delivery credential")


class UnusedTransport:
    def check_installation(self, **_: str):
        raise AssertionError("routing must not call Teams transport")

    def send_channel_message(self, **_: str) -> str:
        raise AssertionError("routing must not call Teams transport")


def _insert_project(
    connection,
    *,
    project_id: str,
    display_name: str,
    digest: str,
    tenant_id: str,
    team_id: str,
    channel_id: str,
    application_id: str,
    role_id: str = "engineering",
) -> None:
    snapshot = {
        "teams": {
            "tenant_id": tenant_id,
            "team_id": team_id,
            "credential": "graph",
            "channels": {"project": channel_id},
            "role_identities": {
                role_id: {
                    "application_id": application_id,
                    "display_name": (
                        f"AM {display_name} {role_id.replace('-', ' ').title()}"
                    ),
                    "credential": f"teams-{role_id}",
                }
            },
            "owner_project_id": project_id,
        },
        "credentials": {
            f"teams-{role_id}": {
                "scope": "project",
                "provider": "teams-bot",
                "reference": f"secret://projects/{project_id}/teams/{role_id}",
            }
        },
    }
    connection.execute(
        "INSERT INTO agentic_mesh_v5.projects(project_id,display_name) "
        "VALUES (%s,%s)",
        (project_id, display_name),
    )
    connection.execute(
        """
        INSERT INTO agentic_mesh_v5.roles
            (project_id,role_id,template_id,package_digest)
        VALUES (%s,%s,%s,%s)
        """,
        (project_id, role_id, role_id, "1" * 64),
    )
    connection.execute(
        """
        INSERT INTO agentic_mesh_v5.project_manifest_snapshots
            (project_id,manifest_digest,source_revision,source_path,
             snapshot,registered_by)
        VALUES (%s,%s,%s,'agentic-mesh/project.yaml',%s,'pm')
        """,
        (project_id, digest, "d" * 40, Jsonb(snapshot)),
    )
    connection.execute(
        """
        INSERT INTO agentic_mesh_v5.project_manifest_active
            (project_id,manifest_digest,activated_by)
        VALUES (%s,%s,'pm')
        """,
        (project_id, digest),
    )
    connection.execute(
        """
        INSERT INTO agentic_mesh_v5.role_bindings
            (project_id,role_id,manifest_digest,role_reference,
             role_digest,role_snapshot,tool_profile_reference,
             tool_profile_digest,tool_profile_id,flow_reference,
             flow_digest,prompt_configuration_digest,role_class,
             memory_scope,collaboration_identity,minimum_instances,
             maximum_instances,activated_by)
        VALUES (%s,%s,%s,%s,%s,%s,
                'tool-profile/general@1.0.0',%s,'general',
                'flow/sdlc@1.0.0',%s,%s,'general','project-role',%s,
                0,2,'pm')
        """,
        (
            project_id,
            role_id,
            digest,
            f"role/{role_id}@1.0.0",
            "1" * 64,
            Jsonb({"role_id": role_id}),
            "2" * 64,
            "3" * 64,
            "4" * 64,
            application_id,
        ),
    )


@pytest.fixture
def teams_router(postgres_database: str):
    assert MigrationRunner(postgres_database).migrate().current_version == 29
    projects = (
        (
            "alpha",
            "Alpha",
            "a" * 64,
            "tenant-one",
            "team-alpha",
            "channel-alpha",
            "shared-engineering-app",
        ),
        (
            "beta",
            "Beta",
            "b" * 64,
            "tenant-one",
            "team-beta",
            "channel-beta",
            "shared-engineering-app",
        ),
        (
            "gamma",
            "Gamma",
            "c" * 64,
            "tenant-one",
            "team-gamma",
            "channel-gamma",
            "gamma-engineering-app",
        ),
    )
    with psycopg.connect(postgres_database) as connection:
        for (
            project_id,
            display_name,
            digest,
            tenant_id,
            team_id,
            channel_id,
            application_id,
        ) in projects:
            _insert_project(
                connection,
                project_id=project_id,
                display_name=display_name,
                digest=digest,
                tenant_id=tenant_id,
                team_id=team_id,
                channel_id=channel_id,
                application_id=application_id,
            )
    access = ProjectAccess()
    connector = ProjectTeamsConnector(
        postgres_database,
        token_provider=UnusedTokenProvider(),
        transport=UnusedTransport(),
    )
    return (
        TeamsProjectRouter(
            postgres_database,
            connector=connector,
            authorizer=access,
        ),
        connector,
        access,
        postgres_database,
    )


def test_channel_routes_only_by_exact_active_authority(teams_router) -> None:
    router, _, _, _ = teams_router

    decision = router.route_channel(
        sender_id="alice",
        tenant_id="tenant-one",
        team_id="team-alpha",
        channel_id="channel-alpha",
        recipient_application_id="28:shared-engineering-app",
    )

    assert decision.status == "routed"
    assert decision.source == "channel"
    assert decision.project_id == "alpha"
    assert decision.project_display_name == "Alpha"
    assert decision.role_id == "engineering"
    assert decision.manifest_digest == "a" * 64
    assert decision.candidates == ()
    assert decision.clarification_question is None


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("tenant_id", "foreign-tenant", "unknown-route"),
        ("team_id", "team-beta", "unknown-route"),
        ("channel_id", "channel-beta", "unknown-route"),
        ("recipient_application_id", "gamma-engineering-app", "unknown-route"),
        ("sender_id", "mallory", "unknown-route"),
        ("channel_id", "invalid channel", "invalid-activity"),
    ],
)
def test_channel_spoofing_unknown_authority_and_malformed_values_fail_closed(
    teams_router, field: str, value: str, code: str
) -> None:
    router, _, _, _ = teams_router
    request = {
        "sender_id": "alice",
        "tenant_id": "tenant-one",
        "team_id": "team-alpha",
        "channel_id": "channel-alpha",
        "recipient_application_id": "shared-engineering-app",
    }
    request[field] = value

    with pytest.raises(TeamsRouteRejected) as rejected:
        router.route_channel(**request)

    assert rejected.value.code == code
    assert "Alpha" not in str(rejected.value)
    assert "authorization detail" not in str(rejected.value)


def test_duplicate_active_channel_authority_is_rejected(teams_router) -> None:
    router, _, _, database_url = teams_router
    with psycopg.connect(database_url) as connection:
        _insert_project(
            connection,
            project_id="delta",
            display_name="Delta",
            digest="d" * 64,
            tenant_id="tenant-one",
            team_id="team-alpha",
            channel_id="channel-alpha",
            application_id="delta-engineering-app",
        )

    with pytest.raises(TeamsRouteRejected) as rejected:
        router.route_channel(
            sender_id="alice",
            tenant_id="tenant-one",
            team_id="team-alpha",
            channel_id="channel-alpha",
            recipient_application_id="shared-engineering-app",
        )

    assert rejected.value.code == "ambiguous-channel"


def test_ambiguous_personal_message_asks_for_an_authorized_project(
    teams_router,
) -> None:
    router, _, _, _ = teams_router

    decision = router.route_personal(
        sender_id="alice",
        tenant_id="tenant-one",
        recipient_application_id="shared-engineering-app",
    )

    assert decision.status == "clarification_required"
    assert decision.source == "personal"
    assert decision.project_id is None
    assert decision.role_id is None
    assert decision.manifest_digest is None
    assert tuple(item.project_id for item in decision.candidates) == (
        "alpha",
        "beta",
    )
    assert "Alpha (alpha)" in decision.clarification_question
    assert "Beta (beta)" in decision.clarification_question
    assert "Gamma" not in decision.clarification_question


def test_structured_personal_selection_resumes_only_an_exact_candidate(
    teams_router,
) -> None:
    router, _, _, _ = teams_router

    decision = router.route_personal(
        sender_id="alice",
        tenant_id="tenant-one",
        recipient_application_id="shared-engineering-app",
        selected_project_id="beta",
    )

    assert decision.status == "routed"
    assert decision.project_id == "beta"
    assert decision.role_id == "engineering"
    with pytest.raises(TeamsRouteRejected) as rejected:
        router.route_personal(
            sender_id="alice",
            tenant_id="tenant-one",
            recipient_application_id="shared-engineering-app",
            selected_project_id="gamma",
        )
    assert rejected.value.code == "invalid-selection"
    with pytest.raises(TeamsRouteRejected) as malformed:
        router.route_personal(
            sender_id="alice",
            tenant_id="tenant-one",
            recipient_application_id="shared-engineering-app",
            selected_project_id="not a project id",
        )
    assert malformed.value.code == "invalid-selection"


def test_shared_bot_mapped_to_different_roles_cannot_prompt_or_route(
    teams_router,
) -> None:
    router, _, access, database_url = teams_router
    with psycopg.connect(database_url) as connection:
        _insert_project(
            connection,
            project_id="delta",
            display_name="Delta",
            digest="d" * 64,
            tenant_id="tenant-one",
            team_id="team-delta",
            channel_id="channel-delta",
            application_id="shared-engineering-app",
            role_id="qa-engineer",
        )
    access.allowed["alice"].add("delta")

    with pytest.raises(TeamsRouteRejected) as rejected:
        router.route_personal(
            sender_id="alice",
            tenant_id="tenant-one",
            recipient_application_id="shared-engineering-app",
        )

    assert rejected.value.code == "routing-unavailable"


def test_personal_message_routes_when_authority_has_one_candidate(
    teams_router,
) -> None:
    router, _, _, _ = teams_router

    authorized = router.route_personal(
        sender_id="bob",
        tenant_id="tenant-one",
        recipient_application_id="shared-engineering-app",
    )
    unique_bot = router.route_personal(
        sender_id="alice",
        tenant_id="tenant-one",
        recipient_application_id="gamma-engineering-app",
    )

    assert authorized.project_id == "alpha"
    assert unique_bot.project_id == "gamma"


def test_unknown_or_unauthorized_personal_authority_does_not_expose_projects(
    teams_router,
) -> None:
    router, _, access, _ = teams_router
    requests = (
        {
            "sender_id": "mallory",
            "tenant_id": "tenant-one",
            "recipient_application_id": "shared-engineering-app",
        },
        {
            "sender_id": "alice",
            "tenant_id": "foreign-tenant",
            "recipient_application_id": "shared-engineering-app",
        },
        {
            "sender_id": "alice",
            "tenant_id": "tenant-one",
            "recipient_application_id": "unknown-app",
        },
    )
    for request in requests:
        with pytest.raises(TeamsRouteRejected) as rejected:
            router.route_personal(**request)
        assert rejected.value.code == "unknown-route"
        assert "Alpha" not in str(rejected.value)
        assert "Beta" not in str(rejected.value)
    access.fail = True
    with pytest.raises(TeamsRouteRejected) as rejected:
        router.route_personal(
            sender_id="alice",
            tenant_id="tenant-one",
            recipient_application_id="shared-engineering-app",
        )
    assert rejected.value.code == "unknown-route"
    assert "authorization detail" not in str(rejected.value)


def test_active_binding_change_is_rejected_before_route_return(
    teams_router, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, connector, _, _ = teams_router
    original = connector.binding(project_id="alpha", role_id="engineering")
    monkeypatch.setattr(
        connector,
        "binding",
        lambda **_: replace(original, manifest_digest="f" * 64),
    )

    with pytest.raises(TeamsRouteRejected) as rejected:
        router.route_channel(
            sender_id="alice",
            tenant_id="tenant-one",
            team_id="team-alpha",
            channel_id="channel-alpha",
            recipient_application_id="shared-engineering-app",
        )

    assert rejected.value.code == "authority-changed"


def test_teams_project_resolution_does_not_enter_generic_router() -> None:
    source = Path("src/agentic_mesh_v5/routing.py").read_text(encoding="utf-8")
    assert "teams_routing" not in source
    assert "microsoft" not in source.casefold()
