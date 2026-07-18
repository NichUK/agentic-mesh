from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, TypeVar
from uuid import uuid4

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
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_AFFINITY_LOCK_SEED = 5025
UNPINNED_DIGEST = "unpinned"
_BINDING_COLUMNS = """
    provider_id, thread_id, prompt_digest, generation, affinity_state,
    last_instance_id, active_operation_id, active_instance_id,
    active_started_at::text, created_at::text, last_resumed_at::text,
    updated_at::text, pending_reseed_id
"""


class ThreadAffinityError(DatabaseError):
    pass


class ThreadAffinityNotFound(ThreadAffinityError):
    pass


class ThreadAffinityConflict(ThreadAffinityError):
    pass


class ThreadAffinityAuthorizationError(ThreadAffinityError):
    pass


class ThreadAffinityBusy(ThreadAffinityError):
    pass


class ThreadPromptMismatch(ThreadAffinityConflict):
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
    provider_id: str | None
    thread_id: str | None
    prompt_digest: str
    generation: int
    affinity_state: str
    last_instance_id: str
    active_operation_id: str | None
    active_instance_id: str | None
    active_started_at: str | None
    created_at: str
    last_resumed_at: str | None
    updated_at: str
    pending_reseed_id: str | None


@dataclass(frozen=True, slots=True)
class ThreadOperationClaim:
    key: ThreadAffinityKey
    operation_id: str
    instance_id: str
    prompt_digest: str
    started_at: str


@dataclass(frozen=True, slots=True)
class ThreadReseedRecord:
    key: ThreadAffinityKey
    reseed_id: str
    generation: int
    old_provider_id: str
    old_thread_id: str
    old_prompt_digest: str
    new_prompt_digest: str
    actor_id: str
    reason: str
    recorded_at: str


class ThreadAffinityStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def authorize(self, key: ThreadAffinityKey, *, instance_id: str) -> None:
        key = _key(key)
        instance_id = _required(instance_id, "instance_id")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
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
        prompt_digest: str,
        create_thread: Callable[[], str],
    ) -> tuple[ThreadBinding, bool]:
        return self._bind_or_read(
            key,
            instance_id=instance_id,
            provider_id=provider_id,
            prompt_digest=prompt_digest,
            create_thread=create_thread,
            operation_id=None,
        )

    def bind_and_claim(
        self,
        key: ThreadAffinityKey,
        *,
        instance_id: str,
        provider_id: str,
        prompt_digest: str,
        create_thread: Callable[[], str],
    ) -> tuple[ThreadBinding, bool, ThreadOperationClaim]:
        operation_id = uuid4().hex
        binding, created = self._bind_or_read(
            key,
            instance_id=instance_id,
            provider_id=provider_id,
            prompt_digest=prompt_digest,
            create_thread=create_thread,
            operation_id=operation_id,
        )
        started_at = _required(binding.active_started_at, "active_started_at")
        return (
            binding,
            created,
            ThreadOperationClaim(
                binding.key,
                operation_id,
                instance_id,
                binding.prompt_digest,
                started_at,
            ),
        )

    def _bind_or_read(
        self,
        key: ThreadAffinityKey,
        *,
        instance_id: str,
        provider_id: str,
        prompt_digest: str,
        create_thread: Callable[[], str],
        operation_id: str | None,
    ) -> tuple[ThreadBinding, bool]:
        key = _key(key)
        instance_id = _required(instance_id, "instance_id")
        provider_id = _required(provider_id, "provider_id")
        prompt_digest = _digest(prompt_digest)
        if operation_id is not None:
            operation_id = _required(operation_id, "operation_id")
        if not callable(create_thread):
            raise ValueError("create_thread must be callable")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
                        (_lock_key(key), _AFFINITY_LOCK_SEED),
                    )
                    if not self._authorized(connection, key, instance_id):
                        raise ThreadAffinityAuthorizationError(
                            "role instance is not authorized for thread affinity"
                        )
                    existing = self._select(connection, key)
                    if existing is not None:
                        if existing.prompt_digest != prompt_digest:
                            raise _prompt_mismatch(existing.prompt_digest)
                        if existing.affinity_state == "pending_seed":
                            thread_id = _required(create_thread(), "thread_id")
                            row = connection.execute(
                                f"""
                                UPDATE {SCHEMA}.thread_affinities
                                SET provider_id = %s, thread_id = %s,
                                    last_instance_id = %s,
                                    affinity_state = 'active',
                                    pending_reseed_id = NULL,
                                    updated_at = clock_timestamp()
                                WHERE project_id = %s AND work_item_id = %s
                                  AND role_id = %s AND conversation_id = %s
                                  AND affinity_state = 'pending_seed'
                                  AND prompt_digest = %s
                                RETURNING {_BINDING_COLUMNS}
                                """,
                                (
                                    provider_id,
                                    thread_id,
                                    instance_id,
                                    key.project_id,
                                    key.work_item_id,
                                    key.role_id,
                                    key.conversation_id,
                                    prompt_digest,
                                ),
                            ).fetchone()
                            if row is None:
                                raise ThreadAffinityConflict(
                                    "pending thread affinity changed"
                                )
                            binding = _binding(key, row)
                            if operation_id is not None:
                                binding = self._claim_binding(
                                    connection,
                                    key,
                                    instance_id,
                                    prompt_digest,
                                    operation_id,
                                )
                            return binding, True
                        if existing.provider_id != provider_id:
                            raise ThreadAffinityConflict(
                                "thread affinity provider does not match"
                            )
                        if operation_id is not None:
                            existing = self._claim_binding(
                                connection,
                                key,
                                instance_id,
                                prompt_digest,
                                operation_id,
                            )
                        return existing, False
                    thread_id = _required(create_thread(), "thread_id")
                    row = connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.thread_affinities
                            (project_id, work_item_id, role_id, conversation_id,
                             provider_id, thread_id, prompt_digest,
                             last_instance_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING {_BINDING_COLUMNS}
                        """,
                        (
                            key.project_id,
                            key.work_item_id,
                            key.role_id,
                            key.conversation_id,
                            provider_id,
                            thread_id,
                            prompt_digest,
                            instance_id,
                        ),
                    ).fetchone()
                    binding = _binding(key, row)
                    if operation_id is not None:
                        binding = self._claim_binding(
                            connection,
                            key,
                            instance_id,
                            prompt_digest,
                            operation_id,
                        )
                    return binding, True
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
        prompt_digest: str,
    ) -> ThreadBinding:
        key = _key(key)
        instance_id = _required(instance_id, "instance_id")
        provider_id = _required(provider_id, "provider_id")
        thread_id = _required(thread_id, "thread_id")
        prompt_digest = _digest(prompt_digest)
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
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
                      AND prompt_digest = %s AND affinity_state = 'active'
                    RETURNING {_BINDING_COLUMNS}
                    """,
                    (
                        instance_id,
                        key.project_id,
                        key.work_item_id,
                        key.role_id,
                        key.conversation_id,
                        provider_id,
                        thread_id,
                        prompt_digest,
                    ),
                ).fetchone()
                if row is None:
                    raise ThreadAffinityConflict("thread affinity binding changed")
                return _binding(key, row)
        except ThreadAffinityError:
            raise
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    def claim_operation(
        self,
        key: ThreadAffinityKey,
        *,
        instance_id: str,
        prompt_digest: str,
    ) -> ThreadOperationClaim:
        key = _key(key)
        instance_id = _required(instance_id, "instance_id")
        prompt_digest = _digest(prompt_digest)
        operation_id = uuid4().hex
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                if not self._authorized(connection, key, instance_id):
                    raise ThreadAffinityAuthorizationError(
                        "role instance is not authorized for thread affinity"
                    )
                binding = self._claim_binding(
                    connection,
                    key,
                    instance_id,
                    prompt_digest,
                    operation_id,
                )
                return ThreadOperationClaim(
                    key,
                    operation_id,
                    instance_id,
                    prompt_digest,
                    _required(binding.active_started_at, "active_started_at"),
                )
        except ThreadAffinityError:
            raise
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    def release_operation(self, claim: ThreadOperationClaim) -> None:
        if not isinstance(claim, ThreadOperationClaim):
            raise ValueError("thread operation claim is invalid")
        try:
            with psycopg.connect(self._database_url, connect_timeout=5) as connection:
                row = connection.execute(
                    f"""
                    UPDATE {SCHEMA}.thread_affinities
                    SET active_operation_id = NULL,
                        active_instance_id = NULL,
                        active_started_at = NULL,
                        updated_at = clock_timestamp()
                    WHERE project_id = %s AND work_item_id = %s
                      AND role_id = %s AND conversation_id = %s
                      AND active_operation_id = %s
                    RETURNING 1
                    """,
                    (
                        claim.key.project_id,
                        claim.key.work_item_id,
                        claim.key.role_id,
                        claim.key.conversation_id,
                        claim.operation_id,
                    ),
                ).fetchone()
                if row is None:
                    raise ThreadAffinityBusy(
                        "thread operation claim is no longer current"
                    )
        except ThreadAffinityError:
            raise
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    def reseed(
        self,
        key: ThreadAffinityKey,
        *,
        expected_digest: str,
        new_digest: str,
        actor_id: str,
        reason: str,
    ) -> ThreadReseedRecord:
        key = _key(key)
        expected_digest = _stored_digest(expected_digest)
        new_digest = _digest(new_digest)
        actor_id = _required(actor_id, "actor_id")
        reason = _required(reason, "reason")
        if expected_digest == new_digest:
            raise ThreadAffinityConflict("reseed target must change prompt digest")
        reseed_id = uuid4().hex
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
                        (_lock_key(key), _AFFINITY_LOCK_SEED),
                    )
                    binding = self._select(connection, key, for_update=True)
                    if binding is None:
                        raise ThreadAffinityNotFound("thread affinity not found")
                    if binding.active_operation_id is not None:
                        raise ThreadAffinityBusy("thread affinity is active")
                    if binding.affinity_state != "active":
                        raise ThreadAffinityConflict(
                            "thread affinity reseed is pending"
                        )
                    if binding.prompt_digest != expected_digest:
                        raise ThreadPromptMismatch(
                            "thread affinity prompt digest does not match"
                        )
                    if binding.provider_id is None or binding.thread_id is None:
                        raise ThreadAffinityConflict(
                            "thread affinity has no active thread"
                        )
                    generation = binding.generation + 1
                    row = connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.thread_reseeds
                            (project_id, reseed_id, work_item_id, role_id,
                             conversation_id, generation, old_provider_id,
                             old_thread_id, old_prompt_digest, new_prompt_digest,
                             actor_id, reason)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING recorded_at::text
                        """,
                        (
                            key.project_id,
                            reseed_id,
                            key.work_item_id,
                            key.role_id,
                            key.conversation_id,
                            generation,
                            binding.provider_id,
                            binding.thread_id,
                            binding.prompt_digest,
                            new_digest,
                            actor_id,
                            reason,
                        ),
                    ).fetchone()
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.thread_affinities
                        SET provider_id = NULL, thread_id = NULL,
                            prompt_digest = %s, generation = %s,
                            affinity_state = 'pending_seed',
                            pending_reseed_id = %s,
                            last_resumed_at = NULL,
                            updated_at = clock_timestamp()
                        WHERE project_id = %s AND work_item_id = %s
                          AND role_id = %s AND conversation_id = %s
                        """,
                        (
                            new_digest,
                            generation,
                            reseed_id,
                            key.project_id,
                            key.work_item_id,
                            key.role_id,
                            key.conversation_id,
                        ),
                    )
                    return ThreadReseedRecord(
                        key,
                        reseed_id,
                        generation,
                        binding.provider_id,
                        binding.thread_id,
                        binding.prompt_digest,
                        new_digest,
                        actor_id,
                        reason,
                        row[0],
                    )
        except ThreadAffinityError:
            raise
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    def read_reseeds(
        self, key: ThreadAffinityKey
    ) -> tuple[ThreadReseedRecord, ...]:
        key = _key(key)
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                rows = connection.execute(
                    f"""
                    SELECT reseed_id, generation, old_provider_id, old_thread_id,
                           old_prompt_digest, new_prompt_digest, actor_id, reason,
                           recorded_at::text
                    FROM {SCHEMA}.thread_reseeds
                    WHERE project_id = %s AND work_item_id = %s
                      AND role_id = %s AND conversation_id = %s
                    ORDER BY generation
                    """,
                    (
                        key.project_id,
                        key.work_item_id,
                        key.role_id,
                        key.conversation_id,
                    ),
                ).fetchall()
            return tuple(ThreadReseedRecord(key, *row) for row in rows)
        except ThreadAffinityError:
            raise
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    def read(self, key: ThreadAffinityKey) -> ThreadBinding | None:
        key = _key(key)
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                return self._select(connection, key)
        except ThreadAffinityError:
            raise
        except Exception:
            raise ThreadAffinityError(_STORE_ERROR) from None

    @staticmethod
    def _claim_binding(
        connection: psycopg.Connection[object],
        key: ThreadAffinityKey,
        instance_id: str,
        prompt_digest: str,
        operation_id: str,
    ) -> ThreadBinding:
        row = connection.execute(
            f"""
            UPDATE {SCHEMA}.thread_affinities
            SET active_operation_id = %s,
                active_instance_id = %s,
                active_started_at = clock_timestamp(),
                updated_at = clock_timestamp()
            WHERE project_id = %s AND work_item_id = %s
              AND role_id = %s AND conversation_id = %s
              AND affinity_state = 'active'
              AND prompt_digest = %s
              AND active_operation_id IS NULL
            RETURNING {_BINDING_COLUMNS}
            """,
            (
                operation_id,
                instance_id,
                key.project_id,
                key.work_item_id,
                key.role_id,
                key.conversation_id,
                prompt_digest,
            ),
        ).fetchone()
        if row is None:
            ThreadAffinityStore._raise_claim_conflict(
                connection, key, prompt_digest
            )
        return _binding(key, row)

    @staticmethod
    def _raise_claim_conflict(
        connection: psycopg.Connection[object],
        key: ThreadAffinityKey,
        prompt_digest: str,
    ) -> None:
        binding = ThreadAffinityStore._select(connection, key)
        if binding is None:
            raise ThreadAffinityNotFound("thread affinity not found")
        if binding.prompt_digest != prompt_digest:
            raise _prompt_mismatch(binding.prompt_digest)
        if binding.affinity_state != "active":
            raise ThreadAffinityBusy("thread affinity is pending reseed")
        raise ThreadAffinityBusy("thread affinity already has an active operation")

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
        connection: psycopg.Connection[object],
        key: ThreadAffinityKey,
        *,
        for_update: bool = False,
    ) -> ThreadBinding | None:
        lock_clause = " FOR UPDATE" if for_update else ""
        row = connection.execute(
            f"""
            SELECT {_BINDING_COLUMNS}
            FROM {SCHEMA}.thread_affinities
            WHERE project_id = %s AND work_item_id = %s
              AND role_id = %s AND conversation_id = %s
            {lock_clause}
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
        prompt_digest: str,
        request: ThreadRequest,
        operation: Callable[[WorkerThread], T],
    ) -> T:
        key = _key(key)
        instance_id = _required(instance_id, "instance_id")
        prompt_digest = _digest(prompt_digest)
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

            binding, created, claim = self._store.bind_and_claim(
                key,
                instance_id=instance_id,
                provider_id=provider_id,
                prompt_digest=prompt_digest,
                create_thread=create_thread,
            )
            try:
                if created:
                    thread = created_thread[0]
                else:
                    thread_id = _required(binding.thread_id, "thread_id")
                    thread = engine.resume_thread(thread_id, request)
                    _validate_thread(thread, expected_id=thread_id)
                    self._store.mark_resumed(
                        key,
                        instance_id=instance_id,
                        provider_id=provider_id,
                        thread_id=thread_id,
                        prompt_digest=prompt_digest,
                    )
                result = operation(thread)
            except BaseException as operation_error:
                try:
                    self._store.release_operation(claim)
                except Exception:
                    operation_error.add_note(
                        "durable thread operation claim release also failed"
                    )
                raise
            else:
                self._store.release_operation(claim)
                return result

        return self._pool.run(instance_key, use_engine)


def _binding(key: ThreadAffinityKey, row: object) -> ThreadBinding:
    if not isinstance(row, (tuple, list)) or len(row) != 13:
        raise ThreadAffinityError(_STORE_ERROR)
    binding = ThreadBinding(key, *row)
    try:
        _stored_digest(binding.prompt_digest)
        valid_generation = binding.generation >= 1
    except (TypeError, ValueError):
        raise ThreadAffinityError(_STORE_ERROR) from None
    if not valid_generation or binding.affinity_state not in {
        "active",
        "pending_seed",
    }:
        raise ThreadAffinityError(_STORE_ERROR)
    return binding


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


def _digest(value: object) -> str:
    value = _required(value, "prompt_digest")
    if _DIGEST.fullmatch(value) is None:
        raise ValueError("prompt_digest is invalid")
    return value


def _stored_digest(value: object) -> str:
    value = _required(value, "prompt_digest")
    if value == UNPINNED_DIGEST:
        return value
    return _digest(value)


def _prompt_mismatch(stored_digest: str) -> ThreadPromptMismatch:
    if stored_digest == UNPINNED_DIGEST:
        return ThreadPromptMismatch(
            "thread affinity is unpinned; controlled reseed is required"
        )
    return ThreadPromptMismatch("thread affinity prompt digest does not match")
