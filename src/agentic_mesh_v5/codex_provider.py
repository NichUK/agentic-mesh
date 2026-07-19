from __future__ import annotations

from collections.abc import Mapping as RuntimeMapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from glob import escape as escape_glob
import json
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Iterator, Mapping

from openai_codex import ApprovalMode as CodexApprovalMode
from openai_codex import Codex
from openai_codex import CodexConfig
from openai_codex import Sandbox as CodexSandbox
from openai_codex import CodexError
from openai_codex import InternalRpcError
from openai_codex import InvalidParamsError
from openai_codex import InvalidRequestError
from openai_codex import JsonRpcError
from openai_codex import MethodNotFoundError
from openai_codex import ParseError
from openai_codex import RetryLimitExceededError
from openai_codex import ServerBusyError
from openai_codex import TransportClosedError
from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

from agentic_mesh_v5.worker_provider import ApprovalPolicy
from agentic_mesh_v5.worker_provider import EngineMetadata
from agentic_mesh_v5.worker_provider import PlanStepStatus
from agentic_mesh_v5.worker_provider import ProviderErrorInfo
from agentic_mesh_v5.worker_provider import ProviderErrorKind
from agentic_mesh_v5.worker_provider import ProviderEvent
from agentic_mesh_v5.worker_provider import ProviderEventKind
from agentic_mesh_v5.worker_provider import ProviderCapacity
from agentic_mesh_v5.worker_provider import ProviderCredits
from agentic_mesh_v5.worker_provider import ProviderPlanStep
from agentic_mesh_v5.worker_provider import ProviderRateLimitWindow
from agentic_mesh_v5.worker_provider import ProviderSpendControl
from agentic_mesh_v5.worker_provider import ProviderUsage
from agentic_mesh_v5.worker_provider import SandboxPolicy
from agentic_mesh_v5.worker_provider import ThreadRequest
from agentic_mesh_v5.worker_provider import TurnCompletionStatus
from agentic_mesh_v5.worker_provider import TurnRequest
from agentic_mesh_v5.worker_provider import WorkerProviderError


_SANDBOXES = {
    SandboxPolicy.READ_ONLY: CodexSandbox.read_only,
    SandboxPolicy.WORKSPACE_WRITE: CodexSandbox.workspace_write,
    SandboxPolicy.FULL_ACCESS: CodexSandbox.full_access,
}
_APPROVALS = {
    ApprovalPolicy.DENY_ALL: CodexApprovalMode.deny_all,
    ApprovalPolicy.AUTO_REVIEW: CodexApprovalMode.auto_review,
}
_SAFE_MESSAGES = {
    ProviderErrorKind.AUTHENTICATION: "provider authentication is unavailable",
    ProviderErrorKind.CAPACITY: "provider capacity is unavailable",
    ProviderErrorKind.OVERLOADED: "provider is temporarily overloaded",
    ProviderErrorKind.TRANSPORT: "provider transport is unavailable",
    ProviderErrorKind.INVALID_REQUEST: "provider rejected the request",
    ProviderErrorKind.EXECUTION: "provider execution failed",
    ProviderErrorKind.PROTOCOL: "provider protocol response was invalid",
    ProviderErrorKind.INTERNAL: "provider operation failed",
}
_TURN_STATE_POLL_SECONDS = 1.0
_ROLLOUT_TAIL_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CodexProviderConfig:
    codex_bin: Path | None = None
    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)


class CodexWorkerProvider:
    provider_id = "codex-local"

    def __init__(
        self,
        config: CodexProviderConfig | None = None,
        *,
        sdk_factory: Callable[[CodexConfig], Any] = Codex,
        observer_sdk_factory: Callable[[CodexConfig], Any] | None = None,
    ) -> None:
        self._config = config or CodexProviderConfig()
        self._sdk_factory = sdk_factory
        self._observer_sdk_factory = observer_sdk_factory or sdk_factory

    def open(self) -> CodexWorkerEngine:
        config = _sdk_config(self._config)
        sdk: Any | None = None
        try:
            sdk = self._sdk_factory(config)
            return CodexWorkerEngine(
                self.provider_id,
                sdk,
                terminal_reader=_FreshTerminalReader(
                    config, self._observer_sdk_factory
                ),
                terminal_hint=_rollout_terminal_hint(self._config),
            )
        except Exception as exc:
            if sdk is not None:
                try:
                    sdk.close()
                except Exception:
                    pass
            raise _exception(exc) from None


