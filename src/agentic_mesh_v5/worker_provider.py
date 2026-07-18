from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable


class SandboxPolicy(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


class ApprovalPolicy(str, Enum):
    DENY_ALL = "deny_all"
    AUTO_REVIEW = "auto_review"


class ProviderEventKind(str, Enum):
    TURN_STARTED = "turn_started"
    OUTPUT_DELTA = "output_delta"
    ITEM_STARTED = "item_started"
    ITEM_COMPLETED = "item_completed"
    PLAN_UPDATED = "plan_updated"
    USAGE_UPDATED = "usage_updated"
    ERROR = "error"
    TURN_COMPLETED = "turn_completed"


class TurnCompletionStatus(str, Enum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ProviderErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    CAPACITY = "capacity"
    OVERLOADED = "overloaded"
    TRANSPORT = "transport"
    INVALID_REQUEST = "invalid_request"
    EXECUTION = "execution"
    PROTOCOL = "protocol"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ProviderErrorInfo:
    kind: ProviderErrorKind
    retryable: bool
    safe_message: str


class WorkerProviderError(RuntimeError):
    def __init__(self, info: ProviderErrorInfo) -> None:
        super().__init__(info.safe_message)
        self.info = info


@dataclass(frozen=True, slots=True)
class ThreadRequest:
    cwd: Path
    model: str | None = None
    base_instructions: str | None = field(default=None, repr=False)
    developer_instructions: str | None = field(default=None, repr=False)
    sandbox: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE
    approval: ApprovalPolicy = ApprovalPolicy.DENY_ALL
    ephemeral: bool = False


@dataclass(frozen=True, slots=True)
class TurnRequest:
    prompt: str = field(repr=False)
    cwd: Path | None = None
    model: str | None = None
    sandbox: SandboxPolicy | None = None
    approval: ApprovalPolicy | None = None


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderRateLimitWindow:
    used_percent: int
    resets_at: datetime | None
    window_minutes: int | None


@dataclass(frozen=True, slots=True)
class ProviderCredits:
    balance: str | None
    has_credits: bool
    unlimited: bool


@dataclass(frozen=True, slots=True)
class ProviderSpendControl:
    limit: str
    used: str
    remaining_percent: int
    resets_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderCapacity:
    available: bool
    limit_id: str | None = None
    limit_name: str | None = None
    plan_type: str | None = None
    primary: ProviderRateLimitWindow | None = None
    secondary: ProviderRateLimitWindow | None = None
    credits: ProviderCredits | None = None
    individual_limit: ProviderSpendControl | None = None
    reset_credits_available: int | None = None
    reset_credits_earliest_expiry: datetime | None = None

    @classmethod
    def unknown(cls) -> ProviderCapacity:
        return cls(available=False)


@dataclass(frozen=True, slots=True)
class ProviderPlanStep:
    step: str
    status: PlanStepStatus


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    kind: ProviderEventKind
    thread_id: str
    turn_id: str
    item_id: str | None = None
    text: str | None = None
    completion: TurnCompletionStatus | None = None
    usage: ProviderUsage | None = None
    plan: tuple[ProviderPlanStep, ...] = ()
    error: ProviderErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class EngineMetadata:
    provider_id: str
    runtime_name: str | None
    runtime_version: str | None
    platform_family: str | None
    platform_os: str | None


@runtime_checkable
class WorkerTurn(Protocol):
    thread_id: str
    turn_id: str

    def events(self) -> Iterator[ProviderEvent]: ...

    def interrupt(self) -> None: ...


@runtime_checkable
class WorkerThread(Protocol):
    thread_id: str

    def start_turn(self, request: TurnRequest) -> WorkerTurn: ...


@runtime_checkable
class WorkerEngine(Protocol):
    @property
    def metadata(self) -> EngineMetadata: ...

    def start_thread(self, request: ThreadRequest) -> WorkerThread: ...

    def resume_thread(self, thread_id: str, request: ThreadRequest) -> WorkerThread: ...

    def close(self) -> None: ...


@runtime_checkable
class CapacityAwareWorkerEngine(Protocol):
    def read_capacity(self) -> ProviderCapacity: ...


@runtime_checkable
class WorkerProvider(Protocol):
    provider_id: str

    def open(self) -> WorkerEngine: ...
