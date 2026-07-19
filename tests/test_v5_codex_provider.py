from __future__ import annotations

from enum import Enum
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

from openai_codex import Codex
from openai_codex import InternalRpcError
from openai_codex import InvalidParamsError
from openai_codex import ParseError
from openai_codex import ServerBusyError
from openai_codex import TransportClosedError
import pytest

import agentic_mesh_v5.codex_provider as codex_provider
from agentic_mesh_v5.codex_provider import CodexProviderConfig
from agentic_mesh_v5.codex_provider import CodexWorkerProvider
from agentic_mesh_v5.worker_provider import ApprovalPolicy
from agentic_mesh_v5.worker_provider import PlanStepStatus
from agentic_mesh_v5.worker_provider import ProviderErrorKind
from agentic_mesh_v5.worker_provider import ProviderEventKind
from agentic_mesh_v5.worker_provider import SandboxPolicy
from agentic_mesh_v5.worker_provider import ThreadRequest
from agentic_mesh_v5.worker_provider import TurnCompletionStatus
from agentic_mesh_v5.worker_provider import TurnRequest
from agentic_mesh_v5.worker_provider import WorkerProviderError


ROOT = Path(__file__).resolve().parents[1]


class FakeStatus(str, Enum):
    IN_PROGRESS = "inProgress"
    INTERRUPTED = "interrupted"


def _notification(method: str, payload: object) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=payload)


class FakeHandle:
    id = "turn-1"

    def __init__(self, notifications=None, error: Exception | None = None) -> None:
        self.notifications = notifications or []
        self.error = error
        self.interrupt_count = 0

    def stream(self):
        yield from self.notifications
        if self.error is not None:
            raise self.error

    def interrupt(self):
        self.interrupt_count += 1


class FakeSdkThread:
    id = "thread-1"

    def __init__(self, handle: FakeHandle) -> None:
        self.handle = handle
        self.turn_calls = []

    def turn(self, prompt, **kwargs):
        self.turn_calls.append((prompt, kwargs))
        return self.handle

    def read(self, *, include_turns: bool):
        client = getattr(self.handle, "_client", None)
        return client.thread_read(self.id, include_turns=include_turns)


class FakeSdk:
    def __init__(self, handle: FakeHandle) -> None:
        self.metadata = SimpleNamespace(
            serverInfo=SimpleNamespace(name="Codex", version="test-version"),
            platformFamily="test-family",
            platformOs="test-os",
        )
        self.thread = FakeSdkThread(handle)
        self.start_calls = []
        self.resume_calls = []
        self.close_count = 0
        self.request_calls = []
        self.capacity_response = None
        self._client = SimpleNamespace(request=self.request)

    def thread_start(self, **kwargs):
        self.start_calls.append(kwargs)
        return self.thread

    def thread_resume(self, thread_id, **kwargs):
        self.resume_calls.append((thread_id, kwargs))
        return self.thread

    def close(self):
        self.close_count += 1

    def request(self, method, params, *, response_model):
        self.request_calls.append((method, params, response_model))
        return self.capacity_response


def _scripted_notifications() -> list[SimpleNamespace]:
    scope = {"thread_id": "thread-1", "turn_id": "turn-1"}
    usage = SimpleNamespace(
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=4,
        reasoning_output_tokens=1,
        total_tokens=15,
    )
    return [
        _notification("provider/future-event", SimpleNamespace()),
        _notification(
            "turn/started",
            SimpleNamespace(thread_id="thread-1", turn=SimpleNamespace(id="turn-1")),
        ),
        _notification(
            "item/agentMessage/delta",
            SimpleNamespace(**scope, item_id="item-1", delta="hello"),
        ),
        _notification(
            "item/started",
            SimpleNamespace(
                **scope, item=SimpleNamespace(root=SimpleNamespace(id="item-2"))
            ),
        ),
        _notification(
            "item/completed",
            SimpleNamespace(
                **scope, item=SimpleNamespace(root=SimpleNamespace(id="item-2"))
            ),
        ),
        _notification(
            "turn/plan/updated",
            SimpleNamespace(
                **scope,
                plan=[SimpleNamespace(step="Implement", status=FakeStatus.IN_PROGRESS)],
            ),
        ),
        _notification(
            "thread/tokenUsage/updated",
            SimpleNamespace(**scope, token_usage=SimpleNamespace(last=usage)),
        ),
        _notification(
            "error",
            SimpleNamespace(
                **scope,
                error=SimpleNamespace(
                    message="raw provider secret",
                    codex_error_info=SimpleNamespace(root="serverOverloaded"),
                ),
                will_retry=True,
            ),
        ),
        _notification(
            "turn/completed",
            SimpleNamespace(
                thread_id="thread-1",
                turn=SimpleNamespace(
                    id="turn-1", status=FakeStatus.INTERRUPTED, error=None
                ),
            ),
        ),
    ]