class CodexWorkerEngine:
    def __init__(
        self,
        provider_id: str,
        sdk: Any,
        *,
        terminal_reader: Callable[[str], object],
        terminal_hint: Callable[[str, str], bool],
    ) -> None:
        self._provider_id = provider_id
        self._sdk = sdk
        self._terminal_reader = terminal_reader
        self._terminal_hint = terminal_hint
        self._closed = False
        self._metadata = _metadata(provider_id, getattr(sdk, "metadata", None))

    @property
    def metadata(self) -> EngineMetadata:
        return self._metadata

    def start_thread(self, request: ThreadRequest) -> CodexWorkerThread:
        self._ensure_open()
        _validate_thread_request(request)
        try:
            thread = self._sdk.thread_start(
                approval_mode=_APPROVALS[request.approval],
                base_instructions=request.base_instructions,
                cwd=str(request.cwd.resolve()),
                developer_instructions=request.developer_instructions,
                ephemeral=request.ephemeral,
                model=request.model,
                sandbox=_SANDBOXES[request.sandbox],
                service_name="agentic_mesh_v5",
            )
        except Exception as exc:
            raise _exception(exc) from None
        return CodexWorkerThread(
            thread,
            terminal_reader=self._terminal_reader,
            terminal_hint=self._terminal_hint,
        )

    def resume_thread(
        self, thread_id: str, request: ThreadRequest
    ) -> CodexWorkerThread:
        self._ensure_open()
        thread_id = _required_text(thread_id, "thread_id")
        _validate_thread_request(request)
        if request.ephemeral:
            raise _invalid("ephemeral cannot be changed while resuming a thread")
        try:
            thread = self._sdk.thread_resume(
                thread_id,
                approval_mode=_APPROVALS[request.approval],
                base_instructions=request.base_instructions,
                cwd=str(request.cwd.resolve()),
                developer_instructions=request.developer_instructions,
                model=request.model,
                sandbox=_SANDBOXES[request.sandbox],
            )
        except Exception as exc:
            raise _exception(exc) from None
        return CodexWorkerThread(
            thread,
            terminal_reader=self._terminal_reader,
            terminal_hint=self._terminal_hint,
        )

    def read_capacity(self) -> ProviderCapacity:
        self._ensure_open()
        try:
            client = getattr(self._sdk, "_client", self._sdk)
            request = getattr(client, "request", None)
            if not callable(request):
                raise _protocol_error()
            response = request(
                "account/rateLimits/read",
                None,
                response_model=GetAccountRateLimitsResponse,
            )
            return _capacity(response)
        except Exception as exc:
            raise _exception(exc) from None

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._sdk.close()
        except Exception as exc:
            raise _exception(exc) from None
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise _invalid("worker engine is closed")


class CodexWorkerThread:
    def __init__(
        self,
        thread: Any,
        *,
        terminal_reader: Callable[[str], object],
        terminal_hint: Callable[[str, str], bool],
    ) -> None:
        self._thread = thread
        self._terminal_reader = terminal_reader
        self._terminal_hint = terminal_hint
        self.thread_id = _required_text(getattr(thread, "id", None), "thread_id")

    def start_turn(self, request: TurnRequest) -> CodexWorkerTurn:
        _validate_turn_request(request)
        try:
            handle = self._thread.turn(
                request.prompt,
                approval_mode=(
                    None if request.approval is None else _APPROVALS[request.approval]
                ),
                cwd=None if request.cwd is None else str(request.cwd.resolve()),
                model=request.model,
                sandbox=(
                    None if request.sandbox is None else _SANDBOXES[request.sandbox]
                ),
            )
        except Exception as exc:
            raise _exception(exc) from None
        return CodexWorkerTurn(
            self.thread_id,
            handle,
            terminal_reader=self._terminal_reader,
            terminal_hint=self._terminal_hint,
        )


