from __future__ import annotations

from dataclasses import dataclass, field
import math
from threading import Lock, RLock
import time
from typing import Callable, TypeVar

from agentic_mesh_v5.worker_provider import ProviderErrorKind
from agentic_mesh_v5.worker_provider import WorkerEngine
from agentic_mesh_v5.worker_provider import WorkerProvider
from agentic_mesh_v5.worker_provider import WorkerProviderError


_LIFECYCLE_ERROR = "warm worker engine lifecycle failed"
_FATAL_ENGINE_ERRORS = {
    ProviderErrorKind.TRANSPORT,
    ProviderErrorKind.PROTOCOL,
}
T = TypeVar("T")


class WarmEngineConfigurationError(ValueError):
    pass


class WarmEngineLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RoleInstanceKey:
    project_id: str
    instance_id: str

    def __post_init__(self) -> None:
        if not _valid_id(self.project_id) or not _valid_id(self.instance_id):
            raise WarmEngineConfigurationError("role instance identity is invalid")


@dataclass(frozen=True, slots=True)
class WarmEngineSnapshot:
    key: RoleInstanceKey
    provider_id: str
    completed_uses: int
    idle_seconds: float
    active: bool


@dataclass(slots=True)
class _Entry:
    engine: WorkerEngine
    provider_id: str
    opened_at: float
    last_used_at: float
    completed_uses: int = 0
    active: bool = False
    operation_lock: Lock = field(default_factory=Lock)