def test_codex_adapter_maps_configuration_events_and_interrupts(tmp_path: Path) -> None:
    handle = FakeHandle(_scripted_notifications())
    sdk = FakeSdk(handle)
    config = CodexProviderConfig(
        cwd=tmp_path,
        environment={"CODEX_HOME": "C:/secret/auth-cache"},
    )
    captured_configs = []

    def factory(sdk_config):
        captured_configs.append(sdk_config)
        return sdk

    engine = CodexWorkerProvider(config, sdk_factory=factory).open()
    request = ThreadRequest(
        cwd=tmp_path,
        model="model-neutral",
        base_instructions="base prompt",
        developer_instructions="developer prompt",
        sandbox=SandboxPolicy.READ_ONLY,
        approval=ApprovalPolicy.AUTO_REVIEW,
        ephemeral=True,
    )
    thread = engine.start_thread(request)
    turn = thread.start_turn(
        TurnRequest(
            "do the work",
            cwd=tmp_path,
            sandbox=SandboxPolicy.WORKSPACE_WRITE,
            approval=ApprovalPolicy.DENY_ALL,
        )
    )
    events = list(turn.events())
    turn.interrupt()
    turn.interrupt()
    engine.close()
    engine.close()

    assert engine.metadata.provider_id == "codex-local"
    assert engine.metadata.runtime_version == "test-version"
    assert captured_configs[0].experimental_api is False
    assert captured_configs[0].client_name == "agentic_mesh_v5"
    assert captured_configs[0].env == {"CODEX_HOME": "C:/secret/auth-cache"}
    assert "auth-cache" not in repr(config)
    assert sdk.start_calls[0]["ephemeral"] is True
    assert sdk.start_calls[0]["model"] == "model-neutral"
    assert sdk.start_calls[0]["sandbox"].value == "read-only"
    assert sdk.start_calls[0]["approval_mode"].value == "auto_review"
    assert sdk.thread.turn_calls[0][1]["sandbox"].value == "workspace-write"
    assert sdk.thread.turn_calls[0][1]["approval_mode"].value == "deny_all"
    assert [event.kind for event in events] == [
        ProviderEventKind.TURN_STARTED,
        ProviderEventKind.OUTPUT_DELTA,
        ProviderEventKind.ITEM_STARTED,
        ProviderEventKind.ITEM_COMPLETED,
        ProviderEventKind.PLAN_UPDATED,
        ProviderEventKind.USAGE_UPDATED,
        ProviderEventKind.ERROR,
        ProviderEventKind.TURN_COMPLETED,
    ]
    assert events[1].text == "hello"
    assert events[4].plan[0].status is PlanStepStatus.IN_PROGRESS
    assert events[5].usage is not None and events[5].usage.total_tokens == 15
    assert events[6].error is not None
    assert events[6].error.kind is ProviderErrorKind.OVERLOADED
    assert events[6].error.retryable is True
    assert "secret" not in repr(events[6])
    assert events[-1].completion is TurnCompletionStatus.INTERRUPTED
    assert handle.interrupt_count == 1
    assert sdk.close_count == 1


