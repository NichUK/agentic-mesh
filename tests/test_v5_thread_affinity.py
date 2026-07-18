from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import Barrier, Lock
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.codex_provider import CodexProviderConfig
from agentic_mesh_v5.codex_provider import CodexWorkerProvider
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.thread_affinity import ThreadAffinityAuthorizationError
from agentic_mesh_v5.thread_affinity import ThreadAffinityConflict
from agentic_mesh_v5.thread_affinity import ThreadAffinityCoordinator
from agentic_mesh_v5.thread_affinity import ThreadAffinityError
from agentic_mesh_v5.thread_affinity import ThreadAffinityKey
from agentic_mesh_v5.thread_affinity import ThreadAffinityStore
from agentic_mesh_v5.warm_engines import RoleInstanceKey
from agentic_mesh_v5.warm_engines import WarmEnginePool
from agentic_mesh_v5.worker_provider import EngineMetadata
from agentic_mesh_v5.worker_provider import ProviderErrorInfo
from agentic_mesh_v5.worker_provider import ProviderErrorKind
from agentic_mesh_v5.worker_provider import ProviderEventKind
from agentic_mesh_v5.worker_provider import SandboxPolicy
from agentic_mesh_v5.worker_provider import ThreadRequest
from agentic_mesh_v5.worker_provider import TurnCompletionStatus
from agentic_mesh_v5.worker_provider import TurnRequest
from agentic_mesh_v5.worker_provider import WorkerProviderError


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required")
    database_name = f"mesh_v5_affinity_{uuid.uuid4().hex}"
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
def affinity_database(postgres_database: str) -> tuple[str, ThreadAffinityStore]:
    MigrationRunner(postgres_database).migrate()
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.projects(project_id, display_name)
            VALUES ('alpha', 'Alpha'), ('bravo', 'Bravo')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering'),
                   ('alpha', 'qa', 'qa'),
                   ('bravo', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status, provider_ref)
            VALUES ('alpha', 'engineering-1', 'engineering', 'running', 'fake'),
                   ('alpha', 'engineering-2', 'engineering', 'running', 'fake'),
                   ('alpha', 'qa-1', 'qa', 'running', 'fake'),
                   ('bravo', 'engineering-1', 'engineering', 'running', 'fake')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items
                (project_id, work_item_id, assigned_role_id, title)
            VALUES ('alpha', 'work-1', 'engineering', 'First'),
                   ('alpha', 'work-2', 'engineering', 'Second'),
                   ('bravo', 'work-1', 'engineering', 'Other project')
            """
        )
    return postgres_database, ThreadAffinityStore(postgres_database)


class FakeThread:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id

    def start_turn(self, request):
        return request


class FakeBackend:
    def __init__(self) -> None:
        self.lock = Lock()
        self.threads: set[str] = set()
        self.start_count = 0
        self.resume_ids: list[str] = []
        self.force_resumed_id: str | None = None

    def start(self) -> FakeThread:
        with self.lock:
            self.start_count += 1
            thread_id = f"thread-{self.start_count}"
            self.threads.add(thread_id)
        return FakeThread(thread_id)

    def resume(self, thread_id: str) -> FakeThread:
        with self.lock:
            self.resume_ids.append(thread_id)
            if thread_id not in self.threads:
                raise WorkerProviderError(
                    ProviderErrorInfo(
                        ProviderErrorKind.INVALID_REQUEST,
                        False,
                        "provider rejected the request",
                    )
                )
            return FakeThread(self.force_resumed_id or thread_id)


class FakeEngine:
    def __init__(self, provider_id: str, backend: FakeBackend, number: int) -> None:
        self.backend = backend
        self.number = number
        self.close_count = 0
        self.metadata = EngineMetadata(provider_id, "fake", "1", "test", "test")

    def start_thread(self, request: ThreadRequest) -> FakeThread:
        return self.backend.start()

    def resume_thread(self, thread_id: str, request: ThreadRequest) -> FakeThread:
        return self.backend.resume(thread_id)

    def close(self) -> None:
        self.close_count += 1


class FakeProvider:
    def __init__(self, provider_id: str, engine: FakeEngine) -> None:
        self.provider_id = provider_id
        self.engine = engine

    def open(self) -> FakeEngine:
        return self.engine


class FakeProviderFactory:
    def __init__(self, backend: FakeBackend, provider_id: str = "fake") -> None:
        self.backend = backend
        self.provider_id = provider_id
        self.keys: list[RoleInstanceKey] = []
        self.engines: list[FakeEngine] = []

    def __call__(self, key: RoleInstanceKey) -> FakeProvider:
        self.keys.append(key)
        engine = FakeEngine(self.provider_id, self.backend, len(self.engines) + 1)
        self.engines.append(engine)
        return FakeProvider(self.provider_id, engine)


KEY = ThreadAffinityKey("alpha", "work-1", "engineering", "primary")


def _request(tmp_path: Path, *, ephemeral: bool = False) -> ThreadRequest:
    return ThreadRequest(
        cwd=tmp_path,
        sandbox=SandboxPolicy.WORKSPACE_WRITE,
        ephemeral=ephemeral,
    )


def _coordinator(
    store: ThreadAffinityStore,
    backend: FakeBackend | None = None,
    provider_id: str = "fake",
) -> tuple[ThreadAffinityCoordinator, WarmEnginePool, FakeProviderFactory, FakeBackend]:
    selected = backend or FakeBackend()
    factory = FakeProviderFactory(selected, provider_id)
    pool = WarmEnginePool(factory)
    return ThreadAffinityCoordinator(store, pool), pool, factory, selected


def test_migration_has_project_role_conversation_and_global_thread_constraints(
    affinity_database: tuple[str, ThreadAffinityStore],
) -> None:
    database_url, _store = affinity_database
    with psycopg.connect(database_url) as connection:
        columns = {
            row[0]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'agentic_mesh_v5'
                  AND table_name = 'thread_affinities'
                """
            )
        }
        constraints = "\n".join(
            row[0]
            for row in connection.execute(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'agentic_mesh_v5.thread_affinities'::regclass
                """
            )
        )
    assert columns == {
        "project_id",
        "work_item_id",
        "role_id",
        "conversation_id",
        "provider_id",
        "thread_id",
        "last_instance_id",
        "created_at",
        "last_resumed_at",
        "updated_at",
    }
    assert "PRIMARY KEY (project_id, work_item_id, role_id, conversation_id)" in constraints
    assert "UNIQUE (provider_id, thread_id)" in constraints

    with psycopg.connect(database_url) as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.thread_affinities
                    (project_id, work_item_id, role_id, conversation_id,
                     provider_id, thread_id, last_instance_id)
                VALUES ('alpha', 'work-1', 'engineering', 'invalid-instance-role',
                        'fake', 'wrong-role-thread', 'qa-1')
                """
            )


def test_concurrent_first_bind_creates_one_provider_thread(
    affinity_database: tuple[str, ThreadAffinityStore],
) -> None:
    _database_url, store = affinity_database
    count = 0
    lock = Lock()

    def create() -> str:
        nonlocal count
        with lock:
            count += 1
        time.sleep(0.1)
        return "thread-one"

    def bind():
        return store.bind_or_read(
            KEY,
            instance_id="engineering-1",
            provider_id="fake",
            create_thread=create,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = tuple(executor.map(lambda _item: bind(), range(2)))
    assert count == 1
    assert first[0] == second[0]
    assert {first[1], second[1]} == {True, False}


def test_repeated_operations_resume_one_recorded_thread(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    coordinator, pool, factory, backend = _coordinator(store)

    first = coordinator.run(
        KEY,
        instance_id="engineering-1",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    )
    second = coordinator.run(
        KEY,
        instance_id="engineering-1",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    )

    assert first == second == "thread-1"
    assert backend.start_count == 1
    assert backend.resume_ids == ["thread-1"]
    assert len(factory.engines) == 1
    binding = store.read(KEY)
    assert binding is not None
    assert binding.last_resumed_at is not None
    assert binding.last_instance_id == "engineering-1"
    pool.shutdown()


def test_another_same_role_instance_can_take_over_after_hibernation(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    coordinator, pool, factory, backend = _coordinator(store)
    assert coordinator.run(
        KEY,
        instance_id="engineering-1",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    ) == "thread-1"
    assert pool.hibernate(RoleInstanceKey("alpha", "engineering-1"))

    resumed = coordinator.run(
        KEY,
        instance_id="engineering-2",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    )
    assert resumed == "thread-1"
    assert len(factory.engines) == 2
    assert backend.start_count == 1
    assert backend.resume_ids == ["thread-1"]
    assert store.read(KEY).last_instance_id == "engineering-2"  # type: ignore[union-attr]
    pool.shutdown()


def test_fatal_engine_failure_reopens_and_resumes_same_thread(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    coordinator, pool, factory, backend = _coordinator(store)
    coordinator.run(
        KEY,
        instance_id="engineering-1",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    )
    transport = WorkerProviderError(
        ProviderErrorInfo(
            ProviderErrorKind.TRANSPORT,
            True,
            "provider transport is unavailable",
        )
    )
    with pytest.raises(WorkerProviderError):
        coordinator.run(
            KEY,
            instance_id="engineering-1",
            request=_request(tmp_path),
            operation=lambda _thread: (_ for _ in ()).throw(transport),
        )
    assert factory.engines[0].close_count == 1
    assert coordinator.run(
        KEY,
        instance_id="engineering-1",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    ) == "thread-1"
    assert len(factory.engines) == 2
    assert backend.start_count == 1
    assert backend.resume_ids == ["thread-1", "thread-1"]
    pool.shutdown()


def test_every_affinity_dimension_prevents_context_sharing(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    coordinator, pool, _factory, backend = _coordinator(store)
    cases = [
        (KEY, "engineering-1"),
        (ThreadAffinityKey("alpha", "work-2", "engineering", "primary"), "engineering-1"),
        (ThreadAffinityKey("alpha", "work-1", "engineering", "review"), "engineering-1"),
        (ThreadAffinityKey("alpha", "work-1", "qa", "primary"), "qa-1"),
        (ThreadAffinityKey("bravo", "work-1", "engineering", "primary"), "engineering-1"),
    ]
    thread_ids = [
        coordinator.run(
            key,
            instance_id=instance,
            request=_request(tmp_path),
            operation=lambda thread: thread.thread_id,
        )
        for key, instance in cases
    ]
    assert len(set(thread_ids)) == len(cases)
    assert backend.start_count == len(cases)
    pool.shutdown()


def test_different_work_items_run_concurrently_without_sharing_context(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    coordinator, pool, _factory, backend = _coordinator(store)
    rendezvous = Barrier(2)

    def operation(thread: FakeThread) -> str:
        rendezvous.wait(timeout=5)
        return thread.thread_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            coordinator.run,
            KEY,
            instance_id="engineering-1",
            request=_request(tmp_path),
            operation=operation,
        )
        second = executor.submit(
            coordinator.run,
            ThreadAffinityKey("alpha", "work-2", "engineering", "primary"),
            instance_id="engineering-2",
            request=_request(tmp_path),
            operation=operation,
        )
        assert len({first.result(timeout=10), second.result(timeout=10)}) == 2
    assert backend.start_count == 2
    pool.shutdown()


@pytest.mark.parametrize(
    "key,instance_id",
    [
        (KEY, "qa-1"),
        (KEY, "missing"),
        (ThreadAffinityKey("bravo", "work-1", "engineering", "primary"), "engineering-2"),
    ],
)
def test_wrong_role_or_project_is_rejected_before_provider_open(
    affinity_database: tuple[str, ThreadAffinityStore],
    tmp_path: Path,
    key: ThreadAffinityKey,
    instance_id: str,
) -> None:
    _database_url, store = affinity_database
    coordinator, pool, factory, _backend = _coordinator(store)
    with pytest.raises(ThreadAffinityAuthorizationError):
        coordinator.run(
            key,
            instance_id=instance_id,
            request=_request(tmp_path),
            operation=lambda thread: thread.thread_id,
        )
    assert factory.keys == []
    pool.shutdown()


def test_provider_thread_id_is_unique_across_projects_and_contexts(
    affinity_database: tuple[str, ThreadAffinityStore],
) -> None:
    _database_url, store = affinity_database
    alpha, created = store.bind_or_read(
        KEY,
        instance_id="engineering-1",
        provider_id="fake",
        create_thread=lambda: "shared-thread",
    )
    assert created
    assert alpha.thread_id == "shared-thread"
    with pytest.raises(ThreadAffinityConflict, match="already bound"):
        store.bind_or_read(
            ThreadAffinityKey("bravo", "work-1", "engineering", "primary"),
            instance_id="engineering-1",
            provider_id="fake",
            create_thread=lambda: "shared-thread",
        )


def test_provider_change_for_existing_affinity_fails_closed(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    backend = FakeBackend()
    first, first_pool, _factory, _backend = _coordinator(store, backend, "fake-a")
    first.run(
        KEY,
        instance_id="engineering-1",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    )
    first_pool.shutdown()
    second, second_pool, factory, _backend = _coordinator(store, backend, "fake-b")
    with pytest.raises(ThreadAffinityConflict, match="provider does not match"):
        second.run(
            KEY,
            instance_id="engineering-1",
            request=_request(tmp_path),
            operation=lambda thread: thread.thread_id,
        )
    assert factory.engines[0].close_count == 0
    second_pool.shutdown()


def test_missing_recorded_thread_never_creates_replacement(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    coordinator, pool, _factory, backend = _coordinator(store)
    coordinator.run(
        KEY,
        instance_id="engineering-1",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    )
    backend.threads.clear()
    with pytest.raises(WorkerProviderError) as captured:
        coordinator.run(
            KEY,
            instance_id="engineering-1",
            request=_request(tmp_path),
            operation=lambda thread: thread.thread_id,
        )
    assert captured.value.info.kind is ProviderErrorKind.INVALID_REQUEST
    assert backend.start_count == 1
    assert store.read(KEY).thread_id == "thread-1"  # type: ignore[union-attr]
    pool.shutdown()


def test_malformed_resume_is_protocol_failure_and_evicts_engine(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    coordinator, pool, factory, backend = _coordinator(store)
    coordinator.run(
        KEY,
        instance_id="engineering-1",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    )
    backend.force_resumed_id = "foreign-thread"
    with pytest.raises(WorkerProviderError) as captured:
        coordinator.run(
            KEY,
            instance_id="engineering-1",
            request=_request(tmp_path),
            operation=lambda thread: thread.thread_id,
        )
    assert captured.value.info.kind is ProviderErrorKind.PROTOCOL
    assert factory.engines[0].close_count == 1
    assert pool.snapshot(RoleInstanceKey("alpha", "engineering-1")) is None
    pool.shutdown()


def test_ephemeral_request_is_rejected_before_database_or_provider(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    coordinator, pool, factory, _backend = _coordinator(store)
    with pytest.raises(ValueError, match="persistent thread request"):
        coordinator.run(
            KEY,
            instance_id="engineering-1",
            request=_request(tmp_path, ephemeral=True),
            operation=lambda thread: thread.thread_id,
        )
    assert factory.keys == []
    assert store.read(KEY) is None
    pool.shutdown()


def test_store_failures_are_redacted() -> None:
    store = ThreadAffinityStore("postgresql://127.0.0.1:1/unavailable")
    with pytest.raises(ThreadAffinityError) as captured:
        store.read(KEY)
    assert str(captured.value) == "thread affinity operation failed"
    assert "127.0.0.1" not in str(captured.value)


@pytest.mark.skipif(
    not os.environ.get("AGENTIC_MESH_TEST_CODEX_HOME"),
    reason="explicit external Codex home not supplied",
)
def test_current_codex_engine_resumes_persistent_thread_after_hibernation(
    affinity_database: tuple[str, ThreadAffinityStore], tmp_path: Path
) -> None:
    _database_url, store = affinity_database
    codex_home = Path(os.environ["AGENTIC_MESH_TEST_CODEX_HOME"])
    config = CodexProviderConfig(environment={"CODEX_HOME": str(codex_home)})
    pool = WarmEnginePool(lambda _key: CodexWorkerProvider(config))
    coordinator = ThreadAffinityCoordinator(store, pool)

    def complete_persistence_probe(thread) -> str:
        turn = thread.start_turn(TurnRequest("Reply with the single word OK."))
        events = tuple(turn.events())
        completions = [
            event
            for event in events
            if event.kind is ProviderEventKind.TURN_COMPLETED
        ]
        assert len(completions) == 1
        assert completions[0].completion is TurnCompletionStatus.COMPLETED
        return thread.thread_id

    first = coordinator.run(
        KEY,
        instance_id="engineering-1",
        request=_request(tmp_path),
        operation=complete_persistence_probe,
    )
    assert pool.hibernate(RoleInstanceKey("alpha", "engineering-1"))
    resumed = coordinator.run(
        KEY,
        instance_id="engineering-2",
        request=_request(tmp_path),
        operation=lambda thread: thread.thread_id,
    )

    assert resumed == first
    assert store.read(KEY).thread_id == first  # type: ignore[union-attr]
    pool.shutdown()
