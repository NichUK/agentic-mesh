from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

from agentic_mesh_v2.db import V2Database


HIBERNATED_STATUSES = {"hibernating", "hibernated"}


@dataclass(frozen=True)
class HibernationPolicy:
    enabled: bool = True
    idle_after_seconds: int = 900
    min_warm_instances: int = 0

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "HibernationPolicy":
        enabled = mapping.get("enabled", True)
        idle_after_seconds = mapping.get("idle_after_seconds", 900)
        min_warm_instances = mapping.get("min_warm_instances", 0)
        if not isinstance(enabled, bool):
            raise ValueError("hibernation enabled must be a boolean")
        if isinstance(idle_after_seconds, bool) or not isinstance(idle_after_seconds, int) or idle_after_seconds < 1:
            raise ValueError("hibernation idle_after_seconds must be a positive integer")
        if isinstance(min_warm_instances, bool) or not isinstance(min_warm_instances, int) or min_warm_instances < 0:
            raise ValueError("hibernation min_warm_instances must be zero or greater")
        return cls(
            enabled=enabled,
            idle_after_seconds=idle_after_seconds,
            min_warm_instances=min_warm_instances,
        )


@dataclass(frozen=True)
class HibernationDecision:
    role_id: str
    role_instance_id: str
    can_hibernate: bool
    reason: str
    idle_seconds: int | None = None


@dataclass(frozen=True)
class HydrationDecision:
    role_id: str
    role_instance_id: str
    hydrated: bool
    reason: str


class HibernationService:
    def __init__(self, db: V2Database, policy: HibernationPolicy | None = None) -> None:
        self.db = db
        self.policy = policy or HibernationPolicy()

    def evaluate(self, *, role_id: str, role_instance_id: str) -> HibernationDecision:
        if not self.policy.enabled:
            return HibernationDecision(role_id, role_instance_id, False, "Hibernation policy is disabled.")
        instance = self._instance(role_instance_id)
        if instance is None:
            return HibernationDecision(role_id, role_instance_id, False, "Role instance has no durable status.")
        if str(instance["role_id"]) != role_id:
            return HibernationDecision(role_id, role_instance_id, False, "Role instance belongs to another role.")
        status = str(instance["status"])
        if status in HIBERNATED_STATUSES:
            return HibernationDecision(role_id, role_instance_id, False, "Role instance is already hibernating or hibernated.")
        if status != "idle":
            return HibernationDecision(role_id, role_instance_id, False, f"Role instance status is {status}, not idle.")
        if instance.get("current_assignment_id"):
            return HibernationDecision(role_id, role_instance_id, False, "Role instance has a current assignment.")
        pending_reason = self._pending_work_reason(role_id=role_id, role_instance_id=role_instance_id)
        if pending_reason is not None:
            return HibernationDecision(role_id, role_instance_id, False, pending_reason)
        warm_instances = self._warm_instance_count(role_id)
        if warm_instances <= self.policy.min_warm_instances:
            return HibernationDecision(
                role_id,
                role_instance_id,
                False,
                "Hibernation would violate the configured warm-instance floor.",
            )
        idle_seconds = self._idle_seconds(instance)
        if idle_seconds is None:
            return HibernationDecision(role_id, role_instance_id, False, "Role instance heartbeat is missing or unreadable.")
        if idle_seconds < self.policy.idle_after_seconds:
            return HibernationDecision(
                role_id,
                role_instance_id,
                False,
                "Role instance has not been idle for the configured grace period.",
                idle_seconds=idle_seconds,
            )
        return HibernationDecision(
            role_id,
            role_instance_id,
            True,
            "Role instance is idle, safe, and past the hibernation grace period.",
            idle_seconds=idle_seconds,
        )

    def mark_hibernating(self, *, role_id: str, role_instance_id: str, reason: str) -> HibernationDecision:
        decision = self.evaluate(role_id=role_id, role_instance_id=role_instance_id)
        if not decision.can_hibernate:
            return decision
        self.db.update_role_instance_hibernation(
            role_id=role_id,
            role_instance_id=role_instance_id,
            status="hibernating",
            reason=reason,
        )
        return HibernationDecision(role_id, role_instance_id, True, reason, idle_seconds=decision.idle_seconds)

    def mark_hibernated(self, *, role_id: str, role_instance_id: str, reason: str) -> None:
        self.db.update_role_instance_hibernation(
            role_id=role_id,
            role_instance_id=role_instance_id,
            status="hibernated",
            reason=reason,
        )

    def mark_hydrating(self, *, role_id: str, role_instance_id: str, reason: str) -> None:
        self.db.update_role_instance_hibernation(
            role_id=role_id,
            role_instance_id=role_instance_id,
            status="hydrating",
            reason=reason,
        )

    def hydrate_for_pending_work(self, *, role_id: str, reason: str) -> list[HydrationDecision]:
        if not reason.strip():
            raise ValueError("hydration reason is required")
        if not self._role_has_queued_work(role_id):
            return []
        hydrated: list[HydrationDecision] = []
        for instance in self.db.list_role_instance_statuses():
            if instance["role_id"] != role_id or instance["status"] not in HIBERNATED_STATUSES:
                continue
            role_instance_id = str(instance["role_instance_id"])
            self.mark_hydrating(role_id=role_id, role_instance_id=role_instance_id, reason=reason)
            hydrated.append(
                HydrationDecision(
                    role_id=role_id,
                    role_instance_id=role_instance_id,
                    hydrated=True,
                    reason=reason,
                )
            )
            break
        return hydrated

    def _instance(self, role_instance_id: str) -> dict[str, Any] | None:
        for item in self.db.list_role_instance_statuses():
            if item["role_instance_id"] == role_instance_id:
                return item
        return None

    def _pending_work_reason(self, *, role_id: str, role_instance_id: str) -> str | None:
        for assignment in self.db.list_role_assignments():
            status = str(assignment["status"])
            if assignment["role_id"] == role_id and status == "queued":
                return "Role has queued work waiting."
            if assignment.get("role_instance_id") == role_instance_id and status == "claimed":
                return "Role instance has claimed work."
        return None

    def _role_has_queued_work(self, role_id: str) -> bool:
        return any(
            assignment["role_id"] == role_id and assignment["status"] == "queued"
            for assignment in self.db.list_role_assignments()
        )

    def _warm_instance_count(self, role_id: str) -> int:
        return sum(
            1
            for item in self.db.list_role_instance_statuses()
            if item["role_id"] == role_id and item["status"] not in HIBERNATED_STATUSES
        )

    def _idle_seconds(self, instance: dict[str, Any]) -> int | None:
        heartbeat = instance.get("heartbeat_at")
        if not isinstance(heartbeat, str) or not heartbeat.strip():
            return None
        try:
            heartbeat_at = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
        except ValueError:
            return None
        if heartbeat_at.tzinfo is None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            now = datetime.now(timezone.utc)
        return max(0, int((now - heartbeat_at).total_seconds()))
