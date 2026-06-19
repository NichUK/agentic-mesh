from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
import subprocess
import time
from typing import Iterable
from typing import Protocol

from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.reporting import AgentStatus


_TRANSIENT_DOCKER_LIFECYCLE_ERRORS = (
    "container is marked for removal",
    "container name",
    "already in use",
)


@dataclass(frozen=True)
class RoleContainerSpec:
    role_instance_id: str
    image: str
    source_repo: Path
    organisation_config_repo: Path
    project_config_repo: Path
    agent_config_dir: Path
    runtime_state_dir: Path
    document_library_root: Path
    environment: dict[str, str]
    target_repositories: dict[str, Path] = field(default_factory=dict)

    def volume_mounts(self) -> dict[str, str]:
        mounts = {
            str(self.source_repo): "/mesh/source",
            str(self.organisation_config_repo): "/mesh/org",
            str(self.project_config_repo): "/mesh/project",
            str(self.agent_config_dir): "/mesh/agent",
            str(self.runtime_state_dir): "/mesh/state",
            str(self.document_library_root): "/documents",
        }
        for repository_id, path in sorted(self.target_repositories.items()):
            mounts[str(path)] = f"/mesh/workspaces/{_safe_mount_name(repository_id)}"
        return mounts

    def service_command(
        self,
        *,
        db_path: str = "/mesh/state/agentic-mesh-v3.sqlite3",
        project_config_path: str = "/mesh/project/agentic-mesh/project.yaml",
        poll_interval_seconds: float = 5.0,
        idle_exit_seconds: int = 1800,
    ) -> list[str]:
        """Return the command a role container should run for its service loop."""

        project_id, role_id, instance_id = _project_role_and_instance(self.role_instance_id)
        return [
            "agentic-mesh-v3",
            "--db",
            db_path,
            "--project-id",
            self.environment.get("PROJECT_ID", project_id),
            "--project-config",
            project_config_path,
            "run-agent-service",
            "--role-id",
            role_id,
            "--instance-id",
            instance_id,
            "--agent-config-dir",
            "/mesh/agent",
            "--runtime-state-dir",
            "/mesh/state",
            "--poll-interval-seconds",
            _format_seconds(poll_interval_seconds),
            "--idle-exit-seconds",
            str(idle_exit_seconds),
        ]


@dataclass(frozen=True)
class HibernationPolicy:
    idle_after_seconds: int = 1800
    min_warm_instances_per_role: int = 1
    pending_inbox_stale_after_seconds: int = 120
    active_work_stale_after_seconds: int = 14400


@dataclass(frozen=True)
class LifecycleDecision:
    action: str
    role_instance_id: str
    reason: str


@dataclass(frozen=True)
class ComposeLifecycleConfig:
    compose_files: tuple[Path, ...]
    working_directory: Path | None = None
    timeout_seconds: int = 300
    project_name: str | None = None
    profiles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.compose_files:
            raise ValueError("at least one compose file is required")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if any(not profile.strip() for profile in self.profiles):
            raise ValueError("compose profiles must be non-empty")


@dataclass(frozen=True)
class LifecycleCommand:
    decision: LifecycleDecision
    service_name: str
    command: tuple[str, ...]
    working_directory: Path | None


@dataclass(frozen=True)
class LifecycleCommandResult:
    decision: LifecycleDecision
    service_name: str
    command: tuple[str, ...]
    working_directory: Path | None
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    executed: bool = False


@dataclass(frozen=True)
class CommandExecutionResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class LifecycleCommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path | None,
        timeout_seconds: int,
    ) -> CommandExecutionResult:
        ...


