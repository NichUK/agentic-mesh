from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest

from agentic_mesh_v5.warm_engines import RoleInstanceKey
from agentic_mesh_v5.warm_engines import WarmEngineConfigurationError
from agentic_mesh_v5.warm_engines import WarmEngineLifecycleError
from agentic_mesh_v5.warm_engines import WarmEnginePool
from agentic_mesh_v5.worker_provider import EngineMetadata
from agentic_mesh_v5.worker_provider import ProviderErrorInfo
from agentic_mesh_v5.worker_provider import ProviderErrorKind
from agentic_mesh_v5.worker_provider import WorkerProviderError


class FakeEngine:
    def __init__(self, number: int, *, close_failures: int = 0) -> None:
        self.number = number
        self.close_failures = close_failures
        self.close_count = 0
        self.metadata = EngineMetadata(
            "fake", "fake-runtime", "1", "test", "test"
        )

    def start_thread(self, request):
        raise NotImplementedError

    def resume_thread(self, thread_id, request):
        raise NotImplementedError

    def close(self) -> None:
        self.close_count += 1
        if self.close_count <= self.close_failures:
            raise RuntimeError("private close detail")


class FakeProvider:
    provider_id = "fake"

    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self.open_count = 0

    def open(self) -> FakeEngine:
        self.open_count += 1
        return self.engine


class ProviderFactory:
    def __init__(self, *, close_failures: int = 0) -> None:
        self.close_failures = close_failures
        self.keys: list[RoleInstanceKey] = []
        self.providers: list[FakeProvider] = []

    def __call__(self, key: RoleInstanceKey) -> FakeProvider:
        self.keys.append(key)
        provider = FakeProvider(
            FakeEngine(len(self.providers) + 1, close_failures=self.close_failures)
        )
        self.providers.append(provider)
        return provider


class Clock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


KEY = RoleInstanceKey("project-one", "engineering-1")


def test_reuses_one_engine_for_repeated_role_instance_operations() -> None:
    factory = ProviderFactory()
    pool = WarmEnginePool(factory)

    observed = [pool.run(KEY, lambda engine: engine.number) for _ in range(3)]

    assert observed == [1, 1, 1]
    assert factory.keys == [KEY]
    assert factory.providers[0].open_count == 1
    snapshot = pool.snapshot(KEY)
    assert snapshot is not None
    assert snapshot.provider_id == "fake"
    assert snapshot.completed_uses == 3
    assert snapshot.active is False
    pool.shutdown()
    assert factory.providers[0].engine.close_count == 1


def test_project_identity_prevents_same_instance_id_from_sharing_engine() -> None:
    factory = ProviderFactory()
    pool = WarmEnginePool(factory)
    first = RoleInstanceKey("project-one", "qa-1")
    second = RoleInstanceKey("project-two", "qa-1")

    assert pool.run(first, lambda engine: engine.number) == 1
    assert pool.run(second, lambda engine: engine.number) == 2
    assert pool.run(first, lambda engine: engine.number) == 1
    assert factory.keys == [first, second]
    pool.shutdown()


def test_operations_are_serial_within_one_instance() -> None:
    factory = ProviderFactory()
    pool = WarmEnginePool(factory)
    first_entered = Event()
    second_entered = Event()
    release_first = Event()
    active = 0
    maximum_active = 0
    counter_lock = Lock()

    def operation(engine: FakeEngine) -> int:
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 1 and not first_entered.is_set():
                first_entered.set()
            else:
                second_entered.set()
        if not release_first.is_set():
            assert release_first.wait(5)
        with counter_lock:
            active -= 1
        return engine.number

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(pool.run, KEY, operation)
        assert first_entered.wait(5)
        second = executor.submit(pool.run, KEY, operation)
        assert not second_entered.wait(0.2)
        release_first.set()
        assert first.result(timeout=5) == 1
        assert second.result(timeout=5) == 1

    assert maximum_active == 1
    assert factory.keys == [KEY]
    pool.shutdown()


