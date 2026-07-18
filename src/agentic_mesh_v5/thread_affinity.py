from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

import psycopg

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.warm_engines import RoleInstanceKey
from agentic_mesh_v5.warm_engines import WarmEnginePool
from agentic_mesh_v5.worker_provider import ProviderErrorInfo
from agentic_mesh_v5.worker_provider import ProviderErrorKind
from agentic_mesh_v5.worker_provider import ThreadRequest
from agentic_mesh_v5.worker_provider import WorkerEngine
from agentic_mesh_v5.worker_provider import WorkerProviderError
from agentic_mesh_v5.worker_provider import WorkerThread


T = TypeVar("T")
_STORE_ERROR = "thread affinity operation failed"


class ThreadAffinityError(DatabaseError):
    pass


class ThreadAffinityNotFound(ThreadAffinityError):
    pass


class ThreadAffinityConflict(ThreadAffinityError):
    pass


class ThreadAffinityAuthorizationError(ThreadAffinityError):
    pass


@dataclass(frozen=True, slots=True)
class ThreadAffinityKey:
    project_id: str
    work_item_id: str
    role_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "project_id",
            "work_item_id",
            "role_id",
            "conversation_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, slots=True)
class ThreadBinding:
    key: ThreadAffinityKey
    provider_id: str
    thread_id: str
    last_instance_id: str
    created_at: str
    last_resumed_at: str | None
    updated_at: str


class ThreadAffinityStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def authorize(self, key: ThreadAffinityKey, *, instance_id: str) -> None:
        key = _key(key)
        instance_id = _required(instance_id, "instance_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                if not self._authorized(connection, key, instance_id):
                    raise ThreadAffinityAuthorizationError(
                        "role instance is not authorized for thread affinity"
                    )
        except ThreadAffinityError:
            raise
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    def bind_or_read(
        self,
        key: ThreadAffinityKey,
        *,
        instance_id: str,
        provider_id: str,
        create_thread: Callable[[], str],
    ) -> tuple[ThreadBinding, bool]:
        key = _key(key)
        instance_id = _required(instance_id, "instance_id")
        provider_id = _required(provider_id, "provider_id")
        if not callable(create_thread):
            raise ValueError("create_thread must be callable")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 5025))",
                        (_lock_key(key),),
                    )
                    if not self._authorized(connection, key, instance_id):
                        raise ThreadAffinityAuthorizationError(
                            "role instance is not authorized for thread affinity"
                        )
                    existing = self._select(connection, key)
                    if existing is not None:
                        if existing.provider_id != provider_id:
                            raise ThreadAffinityConflict(
                                "thread affinity provider does not match"
                            )
                        return existing, False
                    thread_id = _required(create_thread(), "thread_id")
                    row = connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.thread_affinities
                            (project_id, work_item_id, role_id, conversation_id,
                             provider_id, thread_id, last_instance_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING provider_id, thread_id, last_instance_id,
                                  created_at::text, last_resumed_at::text,
                                  updated_at::text
                        """,
                        (
                            key.project_id,
                            key.work_item_id,
                            key.role_id,
                            key.conversation_id,
                            provider_id,
                            thread_id,
                            instance_id,
                        ),
                    ).fetchone()
                    return _binding(key, row), True
        except (ThreadAffinityError, ValueError, WorkerProviderError):
            raise
        except psycopg.errors.UniqueViolation:
            raise ThreadAffinityConflict(
                "provider thread is already bound to another context"
            ) from None
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    def mark_resumed(
        self,
        key: ThreadAffinityKey,
        *,
        instance_id: str,
        provider_id: str,
        thread_id: str,
    ) -> ThreadBinding:
        key = _key(key)
        instance_id = _required(instance_id, "instance_id")
        provider_id = _required(provider_id, "provider_id")
        thread_id = _required(thread_id, "thread_id")
        try:
            with psycopg.connect(self._database_url) as connection:
                if not self._authorized(connection, key, instance_id):
                    raise ThreadAffinityAuthorizationError(
                        "role instance is not authorized for thread affinity"
                    )
                row = connection.execute(
                    f"""
                    UPDATE {SCHEMA}.thread_affinities
                    SET last_instance_id = %s,
                        last_resumed_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE project_id = %s AND work_item_id = %s
                      AND role_id = %s AND conversation_id = %s
                      AND provider_id = %s AND thread_id = %s
                    RETURNING provider_id, thread_id, last_instance_id,
                              created_at::text, last_resumed_at::text,
                              updated_at::text
                    """,
                    (
                        instance_id,
                        key.project_id,
                        key.work_item_id,
                        key.role_id,
                        key.conversation_id,
                        provider_id,
                        thread_id,
                    ),
                ).fetchone()
                if row is None:
                    raise ThreadAffinityConflict("thread affinity binding changed")
                return _binding(key, row)
        except ThreadAffinityError:
            raise
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    def read(self, key: ThreadAffinityKey) -> ThreadBinding | None:
        key = _key(key)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                return self._select(connection, key)
        except ThreadAffinityError:
            raise
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    @staticmethod
    def _authorized(
        connection: psycopg.Connection[object],
        key: ThreadAffinityKey,
        instance_id: str,
    ) -> bool:
        return (
            connection.execute(
                f"""
                SELECT 1
                FROM {SCHEMA}.work_items w
                JOIN {SCHEMA}.role_instances i
                  ON i.project_id = w.project_id
                 AND i.instance_id = %s
                 AND i.role_id = %s
                WHERE w.project_id = %s AND w.work_item_id = %s
                """,
                (instance_id, key.role_id, key.project_id, key.work_item_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _select(
        connection: psycopg.Connection[object], key: ThreadAffinityKey
    ) -> ThreadBinding | None:
        row = connection.execute(
            f"""
            SELECT provider_id, thread_id, last_instance_id,
                   created_at::text, last_resumed_at::text, updated_at::text
            FROM {SCHEMA}.thread_affinities
            WHERE project_id = %s AND work_item_id = %s
              AND role_id = %s AND conversation_id = %s
            """,
            (key.project_id, key.work_item_id, key.role_id, key.conversation_id),
        ).fetchone()
        return None if row is None else _binding(key, row)


class ThreadAffinityCoordinator:
    def __init__(self, store: ThreadAffinityStore, pool: WarmEnginePool) -> None:
        if not isinstance(store, ThreadAffinityStore) or not isinstance(
            pool, WarmEnginePool
        ):
            raise ValueError("thread affinity coordinator configuration is invalid")
        self._store = store
        self._pool = pool

    def run(
        self,
        key: ThreadAffinityKey,
        *,
        instance_id: str,
        request: ThreadRequest,
        operation: Callable[[WorkerThread], T],
    ) -> T:
        key = _key(key)
        instance_id = _required(instance_id, "instance_id")
        if not isinstance(request, ThreadRequest) or request.ephemeral:
            raise ValueError("persistent thread request is required")
        if not callable(operation):
            raise ValueError("thread operation must be callable")
        self._store.authorize(key, instance_id=instance_id)
        instance_key = RoleInstanceKey(key.project_id, instance_id)

        def use_engine(engine: WorkerEngine) -> T:
            provider_id = _required(engine.metadata.provider_id, "provider_id")
            created_thread: list[WorkerThread] = []

            def create_thread() -> str:
                thread = engine.start_thread(request)
                _validate_thread(thread)
                created_thread.append(thread)
                return thread.thread_id

            binding, created = self._store.bind_or_read(
                key,
                instance_id=instance_id,
                provider_id=provider_id,
                create_thread=create_thread,
            )
            if created:
                thread = created_thread[0]
            else:
                thread = engine.resume_thread(binding.thread_id, request)
                _validate_thread(thread, expected_id=binding.thread_id)
                self._store.mark_resumed(
                    key,
                    instance_id=instance_id,
                    provider_id=provider_id,
                    thread_id=binding.thread_id,
                )
            return operation(thread)

        return self._pool.run(instance_key, use_engine)


def _binding(key: ThreadAffinityKey, row: object) -> ThreadBinding:
    if not isinstance(row, (tuple, list)) or len(row) != 6:
        raise ThreadAffinityError(_STORE_ERROR)
    return ThreadBinding(key, *row)


def _validate_thread(thread: object, expected_id: str | None = None) -> None:
    if not isinstance(thread, WorkerThread):
        raise _protocol_error()
    thread_id = getattr(thread, "thread_id", None)
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise _protocol_error()
    if expected_id is not None and thread_id != expected_id:
        raise _protocol_error()


def _protocol_error() -> WorkerProviderError:
    return WorkerProviderError(
        ProviderErrorInfo(
            ProviderErrorKind.PROTOCOL,
            False,
            "provider protocol response was invalid",
        )
    )


def _lock_key(key: ThreadAffinityKey) -> str:
    return "\x1f".join(
        (key.project_id, key.work_item_id, key.role_id, key.conversation_id)
    )


def _key(value: object) -> ThreadAffinityKey:
    if not isinstance(value, ThreadAffinityKey):
        raise ValueError("thread affinity key is invalid")
    return value


def _required(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} is invalid")
    return value
