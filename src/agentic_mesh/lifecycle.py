from __future__ import annotations

import json
from pathlib import Path

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import MeshConfig, RoleInstanceConfig, utc_now_iso
from agentic_mesh.storage import FileMessageStore


class LifecycleStore:
    def __init__(self, state_root: Path, project_id: str, journal: EventJournal) -> None:
        self.root = state_root / "projects" / project_id / "lifecycle"
        self.project_id = project_id
        self.journal = journal
        self.root.mkdir(parents=True, exist_ok=True)

    def ensure_instances(self, mesh_config: MeshConfig) -> None:
        for instance in mesh_config.instances.values():
            if not self._path(instance.instance_id).exists():
                self.set_state(instance, "idle", reason="configured")

    def set_state(self, instance: RoleInstanceConfig, state: str, reason: str) -> None:
        attrs = telemetry.span_attributes(
            project_id=instance.project_id,
            role_id=instance.role_id,
            role_instance_id=instance.instance_id,
            lifecycle_state=state,
            reason=reason,
        )
        with telemetry.start_span("lifecycle.transition", attributes=attrs):
            data = {
                "project_id": instance.project_id,
                "role_id": instance.role_id,
                "role_instance_id": instance.instance_id,
                "state": state,
                "reason": reason,
                "updated_at": utc_now_iso(),
            }
            path = self._path(instance.instance_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")

    def get_state(self, instance_id: str) -> dict:
        path = self._path(instance_id)
        if not path.exists():
            return {"role_instance_id": instance_id, "state": "configured"}
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def control_plane_tick(
        self,
        mesh_config: MeshConfig,
        message_store: FileMessageStore,
        idle_grace_seconds: int,
    ) -> list[dict]:
        self.ensure_instances(mesh_config)
        transitions: list[dict] = []
        for instance in mesh_config.instances.values():
            current = self.get_state(instance.instance_id)
            state = current.get("state")
            pending = message_store.pending_count(instance.role_id)
            telemetry.set_gauge(
                "agentic_mesh.role_queue.pending",
                pending,
                {
                    "project_id": instance.project_id,
                    "role_id": instance.role_id,
                    "role_instance_id": instance.instance_id,
                },
            )

            if state == "hibernated" and pending > 0:
                self.set_state(instance, "idle", reason="wake_on_inbox")
                event = self.journal.append(
                    "agent_woke",
                    project_id=instance.project_id,
                    role_id=instance.role_id,
                    role_instance_id=instance.instance_id,
                    reason="wake_on_inbox",
                )
                transitions.append(event)
                continue

            if state == "idle" and pending == 0 and idle_grace_seconds <= 0:
                self.set_state(instance, "hibernated", reason="idle_grace_elapsed")
                event = self.journal.append(
                    "agent_hibernated",
                    project_id=instance.project_id,
                    role_id=instance.role_id,
                    role_instance_id=instance.instance_id,
                    reason="idle_grace_elapsed",
                )
                transitions.append(event)

        return transitions

    def _path(self, instance_id: str) -> Path:
        return self.root / f"{instance_id}.json"
