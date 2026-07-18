from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import load_migrations
from agentic_mesh_v5.memory_policy import ConservativeMemoryPolicy
from agentic_mesh_v5.memory_policy import MemoryPolicyError
from agentic_mesh_v5.memory_policy import MemorySensitiveContentError
from agentic_mesh_v5.shared_memory import MemoryContext
from agentic_mesh_v5.shared_memory import MemorySource
from agentic_mesh_v5.shared_memory import SharedMemoryStore
from agentic_mesh_v5.shared_memory import SourceCheck


class SourceVerifier:
    versions = {
        "document://alpha/architecture": "doc-1",
        "policy://seerstone/engineering": "policy-1",
        "event://shared/100": "event-1",
        "policy://seerstone/versioned": "alpha-v1",
    }

    def verify(self, source: MemorySource) -> SourceCheck:
        version = self.versions.get(source.reference)
        if version is None:
            return SourceCheck("removed", None)
        return SourceCheck(
            "current" if version == source.observed_version else "stale", version
        )


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


@pytest.fixture
def governed(postgres_database: str) -> SharedMemoryStore:
    assert MigrationRunner(postgres_database).migrate().current_version == 23
    with psycopg.connect(postgres_database, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects
                (project_id, display_name, organization_id)
            VALUES ('alpha', 'Alpha', 'seerstone'), ('beta', 'Beta', 'seerstone')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering'),
                   ('beta', 'engineering', 'engineering')
            """
        )
    return SharedMemoryStore(
        postgres_database,
        verifier=SourceVerifier(),
        policy=ConservativeMemoryPolicy(project_aliases={"alpha": ["apollo"]}),
    )


ALPHA = MemoryContext("seerstone", "alpha", "engineering")
BETA = MemoryContext("seerstone", "beta", "engineering")
PROJECT_SOURCE = MemorySource(
    "document", "document://alpha/architecture", "doc-1"
)
ORGANIZATION_SOURCE = MemorySource(
    "policy", "policy://seerstone/engineering", "policy-1"
)


def _remember(
    store: SharedMemoryStore,
    *,
    summary: str = "Keep delivery decisions concise.",
    subject: str = "delivery-practice",
    tags: list[str] | None = None,
    source: MemorySource = ORGANIZATION_SOURCE,
    operation_id: str = "policy-create",
):
    return store.create(
        ALPHA,
        scope="organization_role",
        subject=subject,
        summary=summary,
        tags=tags or ["delivery"],
        source=source,
        actor_id="engineering",
        operation_id=operation_id,
    )


def test_migration_backfills_legacy_organization_memory(postgres_database):
    MigrationRunner(postgres_database, migrations=load_migrations()[:19]).migrate()
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects
                (project_id, display_name, organization_id)
            VALUES ('alpha', 'Alpha', 'seerstone')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.shared_memory_entries
                (memory_id, scope, organization_id, role_id, subject, summary, tags,
                 source_kind, source_ref, observed_source_version, source_state,
                 current_source_version, status, version, created_by, updated_by)
            VALUES (%s, 'organization_role', 'seerstone', 'engineering',
                    'legacy-practice', 'Legacy generic memory.', '[]', 'policy',
                    'policy://seerstone/legacy', 'v1', 'current', 'v1', 'active',
                    1, 'migration-test', 'migration-test')
            """,
            (f"mem-{'1' * 32}",),
        )

    assert MigrationRunner(postgres_database).migrate().current_version == 23
    with psycopg.connect(postgres_database, autocommit=True) as connection:
        row = connection.execute(
            """
            SELECT classification, policy_evidence
            FROM agentic_mesh_v5.shared_memory_entries
            WHERE subject = 'legacy-practice'
            """
        ).fetchone()
    assert row == (
        "organization_generic",
        {
            "reasons": ["legacy-organization-entry"],
            "policy_version": "migration-default",
        },
    )


def test_uncertain_and_project_specific_candidates_fail_closed(governed):
    uncertain = _remember(
        governed,
        source=MemorySource("event", "event://shared/100", "event-1"),
        operation_id="uncertain-create",
    )
    project_specific = _remember(
        governed,
        source=PROJECT_SOURCE,
        operation_id="project-create",
        subject="project-practice",
    )

    assert (uncertain.scope, uncertain.classification) == ("project_role", "uncertain")
    assert (project_specific.scope, project_specific.classification) == (
        "project_role",
        "project_specific",
    )
    assert "organization-promotion-denied" in uncertain.policy_evidence["reasons"]
    assert governed.list_current(BETA) == ()


def test_generic_promotion_records_evidence_and_cross_project_visibility(governed):
    promoted = _remember(governed)

    assert promoted.scope == "organization_role"
    assert promoted.classification == "organization_generic"
    assert promoted.policy_evidence == {
        "policy_version": "deterministic-v1",
        "reasons": ["approved-organization-source"],
    }
    assert governed.list_current(BETA) == (promoted,)
    revision = governed.history(BETA, promoted.memory_id)[0]
    assert revision.entry.policy_evidence == promoted.policy_evidence