class CodexWorkerTurn:
    def __init__(
        self,
        thread_id: str,
        handle: Any,
        *,
        terminal_reader: Callable[[str], object],
        terminal_hint: Callable[[str, str], bool],
    ) -> None:
        self.thread_id = thread_id
        self.turn_id = _required_text(getattr(handle, "id", None), "turn_id")
        self._handle = handle
        self._terminal_reader = terminal_reader
        self._terminal_hint = terminal_hint
        self._interrupt_requested = False

    def events(self) -> Iterator[ProviderEvent]:
        completed = False
        try:
            notification_queue = _sdk_turn_notification_queue(self._handle)
            if notification_queue is None:
                notifications = self._handle.stream()
            else:
                client, selected_queue = notification_queue
                notifications = _reconciled_notifications(
                    client,
                    selected_queue,
                    self.thread_id,
                    self.turn_id,
                    terminal_reader=self._terminal_reader,
                    terminal_hint=self._terminal_hint,
                )
            for notification in notifications:
                event = (
                    notification
                    if isinstance(notification, ProviderEvent)
                    else _event(notification, self.thread_id, self.turn_id)
                )
                if event is None:
                    continue
                if event.kind is ProviderEventKind.TURN_COMPLETED:
                    completed = True
                yield event
        except WorkerProviderError:
            raise
        except Exception as exc:
            raise _exception(exc) from None
        if not completed:
            raise _protocol_error()

    def interrupt(self) -> None:
        if self._interrupt_requested:
            return
        try:
            self._handle.interrupt()
        except Exception as exc:
            raise _exception(exc) from None
        self._interrupt_requested = True


def _sdk_turn_notification_queue(handle: object) -> tuple[object, Queue[object]] | None:
    """Use the pinned SDK queue so a missing terminal event can be reconciled."""
    client = getattr(handle, "_client", None)
    router = getattr(client, "_router", None)
    queues = getattr(router, "_turn_notifications", None)
    register = getattr(client, "register_turn_notifications", None)
    unregister = getattr(client, "unregister_turn_notifications", None)
    turn_id = getattr(handle, "id", None)
    if (
        not isinstance(queues, dict)
        or not callable(register)
        or not callable(unregister)
        or not isinstance(turn_id, str)
    ):
        return None
    register(turn_id)
    selected = queues.get(turn_id)
    if not isinstance(selected, Queue):
        unregister(turn_id)
        return None
    return client, selected


def _reconciled_notifications(
    client: object,
    notifications: Queue[object],
    thread_id: str,
    turn_id: str,
    *,
    terminal_reader: Callable[[str], object],
    terminal_hint: Callable[[str, str], bool],
) -> Iterator[object]:
    unregister = getattr(client, "unregister_turn_notifications")
    retrying = False
    try:
        while True:
            try:
                notification = notifications.get(timeout=_TURN_STATE_POLL_SECONDS)
            except Empty:
                if not terminal_hint(thread_id, turn_id):
                    continue
                terminal = _read_terminal_turn(
                    terminal_reader,
                    thread_id,
                    turn_id,
                )
                if terminal is not None:
                    # The exact rollout hint is written by the owner before the
                    # observer read. Drain owner notifications already routed
                    # so terminal usage and error events remain available.
                    while True:
                        try:
                            pending = notifications.get_nowait()
                        except Empty:
                            break
                        if isinstance(pending, BaseException):
                            raise pending
                        event = _event(pending, thread_id, turn_id)
                        if (
                            event is not None
                            and event.kind is ProviderEventKind.ERROR
                        ):
                            pending_payload = getattr(pending, "payload", None)
                            retrying = (
                                getattr(pending_payload, "will_retry", None) is True
                            )
                        if (
                            event is not None
                            and event.kind is ProviderEventKind.TURN_COMPLETED
                            and retrying
                            and event.completion is TurnCompletionStatus.FAILED
                        ):
                            continue
                        yield pending
                        if (
                            event is not None
                            and event.kind is ProviderEventKind.TURN_COMPLETED
                        ):
                            return
                    yield terminal
                    return
                continue
            if isinstance(notification, BaseException):
                raise notification
            event = _event(notification, thread_id, turn_id)
            if event is not None and event.kind is ProviderEventKind.ERROR:
                notification_payload = getattr(notification, "payload", None)
                retrying = getattr(notification_payload, "will_retry", None) is True
            if (
                event is not None
                and event.kind is ProviderEventKind.TURN_COMPLETED
                and retrying
                and event.completion is TurnCompletionStatus.FAILED
            ):
                continue
            yield notification
            if event is not None and event.kind is ProviderEventKind.TURN_COMPLETED:
                return
    finally:
        unregister(turn_id)