def test_different_role_instances_can_run_concurrently() -> None:
    factory = ProviderFactory()
    pool = WarmEnginePool(factory)
    rendezvous = Barrier(2)

    def operation(engine: FakeEngine) -> int:
        rendezvous.wait(timeout=5)
        return engine.number

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(pool.run, KEY, operation)
        second = executor.submit(
            pool.run,
            RoleInstanceKey("project-one", "qa-1"),
            operation,
        )
        assert {first.result(timeout=5), second.result(timeout=5)} == {1, 2}
    pool.shutdown()


def test_idle_engine_stays_warm_until_explicit_hibernation() -> None:
    clock = Clock()
    factory = ProviderFactory()
    pool = WarmEnginePool(factory, clock=clock)
    assert pool.run(KEY, lambda engine: engine.number) == 1

    clock.value = 100.0
    snapshot = pool.snapshot(KEY)
    assert snapshot is not None
    assert snapshot.idle_seconds == 90.0
    assert pool.is_idle(KEY, 90)
    assert not pool.is_idle(KEY, 91)
    assert factory.providers[0].engine.close_count == 0
    assert pool.run(KEY, lambda engine: engine.number) == 1

    assert pool.hibernate(KEY)
    assert pool.snapshot(KEY) is None
    assert factory.providers[0].engine.close_count == 1
    assert pool.hibernate(KEY) is False
    assert pool.run(KEY, lambda engine: engine.number) == 2
    pool.shutdown()


def test_hibernation_waits_for_active_operation() -> None:
    factory = ProviderFactory()
    pool = WarmEnginePool(factory)
    entered = Event()
    release = Event()

    def operation(engine: FakeEngine) -> int:
        entered.set()
        assert release.wait(5)
        return engine.number

    with ThreadPoolExecutor(max_workers=2) as executor:
        running = executor.submit(pool.run, KEY, operation)
        assert entered.wait(5)
        hibernating = executor.submit(pool.hibernate, KEY)
        assert not hibernating.done()
        release.set()
        assert running.result(timeout=5) == 1
        assert hibernating.result(timeout=5) is True
    assert factory.providers[0].engine.close_count == 1
    pool.shutdown()


@pytest.mark.parametrize(
    "kind,evicted",
    [
        (ProviderErrorKind.TRANSPORT, True),
        (ProviderErrorKind.PROTOCOL, True),
        (ProviderErrorKind.AUTHENTICATION, False),
        (ProviderErrorKind.CAPACITY, False),
        (ProviderErrorKind.OVERLOADED, False),
        (ProviderErrorKind.INVALID_REQUEST, False),
        (ProviderErrorKind.EXECUTION, False),
        (ProviderErrorKind.INTERNAL, False),
    ],
)
def test_only_engine_fatal_provider_errors_replace_engine(
    kind: ProviderErrorKind,
    evicted: bool,
) -> None:
    factory = ProviderFactory()
    pool = WarmEnginePool(factory)
    error = WorkerProviderError(ProviderErrorInfo(kind, True, "safe provider error"))

    with pytest.raises(WorkerProviderError) as captured:
        pool.run(KEY, lambda _engine: (_ for _ in ()).throw(error))
    assert captured.value is error

    expected = 2 if evicted else 1
    assert pool.run(KEY, lambda engine: engine.number) == expected
    assert len(factory.providers) == expected
    assert factory.providers[0].engine.close_count == (1 if evicted else 0)
    pool.shutdown()


def test_consumer_exception_does_not_replace_healthy_engine() -> None:
    clock = Clock()
    factory = ProviderFactory()
    pool = WarmEnginePool(factory, clock=clock)
    pool.run(KEY, lambda engine: engine.number)
    clock.value = 100

    with pytest.raises(RuntimeError, match="consumer bug"):
        pool.run(
            KEY,
            lambda _engine: (_ for _ in ()).throw(RuntimeError("consumer bug")),
        )
    after_failure = pool.snapshot(KEY)
    assert after_failure is not None
    assert after_failure.completed_uses == 1
    assert after_failure.idle_seconds == 0
    assert pool.run(KEY, lambda engine: engine.number) == 1
    assert len(factory.providers) == 1
    snapshot = pool.snapshot(KEY)
    assert snapshot is not None
    assert snapshot.completed_uses == 2
    assert snapshot.idle_seconds == 0
    pool.shutdown()