def compose_lifecycle_command(
    decision: LifecycleDecision,
    *,
    config: ComposeLifecycleConfig,
) -> LifecycleCommand | None:
    if decision.action == "none":
        return None
    service_name = service_name_for_role(decision.role_instance_id)
    prefix = [
        "docker",
        "compose",
        *(() if not config.project_name else ("--project-name", config.project_name)),
        *[part for profile in config.profiles for part in ("--profile", profile)],
        *[
            part
            for compose_file in config.compose_files
            for part in ("-f", str(compose_file))
        ],
    ]
    if decision.action in {"start", "wake"}:
        command = (*prefix, "up", "-d", "--no-deps", "--no-recreate", service_name)
    elif decision.action == "hibernate":
        command = (*prefix, "stop", service_name)
    else:
        raise ValueError(f"unsupported lifecycle action: {decision.action}")
    return LifecycleCommand(
        decision=decision,
        service_name=service_name,
        command=tuple(command),
        working_directory=config.working_directory,
    )


def _validate_role_scoped_lifecycle_command(command: LifecycleCommand) -> None:
    """Guard role lifecycle execution from becoming a broad Compose deploy.

    A wake/start may only start the exact role service and must never recreate
    dependencies such as the broker or runtime. A hibernate may only stop the
    exact role service.
    """

    parts = command.command
    action = command.decision.action
    if action in {"start", "wake"}:
        if parts[-1:] != (command.service_name,):
            raise ValueError("role lifecycle wake/start command must target exactly one role service")
        if "--no-deps" not in parts or "--no-recreate" not in parts:
            raise ValueError("role lifecycle wake/start command must use --no-deps and --no-recreate")
        if "up" not in parts:
            raise ValueError("role lifecycle wake/start command must use docker compose up")
        return
    if action == "hibernate":
        if parts[-1:] != (command.service_name,):
            raise ValueError("role lifecycle hibernate command must target exactly one role service")
        if "stop" not in parts:
            raise ValueError("role lifecycle hibernate command must use docker compose stop")


class ComposeLifecycleExecutor:
    def __init__(
        self,
        config: ComposeLifecycleConfig,
        *,
        runner: LifecycleCommandRunner | None = None,
        transient_retries: int = 2,
        transient_retry_delay_seconds: float = 1.0,
    ) -> None:
        self.config = config
        self.runner = runner or _run_lifecycle_command
        self.transient_retries = transient_retries
        self.transient_retry_delay_seconds = transient_retry_delay_seconds

    def apply(
        self,
        decisions: Iterable[LifecycleDecision],
        *,
        execute: bool = False,
    ) -> tuple[LifecycleCommandResult, ...]:
        results: list[LifecycleCommandResult] = []
        for decision in decisions:
            command = compose_lifecycle_command(decision, config=self.config)
            if command is None:
                continue
            _validate_role_scoped_lifecycle_command(command)
            if execute:
                result = self._run_with_transient_retries(command)
                results.append(
                    LifecycleCommandResult(
                        decision=decision,
                        service_name=command.service_name,
                        command=command.command,
                        working_directory=command.working_directory,
                        exit_code=result.exit_code,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        executed=True,
                    )
                )
            else:
                results.append(
                    LifecycleCommandResult(
                        decision=decision,
                        service_name=command.service_name,
                        command=command.command,
                        working_directory=command.working_directory,
                        exit_code=None,
                        executed=False,
                    )
                )
        return tuple(results)

    def _run_with_transient_retries(self, command: LifecycleCommand) -> CommandExecutionResult:
        result = self.runner(
            list(command.command),
            cwd=command.working_directory,
            timeout_seconds=self.config.timeout_seconds,
        )
        attempts = 0
        while attempts < self.transient_retries and _is_transient_docker_lifecycle_error(result):
            attempts += 1
            if self.transient_retry_delay_seconds > 0:
                time.sleep(self.transient_retry_delay_seconds)
            retry_result = self.runner(
                list(command.command),
                cwd=command.working_directory,
                timeout_seconds=self.config.timeout_seconds,
            )
            result = _merge_retry_result(result, retry_result, attempt=attempts)
        return result