def _read_terminal_turn(
    reader: Callable[[str], object], thread_id: str, turn_id: str
) -> ProviderEvent | None:
    try:
        response = reader(thread_id)
    except Exception:
        return None
    thread = getattr(response, "thread", None)
    if getattr(thread, "id", None) != thread_id:
        raise _protocol_error()
    turns = getattr(thread, "turns", None)
    if not isinstance(turns, list):
        raise _protocol_error()
    selected = next(
        (turn for turn in turns if getattr(turn, "id", None) == turn_id),
        None,
    )
    if selected is None:
        return None
    status = _enum_value(getattr(selected, "status", None))
    if status in {"inProgress", "pending"}:
        return None
    completion = _completion(status)
    info = (
        _turn_error(getattr(selected, "error", None), retryable=False)
        if completion is TurnCompletionStatus.FAILED
        else None
    )
    return ProviderEvent(
        ProviderEventKind.TURN_COMPLETED,
        thread_id,
        turn_id,
        completion=completion,
        error=info,
    )


class _FreshTerminalReader:
    def __init__(
        self,
        config: CodexConfig,
        sdk_factory: Callable[[CodexConfig], Any],
    ) -> None:
        self._config = config
        self._sdk_factory = sdk_factory

    def __call__(self, thread_id: str) -> object:
        sdk = self._sdk_factory(self._config)
        try:
            thread = sdk.thread_resume(thread_id)
            return thread.read(include_turns=True)
        finally:
            try:
                sdk.close()
            except Exception:
                pass


def _rollout_terminal_hint(
    config: CodexProviderConfig,
) -> Callable[[str, str], bool]:
    raw_home = config.environment.get("CODEX_HOME")
    home = Path(raw_home) if isinstance(raw_home, str) and raw_home else None

    def has_terminal(thread_id: str, turn_id: str) -> bool:
        if home is None:
            return False
        sessions = home / "sessions"
        if not sessions.is_dir():
            return False
        pattern = f"rollout-*-{escape_glob(thread_id)}.jsonl"
        try:
            for path in sessions.rglob(pattern):
                if _rollout_has_completed_turn(path, turn_id):
                    return True
        except OSError:
            return False
        return False

    return has_terminal


def _rollout_has_completed_turn(path: Path, turn_id: str) -> bool:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - _ROLLOUT_TAIL_BYTES)
            stream.seek(start)
            if start:
                stream.readline()
            for raw_line in stream:
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "task_complete"
                    and payload.get("turn_id") == turn_id
                ):
                    return True
    except OSError:
        return False
    return False


def _sdk_config(config: CodexProviderConfig) -> CodexConfig:
    if not isinstance(config, CodexProviderConfig):
        raise _invalid("provider configuration is invalid")
    codex_bin: str | None = None
    if config.codex_bin is not None:
        if (
            not isinstance(config.codex_bin, Path)
            or not config.codex_bin.is_absolute()
            or not config.codex_bin.is_file()
        ):
            raise _invalid("codex_bin must be an existing absolute file")
        codex_bin = str(config.codex_bin)
    cwd: str | None = None
    if config.cwd is not None:
        cwd = str(_directory(config.cwd, "cwd"))
    environment: dict[str, str] = {}
    if not isinstance(config.environment, RuntimeMapping):
        raise _invalid("provider environment is invalid")
    for key, value in config.environment.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise _invalid("provider environment must contain string names and values")
        environment[key] = value
    return CodexConfig(
        codex_bin=codex_bin,
        cwd=cwd,
        env=environment or None,
        client_name="agentic_mesh_v5",
        client_title="Agentic Mesh V5",
        client_version="0.1.0",
        experimental_api=False,
    )


def _metadata(provider_id: str, metadata: object) -> EngineMetadata:
    server = getattr(metadata, "serverInfo", None)
    return EngineMetadata(
        provider_id=provider_id,
        runtime_name=_optional_text(getattr(server, "name", None)),
        runtime_version=_optional_text(getattr(server, "version", None)),
        platform_family=_optional_text(getattr(metadata, "platformFamily", None)),
        platform_os=_optional_text(getattr(metadata, "platformOs", None)),
    )