def test_codex_adapter_reads_typed_capacity_without_account_identity(
    tmp_path: Path,
) -> None:
    sdk = FakeSdk(FakeHandle())
    sdk.capacity_response = SimpleNamespace(
        rate_limits=SimpleNamespace(
            limit_id="codex",
            limit_name="Codex weekly",
            plan_type="plus",
            primary=SimpleNamespace(
                used_percent=25,
                resets_at=1_800_000_000,
                window_duration_mins=300,
            ),
            secondary=SimpleNamespace(
                used_percent=80,
                resets_at=1_800_086_400,
                window_duration_mins=10_080,
            ),
            credits=SimpleNamespace(
                balance="12.50", has_credits=True, unlimited=False
            ),
            individual_limit=SimpleNamespace(
                limit="50", used="7.5", remaining_percent=85,
                resets_at=1_800_086_400,
            ),
        ),
        rate_limit_reset_credits=SimpleNamespace(
            available_count=2,
            credits=[
                SimpleNamespace(expires_at=1_800_172_800),
                SimpleNamespace(expires_at=1_800_086_400),
            ],
        ),
    )

    engine = CodexWorkerProvider(
        CodexProviderConfig(cwd=tmp_path), sdk_factory=lambda _config: sdk
    ).open()
    capacity = engine.read_capacity()

    assert sdk.request_calls[0][0:2] == ("account/rateLimits/read", None)
    assert capacity.available is True
    assert capacity.limit_id == "codex"
    assert capacity.limit_name == "Codex weekly"
    assert capacity.primary is not None
    assert capacity.primary.used_percent == 25
    assert capacity.secondary is not None
    assert capacity.secondary.window_minutes == 10_080
    assert capacity.credits is not None and capacity.credits.balance == "12.50"
    assert capacity.individual_limit is not None
    assert capacity.individual_limit.remaining_percent == 85
    assert capacity.reset_credits_available == 2
    assert capacity.reset_credits_earliest_expiry is not None
    assert capacity.reset_credits_earliest_expiry.timestamp() == 1_800_086_400


def test_codex_adapter_accepts_absent_optional_capacity_fields(tmp_path: Path) -> None:
    sdk = FakeSdk(FakeHandle())
    sdk.capacity_response = SimpleNamespace(
        rate_limits=SimpleNamespace(**dict.fromkeys(
            ("limit_id", "limit_name", "plan_type", "primary", "secondary", "credits", "individual_limit")
        )), rate_limit_reset_credits=None)
    engine = CodexWorkerProvider(CodexProviderConfig(cwd=tmp_path), sdk_factory=lambda _config: sdk).open()
    capacity = engine.read_capacity()

    assert capacity.available is True
    assert capacity.primary is None
    assert capacity.reset_credits_available is None


def test_resume_validation_and_cross_turn_events_fail_closed(tmp_path: Path) -> None:
    mismatched = _notification(
        "item/agentMessage/delta",
        SimpleNamespace(
            thread_id="foreign-thread",
            turn_id="turn-1",
            item_id="item",
            delta="unsafe",
        ),
    )
    sdk = FakeSdk(FakeHandle([mismatched]))
    engine = CodexWorkerProvider(sdk_factory=lambda _config: sdk).open()
    normal = ThreadRequest(cwd=tmp_path)
    engine.resume_thread("thread-1", normal)
    assert sdk.resume_calls[0][0] == "thread-1"

    with pytest.raises(WorkerProviderError) as resume_error:
        engine.resume_thread("thread-1", ThreadRequest(cwd=tmp_path, ephemeral=True))
    assert resume_error.value.info.kind is ProviderErrorKind.INVALID_REQUEST

    turn = engine.start_thread(normal).start_turn(TurnRequest("work"))
    with pytest.raises(WorkerProviderError) as event_error:
        list(turn.events())
    assert event_error.value.info.kind is ProviderErrorKind.PROTOCOL
    assert "foreign" not in str(event_error.value)

    engine.close()
    with pytest.raises(WorkerProviderError) as closed_error:
        engine.start_thread(normal)
    assert closed_error.value.info.kind is ProviderErrorKind.INVALID_REQUEST