def running_compose_services(
    config: ComposeLifecycleConfig,
    *,
    runner: LifecycleCommandRunner | None = None,
) -> frozenset[str]:
    """Return running Compose services for the configured project.

    The supervisor uses this as a guard against stale DB projections. If a
    short-lived role container exits after processing a message, its last DB
    heartbeat can still say `running`; Compose is the runtime source for
    whether the service is actually alive.
    """

    command = [
        "docker",
        "compose",
        *(() if not config.project_name else ("--project-name", config.project_name)),
        *[part for profile in config.profiles for part in ("--profile", profile)],
        *[
            part
            for compose_file in config.compose_files
            for part in ("-f", str(compose_file))
        ],
        "ps",
        "--services",
        "--filter",
        "status=running",
    ]
    result = (runner or _run_lifecycle_command)(
        command,
        cwd=config.working_directory,
        timeout_seconds=config.timeout_seconds,
    )
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "docker compose ps failed")
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


def reconcile_agent_statuses_with_compose(
    statuses: Iterable[AgentStatus],
    *,
    running_services: Iterable[str],
) -> tuple[AgentStatus, ...]:
    """Reconcile role projections with the actual Compose process state.

    `container_state=running` should mean there is a running role service. When
    Compose says the service is absent/stopped, keep the inbox/dead-letter
    depths but clear active work so lifecycle planning can wake the role again
    for pending messages.

    Likewise, a previous lifecycle failure should not stay current forever once
    Compose has recovered or there is no longer pending work to start. Those
    stale failures are operational history, not the current desired state.
    """

    running_service_names = set(running_services)
    reconciled: list[AgentStatus] = []
    for status in statuses:
        service_name = service_name_for_role(status.role_instance_id)
        if service_name in running_service_names and status.container_state != "running":
            reconciled.append(_with_reconciled_lifecycle(status, container_state="running"))
            continue
        if status.container_state == "running" and service_name not in running_service_names:
            reconciled.append(
                _with_reconciled_lifecycle(
                    status,
                    container_state="hibernated",
                    current_work=None,
                )
            )
            continue
        if (
            status.container_state == "lifecycle_failed"
            and service_name not in running_service_names
            and not status.current_work
            and status.inbox_depth == 0
            and status.dead_letter_depth == 0
        ):
            reconciled.append(_with_reconciled_lifecycle(status, container_state="hibernated"))
            continue
        if (
            service_name not in running_service_names
            and status.last_lifecycle_exit_code is not None
            and status.last_lifecycle_exit_code != 0
            and not status.current_work
            and status.inbox_depth == 0
            and status.dead_letter_depth == 0
        ):
            reconciled.append(_with_reconciled_lifecycle(status))
            continue
        reconciled.append(status)
    return tuple(reconciled)


def _with_reconciled_lifecycle(status: AgentStatus, **changes: object) -> AgentStatus:
    """Return status after Compose has proven a lifecycle failure is stale.

    Historical lifecycle failures remain in the event log. The read-model row
    should describe the current state, so once Compose reconciliation proves the
    service recovered or no longer needs action, the active failure fields must
    not keep rendering as fresh dashboard alerts.
    """

    lifecycle_cleanup: dict[str, object] = {}
    if status.container_state == "lifecycle_failed" or (
        status.last_lifecycle_exit_code is not None and status.last_lifecycle_exit_code != 0
    ):
        lifecycle_cleanup = {
            "last_lifecycle_action": None,
            "last_lifecycle_reason": None,
            "last_lifecycle_service": None,
            "last_lifecycle_exit_code": None,
            "last_lifecycle_executed": None,
            "last_lifecycle_error": None,
        }
    return replace(status, **lifecycle_cleanup, **changes)