def _event(
    notification: object, thread_id: str, turn_id: str
) -> ProviderEvent | None:
    method = getattr(notification, "method", None)
    payload = getattr(notification, "payload", None)
    if method == "turn/started":
        _event_scope(payload, thread_id, turn_id, nested_turn=True)
        return ProviderEvent(ProviderEventKind.TURN_STARTED, thread_id, turn_id)
    if method == "item/agentMessage/delta":
        _event_scope(payload, thread_id, turn_id)
        text = getattr(payload, "delta", None)
        if not isinstance(text, str):
            raise _protocol_error()
        return ProviderEvent(
            ProviderEventKind.OUTPUT_DELTA,
            thread_id,
            turn_id,
            item_id=_optional_text(getattr(payload, "item_id", None)),
            text=text,
        )
    if method in {"item/started", "item/completed"}:
        _event_scope(payload, thread_id, turn_id)
        kind = (
            ProviderEventKind.ITEM_STARTED
            if method == "item/started"
            else ProviderEventKind.ITEM_COMPLETED
        )
        return ProviderEvent(
            kind,
            thread_id,
            turn_id,
            item_id=_item_id(getattr(payload, "item", None)),
        )
    if method == "turn/plan/updated":
        _event_scope(payload, thread_id, turn_id)
        raw_plan = getattr(payload, "plan", None)
        if not isinstance(raw_plan, list):
            raise _protocol_error()
        plan = tuple(
            ProviderPlanStep(
                step=_provider_text(getattr(item, "step", None)),
                status=_plan_status(getattr(item, "status", None)),
            )
            for item in raw_plan
        )
        return ProviderEvent(
            ProviderEventKind.PLAN_UPDATED,
            thread_id,
            turn_id,
            plan=plan,
        )
    if method == "thread/tokenUsage/updated":
        _event_scope(payload, thread_id, turn_id)
        usage = _usage(getattr(getattr(payload, "token_usage", None), "last", None))
        return ProviderEvent(
            ProviderEventKind.USAGE_UPDATED,
            thread_id,
            turn_id,
            usage=usage,
        )
    if method == "error":
        _event_scope(payload, thread_id, turn_id)
        will_retry = getattr(payload, "will_retry", None)
        if type(will_retry) is not bool:
            raise _protocol_error()
        info = _turn_error(
            getattr(payload, "error", None),
            retryable=will_retry,
        )
        return ProviderEvent(
            ProviderEventKind.ERROR,
            thread_id,
            turn_id,
            error=info,
        )
    if method == "turn/completed":
        _event_scope(payload, thread_id, turn_id, nested_turn=True)
        turn = getattr(payload, "turn", None)
        completion = _completion(getattr(turn, "status", None))
        info = (
            _turn_error(getattr(turn, "error", None), retryable=False)
            if completion is TurnCompletionStatus.FAILED
            else None
        )
        return ProviderEvent(
            ProviderEventKind.TURN_COMPLETED,
            thread_id,
            turn_id,
            completion=completion,
            error=info,
        )
    return None


def _event_scope(
    payload: object,
    thread_id: str,
    turn_id: str,
    *,
    nested_turn: bool = False,
) -> None:
    event_thread = getattr(payload, "thread_id", None)
    event_turn = (
        getattr(getattr(payload, "turn", None), "id", None)
        if nested_turn
        else getattr(payload, "turn_id", None)
    )
    if event_thread != thread_id or event_turn != turn_id:
        raise _protocol_error()


def _usage(value: object) -> ProviderUsage:
    names = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    counts = []
    for name in names:
        count = getattr(value, name, None)
        if type(count) is not int or count < 0:
            raise _protocol_error()
        counts.append(count)
    return ProviderUsage(*counts)