def test_active_turn_interrupt_and_failed_completion_are_neutral(tmp_path: Path) -> None:
    started = _notification(
        "turn/started",
        SimpleNamespace(thread_id="thread-1", turn=SimpleNamespace(id="turn-1")),
    )
    interrupted = _notification(
        "turn/completed",
        SimpleNamespace(
            thread_id="thread-1",
            turn=SimpleNamespace(
                id="turn-1", status="interrupted", error=None
            ),
        ),
    )
    handle = FakeHandle([started, interrupted])
    engine = CodexWorkerProvider(
        sdk_factory=lambda _config: FakeSdk(handle)
    ).open()
    turn = engine.start_thread(ThreadRequest(cwd=tmp_path)).start_turn(
        TurnRequest("work")
    )
    stream = turn.events()
    assert next(stream).kind is ProviderEventKind.TURN_STARTED
    turn.interrupt()
    completed = list(stream)
    assert completed[-1].completion is TurnCompletionStatus.INTERRUPTED
    assert handle.interrupt_count == 1
    engine.close()

    failed = _notification(
        "turn/completed",
        SimpleNamespace(
            thread_id="thread-1",
            turn=SimpleNamespace(
                id="turn-1",
                status="failed",
                error=SimpleNamespace(
                    message="secret credential detail",
                    codex_error_info=SimpleNamespace(root="unauthorized"),
                ),
            ),
        ),
    )
    failed_sdk = FakeSdk(FakeHandle([failed]))
    failed_engine = CodexWorkerProvider(
        sdk_factory=lambda _config: failed_sdk
    ).open()
    failed_turn = failed_engine.start_thread(ThreadRequest(cwd=tmp_path)).start_turn(
        TurnRequest("work")
    )
    event = list(failed_turn.events())[-1]
    assert event.completion is TurnCompletionStatus.FAILED
    assert event.error is not None
    assert event.error.kind is ProviderErrorKind.AUTHENTICATION
    assert event.error.retryable is False
    assert "credential" not in repr(event)
    failed_engine.close()

    overloaded = _notification(
        "turn/completed",
        SimpleNamespace(
            thread_id="thread-1",
            turn=SimpleNamespace(
                id="turn-1",
                status="failed",
                error=SimpleNamespace(
                    message="raw overload detail",
                    codex_error_info=SimpleNamespace(root="serverOverloaded"),
                ),
            ),
        ),
    )
    overloaded_sdk = FakeSdk(FakeHandle([overloaded]))
    overloaded_engine = CodexWorkerProvider(
        sdk_factory=lambda _config: overloaded_sdk
    ).open()
    overloaded_turn = overloaded_engine.start_thread(
        ThreadRequest(cwd=tmp_path)
    ).start_turn(TurnRequest("work"))
    overloaded_event = list(overloaded_turn.events())[-1]
    assert overloaded_event.error is not None
    assert overloaded_event.error.kind is ProviderErrorKind.OVERLOADED
    assert overloaded_event.error.retryable is True
    overloaded_engine.close()


@pytest.mark.parametrize(
    ("provider_exception", "kind", "retryable"),
    [
        (
            ServerBusyError(-32001, "secret overload", {"secret": "value"}),
            ProviderErrorKind.OVERLOADED,
            True,
        ),
        (
            TransportClosedError("secret transport"),
            ProviderErrorKind.TRANSPORT,
            True,
        ),
        (
            InvalidParamsError(-32602, "secret invalid"),
            ProviderErrorKind.INVALID_REQUEST,
            False,
        ),
        (
            ParseError(-32700, "secret protocol"),
            ProviderErrorKind.PROTOCOL,
            False,
        ),
        (
            InternalRpcError(-32603, "secret internal"),
            ProviderErrorKind.INTERNAL,
            True,
        ),
        (RuntimeError("secret unknown"), ProviderErrorKind.INTERNAL, False),
    ],
)
def test_provider_exceptions_have_stable_safe_classification(
    provider_exception: Exception,
    kind: ProviderErrorKind,
    retryable: bool,
) -> None:
    def failing_factory(_config):
        raise provider_exception

    with pytest.raises(WorkerProviderError) as captured:
        CodexWorkerProvider(sdk_factory=failing_factory).open()

    assert captured.value.info.kind is kind
    assert captured.value.info.retryable is retryable
    assert "secret" not in str(captured.value)
    assert "secret" not in captured.value.info.safe_message
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True


