from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
import subprocess
from typing import Iterable
from typing import Protocol

from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.reporting import AgentStatus


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

        role_id, instance_id = _role_and_instance(self.role_instance_id)
        return [
            "agentic-mesh-v3",
            "--db",
            db_path,
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

    def __post_init__(self) -> None:
        if not self.compose_files:
            raise ValueError("at least one compose file is required")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")


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
        *[
            part
            for compose_file in config.compose_files
            for part in ("-f", str(compose_file))
        ],
    ]
    if decision.action in {"start", "wake"}:
        command = (*prefix, "up", "-d", service_name)
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


class ComposeLifecycleExecutor:
    def __init__(
        self,
        config: ComposeLifecycleConfig,
        *,
        runner: LifecycleCommandRunner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or _run_lifecycle_command

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
            if execute:
                result = self.runner(
                    list(command.command),
                    cwd=command.working_directory,
                    timeout_seconds=self.config.timeout_seconds,
                )
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
    if status.container_state == "missing":
        return LifecycleDecision("start", status.role_instance_id, "role instance is missing")
    if status.container_state != "running":
        return LifecycleDecision("none", status.role_instance_id, f"state {status.container_state} does not require action")
    if status.current_work:
        return LifecycleDecision("none", status.role_instance_id, "agent has active work")
    if status.inbox_depth > 0:
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


def _role_and_instance(role_instance_id: str) -> tuple[str, str]:
    parts = role_instance_id.split(".")
    if len(parts) < 3 or not parts[-2] or not parts[-1]:
        raise ValueError("role_instance_id must use {project_id}.{role_id}.{instance_id}")
    return parts[-2], parts[-1]


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