def plan_lifecycle_action(
    *,
    status: AgentStatus,
    policy: HibernationPolicy,
    now: datetime | None = None,
    warm_instances_for_role: int = 1,
) -> LifecycleDecision:
    current_time = now or datetime.now(timezone.utc)
    if status.container_state in {"stopped", "hibernated"} and status.inbox_depth > 0:
        return LifecycleDecision("wake", status.role_instance_id, "pending inbox messages")
    if (
        status.container_state in {"stopped", "hibernated"}
        and warm_instances_for_role < policy.min_warm_instances_per_role
    ):
        return LifecycleDecision("wake", status.role_instance_id, "minimum warm pool requires a running instance")
    if status.container_state == "missing":
        if status.inbox_depth > 0:
            return LifecycleDecision("start", status.role_instance_id, "pending inbox messages")
        if warm_instances_for_role < policy.min_warm_instances_per_role:
            return LifecycleDecision("start", status.role_instance_id, "minimum warm pool requires a running instance")
        return LifecycleDecision("none", status.role_instance_id, "role instance is configured but asleep")
    if status.container_state == "lifecycle_failed" and status.inbox_depth > 0:
        return LifecycleDecision("wake", status.role_instance_id, "retry failed lifecycle action for pending inbox messages")
    if status.container_state != "running":
        return LifecycleDecision("none", status.role_instance_id, f"state {status.container_state} does not require action")
    if status.current_work:
        heartbeat = _parse_datetime(status.heartbeat_at)
        if heartbeat is None:
            return LifecycleDecision("none", status.role_instance_id, "agent has active work")
        stale_for = current_time - heartbeat
        if stale_for >= timedelta(seconds=policy.active_work_stale_after_seconds):
            return LifecycleDecision(
                "wake",
                status.role_instance_id,
                f"active work and stale heartbeat for {int(stale_for.total_seconds())} seconds",
            )
        return LifecycleDecision("none", status.role_instance_id, "agent has active work")
    if status.inbox_depth > 0:
        heartbeat = _parse_datetime(status.heartbeat_at)
        if heartbeat is None:
            return LifecycleDecision("wake", status.role_instance_id, "pending inbox messages and missing heartbeat")
        stale_for = current_time - heartbeat
        if stale_for >= timedelta(seconds=policy.pending_inbox_stale_after_seconds):
            return LifecycleDecision(
                "wake",
                status.role_instance_id,
                f"pending inbox messages and stale heartbeat for {int(stale_for.total_seconds())} seconds",
            )
        return LifecycleDecision("none", status.role_instance_id, "agent has pending inbox messages")
    if warm_instances_for_role <= policy.min_warm_instances_per_role:
        return LifecycleDecision("none", status.role_instance_id, "minimum warm pool would be violated")
    heartbeat = _parse_datetime(status.heartbeat_at)
    if heartbeat is None:
        return LifecycleDecision("none", status.role_instance_id, "heartbeat is unknown")
    idle_for = current_time - heartbeat
    if idle_for >= timedelta(seconds=policy.idle_after_seconds):
        return LifecycleDecision("hibernate", status.role_instance_id, f"idle for {int(idle_for.total_seconds())} seconds")
    return LifecycleDecision("none", status.role_instance_id, "idle threshold not reached")


def plan_lifecycle_actions(
    statuses: Iterable[AgentStatus],
    *,
    policy: HibernationPolicy,
    now: datetime | None = None,
) -> tuple[LifecycleDecision, ...]:
    """Plan lifecycle actions across role groups while preserving warm pools."""

    grouped: dict[str, list[AgentStatus]] = {}
    for status in statuses:
        role_id, _ = _role_and_instance(status.role_instance_id)
        grouped.setdefault(role_id, []).append(status)

    decisions: list[LifecycleDecision] = []
    for role_id in sorted(grouped):
        role_statuses = sorted(grouped[role_id], key=lambda status: status.role_instance_id)
        warm_instances = sum(1 for status in role_statuses if status.container_state == "running")
        non_running = [status for status in role_statuses if status.container_state != "running"]
        running = [status for status in role_statuses if status.container_state == "running"]

        for status in non_running:
            decision = plan_lifecycle_action(
                status=status,
                policy=policy,
                now=now,
                warm_instances_for_role=warm_instances,
            )
            decisions.append(decision)
            if decision.action in {"start", "wake"}:
                warm_instances += 1

        for status in running:
            decision = plan_lifecycle_action(
                status=status,
                policy=policy,
                now=now,
                warm_instances_for_role=warm_instances,
            )
            decisions.append(decision)
            if decision.action == "hibernate":
                warm_instances -= 1

    return tuple(decisions)


