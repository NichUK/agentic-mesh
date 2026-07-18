from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import threading
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.shared_memory import MemoryContext
from agentic_mesh_v5.shared_memory import MemorySource
from agentic_mesh_v5.shared_memory import SharedMemoryAuthorizationError
from agentic_mesh_v5.shared_memory import SharedMemoryConflict
from agentic_mesh_v5.shared_memory import SharedMemoryNotFound
from agentic_mesh_v5.shared_memory import SharedMemorySourceError
from agentic_mesh_v5.shared_memory import SharedMemoryStore
from agentic_mesh_v5.shared_memory import SourceCheck


class MutableSourceVerifier:
    def __init__(self, versions: dict[str, str | None]) -> None:
        self.versions = versions
        self._lock = threading.Lock()

    def set(self, reference: str, version: str | None) -> None:
        with self._lock:
            self.versions[reference] = version

    def verify(self, source: MemorySource) -> SourceCheck:
        with self._lock:
            current = self.versions.get(source.reference)
        if current is None:
            return SourceCheck("removed", None)
        if current == source.observed_version:
            return SourceCheck("current", current)
        return SourceCheck("stale", current)


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
def memory(postgres_database: str) -> tuple[SharedMemoryStore, MutableSourceVerifier]:
    status = MigrationRunner(postgres_database).migrate()
    assert status.current_version == 23
    with psycopg.connect(postgres_database, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects
                (project_id, display_name, organization_id)
            VALUES ('alpha', 'Alpha', 'seerstone'),
                   ('beta', 'Beta', 'seerstone'),
                   ('gamma', 'Gamma', 'other')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering'),
                   ('alpha', 'qa', 'qa'),
                   ('beta', 'engineering', 'engineering'),
                   ('beta', 'qa', 'qa'),
                   ('gamma', 'engineering', 'engineering')
            """
        )
    verifier = MutableSourceVerifier(
        {
            "document://alpha/architecture": "etag-1",
            "document://gamma/architecture": "etag-g1",
            "work-item://alpha/39": "rev-1",
            "event://alpha/100": "event-1",
            "policy://seerstone/engineering-practice": "policy-1",
        }
    )
    return SharedMemoryStore(postgres_database, verifier=verifier), verifier


ALPHA_ENGINEERING = MemoryContext("seerstone", "alpha", "engineering")
ALPHA_QA = MemoryContext("seerstone", "alpha", "qa")
BETA_ENGINEERING = MemoryContext("seerstone", "beta", "engineering")
BETA_QA = MemoryContext("seerstone", "beta", "qa")
GAMMA_ENGINEERING = MemoryContext("other", "gamma", "engineering")
ARCHITECTURE = MemorySource(
    "document", "document://alpha/architecture", "etag-1"
)
ENGINEERING_POLICY = MemorySource(
    "policy", "policy://seerstone/engineering-practice", "policy-1"
)


def _create(
    store: SharedMemoryStore,
    *,
    context: MemoryContext = ALPHA_ENGINEERING,
    scope: str = "project_role",
    subject: str = "architecture-boundary",
    operation_id: str = "op-create-memory",
):
    return store.create(
        context,
        scope=scope,
        subject=subject,
        summary="The document library is authoritative.",
        tags=["architecture", "memory"],
        source=ARCHITECTURE,
        actor_id="engineering",
        operation_id=operation_id,
    )


def _synchronize_initial_replay(store: SharedMemoryStore) -> None:
    original = store._find_replay
    barrier = threading.Barrier(2)

    def synchronized(*args):
        result = original(*args)
        barrier.wait(timeout=5)
        return result

    store._find_replay = synchronized


def test_same_logical_role_shares_memory_without_instance_or_thread_context(memory):
    store, _verifier = memory
    created = _create(store)

    # A second worker instance uses the same logical context; there is no
    # instance or provider-thread coordinate in the contract.
    second_instance = MemoryContext("seerstone", "alpha", "engineering")
    assert store.list_current(second_instance) == (created,)

    updated = store.update(
        second_instance,
        created.memory_id,
        expected_version=1,
        summary="The source document, not shared memory, is authoritative.",
        tags=["architecture", "memory"],
        source=ARCHITECTURE,
        actor_id="engineering-2",
        operation_id="op-update-memory",
    )

    assert updated.version == 2
    assert store.list_current(ALPHA_ENGINEERING)[0].summary == updated.summary
    assert store.list_current(ALPHA_QA) == ()
    assert store.list_current(BETA_ENGINEERING) == ()
    with pytest.raises(SharedMemoryNotFound):
        store.history(ALPHA_QA, created.memory_id)


def test_project_and_organization_role_visibility_is_structural(memory):
    store, _verifier = memory
    project_entry = _create(
        store,
        scope="project",
        subject="delivery-policy",
        operation_id="op-project-memory",
    )
    organization_entry = store.create(
        ALPHA_ENGINEERING,
        scope="organization_role",
        subject="engineering-practice",
        summary="Engineering changes require review evidence.",
        tags=["engineering", "review"],
        source=ENGINEERING_POLICY,
        actor_id="engineering",
        operation_id="op-organization-memory",
    )

    assert [item.memory_id for item in store.list_current(ALPHA_QA)] == [
        project_entry.memory_id
    ]
    assert [item.memory_id for item in store.list_current(BETA_ENGINEERING)] == [
        organization_entry.memory_id
    ]
    assert store.list_current(BETA_QA) == ()
    assert store.list_current(GAMMA_ENGINEERING) == ()

    with pytest.raises(SharedMemoryAuthorizationError):
        store.list_current(MemoryContext("other", "alpha", "engineering"))


def test_writes_require_a_current_authoritative_source(memory):
    store, verifier = memory
    missing = MemorySource("document", "document://alpha/missing", "etag-1")
    with pytest.raises(SharedMemorySourceError, match="does not exist"):
        store.create(
            ALPHA_ENGINEERING,
            scope="project_role",
            subject="missing-source",
            summary="This must not persist.",
            tags=[],
            source=missing,
            actor_id="engineering",
            operation_id="op-missing-source",
        )

    verifier.set(ARCHITECTURE.reference, "etag-2")
    with pytest.raises(SharedMemorySourceError, match="stale"):
        _create(store, operation_id="op-stale-source")


def test_stale_and_removed_sources_are_excluded_but_remain_inspectable(memory):
    store, verifier = memory
    created = _create(store)

    verifier.set(ARCHITECTURE.reference, "etag-2")
    with ThreadPoolExecutor(max_workers=2) as executor:
        refreshed = list(
            executor.map(lambda _index: store.list_current(ALPHA_ENGINEERING), (1, 2))
        )
    assert refreshed == [(), ()]
    stale = store.inspect(ALPHA_ENGINEERING)[0]
    assert (stale.source_state, stale.current_source_version, stale.version) == (
        "stale",
        "etag-2",
        2,
    )

    verifier.set(ARCHITECTURE.reference, None)
    removed = store.inspect(ALPHA_ENGINEERING)[0]
    assert (removed.source_state, removed.current_source_version, removed.version) == (
        "removed",
        None,
        3,
    )
    history = store.history(ALPHA_ENGINEERING, created.memory_id)
    assert [revision.action for revision in history] == [
        "create",
        "source_status",
        "source_status",
    ]
    assert [revision.entry.source_state for revision in history] == [
        "current",
        "stale",
        "removed",
    ]


def test_source_can_become_current_again_without_silent_authority(memory):
    store, verifier = memory
    created = _create(store)
    verifier.set(ARCHITECTURE.reference, "etag-2")
    assert store.list_current(ALPHA_ENGINEERING) == ()
    verifier.set(ARCHITECTURE.reference, "etag-1")

    restored = store.list_current(ALPHA_ENGINEERING)[0]

    assert restored.memory_id == created.memory_id
    assert restored.source_state == "current"
    assert restored.version == 3
    assert [item.action for item in store.history(ALPHA_ENGINEERING, created.memory_id)] == [
        "create",
        "source_status",
        "source_status",
    ]


def test_concurrent_updates_allow_exactly_one_owner(memory):
    store, _verifier = memory
    created = _create(store)

    def update(index: int):
        try:
            return store.update(
                ALPHA_ENGINEERING,
                created.memory_id,
                expected_version=1,
                summary=f"Concurrent summary {index}",
                tags=["architecture"],
                source=ARCHITECTURE,
                actor_id=f"engineering-{index}",
                operation_id=f"op-concurrent-{index}",
            )
        except SharedMemoryConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, (1, 2)))

    successes = [item for item in results if not isinstance(item, Exception)]
    conflicts = [item for item in results if isinstance(item, SharedMemoryConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0].version == 2
    assert len(store.history(ALPHA_ENGINEERING, created.memory_id)) == 2


def test_operations_are_idempotent_but_cannot_be_reused_for_other_requests(memory):
    store, _verifier = memory
    first = _create(store)
    replay = _create(store)

    assert replay == first
    assert len(store.history(ALPHA_ENGINEERING, first.memory_id)) == 1
    with pytest.raises(SharedMemoryConflict, match="operation id"):
        store.create(
            ALPHA_ENGINEERING,
            scope="project_role",
            subject="different-subject",
            summary="Different request.",
            tags=[],
            source=ARCHITECTURE,
            actor_id="engineering",
            operation_id="op-create-memory",
        )

    def duplicate_update():
        return store.update(
            ALPHA_ENGINEERING,
            first.memory_id,
            expected_version=1,
            summary="Updated once.",
            tags=[],
            source=ARCHITECTURE,
            actor_id="engineering",
            operation_id="op-idempotent-update",
        )

    _synchronize_initial_replay(store)
    with ThreadPoolExecutor(max_workers=2) as executor:
        updates = list(executor.map(lambda _index: duplicate_update(), (1, 2)))
    assert updates[0] == updates[1]
    assert len(store.history(ALPHA_ENGINEERING, first.memory_id)) == 2


def test_operation_ids_are_isolated_between_organizations(memory):
    store, _verifier = memory
    alpha = _create(store, operation_id="op-cross-organization")
    gamma = store.create(
        GAMMA_ENGINEERING,
        scope="project_role",
        subject="architecture-boundary",
        summary="Gamma has its own authoritative memory.",
        tags=["architecture"],
        source=MemorySource(
            "document", "document://gamma/architecture", "etag-g1"
        ),
        actor_id="engineering",
        operation_id="op-cross-organization",
    )

    assert alpha.memory_id != gamma.memory_id
    assert store.list_current(GAMMA_ENGINEERING) == (gamma,)


def test_retirement_is_versioned_and_hidden_from_default_inspection(memory):
    store, _verifier = memory
    created = _create(store)

    def duplicate_retire():
        return store.retire(
            ALPHA_ENGINEERING,
            created.memory_id,
            expected_version=1,
            actor_id="engineering",
            operation_id="op-retire-memory",
        )

    _synchronize_initial_replay(store)
    with ThreadPoolExecutor(max_workers=2) as executor:
        retirements = list(executor.map(lambda _index: duplicate_retire(), (1, 2)))
    retired = retirements[0]

    assert retirements[1] == retired
    assert (retired.status, retired.version) == ("retired", 2)
    assert store.inspect(ALPHA_ENGINEERING) == ()
    assert store.inspect(ALPHA_ENGINEERING, include_retired=True) == (retired,)
    assert [item.action for item in store.history(ALPHA_ENGINEERING, created.memory_id)] == [
        "create",
        "retire",
    ]


@pytest.mark.parametrize(
    ("kind", "reference"),
    [
        ("thread", "thread://provider/123"),
        ("document", "thread://provider/123"),
        ("event", "conversation://123"),
        ("work_item", "prompt://system"),
    ],
)
def test_thread_conversation_and_prompt_sources_are_rejected(kind, reference):
    with pytest.raises(SharedMemorySourceError):
        MemorySource(kind, reference, "v1")


def test_schema_has_no_provider_context_authority_and_revisions_are_immutable(
    postgres_database,
):
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database, autocommit=True) as connection:
        columns = connection.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'agentic_mesh_v5'
              AND table_name LIKE 'shared_memory_%'
            """
        ).fetchall()
        forbidden = ("thread", "conversation", "prompt", "turn", "instance")
        assert not [
            (table, column)
            for table, column in columns
            if any(term in column for term in forbidden)
        ]

        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects
                (project_id, display_name, organization_id)
            VALUES ('alpha', 'Alpha', 'seerstone')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering')
            """
        )
        verifier = MutableSourceVerifier({ARCHITECTURE.reference: "etag-1"})
        store = SharedMemoryStore(postgres_database, verifier=verifier)
        entry = _create(store)
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                UPDATE agentic_mesh_v5.shared_memory_entries
                SET source_ref = 'thread://provider/123' WHERE memory_id = %s
                """,
                (entry.memory_id,),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            connection.execute(
                """
                UPDATE agentic_mesh_v5.shared_memory_revisions
                SET summary = 'tampered' WHERE memory_id = %s
                """,
                (entry.memory_id,),
            )
