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
from agentic_mesh_v5.teams_connector import TeamsConnectorBlocked
from agentic_mesh_v5.teams_connector import TeamsConnectorConfigurationError
from agentic_mesh_v5.teams_connector import TeamsInboundRejected
from agentic_mesh_v5.teams_connector import TeamsInstallation


ROLE_IDS = (
    "business-analyst",
    "delivery-manager",
    "engineering",
    "enterprise-architect",
    "platform-engineer",
    "product-manager",
    "project-manager",
    "prompt-engineer",
    "qa-engineer",
    "release-manager",
    "research-analyst",
    "security-architect",
    "solution-architect",
    "technical-writer",
    "ux-designer",
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


@pytest.fixture
def teams_project(postgres_database: str) -> str:
    assert MigrationRunner(postgres_database).migrate().current_version == 29
    digest = "d" * 64
    identities = {
        role_id: {
            "application_id": f"{role_id}-app",
            "display_name": f"AM Alpha {role_id.replace('-', ' ').title()}",
            "credential": f"teams-{role_id}",
        }
        for role_id in ROLE_IDS
    }
    snapshot = {
        "teams": {
            "tenant_id": "tenant-alpha",
            "team_id": "team-alpha",
            "credential": "graph",
            "channels": {
                "project": "channel-project",
                "approvals": "channel-approvals",
            },
            "role_identities": identities,
            "owner_project_id": "alpha",
        },
        "credentials": {
            "graph": {
                "scope": "project",
                "provider": "graph",
                "reference": "secret://projects/alpha/graph",
            },
            **{
                f"teams-{role_id}": {
                    "scope": "project",
                    "provider": "teams-bot",
                    "reference": f"secret://projects/alpha/teams/{role_id}",
                }
                for role_id in ROLE_IDS
            },
        },
    }
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            "INSERT INTO agentic_mesh_v5.projects(project_id,display_name) "
            "VALUES ('alpha','Alpha')"
        )
        for role_id in identities:
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.roles
                    (project_id,role_id,template_id,package_digest)
                VALUES ('alpha',%s,%s,%s)
                """,
                (role_id, role_id, "1" * 64),
            )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_manifest_snapshots
                (project_id,manifest_digest,source_revision,source_path,
                 snapshot,registered_by)
            VALUES ('alpha',%s,%s,'agentic-mesh/project.yaml',%s,'pm')
            """,
            (digest, "a" * 40, Jsonb(snapshot)),
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_manifest_active
                (project_id,manifest_digest,activated_by)
            VALUES ('alpha',%s,'pm')
            """,
            (digest,),
        )
        for role_id, identity in identities.items():
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.role_bindings
                    (project_id,role_id,manifest_digest,role_reference,
                     role_digest,role_snapshot,tool_profile_reference,
                     tool_profile_digest,tool_profile_id,flow_reference,
                     flow_digest,prompt_configuration_digest,role_class,
                     memory_scope,collaboration_identity,minimum_instances,
                     maximum_instances,activated_by)
                VALUES ('alpha',%s,%s,%s,%s,%s,%s,%s,'general',%s,%s,%s,
                        'general','project-role',%s,0,2,'pm')
                """,
                (
                    role_id,
                    digest,
                    f"role/{role_id}@1.0.0",
                    "1" * 64,
                    Jsonb({"role_id": role_id}),
                    "tool-profile/general@1.0.0",
                    "2" * 64,
                    "flow/sdlc@1.0.0",
                    "3" * 64,
                    "4" * 64,
                    identity["application_id"],
                ),
            )
    return postgres_database


class TokenProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    def access_token(self, *, provider: str, reference: str) -> str:
        self.calls.append((provider, reference))
        if self.fail:
            raise RuntimeError("credential-value-must-never-leak")
        return f"token-for:{reference.rsplit('/', 1)[-1]}"


class FakeTransport:
    def __init__(self) -> None:
        self.installations: dict[str, TeamsInstallation] = {}
        self.check_error = False
        self.send_error = False
        self.checks: list[dict[str, str]] = []
        self.sends: list[dict[str, str]] = []

    def check_installation(self, **values) -> TeamsInstallation:
        self.checks.append(dict(values))
        if self.check_error:
            raise RuntimeError("transport-secret-must-never-leak")
        return self.installations.get(
            values["application_id"], TeamsInstallation(True, True)
        )

    def send_channel_message(self, **values) -> str:
        self.sends.append(dict(values))
        if self.send_error:
            raise RuntimeError("transport-secret-must-never-leak")
        return f"delivery-{values['application_id']}"


def _connector(database_url: str):
    tokens = TokenProvider()
    transport = FakeTransport()
    return (
        ProjectTeamsConnector(
            database_url, token_provider=tokens, transport=transport
        ),
        tokens,
        transport,
    )


def test_inbound_identity_maps_exact_bot_authority_to_logical_role(
    teams_project: str,
) -> None:
    connector, _, _ = _connector(teams_project)

    bindings = tuple(
        connector.identify_inbound(
            project_id="alpha",
            tenant_id="tenant-alpha",
            team_id="team-alpha",
            recipient_application_id=(
                f"28:{role_id}-app"
                if role_id != "project-manager"
                else "project-manager-app"
            ),
        )
        for role_id in ROLE_IDS
    )

    assert {item.role_id for item in bindings} == set(ROLE_IDS)
    assert len({item.application_id for item in bindings}) == len(ROLE_IDS)
    assert all(item.display_name.startswith("AM Alpha ") for item in bindings)
    for field, value in (
        ("tenant_id", "foreign-tenant"),
        ("team_id", "foreign-team"),
        ("recipient_application_id", "foreign-app"),
    ):
        request = {
            "project_id": "alpha",
            "tenant_id": "tenant-alpha",
            "team_id": "team-alpha",
            "recipient_application_id": "engineering-app",
        }
        request[field] = value
        with pytest.raises(TeamsInboundRejected):
            connector.identify_inbound(**request)


