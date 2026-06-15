from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

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

    def volume_mounts(self) -> dict[str, str]:
        return {
            str(self.source_repo): "/mesh/source",
            str(self.organisation_config_repo): "/mesh/org",
            str(self.project_config_repo): "/mesh/project",
            str(self.agent_config_dir): "/mesh/agent",
            str(self.runtime_state_dir): "/mesh/state",
            str(self.document_library_root): "/documents",
        }


@dataclass(frozen=True)
class HibernationPolicy:
    idle_after_seconds: int = 1800
    min_warm_instances_per_role: int = 1


@dataclass(frozen=True)
class LifecycleDecision:
    action: str
    role_instance_id: str
    reason: str


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