class WarmEnginePool:
    def __init__(
        self,
        provider_factory: Callable[[RoleInstanceKey], WorkerProvider],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(provider_factory) or not callable(clock):
            raise WarmEngineConfigurationError("warm engine configuration is invalid")
        self._provider_factory = provider_factory
        self._clock = clock
        self._entries: dict[RoleInstanceKey, _Entry] = {}
        self._opening: dict[RoleInstanceKey, Lock] = {}
        self._lock = RLock()
        self._closed = False

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"{type(self).__name__}(engine_count={len(self._entries)}, "
                f"closed={self._closed})"
            )

    def run(self, key: RoleInstanceKey, operation: Callable[[WorkerEngine], T]) -> T:
        key = _key(key)
        if not callable(operation):
            raise WarmEngineConfigurationError("engine operation is invalid")
        entry = self._lock_current_entry(key)
        try:
            with self._lock:
                entry.active = True
            try:
                result = operation(entry.engine)
            except WorkerProviderError as exc:
                if exc.info.kind in _FATAL_ENGINE_ERRORS:
                    self._evict_current(key, entry)
                    _close_twice(entry.engine)
                raise
            with self._lock:
                if self._entries.get(key) is entry:
                    entry.completed_uses += 1
            return result
        finally:
            try:
                with self._lock:
                    if self._entries.get(key) is entry:
                        entry.last_used_at = self._now()
                    entry.active = False
            finally:
                entry.operation_lock.release()

    def discard(self, key: RoleInstanceKey, engine: WorkerEngine) -> bool:
        key = _key(key)
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is None or entry.engine is not engine:
                    return False
            entry.operation_lock.acquire()
            with self._lock:
                if self._entries.get(key) is entry and entry.engine is engine:
                    del self._entries[key]
                    break
            entry.operation_lock.release()
        try:
            if not _close_twice(entry.engine):
                raise WarmEngineLifecycleError(_LIFECYCLE_ERROR)
            return True
        finally:
            entry.operation_lock.release()

    def abort_active(self, key: RoleInstanceKey) -> bool:
        """Evict and close the engine that owns a timed-out active operation."""
        key = _key(key)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not entry.active:
                return False
            del self._entries[key]
        if not _close_twice(entry.engine):
            raise WarmEngineLifecycleError(_LIFECYCLE_ERROR)
        return True

    def hibernate(self, key: RoleInstanceKey) -> bool:
        key = _key(key)
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is None:
                    return False
            entry.operation_lock.acquire()
            with self._lock:
                if self._entries.get(key) is entry:
                    del self._entries[key]
                    break
            entry.operation_lock.release()
        try:
            if not _close_twice(entry.engine):
                raise WarmEngineLifecycleError(_LIFECYCLE_ERROR)
            return True
        finally:
            entry.operation_lock.release()

    def is_idle(self, key: RoleInstanceKey, idle_seconds: float) -> bool:
        key = _key(key)
        if (
            isinstance(idle_seconds, bool)
            or not isinstance(idle_seconds, (int, float))
            or not math.isfinite(float(idle_seconds))
            or idle_seconds < 0
        ):
            raise WarmEngineConfigurationError("idle threshold is invalid")
        snapshot = self.snapshot(key)
        return (
            snapshot is not None
            and not snapshot.active
            and snapshot.idle_seconds >= float(idle_seconds)
        )

    def snapshot(self, key: RoleInstanceKey) -> WarmEngineSnapshot | None:
        key = _key(key)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            return WarmEngineSnapshot(
                key=key,
                provider_id=entry.provider_id,
                completed_uses=entry.completed_uses,
                idle_seconds=max(0.0, self._now() - entry.last_used_at),
                active=entry.active,
            )

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._entries.values())
            self._entries.clear()
        failed = False
        for entry in entries:
            with entry.operation_lock:
                if not _close_twice(entry.engine):
                    failed = True
        if failed:
            raise WarmEngineLifecycleError(_LIFECYCLE_ERROR)

    def _lock_current_entry(self, key: RoleInstanceKey) -> _Entry:
        while True:
            entry = self._get_or_open(key)
            entry.operation_lock.acquire()
            with self._lock:
                if self._entries.get(key) is entry:
                    return entry
            entry.operation_lock.release()

    def _get_or_open(self, key: RoleInstanceKey) -> _Entry:
        with self._lock:
            if self._closed:
                raise WarmEngineLifecycleError("warm engine pool is closed")
            existing = self._entries.get(key)
            if existing is not None:
                return existing
            opening = self._opening.setdefault(key, Lock())
        with opening:
            with self._lock:
                if self._closed:
                    raise WarmEngineLifecycleError("warm engine pool is closed")
                existing = self._entries.get(key)
                if existing is not None:
                    return existing
            opened = self._open_entry(key)
            with self._lock:
                if self._closed:
                    _close_twice(opened.engine)
                    raise WarmEngineLifecycleError("warm engine pool is closed")
                existing = self._entries.get(key)
                if existing is None:
                    self._entries[key] = opened
                    return opened
            _close_twice(opened.engine)
            return existing

    def _open_entry(self, key: RoleInstanceKey) -> _Entry:
        provider_failed = False
        try:
            provider = self._provider_factory(key)
        except WorkerProviderError:
            raise
        except Exception:
            provider_failed = True
        if provider_failed:
            raise WarmEngineLifecycleError(_LIFECYCLE_ERROR)
        if not isinstance(provider, WorkerProvider):
            raise WarmEngineConfigurationError("worker provider is invalid")
        open_failed = False
        try:
            engine = provider.open()
        except WorkerProviderError:
            raise
        except Exception:
            open_failed = True
        if open_failed:
            raise WarmEngineLifecycleError(_LIFECYCLE_ERROR)
        if not isinstance(engine, WorkerEngine):
            _close_twice(engine)
            raise WarmEngineConfigurationError("worker engine is invalid")
        if not _valid_id(provider.provider_id):
            _close_twice(engine)
            raise WarmEngineConfigurationError("worker provider is invalid")
        now = self._now()
        return _Entry(engine, provider.provider_id, now, now)

    def _evict_current(self, key: RoleInstanceKey, entry: _Entry) -> None:
        with self._lock:
            if self._entries.get(key) is entry:
                del self._entries[key]

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise WarmEngineLifecycleError("warm engine clock is invalid")
        return float(value)


def _close_twice(engine: object) -> bool:
    for _attempt in range(2):
        try:
            engine.close()  # type: ignore[attr-defined]
            return True
        except Exception:
            pass
    return False


def _key(value: object) -> RoleInstanceKey:
    if not isinstance(value, RoleInstanceKey):
        raise WarmEngineConfigurationError("role instance identity is invalid")
    return value


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()