def refresh_agent_statuses_from_broker(
    statuses: Iterable[AgentStatus],
    *,
    role_instance_ids: Iterable[str],
    broker: BrokerAdapter,
    stream: str,
    pending_limit: int = 10_000,
) -> tuple[AgentStatus, ...]:
    """Refresh inbox/dead-letter depths from the broker before lifecycle planning.

    A hibernated or stopped role cannot update its own `inbox_depth`, so the
    runtime supervisor must inspect the broker directly before deciding whether
    to wake it.
    """

    if pending_limit < 1:
        raise ValueError("pending_limit must be positive")
    by_instance = {status.role_instance_id: status for status in statuses}
    for role_instance_id in role_instance_ids:
        by_instance.setdefault(
            role_instance_id,
            AgentStatus(
                role_instance_id=role_instance_id,
                container_state="missing",
                heartbeat_at=None,
            ),
        )

    refreshed: list[AgentStatus] = []
    for role_instance_id in sorted(by_instance):
        status = by_instance[role_instance_id]
        role_id, instance_id = _role_and_instance(role_instance_id)
        inbox_depth = 0
        for consumer, subject in (
            (f"{_inbox_consumer_name(role_id, instance_id)}.priority", f"agent.{role_id}.priority"),
            (_inbox_consumer_name(role_id, instance_id), f"agent.{role_id}"),
            (f"{_inbox_consumer_name(role_id, instance_id)}.relevance", f"agent.{role_id}.relevance"),
        ):
            broker.ensure_consumer(stream, consumer, filter_subject=subject)
            inbox_depth += len(broker.pending(stream, consumer, limit=pending_limit))
        refreshed.append(
            replace(
                status,
                inbox_depth=inbox_depth,
                dead_letter_depth=len(broker.dead_letters(stream, limit=pending_limit)),
            )
        )
    return tuple(refreshed)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_transient_docker_lifecycle_error(result: CommandExecutionResult) -> bool:
    if result.exit_code == 0:
        return False
    text = f"{result.stderr}\n{result.stdout}".lower()
    return any(fragment in text for fragment in _TRANSIENT_DOCKER_LIFECYCLE_ERRORS)


def _merge_retry_result(
    previous: CommandExecutionResult,
    current: CommandExecutionResult,
    *,
    attempt: int,
) -> CommandExecutionResult:
    retry_note = f"[agentic-mesh] transient Docker lifecycle retry {attempt}"
    stdout_parts = [part for part in (previous.stdout, retry_note, current.stdout) if part]
    stderr_parts = [part for part in (previous.stderr, retry_note, current.stderr) if part]
    return CommandExecutionResult(
        exit_code=current.exit_code,
        stdout="\n".join(stdout_parts),
        stderr="\n".join(stderr_parts),
    )


def _role_and_instance(role_instance_id: str) -> tuple[str, str]:
    parts = role_instance_id.split(".")
    if len(parts) < 3 or not parts[-2] or not parts[-1]:
        raise ValueError("role_instance_id must use {project_id}.{role_id}.{instance_id}")
    return parts[-2], parts[-1]


def _project_role_and_instance(role_instance_id: str) -> tuple[str, str, str]:
    role_id, instance_id = _role_and_instance(role_instance_id)
    project_id = ".".join(role_instance_id.split(".")[:-2])
    return project_id, role_id, instance_id


def _inbox_consumer_name(role_id: str, instance_id: str) -> str:
    return f"{role_id}.{instance_id}"


def _format_seconds(value: float) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def service_name_for_role(role_instance_id: str) -> str:
    if not role_instance_id.strip():
        raise ValueError("role_instance_id is required")
    return role_instance_id.replace(".", "-")


def _safe_mount_name(value: str) -> str:
    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError(f"invalid target repository id for mount: {value}")
    return value


def _run_lifecycle_command(
    command: list[str],
    *,
    cwd: Path | None,
    timeout_seconds: int,
) -> CommandExecutionResult:
    completed = subprocess.run(
        command,
        cwd=cwd,
        timeout=timeout_seconds,
        text=True,
        capture_output=True,
        check=False,
    )
    return CommandExecutionResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