def test_stream_transport_failure_is_retryable_and_redacted(tmp_path: Path) -> None:
    sdk = FakeSdk(FakeHandle(error=TransportClosedError("secret stream failure")))
    engine = CodexWorkerProvider(sdk_factory=lambda _config: sdk).open()
    turn = engine.start_thread(ThreadRequest(cwd=tmp_path)).start_turn(
        TurnRequest("work")
    )

    with pytest.raises(WorkerProviderError) as captured:
        list(turn.events())
    assert captured.value.info.kind is ProviderErrorKind.TRANSPORT
    assert captured.value.info.retryable is True
    assert "secret" not in str(captured.value)
    engine.close()


def test_completed_rollout_uses_fresh_reader_and_drains_pending_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ReconciledClient:
        def __init__(self) -> None:
            self._router = SimpleNamespace(_turn_notifications={})
            self.unregister_count = 0
            self.read_count = 0

        def register_turn_notifications(self, turn_id: str) -> None:
            selected: Queue[object] = Queue()
            selected.put(
                _notification(
                    "turn/started",
                    SimpleNamespace(
                        thread_id="thread-1", turn=SimpleNamespace(id=turn_id)
                    ),
                )
            )
            self._router._turn_notifications[turn_id] = selected

        def unregister_turn_notifications(self, turn_id: str) -> None:
            self._router._turn_notifications.pop(turn_id, None)
            self.unregister_count += 1

        def thread_read(self, thread_id: str, *, include_turns: bool):
            self.read_count += 1
            raise AssertionError("owner thread/read must not be used")

    class ObserverClient:
        def __init__(self, owner: ReconciledClient) -> None:
            self.owner = owner
            self.read_count = 0

        def thread_read(self, thread_id: str, *, include_turns: bool):
            assert include_turns is True
            self.read_count += 1
            self.owner._router._turn_notifications["turn-1"].put(
                _notification(
                    "thread/tokenUsage/updated",
                    SimpleNamespace(
                        thread_id=thread_id,
                        turn_id="turn-1",
                        token_usage=SimpleNamespace(
                            last=SimpleNamespace(
                                input_tokens=8,
                                cached_input_tokens=2,
                                output_tokens=3,
                                reasoning_output_tokens=1,
                                total_tokens=12,
                            )
                        ),
                    ),
                )
            )
            return SimpleNamespace(
                thread=SimpleNamespace(
                    id=thread_id,
                    turns=[
                        SimpleNamespace(
                            id="turn-1", status="completed", error=None
                        )
                    ],
                )
            )

    home = tmp_path / "codex-home"
    rollout = home / "sessions" / "2026" / "07" / "19" / (
        "rollout-2026-07-19T00-00-00-thread-1.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        '{"payload":{"type":"task_complete","turn_id":"turn-1"}}\n',
        encoding="utf-8",
    )
    client = ReconciledClient()
    observer_client = ObserverClient(client)
    handle = FakeHandle()
    handle._client = client
    observer_handle = FakeHandle()
    observer_handle._client = observer_client
    owner_sdk = FakeSdk(handle)
    observer_sdk = FakeSdk(observer_handle)
    monkeypatch.setattr(codex_provider, "_TURN_STATE_POLL_SECONDS", 0.001)
    engine = CodexWorkerProvider(
        CodexProviderConfig(environment={"CODEX_HOME": str(home)}),
        sdk_factory=lambda _config: owner_sdk,
        observer_sdk_factory=lambda _config: observer_sdk,
    ).open()

    turn = engine.start_thread(ThreadRequest(cwd=tmp_path)).start_turn(
        TurnRequest("work")
    )
    events = list(turn.events())

    assert [event.kind for event in events] == [
        ProviderEventKind.TURN_STARTED,
        ProviderEventKind.USAGE_UPDATED,
        ProviderEventKind.TURN_COMPLETED,
    ]
    assert events[-1].completion is TurnCompletionStatus.COMPLETED
    assert events[-2].usage is not None and events[-2].usage.total_tokens == 12
    assert client.unregister_count == 1
    assert client.read_count == 0
    assert observer_client.read_count == 1
    assert observer_sdk.close_count == 1
    engine.close()