def test_explicit_discard_is_identity_safe_and_opens_replacement() -> None:
    factory = ProviderFactory()
    pool = WarmEnginePool(factory)
    first = pool.run(KEY, lambda engine: engine)

    assert pool.discard(KEY, FakeEngine(99)) is False
    assert pool.discard(KEY, first) is True
    assert first.close_count == 1
    second = pool.run(KEY, lambda engine: engine)
    assert second is not first
    assert pool.discard(KEY, first) is False
    assert pool.run(KEY, lambda engine: engine) is second
    pool.shutdown()


def test_close_retries_and_lifecycle_errors_are_safe() -> None:
    once_factory = ProviderFactory(close_failures=1)
    once = WarmEnginePool(once_factory)
    once.run(KEY, lambda engine: engine.number)
    assert once.hibernate(KEY)
    assert once_factory.providers[0].engine.close_count == 2

    always_factory = ProviderFactory(close_failures=3)
    always = WarmEnginePool(always_factory)
    always.run(KEY, lambda engine: engine.number)
    with pytest.raises(WarmEngineLifecycleError) as captured:
        always.hibernate(KEY)
    assert str(captured.value) == "warm worker engine lifecycle failed"
    assert "private" not in str(captured.value)
    assert always.snapshot(KEY) is None


def test_shutdown_closes_every_engine_and_prevents_reopen() -> None:
    factory = ProviderFactory()
    pool = WarmEnginePool(factory)
    keys = [KEY, RoleInstanceKey("project-one", "qa-1")]
    for key in keys:
        pool.run(key, lambda engine: engine.number)

    pool.shutdown()
    pool.shutdown()
    assert [provider.engine.close_count for provider in factory.providers] == [1, 1]
    assert "engine_count=0" in repr(pool)
    assert "closed=True" in repr(pool)
    with pytest.raises(WarmEngineLifecycleError, match="pool is closed"):
        pool.run(KEY, lambda engine: engine.number)


def test_unexpected_provider_startup_failures_are_redacted() -> None:
    def broken_factory(_key: RoleInstanceKey):
        raise RuntimeError("private provider factory detail")

    with pytest.raises(WarmEngineLifecycleError) as factory_error:
        WarmEnginePool(broken_factory).run(KEY, lambda engine: engine)
    assert str(factory_error.value) == "warm worker engine lifecycle failed"
    assert factory_error.value.__context__ is None

    class BrokenProvider(FakeProvider):
        def open(self):
            raise RuntimeError("private provider open detail")

    provider = BrokenProvider(FakeEngine(1))
    with pytest.raises(WarmEngineLifecycleError) as open_error:
        WarmEnginePool(lambda _key: provider).run(KEY, lambda engine: engine)
    assert str(open_error.value) == "warm worker engine lifecycle failed"
    assert open_error.value.__context__ is None


@pytest.mark.parametrize(
    "key",
    [
        RoleInstanceKey,
        None,
        ("project", "instance"),
    ],
)
def test_invalid_runtime_keys_are_rejected(key: object) -> None:
    pool = WarmEnginePool(ProviderFactory())
    with pytest.raises(WarmEngineConfigurationError):
        pool.snapshot(key)  # type: ignore[arg-type]
    pool.shutdown()


@pytest.mark.parametrize(
    "project_id,instance_id",
    [("", "one"), (" one", "two"), ("one", "two ")],
)
def test_invalid_role_instance_identity_is_rejected(
    project_id: str,
    instance_id: str,
) -> None:
    with pytest.raises(WarmEngineConfigurationError):
        RoleInstanceKey(project_id, instance_id)


@pytest.mark.parametrize("threshold", [-1, float("inf"), float("nan"), True])
def test_invalid_idle_threshold_is_rejected(threshold: object) -> None:
    pool = WarmEnginePool(ProviderFactory())
    with pytest.raises(WarmEngineConfigurationError):
        pool.is_idle(KEY, threshold)  # type: ignore[arg-type]
    pool.shutdown()