def test_inbound_identity_rechecks_authority_after_active_binding_read(
    teams_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector, _, _ = _connector(teams_project)
    original = connector.binding(project_id="alpha", role_id="engineering")
    monkeypatch.setattr(
        connector,
        "binding",
        lambda **_: replace(original, application_id="substituted-app"),
    )

    with pytest.raises(TeamsInboundRejected, match="changed"):
        connector.identify_inbound(
            project_id="alpha",
            tenant_id="tenant-alpha",
            team_id="team-alpha",
            recipient_application_id="engineering-app",
        )


def test_each_role_sends_with_its_own_external_identity_and_clear_evidence(
    teams_project: str,
) -> None:
    connector, tokens, transport = _connector(teams_project)

    deliveries = tuple(
        connector.send_channel(
            project_id="alpha",
            role_id=role_id,
            channel="project",
            text=f"Update from {role_id}",
        )
        for role_id in ROLE_IDS
    )

    assert {item.role_id for item in deliveries} == set(ROLE_IDS)
    assert all(item.channel_id == "channel-project" for item in deliveries)
    assert all(
        item.role_id.replace("-", " ").title() in item.display_name
        for item in deliveries
    )
    assert [item[0] for item in tokens.calls] == ["teams-bot"] * len(ROLE_IDS)
    assert {item["application_id"] for item in transport.sends} == {
        f"{role_id}-app" for role_id in ROLE_IDS
    }
    assert {item["display_name"] for item in transport.sends} == {
        f"AM Alpha {role_id.replace('-', ' ').title()}" for role_id in ROLE_IDS
    }
    assert transport.sends[0]["access_token"].startswith("token-for:")


@pytest.mark.parametrize(
    ("installed", "can_send", "code"),
    [
        (False, False, "missing-installation"),
        (True, False, "permission-revoked"),
    ],
)
def test_missing_installation_and_revoked_permission_are_explicit_blockers(
    teams_project: str, installed: bool, can_send: bool, code: str
) -> None:
    connector, _, transport = _connector(teams_project)
    transport.installations["engineering-app"] = TeamsInstallation(
        installed, can_send
    )

    readiness = connector.readiness(project_id="alpha", role_id="engineering")

    assert readiness.status == "blocked"
    assert readiness.blocker_code == code
    with pytest.raises(TeamsConnectorBlocked) as blocked:
        connector.send_channel(
            project_id="alpha",
            role_id="engineering",
            channel="project",
            text="Will not send",
        )
    assert blocked.value.code == code
    assert transport.sends == []


def test_credentials_and_transport_fail_closed_without_secret_leakage(
    teams_project: str,
) -> None:
    connector, tokens, transport = _connector(teams_project)
    tokens.fail = True
    readiness = connector.readiness(project_id="alpha", role_id="engineering")
    assert readiness.blocker_code == "credential-unavailable"
    with pytest.raises(TeamsConnectorBlocked) as credential_blocked:
        connector.send_channel(
            project_id="alpha",
            role_id="engineering",
            channel="project",
            text="Will not send",
        )
    assert "credential-value" not in str(credential_blocked.value)

    tokens.fail = False
    transport.check_error = True
    with pytest.raises(TeamsConnectorBlocked) as check_blocked:
        connector.send_channel(
            project_id="alpha",
            role_id="engineering",
            channel="project",
            text="Will not send",
        )
    assert check_blocked.value.code == "connector-unavailable"
    assert "transport-secret" not in str(check_blocked.value)

    transport.check_error = False
    transport.send_error = True
    with pytest.raises(TeamsConnectorBlocked) as send_blocked:
        connector.send_channel(
            project_id="alpha",
            role_id="engineering",
            channel="project",
            text="Will not send",
        )
    assert send_blocked.value.code == "connector-unavailable"
    assert "transport-secret" not in str(send_blocked.value)


def test_stale_role_activation_and_unknown_channel_fail_before_delivery(
    teams_project: str,
) -> None:
    connector, _, transport = _connector(teams_project)
    with psycopg.connect(teams_project) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.role_bindings
            SET collaboration_identity='substituted-app'
            WHERE project_id='alpha' AND role_id='engineering'
            """
        )
    with pytest.raises(
        TeamsConnectorConfigurationError, match="disagrees with role activation"
    ):
        connector.binding(project_id="alpha", role_id="engineering")
    assert transport.checks == []
    with pytest.raises(TeamsConnectorConfigurationError, match="channel"):
        connector.send_channel(
            project_id="alpha",
            role_id="project-manager",
            channel="unknown",
            text="Will not send",
        )
    assert transport.sends == []


def test_generic_router_does_not_import_connector_specific_code() -> None:
    source = Path("src/agentic_mesh_v5/routing.py").read_text(encoding="utf-8")
    assert "teams_connector" not in source
    assert "microsoft" not in source.casefold()