def test_stale_owner_state_uses_completed_rollout_hint_and_fresh_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TurnClient:
        def __init__(self, status: str) -> None:
            self.status = status
            self._router = SimpleNamespace(_turn_notifications={})
            self.unregister_count = 0
            self.read_count = 0

        def register_turn_notifications(self, turn_id: str) -> None:
            selected: Queue[object] = Queue()
            selected.put(
                _notification(
                    "turn/started",
                    SimpleNamespace(
                        thread_id="thread-1", turn=SimpleNamespace(id=turn_id)
                    ),
                )
            )
            self._router._turn_notifications[turn_id] = selected

        def unregister_turn_notifications(self, turn_id: str) -> None:
            self._router._turn_notifications.pop(turn_id, None)
            self.unregister_count += 1

        def thread_read(self, thread_id: str, *, include_turns: bool):
            assert include_turns is True
            self.read_count += 1
            return SimpleNamespace(
                thread=SimpleNamespace(
                    id=thread_id,
                    turns=[
                        SimpleNamespace(
                            id="turn-1", status=self.status, error=None
                        )
                    ],
                )
            )

    home = tmp_path / "codex-home"
    rollout = home / "sessions" / "2026" / "07" / "19" / (
        "rollout-2026-07-19T00-00-00-thread-1.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "not-json\n"
        + '{"type":"event_msg","payload":{"type":"task_complete",'
        '"turn_id":"turn-1","last_agent_message":"must not be used"}}\n',
        encoding="utf-8",
    )
    owner_client = TurnClient("inProgress")
    observer_client = TurnClient("completed")
    owner_handle = FakeHandle()
    owner_handle._client = owner_client
    observer_handle = FakeHandle()
    observer_handle._client = observer_client
    owner_sdk = FakeSdk(owner_handle)
    observer_sdk = FakeSdk(observer_handle)
    monkeypatch.setattr(codex_provider, "_TURN_STATE_POLL_SECONDS", 0.001)
    engine = CodexWorkerProvider(
        CodexProviderConfig(environment={"CODEX_HOME": str(home)}),
        sdk_factory=lambda _config: owner_sdk,
        observer_sdk_factory=lambda _config: observer_sdk,
    ).open()

    turn = engine.start_thread(ThreadRequest(cwd=tmp_path)).start_turn(
        TurnRequest("work")
    )
    events = list(turn.events())

    assert [event.kind for event in events] == [
        ProviderEventKind.TURN_STARTED,
        ProviderEventKind.TURN_COMPLETED,
    ]
    assert events[-1].completion is TurnCompletionStatus.COMPLETED
    assert owner_client.read_count == 0
    assert observer_client.read_count == 1
    assert observer_sdk.close_count == 1
    assert "must not be used" not in repr(events)
    engine.close()
    assert owner_sdk.close_count == 1


