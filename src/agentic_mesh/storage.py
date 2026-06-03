from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentic_mesh import telemetry
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import Message


class FileMessageStore:
    def __init__(self, state_root: Path, project_id: str, journal: EventJournal) -> None:
        self.root = state_root / "projects" / project_id / "queues"
        self.project_id = project_id
        self.journal = journal
        self.root.mkdir(parents=True, exist_ok=True)

    def enqueue(self, message: Message) -> Message:
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            role_id=message.role_id,
            message_id=message.message_id,
            message_type=message.type,
            work_item_id=message.payload.get("work_item_id"),
            work_item_type=message.payload.get("work_item_type"),
            lifecycle_state=message.payload.get("lifecycle_state"),
            correlation_id=message.correlation_id,
        )
        with telemetry.start_span(
            "work.enqueue",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=attrs,
        ) as trace_context:
            message = replace(message, trace_context=trace_context)
            pending = self._pending_dir(message.role_id)
            pending.mkdir(parents=True, exist_ok=True)
            path = pending / f"{message.created_at.replace(':', '')}-{message.message_id}.json"
            self._write_message(path, message)
            self.journal.append(
                "message_accepted",
                project_id=self.project_id,
                role_id=message.role_id,
                message_id=message.message_id,
                message_type=message.type,
                work_item_id=message.payload.get("work_item_id"),
                work_item_type=message.payload.get("work_item_type"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                correlation_id=message.correlation_id,
                source=message.source,
            )
            return message

    def claim_next(self, role_id: str, instance_id: str) -> Message | None:
        pending = self._pending_dir(role_id)
        claimed = self._claimed_dir(role_id, instance_id)
        claimed.mkdir(parents=True, exist_ok=True)
        for path in sorted(pending.glob("*.json")):
            original = self._read_message(path)
            wait_seconds = telemetry.elapsed_seconds(original.created_at)
            wait_attrs = telemetry.span_attributes(
                project_id=self.project_id,
                role_id=role_id,
                role_instance_id=instance_id,
                message_id=original.message_id,
                message_type=original.type,
                work_item_id=original.payload.get("work_item_id"),
                work_item_type=original.payload.get("work_item_type"),
                lifecycle_state=original.payload.get("lifecycle_state"),
                correlation_id=original.correlation_id,
            )
            with telemetry.start_span(
                "queue.wait",
                correlation_id=original.correlation_id,
                trace_context=original.trace_context,
                attributes=wait_attrs,
            ):
                if wait_seconds is not None:
                    telemetry.record_duration(
                        "agentic_mesh.queue.wait.duration",
                        wait_seconds,
                        wait_attrs,
                    )
            message = original.claimed(instance_id)
            with telemetry.start_span(
                "work.claim",
                correlation_id=message.correlation_id,
                trace_context=message.trace_context,
                attributes=wait_attrs,
            ) as trace_context:
                message = replace(message, trace_context=trace_context)
            target = claimed / path.name
            try:
                path.replace(target)
            except FileNotFoundError:
                continue
            self._write_message(target, message)
            self.journal.append(
                "work_claimed",
                project_id=self.project_id,
                role_id=role_id,
                role_instance_id=instance_id,
                message_id=message.message_id,
                work_item_id=message.payload.get("work_item_id"),
                work_item_type=message.payload.get("work_item_type"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                correlation_id=message.correlation_id,
            )
            return message
        return None

    def complete(self, message: Message, status: str) -> None:
        if not message.claimed_by:
            raise ValueError("Cannot complete an unclaimed message")
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            role_id=message.role_id,
            role_instance_id=message.claimed_by,
            message_id=message.message_id,
            work_item_id=message.payload.get("work_item_id"),
            work_item_type=message.payload.get("work_item_type"),
            lifecycle_state=message.payload.get("lifecycle_state"),
            correlation_id=message.correlation_id,
            status=status,
        )
        with telemetry.start_span(
            "work.complete",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=attrs,
        ):
            claimed_path = self._find_claimed_path(message)
            completed = self._completed_dir(message.role_id)
            completed.mkdir(parents=True, exist_ok=True)
            if claimed_path:
                claimed_path.replace(completed / claimed_path.name)
            self.journal.append(
                "work_completed",
                project_id=self.project_id,
                role_id=message.role_id,
                role_instance_id=message.claimed_by,
                message_id=message.message_id,
                work_item_id=message.payload.get("work_item_id"),
                work_item_type=message.payload.get("work_item_type"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                correlation_id=message.correlation_id,
                status=status,
            )

    def pending_count(self, role_id: str) -> int:
        return len(list(self._pending_dir(role_id).glob("*.json")))

    def work_item_summary(
        self,
        work_item_id: str,
        roles: list[str],
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            role_id: {
                "pending": 0,
                "claimed": 0,
                "completed": 0,
                "artifact_paths": [],
            }
            for role_id in roles
        }
        for role_id in roles:
            for state, paths in [
                ("pending", self._pending_dir(role_id).glob("*.json")),
                ("completed", self._completed_dir(role_id).glob("*.json")),
            ]:
                for path in paths:
                    message = self._read_message(path)
                    if message.payload.get("work_item_id") != work_item_id:
                        continue
                    summary[role_id][state] += 1
                    output_path = message.payload.get("output_path")
                    if output_path:
                        summary[role_id]["artifact_paths"].append(output_path)
            claimed_parent = self.root / role_id / "claimed"
            for path in claimed_parent.glob("*/*.json"):
                message = self._read_message(path)
                if message.payload.get("work_item_id") != work_item_id:
                    continue
                summary[role_id]["claimed"] += 1
                output_path = message.payload.get("output_path")
                if output_path:
                    summary[role_id]["artifact_paths"].append(output_path)
        return summary

    def mark_work_item_publish_ready(
        self,
        work_item_id: str,
        payload: dict[str, Any],
    ) -> bool:
        path = self.root.parent / "work_items" / work_item_id / "publish-ready.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            return True
        except FileExistsError:
            return False

    def _pending_dir(self, role_id: str) -> Path:
        return self.root / role_id / "pending"

    def _claimed_dir(self, role_id: str, instance_id: str) -> Path:
        return self.root / role_id / "claimed" / instance_id

    def _completed_dir(self, role_id: str) -> Path:
        return self.root / role_id / "completed"

    def _find_claimed_path(self, message: Message) -> Path | None:
        if not message.claimed_by:
            return None
        claimed_dir = self._claimed_dir(message.role_id, message.claimed_by)
        matches = list(claimed_dir.glob(f"*-{message.message_id}.json"))
        return matches[0] if matches else None

    @staticmethod
    def _write_message(path: Path, message: Message) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(message), handle, indent=2, sort_keys=True)
            handle.write("\n")

    @staticmethod
    def _read_message(path: Path) -> Message:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return Message(**data)


class FileConnectorOutbox:
    def __init__(self, state_root: Path, project_id: str, journal: EventJournal) -> None:
        self.root = state_root / "projects" / project_id / "connector_outbox"
        self.project_id = project_id
        self.journal = journal
        self.root.mkdir(parents=True, exist_ok=True)

    def enqueue(self, message: ConnectorMessage) -> ConnectorMessage:
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            channel=message.channel,
            message_id=message.message_id,
            message_type=message.type,
            work_item_id=message.payload.get("work_item_id"),
            work_item_type=message.payload.get("work_item_type"),
            lifecycle_state=message.payload.get("lifecycle_state"),
            gate_id=message.payload.get("gate_id"),
            correlation_id=message.correlation_id,
        )
        with telemetry.start_span(
            "connector.outbox.enqueue",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=attrs,
        ) as trace_context:
            message = replace(message, trace_context=trace_context)
            pending = self._pending_dir(message.channel)
            pending.mkdir(parents=True, exist_ok=True)
            path = pending / f"{message.created_at.replace(':', '')}-{message.message_id}.json"
            self._write_connector_message(path, message)
            self.journal.append(
                "connector_message_queued",
                project_id=self.project_id,
                channel=message.channel,
                message_id=message.message_id,
                message_type=message.type,
                work_item_id=message.payload.get("work_item_id"),
                work_item_type=message.payload.get("work_item_type"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                gate_id=message.payload.get("gate_id"),
                correlation_id=message.correlation_id,
                source=message.source,
            )
            return message

    def claim_next(self, channel: str, connector_id: str) -> ConnectorMessage | None:
        pending = self._pending_dir(channel)
        claimed = self._claimed_dir(channel, connector_id)
        claimed.mkdir(parents=True, exist_ok=True)
        for path in sorted(pending.glob("*.json")):
            original = self._read_connector_message(path)
            wait_seconds = telemetry.elapsed_seconds(original.created_at)
            attrs = telemetry.span_attributes(
                project_id=self.project_id,
                channel=channel,
                connector_id=connector_id,
                message_id=original.message_id,
                message_type=original.type,
                work_item_id=original.payload.get("work_item_id"),
                lifecycle_state=original.payload.get("lifecycle_state"),
                gate_id=original.payload.get("gate_id"),
                correlation_id=original.correlation_id,
            )
            with telemetry.start_span(
                "queue.wait",
                correlation_id=original.correlation_id,
                trace_context=original.trace_context,
                attributes=attrs,
            ):
                if wait_seconds is not None:
                    telemetry.record_duration(
                        "agentic_mesh.queue.wait.duration",
                        wait_seconds,
                        attrs,
                    )
            message = original.claimed(connector_id)
            with telemetry.start_span(
                "work.claim",
                correlation_id=message.correlation_id,
                trace_context=message.trace_context,
                attributes=attrs,
            ) as trace_context:
                message = replace(message, trace_context=trace_context)
            target = claimed / path.name
            try:
                path.replace(target)
            except FileNotFoundError:
                continue
            self._write_connector_message(target, message)
            self.journal.append(
                "connector_message_claimed",
                project_id=self.project_id,
                channel=channel,
                connector_id=connector_id,
                message_id=message.message_id,
                message_type=message.type,
                work_item_id=message.payload.get("work_item_id"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                gate_id=message.payload.get("gate_id"),
                correlation_id=message.correlation_id,
            )
            return message
        return None

    def complete(self, message: ConnectorMessage, status: str) -> None:
        if not message.claimed_by:
            raise ValueError("Cannot complete an unclaimed connector message")
        attrs = telemetry.span_attributes(
            project_id=self.project_id,
            channel=message.channel,
            connector_id=message.claimed_by,
            message_id=message.message_id,
            message_type=message.type,
            work_item_id=message.payload.get("work_item_id"),
            lifecycle_state=message.payload.get("lifecycle_state"),
            gate_id=message.payload.get("gate_id"),
            correlation_id=message.correlation_id,
            status=status,
        )
        with telemetry.start_span(
            "work.complete",
            correlation_id=message.correlation_id,
            trace_context=message.trace_context,
            attributes=attrs,
        ):
            claimed_path = self._find_claimed_path(message)
            completed = self._completed_dir(message.channel)
            completed.mkdir(parents=True, exist_ok=True)
            if claimed_path:
                claimed_path.replace(completed / claimed_path.name)
            self.journal.append(
                "connector_message_completed",
                project_id=self.project_id,
                channel=message.channel,
                connector_id=message.claimed_by,
                message_id=message.message_id,
                message_type=message.type,
                work_item_id=message.payload.get("work_item_id"),
                lifecycle_state=message.payload.get("lifecycle_state"),
                gate_id=message.payload.get("gate_id"),
                correlation_id=message.correlation_id,
                status=status,
            )

    def pending_count(self, channel: str) -> int:
        return len(list(self._pending_dir(channel).glob("*.json")))

    def _pending_dir(self, channel: str) -> Path:
        return self.root / channel / "pending"

    def _claimed_dir(self, channel: str, connector_id: str) -> Path:
        return self.root / channel / "claimed" / connector_id

    def _completed_dir(self, channel: str) -> Path:
        return self.root / channel / "completed"

    def _find_claimed_path(self, message: ConnectorMessage) -> Path | None:
        if not message.claimed_by:
            return None
        claimed_dir = self._claimed_dir(message.channel, message.claimed_by)
        matches = list(claimed_dir.glob(f"*-{message.message_id}.json"))
        return matches[0] if matches else None

    @staticmethod
    def _write_connector_message(path: Path, message: ConnectorMessage) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(message), handle, indent=2, sort_keys=True)
            handle.write("\n")

    @staticmethod
    def _read_connector_message(path: Path) -> ConnectorMessage:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return ConnectorMessage(**data)