def _capacity(response: object) -> ProviderCapacity:
    rate_limits = getattr(response, "rate_limits", None)
    if rate_limits is None:
        raise _protocol_error()
    reset_summary = getattr(response, "rate_limit_reset_credits", None)
    reset_count = None
    earliest_expiry = None
    if reset_summary is not None:
        reset_count = _nonnegative_int(
            getattr(reset_summary, "available_count", None)
        )
        credits = getattr(reset_summary, "credits", None)
        if credits is not None:
            if not isinstance(credits, list):
                raise _protocol_error()
            expiries = [
                _timestamp(getattr(credit, "expires_at", None))
                for credit in credits
                if getattr(credit, "expires_at", None) is not None
            ]
            earliest_expiry = min(expiries, default=None)
    return ProviderCapacity(
        available=True,
        limit_id=_capacity_text(getattr(rate_limits, "limit_id", None)),
        limit_name=_capacity_text(getattr(rate_limits, "limit_name", None)),
        plan_type=_optional_enum_value(getattr(rate_limits, "plan_type", None)),
        primary=_capacity_window(getattr(rate_limits, "primary", None)),
        secondary=_capacity_window(getattr(rate_limits, "secondary", None)),
        credits=_credits(getattr(rate_limits, "credits", None)),
        individual_limit=_spend_control(
            getattr(rate_limits, "individual_limit", None)
        ),
        reset_credits_available=reset_count,
        reset_credits_earliest_expiry=earliest_expiry,
    )


def _capacity_window(value: object) -> ProviderRateLimitWindow | None:
    if value is None:
        return None
    used = _percent(getattr(value, "used_percent", None))
    resets = getattr(value, "resets_at", None)
    duration = getattr(value, "window_duration_mins", None)
    if duration is not None and (type(duration) is not int or duration <= 0):
        raise _protocol_error()
    return ProviderRateLimitWindow(
        used_percent=used,
        resets_at=None if resets is None else _timestamp(resets),
        window_minutes=duration,
    )


def _credits(value: object) -> ProviderCredits | None:
    if value is None:
        return None
    has_credits = getattr(value, "has_credits", None)
    unlimited = getattr(value, "unlimited", None)
    if type(has_credits) is not bool or type(unlimited) is not bool:
        raise _protocol_error()
    return ProviderCredits(
        balance=_capacity_text(getattr(value, "balance", None)),
        has_credits=has_credits,
        unlimited=unlimited,
    )


def _spend_control(value: object) -> ProviderSpendControl | None:
    if value is None:
        return None
    return ProviderSpendControl(
        limit=_provider_text(getattr(value, "limit", None)),
        used=_provider_text(getattr(value, "used", None)),
        remaining_percent=_percent(getattr(value, "remaining_percent", None)),
        resets_at=_timestamp(getattr(value, "resets_at", None)),
    )


def _capacity_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise _protocol_error()
    return value.strip()