def test_rollout_terminal_hint_requires_exact_thread_and_turn(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    sessions = home / "sessions" / "2026" / "07" / "19"
    sessions.mkdir(parents=True)
    (sessions / "rollout-2026-07-19T00-00-00-thread-1.jsonl").write_text(
        '{"payload":{"type":"task_complete","turn_id":"turn-2"}}\n',
        encoding="utf-8",
    )
    (sessions / "rollout-2026-07-19T00-00-01-thread-2.jsonl").write_text(
        '{"payload":{"type":"task_complete","turn_id":"turn-1"}}\n',
        encoding="utf-8",
    )

    has_terminal = codex_provider._rollout_terminal_hint(
        CodexProviderConfig(environment={"CODEX_HOME": str(home)})
    )

    assert has_terminal("thread-1", "turn-1") is False


def test_invalid_config_startup_cleanup_and_close_retry_are_safe(
    tmp_path: Path,
) -> None:
    with pytest.raises(WorkerProviderError) as missing_binary:
        CodexWorkerProvider(
            CodexProviderConfig(codex_bin=tmp_path / "missing-codex")
        ).open()
    assert missing_binary.value.info.kind is ProviderErrorKind.INVALID_REQUEST

    with pytest.raises(WorkerProviderError) as invalid_environment:
        CodexWorkerProvider(
            CodexProviderConfig(environment=None)  # type: ignore[arg-type]
        ).open()
    assert invalid_environment.value.info.kind is ProviderErrorKind.INVALID_REQUEST

    for environment in ({"BAD=NAME": "value"}, {"GOOD": "bad\0value"}):
        with pytest.raises(WorkerProviderError) as invalid_entry:
            CodexWorkerProvider(
                CodexProviderConfig(environment=environment)
            ).open()
        assert invalid_entry.value.info.kind is ProviderErrorKind.INVALID_REQUEST

    class BrokenMetadataSdk:
        close_count = 0

        @property
        def metadata(self):
            raise RuntimeError("secret metadata failure")

        def close(self):
            self.close_count += 1

    broken = BrokenMetadataSdk()
    with pytest.raises(WorkerProviderError) as startup:
        CodexWorkerProvider(sdk_factory=lambda _config: broken).open()
    assert startup.value.info.kind is ProviderErrorKind.INTERNAL
    assert "secret" not in str(startup.value)
    assert broken.close_count == 1

    class RetryCloseSdk(FakeSdk):
        def close(self):
            self.close_count += 1
            if self.close_count == 1:
                raise OSError("secret close failure")

    retry_sdk = RetryCloseSdk(FakeHandle())
    engine = CodexWorkerProvider(sdk_factory=lambda _config: retry_sdk).open()
    with pytest.raises(WorkerProviderError) as first_close:
        engine.close()
    assert first_close.value.info.kind is ProviderErrorKind.TRANSPORT
    engine.close()
    engine.close()
    assert retry_sdk.close_count == 2


def test_structured_and_missing_turn_errors_remain_classifiable(tmp_path: Path) -> None:
    structured_root = type("ResponseStreamDisconnectedCodexErrorInfo", (), {})()
    completions = [
        (structured_root, ProviderErrorKind.TRANSPORT, True),
        (None, ProviderErrorKind.INTERNAL, False),
    ]
    for root, expected_kind, expected_retryable in completions:
        completed = _notification(
            "turn/completed",
            SimpleNamespace(
                thread_id="thread-1",
                turn=SimpleNamespace(
                    id="turn-1",
                    status="failed",
                    error=SimpleNamespace(
                        message="secret detail",
                        codex_error_info=(
                            None if root is None else SimpleNamespace(root=root)
                        ),
                    ),
                ),
            ),
        )
        sdk = FakeSdk(FakeHandle([completed]))
        engine = CodexWorkerProvider(sdk_factory=lambda _config, sdk=sdk: sdk).open()
        turn = engine.start_thread(ThreadRequest(cwd=tmp_path)).start_turn(
            TurnRequest("work")
        )
        event = list(turn.events())[-1]
        assert event.error is not None
        assert event.error.kind is expected_kind
        assert event.error.retryable is expected_retryable
        assert "secret" not in repr(event)
        engine.close()


def test_real_sdk_starts_ephemeral_thread_and_closes_app_server() -> None:
    created = []

    def factory(config):
        sdk = Codex(config)
        created.append(sdk)
        return sdk

    engine = CodexWorkerProvider(
        CodexProviderConfig(cwd=ROOT), sdk_factory=factory
    ).open()
    process = created[0]._client._proc
    assert process is not None and process.poll() is None
    assert engine.metadata.provider_id == "codex-local"
    assert engine.metadata.runtime_version is not None
    assert "0.144.4" in engine.metadata.runtime_version
    thread = engine.start_thread(ThreadRequest(cwd=ROOT, ephemeral=True))
    assert thread.thread_id

    engine.close()
    engine.close()
    assert created[0]._client._proc is None


def test_sdk_and_worker_image_versions_are_aligned() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "docker" / "v5" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert '"openai-codex==0.144.4"' in project
    assert "ARG CODEX_VERSION=0.144.4" in dockerfile