@pytest.mark.parametrize(
    "summary",
    [
        "password=hunter2",
        "Authorization: Bearer abcdefghijklmnop",
        "postgresql://admin:plain-text@database.internal/app",
        "download?sig=clear-signature&se=tomorrow",
        "-----BEGIN PRIVATE KEY----- key material",
        "Use ghp_1234567890abcdefghij for access",
    ],
)
def test_secrets_are_rejected_without_durable_trace(
    governed, postgres_database, summary
):
    with pytest.raises(MemorySensitiveContentError):
        _remember(governed, summary=summary, operation_id=f"secret-{uuid.uuid4().hex}")

    with psycopg.connect(postgres_database, autocommit=True) as connection:
        counts = connection.execute(
            """
            SELECT (SELECT count(*) FROM agentic_mesh_v5.shared_memory_entries),
                   (SELECT count(*) FROM agentic_mesh_v5.shared_memory_revisions)
            """
        ).fetchone()
    assert counts == (0, 0)


def test_personal_data_is_redacted_without_retaining_originals(
    governed, postgres_database
):
    original_email = "nich@example.com"
    original_phone = "+44 7700 900123"
    entry = _remember(
        governed,
        summary=f"Contact {original_email} or {original_phone} for the practice.",
        operation_id="redacted-create",
    )

    assert entry.summary == (
        "Contact [REDACTED:email] or [REDACTED:telephone] for the practice."
    )
    assert entry.redactions == (
        {"kind": "email", "count": 1},
        {"kind": "telephone", "count": 1},
    )
    with psycopg.connect(postgres_database, autocommit=True) as connection:
        persisted = "\n".join(
            row[0]
            for row in connection.execute(
                """
                SELECT to_jsonb(item)::text FROM agentic_mesh_v5.shared_memory_entries item
                UNION ALL
                SELECT to_jsonb(item)::text FROM agentic_mesh_v5.shared_memory_revisions item
                """
            ).fetchall()
        )
    assert original_email not in persisted
    assert original_phone not in persisted


@pytest.mark.parametrize(
    "source",
    [
        MemorySource(
            "policy", "policy://seerstone/engineering?sig=plain-signature", "policy-1"
        ),
        MemorySource(
            "policy", "policy://seerstone/engineering", "client_secret=plain-value"
        ),
        MemorySource("policy", "policy://seerstone/engineering", "owner@example.com"),
        MemorySource("policy", "policy://seerstone/engineering", "+447700900123"),
    ],
)
def test_secret_like_source_fields_are_rejected(governed, source):
    with pytest.raises(MemorySensitiveContentError):
        _remember(governed, source=source, operation_id=f"source-{uuid.uuid4().hex}")


@pytest.mark.parametrize(
    ("subject", "tags", "summary"),
    [
        ("alpha-practice", ["delivery"], "Generic wording."),
        ("delivery-practice", ["apollo"], "Generic wording."),
        ("delivery-practice", ["delivery"], "Apply this to Alpha."),
        ("delivery-practice", ["delivery"], "Apply this to the Beta project."),
    ],
)
def test_project_ids_and_aliases_prevent_promotion(
    governed, subject, tags, summary
):
    entry = _remember(
        governed,
        subject=subject,
        tags=tags,
        summary=summary,
        operation_id=f"identifier-{uuid.uuid4().hex}",
    )
    assert (entry.scope, entry.classification) == (
        "project_role",
        "project_specific",
    )


def test_project_identifier_in_source_version_prevents_promotion(governed):
    entry = _remember(
        governed,
        source=MemorySource("policy", "policy://seerstone/versioned", "alpha-v1"),
        operation_id="project-version",
    )
    assert (entry.scope, entry.classification) == (
        "project_role",
        "project_specific",
    )


def test_organization_memory_updates_remain_governed(governed):
    entry = _remember(governed)
    with pytest.raises(MemoryPolicyError, match="generic classification"):
        governed.update(
            ALPHA,
            entry.memory_id,
            expected_version=1,
            summary="Alpha needs a special exception.",
            tags=["delivery"],
            source=ORGANIZATION_SOURCE,
            actor_id="engineering",
            operation_id="unsafe-update",
        )
    with pytest.raises(MemorySensitiveContentError):
        governed.update(
            ALPHA,
            entry.memory_id,
            expected_version=1,
            summary="client_secret=do-not-store",
            tags=["delivery"],
            source=ORGANIZATION_SOURCE,
            actor_id="engineering",
            operation_id="secret-update",
        )
    updated = governed.update(
        ALPHA,
        entry.memory_id,
        expected_version=1,
        summary="Ask owner@example.com about the shared practice.",
        tags=["delivery"],
        source=ORGANIZATION_SOURCE,
        actor_id="engineering",
        operation_id="safe-update",
    )
    assert (updated.scope, updated.classification, updated.version) == (
        "organization_role",
        "organization_generic",
        2,
    )
    assert updated.summary == "Ask [REDACTED:email] about the shared practice."
    assert governed.inspect(ALPHA) == (updated,)


def test_structured_personal_data_rejects_instead_of_changing_identity(governed):
    with pytest.raises(MemorySensitiveContentError, match="structured"):
        _remember(
            governed,
            tags=["owner@example.com"],
            operation_id="structured-personal-data",
        )


def test_database_rejects_non_generic_organization_entries(
    governed, postgres_database
):
    entry = _remember(governed)
    with psycopg.connect(postgres_database, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                UPDATE agentic_mesh_v5.shared_memory_entries
                SET classification = 'project_specific' WHERE memory_id = %s
                """,
                (entry.memory_id,),
            )