def _percent(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 100:
        raise _protocol_error()
    return value


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _protocol_error()
    return value


def _timestamp(value: object) -> datetime:
    if type(value) is not int or value < 0:
        raise _protocol_error()
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise _protocol_error() from None


def _turn_error(error: object, *, retryable: bool) -> ProviderErrorInfo:
    root = getattr(getattr(error, "codex_error_info", None), "root", None)
    value = _optional_enum_value(root)
    class_name = type(root).__name__.lower()
    if value == "unauthorized":
        kind = ProviderErrorKind.AUTHENTICATION
    elif value in {"usageLimitExceeded", "sessionBudgetExceeded"}:
        kind = ProviderErrorKind.CAPACITY
    elif value == "serverOverloaded" or "toomanyfailedattempts" in class_name:
        kind = ProviderErrorKind.OVERLOADED
    elif (
        value == "internalServerError"
        or "connectionfailed" in class_name
        or "streamdisconnected" in class_name
    ):
        kind = ProviderErrorKind.TRANSPORT
    elif value in {"badRequest", "contextWindowExceeded", "cyberPolicy"}:
        kind = ProviderErrorKind.INVALID_REQUEST
    elif value in {"sandboxError", "threadRollbackFailed"}:
        kind = ProviderErrorKind.EXECUTION
    else:
        kind = ProviderErrorKind.INTERNAL
    return _error_info(
        kind,
        retryable=retryable
        or kind in {ProviderErrorKind.OVERLOADED, ProviderErrorKind.TRANSPORT},
    )


def _exception(exc: Exception) -> WorkerProviderError:
    if isinstance(exc, WorkerProviderError):
        return exc
    if isinstance(exc, (ServerBusyError, RetryLimitExceededError)):
        return WorkerProviderError(_error_info(ProviderErrorKind.OVERLOADED, True))
    if isinstance(exc, (TransportClosedError, TimeoutError, ConnectionError, OSError)):
        return WorkerProviderError(_error_info(ProviderErrorKind.TRANSPORT, True))
    if isinstance(exc, (InvalidParamsError, InvalidRequestError, MethodNotFoundError)):
        return WorkerProviderError(_error_info(ProviderErrorKind.INVALID_REQUEST, False))
    if isinstance(exc, ParseError):
        return _protocol_error()
    if isinstance(exc, InternalRpcError):
        return WorkerProviderError(_error_info(ProviderErrorKind.INTERNAL, True))
    if isinstance(exc, JsonRpcError):
        return WorkerProviderError(_error_info(ProviderErrorKind.INTERNAL, False))
    if isinstance(exc, (ValueError, TypeError)):
        return WorkerProviderError(_error_info(ProviderErrorKind.INVALID_REQUEST, False))
    if isinstance(exc, CodexError):
        return WorkerProviderError(_error_info(ProviderErrorKind.INTERNAL, False))
    return WorkerProviderError(_error_info(ProviderErrorKind.INTERNAL, False))


def _completion(value: object) -> TurnCompletionStatus:
    selected = _enum_value(value)
    try:
        return TurnCompletionStatus(selected)
    except ValueError:
        raise _protocol_error() from None


def _plan_status(value: object) -> PlanStepStatus:
    selected = _enum_value(value)
    mapping = {
        "pending": PlanStepStatus.PENDING,
        "inProgress": PlanStepStatus.IN_PROGRESS,
        "completed": PlanStepStatus.COMPLETED,
    }
    if selected not in mapping:
        raise _protocol_error()
    return mapping[selected]


def _item_id(item: object) -> str | None:
    root = getattr(item, "root", item)
    return _optional_text(getattr(root, "id", None))


def _enum_value(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, str):
        return value
    raise _protocol_error()


def _optional_enum_value(value: object) -> str | None:
    if isinstance(value, Enum):
        return str(value.value)
    return value if isinstance(value, str) else None


def _provider_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _protocol_error()
    return value.strip()


def _validate_thread_request(request: ThreadRequest) -> None:
    if not isinstance(request, ThreadRequest):
        raise _invalid("thread request is invalid")
    _directory(request.cwd, "thread cwd")
    if not isinstance(request.sandbox, SandboxPolicy):
        raise _invalid("thread sandbox is invalid")
    if not isinstance(request.approval, ApprovalPolicy):
        raise _invalid("thread approval is invalid")
    _optional_nonempty(request.model, "thread model")
    _optional_string(request.base_instructions, "base instructions")
    _optional_string(request.developer_instructions, "developer instructions")
    if type(request.ephemeral) is not bool:
        raise _invalid("thread ephemeral flag is invalid")


def _validate_turn_request(request: TurnRequest) -> None:
    if not isinstance(request, TurnRequest):
        raise _invalid("turn request is invalid")
    _required_text(request.prompt, "turn prompt")
    if request.cwd is not None:
        _directory(request.cwd, "turn cwd")
    _optional_nonempty(request.model, "turn model")
    if request.sandbox is not None and not isinstance(request.sandbox, SandboxPolicy):
        raise _invalid("turn sandbox is invalid")
    if request.approval is not None and not isinstance(request.approval, ApprovalPolicy):
        raise _invalid("turn approval is invalid")


def _directory(value: object, field_name: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or not value.is_dir():
        raise _invalid(f"{field_name} must be an existing absolute directory")
    return value.resolve()


def _optional_nonempty(value: object, field_name: str) -> None:
    if value is not None:
        _required_text(value, field_name)


def _optional_string(value: object, field_name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise _invalid(f"{field_name} must be text")


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field_name} is required")
    return value.strip()


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _error_info(kind: ProviderErrorKind, retryable: bool) -> ProviderErrorInfo:
    return ProviderErrorInfo(kind, retryable, _SAFE_MESSAGES[kind])


def _invalid(_detail: str) -> WorkerProviderError:
    return WorkerProviderError(_error_info(ProviderErrorKind.INVALID_REQUEST, False))


def _protocol_error() -> WorkerProviderError:
    return WorkerProviderError(_error_info(ProviderErrorKind.PROTOCOL, False))
