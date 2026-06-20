from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.broker_diagnostics import role_consumer_name
from agentic_mesh_v3.connectors import OutboundMessage
from agentic_mesh_v3.connectors import StakeholderBridge
from agentic_mesh_v3.governance import GovernanceChecklist
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import GovernanceInstructionSet
from agentic_mesh_v3.governance import evaluate_governance_checklist
from agentic_mesh_v3.memory import SQLiteRoleMemory
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.reporting import ReportingSnapshot
from agentic_mesh_v3.reporting import agent_has_actionable_lifecycle_alert
from agentic_mesh_v3.state_machine import TERMINAL_STATES
from agentic_mesh_v3.tool_contracts import DO_TOOLS
from agentic_mesh_v3.tool_contracts import REPLY_TOOLS
from agentic_mesh_v3.tool_contracts import TERMINAL_TOOLS


TERMINAL_TOOL_NAMES = TERMINAL_TOOLS


@dataclass(frozen=True)
class RoleInstanceConfig:
    project_id: str
    role_id: str
    instance_id: str
    role_prompt_path: Path
    memory_db_path: Path
    inbox_stream: str
    inbox_consumer: str
    system_prompt_path: Path | None = None
    organisation_prompt_path: Path | None = None
    project_prompt_path: Path | None = None
    raci_path: Path | None = None
    tools_prompt_path: Path | None = None

    @property
    def role_instance_id(self) -> str:
        return f"{self.project_id}.{self.role_id}.{self.instance_id}"


@dataclass(frozen=True)
class AgentMessage:
    message_id: str
    subject: str
    payload: dict[str, object]


@dataclass(frozen=True)
class ClaimedAgentMessage:
    message: BrokerMessage
    consumer: str


@dataclass(frozen=True)
class AgentRunResult:
    message_id: str
    status: str
    tool_calls: tuple[str, ...] = ()
    error: str | None = None


class AgentWorker(Protocol):
    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        """Execute the underlying agent and return recorded tool-call names or ids."""


class AgentMemory(Protocol):
    def load_summary(self, role_instance_id: str) -> str:
        """Load concise source-linked memory for this role instance."""

    def record_observation(self, role_instance_id: str, observation: str, *, source_ref: str = "agent-run") -> None:
        """Record a source-linked memory observation."""


class ConversationContext(Protocol):
    def load_recent(self, conversation_ref: str) -> str:
        """Load recent conversation context for a connector conversation."""


class OperationalContext(Protocol):
    def load(self, *, project_id: str, role_instance_id: str, role_id: str) -> str:
        """Load compact read-only mesh status context for this role run."""


class WorkItemGovernanceContextProvider(Protocol):
    def load_for_work_item(self, work_item_id: str) -> tuple[GovernanceContext | None, GovernanceChecklist | None]:
        """Load governance context/checklist for a work item."""


class WorkItemStateProvider(Protocol):
    def state_for_work_item(self, work_item_id: str) -> str | None:
        """Load the current state for a work item, if available."""

    def owner_for_work_item(self, work_item_id: str) -> str | None:
        """Load the current owner role for a work item, if available."""


class MessageFreshnessProvider(Protocol):
    def superseding_message_id(self, *, message_id: str, target_role: str, work_item_id: str) -> str | None:
        """Return a newer message for this role/work item when the current message is superseded."""


class AgentStatusReporter(Protocol):
    def report(self, status: AgentStatus) -> None:
        """Publish current agent status to the runtime read model."""


class TerminalToolCallAudit(Protocol):
    def snapshot(self, role_instance_id: str) -> object:
        """Capture terminal tool-call state before a worker run."""

    def verify_terminal_call(self, role_instance_id: str, before: object, tool_calls: list[str]) -> tuple[str, ...]:
        """Verify the run recorded terminal calls and return audited call ids/names."""

    def verify_message_terminal_call(
        self,
        role_instance_id: str,
        *,
        message_id: str,
        correlation_id: str | None = None,
    ) -> tuple[str, ...]:
        """Verify a prior attempt for this exact message already recorded terminal calls."""


class SafeOutputImporter(Protocol):
    def import_safe_outputs(self, *, role_instance_id: str, before: object) -> None:
        """Import external safe-output records into the DB-backed tool-call audit projection."""


class AgentRunRecorder(Protocol):
    def interrupt_running(self, *, role_instance_id: str, reason: str, completed_at: str) -> None:
        """Mark older running records for this role instance as interrupted."""

    def record(
        self,
        *,
        run_id: str,
        role_instance_id: str,
        message_id: str,
        subject: str,
        status: str,
        work_item_id: str | None = None,
        tool_calls: tuple[str, ...] = (),
        error: str | None = None,
        started_at: str,
        completed_at: str,
    ) -> None:
        """Record the durable outcome of one role-agent message run."""


class AgentPromptRecorder(Protocol):
    def record(
        self,
        *,
        prompt_id: str,
        run_id: str,
        role_id: str,
        role_instance_id: str,
        assignment_id: str | None,
        prompt_text: str,
        component_manifest: dict[str, object],
    ) -> None:
        """Record the full generated prompt for audit and failed-run debugging."""


class MessageJournalRecorder(Protocol):
    def record_message_journal(
        self,
        *,
        message_id: str,
        stage: str,
        direction: str,
        status: str,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        connector: str | None = None,
        conversation_ref: str | None = None,
        thread_ref: str | None = None,
        source_ref: str | None = None,
        target_role: str | None = None,
        role_instance_id: str | None = None,
        work_item_id: str | None = None,
        queue_item_id: str | None = None,
        broker_subject: str | None = None,
        broker_consumer: str | None = None,
        delivery_attempt: int = 0,
        summary: str = "",
        payload: dict[str, object] | None = None,
    ) -> str:
        """Record one durable message delivery stage."""


class AgentSessionRecorder(Protocol):
    def upsert_agent_session(
        self,
        *,
        session_id: str,
        role_instance_id: str,
        provider: str,
        status: str,
        provider_session_ref: str | None = None,
        mode: str = "unknown",
        last_hydrated_at: str | None = None,
        last_compacted_at: str | None = None,
        prompt_version: str | None = None,
        memory_version: str | None = None,
    ) -> None:
        """Record role-owned worker session metadata."""


class AgentFailureReporter(Protocol):
    def report_dead_letter(
        self,
        *,
        role_instance_id: str,
        role_id: str,
        message_id: str,
        work_item_id: str | None,
        error: str,
        payload: dict[str, object],
    ) -> None:
        """Project an exhausted agent-delivery failure into the work-item read model."""


class NullMessageJournalRecorder:
    def record_message_journal(self, **_: object) -> str:
        return ""


class NullAgentSessionRecorder:
    def upsert_agent_session(self, **_: object) -> None:
        return None


class NullAgentFailureReporter:
    def report_dead_letter(self, **_: object) -> None:
        return None


class NullAgentStatusReporter:
    def report(self, status: AgentStatus) -> None:
        return


class NullAgentRunRecorder:
    def interrupt_running(self, *, role_instance_id: str, reason: str, completed_at: str) -> None:
        del role_instance_id, reason, completed_at

    def record(
        self,
        *,
        run_id: str,
        role_instance_id: str,
        message_id: str,
        subject: str,
        status: str,
        work_item_id: str | None = None,
        tool_calls: tuple[str, ...] = (),
        error: str | None = None,
        started_at: str,
        completed_at: str,
    ) -> None:
        del run_id, role_instance_id, message_id, subject, status, work_item_id, tool_calls, error, started_at, completed_at
        return


class NullAgentPromptRecorder:
    def record(
        self,
        *,
        prompt_id: str,
        run_id: str,
        role_id: str,
        role_instance_id: str,
        assignment_id: str | None,
        prompt_text: str,
        component_manifest: dict[str, object],
    ) -> None:
        del prompt_id, run_id, role_id, role_instance_id, assignment_id, prompt_text, component_manifest
        return


class NullTerminalToolCallAudit:
    def snapshot(self, role_instance_id: str) -> object:
        del role_instance_id
        return None

    def verify_terminal_call(self, role_instance_id: str, before: object, tool_calls: list[str]) -> tuple[str, ...]:
        del role_instance_id, before
        if not tool_calls:
            raise ValueError("agent did not call any safe-output tool")
        if not any(_tool_call_name(call) in DO_TOOLS for call in tool_calls):
            raise ValueError("agent did not call a DO safe-output tool")
        if not any(_tool_call_name(call) in REPLY_TOOLS for call in tool_calls):
            raise ValueError("agent did not call a REPLY safe-output tool")
        if not _has_terminal_tool_call(tool_calls):
            raise ValueError("agent did not call a terminal safe-output tool")
        return tuple(tool_calls)

    def verify_message_terminal_call(
        self,
        role_instance_id: str,
        *,
        message_id: str,
        correlation_id: str | None = None,
    ) -> tuple[str, ...]:
        del role_instance_id, message_id, correlation_id
        raise ValueError("agent did not record message-linked safe-output tool calls")


class DatabaseTerminalToolCallAudit:
    def __init__(self, db: object, *, safe_output_importer: SafeOutputImporter | None = None) -> None:
        self.db = db
        self.safe_output_importer = safe_output_importer

    def snapshot(self, role_instance_id: str) -> frozenset[str]:
        return frozenset(self._tool_call_ids(role_instance_id))

    def verify_terminal_call(self, role_instance_id: str, before: object, tool_calls: list[str]) -> tuple[str, ...]:
        del tool_calls
        if self.safe_output_importer is not None:
            self.safe_output_importer.import_safe_outputs(role_instance_id=role_instance_id, before=before)
        before_ids = set(before) if isinstance(before, (frozenset, set)) else set()
        new_calls = self._new_tool_calls(role_instance_id, before_ids)
        self._verify_tool_call_set(new_calls, context="agent")
        return tuple(str(row["call_id"]) for row in new_calls)

    def verify_message_terminal_call(
        self,
        role_instance_id: str,
        *,
        message_id: str,
        correlation_id: str | None = None,
    ) -> tuple[str, ...]:
        message_calls = self._message_tool_calls(
            role_instance_id,
            message_id=message_id,
            correlation_id=correlation_id,
        )
        self._verify_tool_call_set(message_calls, context=f"message {message_id}")
        return tuple(str(row["call_id"]) for row in message_calls)

    def _tool_call_ids(self, role_instance_id: str) -> tuple[str, ...]:
        return tuple(
            str(row["call_id"])
            for row in self.db.list_tool_calls()  # type: ignore[attr-defined]
            if row.get("role_instance_id") == role_instance_id
        )

    def _new_tool_calls(self, role_instance_id: str, before_ids: set[str]) -> tuple[dict[str, object], ...]:
        return tuple(
            dict(row)
            for row in self.db.list_tool_calls()  # type: ignore[attr-defined]
            if row.get("role_instance_id") == role_instance_id and str(row.get("call_id")) not in before_ids
        )

    def _message_tool_calls(
        self,
        role_instance_id: str,
        *,
        message_id: str,
        correlation_id: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        correlation_candidates = {value for value in (correlation_id, f"corr-{message_id}") if value}
        calls: list[dict[str, object]] = []
        for row in self.db.list_tool_calls():  # type: ignore[attr-defined]
            if row.get("role_instance_id") != role_instance_id:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                payload = _json_object(row.get("payload_json"))
            source_message_id = payload.get("source_message_id") or payload.get("message_id")
            call_correlation_id = payload.get("correlation_id")
            if source_message_id == message_id or call_correlation_id in correlation_candidates:
                calls.append(dict(row))
        return tuple(calls)

    def _verify_tool_call_set(self, calls: tuple[dict[str, object], ...], *, context: str) -> None:
        if not calls:
            raise ValueError(f"{context} did not record any safe-output tool call")
        if not any(str(row.get("tool_name")) in DO_TOOLS for row in calls):
            raise ValueError(f"{context} did not record a DO safe-output tool call")
        if not any(str(row.get("tool_name")) in REPLY_TOOLS for row in calls):
            raise ValueError(f"{context} did not record a REPLY safe-output tool call")
        if not any(bool(row.get("terminal")) for row in calls):
            raise ValueError(f"{context} did not record a terminal safe-output tool call")


class DatabaseAgentStatusReporter:
    def __init__(self, db: object) -> None:
        self.db = db

    def report(self, status: AgentStatus) -> None:
        self.db.upsert_agent_status(status)  # type: ignore[attr-defined]


class DatabaseAgentRunRecorder:
    def __init__(self, db: object) -> None:
        self.db = db

    def interrupt_running(self, *, role_instance_id: str, reason: str, completed_at: str) -> None:
        self.db.interrupt_running_agent_runs(  # type: ignore[attr-defined]
            role_instance_id=role_instance_id,
            reason=reason,
            completed_at=completed_at,
        )

    def record(
        self,
        *,
        run_id: str,
        role_instance_id: str,
        message_id: str,
        subject: str,
        status: str,
        work_item_id: str | None = None,
        tool_calls: tuple[str, ...] = (),
        error: str | None = None,
        started_at: str,
        completed_at: str,
    ) -> None:
        self.db.record_agent_run(  # type: ignore[attr-defined]
            run_id=run_id,
            role_instance_id=role_instance_id,
            message_id=message_id,
            subject=subject,
            status=status,
            work_item_id=work_item_id,
            tool_calls=tool_calls,
            error=error,
            started_at=started_at,
            completed_at=completed_at,
        )


class DatabaseAgentPromptRecorder:
    def __init__(self, db: object) -> None:
        self.db = db

    def record(
        self,
        *,
        prompt_id: str,
        run_id: str,
        role_id: str,
        role_instance_id: str,
        assignment_id: str | None,
        prompt_text: str,
        component_manifest: dict[str, object],
    ) -> None:
        self.db.record_agent_prompt(  # type: ignore[attr-defined]
            prompt_id=prompt_id,
            run_id=run_id,
            role_id=role_id,
            role_instance_id=role_instance_id,
            assignment_id=assignment_id,
            prompt_text=prompt_text,
            component_manifest=component_manifest,
        )


class DatabaseAgentFailureReporter:
    def __init__(
        self,
        db: object,
        stakeholder_bridge: StakeholderBridge | None = None,
        *,
        broker: BrokerAdapter | None = None,
        broker_stream: str | None = None,
        project_manager_role_id: str = "project-manager",
    ) -> None:
        self.db = db
        self.stakeholder_bridge = stakeholder_bridge
        self.broker = broker
        self.broker_stream = broker_stream
        self.project_manager_role_id = project_manager_role_id

    def report_dead_letter(
        self,
        *,
        role_instance_id: str,
        role_id: str,
        message_id: str,
        work_item_id: str | None,
        error: str,
        payload: dict[str, object],
    ) -> None:
        self._notify_source_conversation(
            role_instance_id=role_instance_id,
            role_id=role_id,
            message_id=message_id,
            work_item_id=work_item_id,
            error=error,
            payload=payload,
        )
        if not work_item_id:
            return
        if _is_non_blocking_delivery_failure(payload):
            self.db.record_event(  # type: ignore[attr-defined]
                "agent.delivery_failure_recorded",
                "work_item",
                work_item_id,
                {
                    "role_instance_id": role_instance_id,
                    "role_id": role_id,
                    "message_id": message_id,
                    "message_type": _payload_text(payload, "message_type"),
                    "error": error,
                    "blocking": False,
                },
            )
            return
        operator_recovery = _is_operator_recovery_failure(error)
        owner_role = "project-manager" if operator_recovery else role_id
        current_phase = "operator_recovery" if operator_recovery else _payload_text(payload, "current_phase")
        if operator_recovery:
            next_action = (
                f"Project Manager must recover failed agent delivery for {role_instance_id} on message {message_id}: {error}. "
                "Inspect the message journal, prompt/tool wiring, and latest work-item state; then retry the role, delegate a "
                "focused diagnostic/fix to the right specialist, or record a concrete blocker with owner and next action."
            )
        else:
            next_action = (
                f"Agent delivery dead-lettered for {role_instance_id} on message {message_id}: {error}. "
                "Review the agent/tool wiring or retry the role after correcting the failure."
            )
        self.db.update_work_item_state(  # type: ignore[attr-defined]
            work_item_id=work_item_id,
            state="blocked",
            owner_role=owner_role,
            current_phase=current_phase,
            next_action=next_action,
            source_ref=f"message:{message_id}",
            correlation_id=_message_correlation_id(payload),
            trace_id=_payload_text(payload, "trace_id"),
            created_by_role_instance=role_instance_id,
            origin_message_id=_payload_text(payload, "source_message_id") or message_id,
        )
        if operator_recovery:
            self._publish_project_manager_recovery(
                role_instance_id=role_instance_id,
                role_id=role_id,
                message_id=message_id,
                work_item_id=work_item_id,
                error=error,
                payload=payload,
                next_action=next_action,
            )

    def _notify_source_conversation(
        self,
        *,
        role_instance_id: str,
        role_id: str,
        message_id: str,
        work_item_id: str | None,
        error: str,
        payload: dict[str, object],
    ) -> None:
        if self.stakeholder_bridge is None:
            return
        route = self._reply_route(message_id=message_id, payload=payload)
        target_ref = _payload_text(route, "reply_target_ref")
        connector = _payload_text(route, "connector")
        if target_ref is None or connector is None:
            return
        thread_ref = _payload_text(route, "reply_thread_ref") or _payload_text(route, "thread_ref")
        call_id = f"agent-failure-{message_id}"
        text = _agent_failure_reply_text(
            role_id=role_id,
            work_item_id=work_item_id,
            message_id=message_id,
            error=error,
        )
        try:
            receipt = self.stakeholder_bridge.send(
                OutboundMessage(
                    connector=connector,
                    target_ref=target_ref,
                    text_markdown=text,
                    thread_ref=thread_ref,
                    importance="high",
                    sender_role=role_id,
                )
            )
        except Exception as exc:
            self.db.record_outbound_delivery(  # type: ignore[attr-defined]
                delivery_id=f"delivery-failed-{call_id}",
                call_id=call_id,
                role_instance_id=role_instance_id,
                work_item_id=work_item_id,
                purpose="agent.failure",
                connector=connector,
                target_ref=f"{target_ref} ({type(exc).__name__}: {exc})",
                thread_ref=thread_ref,
                status="failed",
            )
            self.db.record_message_journal(  # type: ignore[attr-defined]
                message_id=message_id,
                correlation_id=_message_correlation_id(payload),
                direction="outbound",
                stage="failed",
                status="failed",
                connector=connector,
                conversation_ref=_payload_text(route, "conversation_ref"),
                thread_ref=thread_ref,
                source_ref=call_id,
                role_instance_id=role_instance_id,
                work_item_id=work_item_id,
                summary=f"agent.failure delivery failed: {type(exc).__name__}: {exc}",
            )
            return
        self.db.record_outbound_delivery(  # type: ignore[attr-defined]
            delivery_id=receipt.delivery_id,
            call_id=call_id,
            role_instance_id=role_instance_id,
            work_item_id=work_item_id,
            purpose="agent.failure",
            connector=receipt.connector,
            target_ref=receipt.target_ref,
            thread_ref=receipt.thread_ref,
            status="sent",
        )
        self.db.record_message_journal(  # type: ignore[attr-defined]
            message_id=message_id,
            correlation_id=_message_correlation_id(payload),
            direction="outbound",
            stage="reply_delivered",
            status="sent",
            connector=receipt.connector,
            conversation_ref=_payload_text(route, "conversation_ref"),
            thread_ref=receipt.thread_ref,
            source_ref=call_id,
            role_instance_id=role_instance_id,
            work_item_id=work_item_id,
            summary=f"agent.failure delivered to {receipt.target_ref}",
        )

    def _reply_route(self, *, message_id: str, payload: dict[str, object]) -> dict[str, object]:
        direct_route = {
            "connector": payload.get("connector"),
            "conversation_ref": payload.get("conversation_ref"),
            "thread_ref": payload.get("thread_ref"),
            "reply_target_ref": payload.get("reply_target_ref") or payload.get("target_ref"),
            "reply_thread_ref": payload.get("reply_thread_ref"),
        }
        if _payload_text(direct_route, "reply_target_ref") is not None:
            return direct_route
        source_message_id = _payload_text(payload, "source_message_id") or message_id
        try:
            route = self.db.conversation_reply_route(source_message_id)  # type: ignore[attr-defined]
        except Exception:
            return direct_route
        if route is None:
            return direct_route
        return dict(route)

    def _publish_project_manager_recovery(
        self,
        *,
        role_instance_id: str,
        role_id: str,
        message_id: str,
        work_item_id: str,
        error: str,
        payload: dict[str, object],
        next_action: str,
    ) -> None:
        if self.broker is None or self.broker_stream is None:
            return
        target_role = self.project_manager_role_id
        subject = f"agent.{target_role}"
        consumer = role_consumer_name(target_role, "1")
        self.broker.ensure_stream(self.broker_stream, [subject])
        self.broker.ensure_consumer(self.broker_stream, consumer, filter_subject=subject)
        message_payload: dict[str, object] = {
            "message_type": "operator_recovery.required",
            "source_role_instance_id": role_instance_id,
            "source_role": role_id,
            "target_role": target_role,
            "work_item_id": work_item_id,
            "failed_message_id": message_id,
            "failed_role_instance_id": role_instance_id,
            "error": error,
            "task": next_action,
            "reason": "Agent delivery dead-lettered with a safe-output/tool-contract failure.",
            "expected_output": (
                "Project Manager either retries the failed role with focused context, delegates diagnosis to a specialist, "
                "or records a concrete blocker/exception with owner and next action."
            ),
            "correlation_id": _message_correlation_id(payload) or f"corr-{message_id}",
        }
        for optional_key in (
            "queue_item_id",
            "source_message_id",
            "conversation_ref",
            "reply_target_ref",
            "reply_thread_ref",
            "connector",
        ):
            value = _payload_text(payload, optional_key)
            if value is not None:
                message_payload[optional_key] = value
        message = self.broker.publish(self.broker_stream, subject, message_payload)
        self.db.record_message_journal(  # type: ignore[attr-defined]
            message_id=message.message_id,
            correlation_id=str(message_payload["correlation_id"]),
            direction="broker",
            stage="published",
            status="published",
            source_ref=f"message:{message_id}",
            target_role=target_role,
            role_instance_id=role_instance_id,
            work_item_id=work_item_id,
            queue_item_id=_payload_text(payload, "queue_item_id"),
            broker_subject=subject,
            broker_consumer=consumer,
            summary=f"Published operator recovery to {target_role}: {next_action[:140]}",
            payload=message_payload,
        )


def _is_non_blocking_delivery_failure(payload: dict[str, object]) -> bool:
    """Return true for FYI-style deliveries that should not seize work ownership."""

    return _payload_text(payload, "message_type") in {"informed.update"}


def _is_operator_recovery_failure(error: str) -> bool:
    text = error.lower()
    return (
        "did not call any tool" in text
        or "without valid terminal safe-output" in text
        or "direct conversation used outputs that are not allowed" in text
        or "worker subprocess stdout must be json" in text
    )


def _is_inform_only_message(
    payload: dict[str, object],
    *,
    role_id: str,
    work_item_state_provider: WorkItemStateProvider,
) -> bool:
    """Return true for messages that only notify a role and require no worker run.

    Governance uses `informed.update` for RACI "I" notifications. If the
    sender needs action, they must use `handoff.require`, `consult.request`, or
    `agent.delegate`; treating pure informed updates as work creates noisy
    failures and hides the useful queue signal.

    However, an informed update addressed to the current owner of the referenced
    work item is not merely informational. The owner is accountable for deciding
    whether the new evidence changes the next step, so it must reach the worker.
    """

    if _payload_text(payload, "message_type") != "informed.update":
        return False
    work_item_id = _message_work_item_id(payload)
    if work_item_id is None:
        return True
    return work_item_state_provider.owner_for_work_item(work_item_id) != role_id


class InMemoryRoleMemory:
    def __init__(self) -> None:
        self._memory: dict[str, list[str]] = {}

    def load_summary(self, role_instance_id: str) -> str:
        return "\n".join(self._memory.get(role_instance_id, []))

    def record_observation(self, role_instance_id: str, observation: str, *, source_ref: str = "agent-run") -> None:
        del source_ref
        self._memory.setdefault(role_instance_id, []).append(observation)


def build_role_memory(config: RoleInstanceConfig) -> AgentMemory:
    return SQLiteRoleMemory(config.memory_db_path)


class NullConversationContext:
    def load_recent(self, conversation_ref: str) -> str:
        return ""


class DatabaseConversationContext:
    def __init__(self, db: object, *, limit: int = 10, summary_limit: int = 5) -> None:
        self.db = db
        self.limit = limit
        self.summary_limit = summary_limit

    def load_recent(self, conversation_ref: str) -> str:
        summaries = self.db.list_conversation_summaries(  # type: ignore[attr-defined]
            conversation_ref,
            limit=self.summary_limit,
        )
        rows = self.db.list_conversation_messages(conversation_ref, limit=self.limit)  # type: ignore[attr-defined]
        parts: list[str] = []
        if summaries:
            parts.append("<conversation-summaries>")
            parts.extend(_conversation_summary_row(summary) for summary in summaries)
            parts.append("</conversation-summaries>")
        if rows:
            parts.append("<recent-messages>")
            parts.extend(_conversation_row_summary(row) for row in rows)
            parts.append("</recent-messages>")
        return "\n".join(parts)


class NullOperationalContext:
    def load(self, *, project_id: str, role_instance_id: str, role_id: str) -> str:
        del project_id, role_instance_id, role_id
        return ""


class DatabaseOperationalContext:
    def __init__(
        self,
        db: object,
        *,
        configured_role_instance_ids: tuple[str, ...] = (),
        max_work_items: int = 8,
        max_agents: int = 16,
        max_recent_completions: int = 5,
    ) -> None:
        self.db = db
        self.configured_role_instance_ids = configured_role_instance_ids
        self.max_work_items = max_work_items
        self.max_agents = max_agents
        self.max_recent_completions = max_recent_completions

    def load(self, *, project_id: str, role_instance_id: str, role_id: str) -> str:
        snapshot = self.db.status_snapshot(  # type: ignore[attr-defined]
            project_id=project_id,
            configured_role_instance_ids=self.configured_role_instance_ids,
        )
        return _operational_context_summary(
            snapshot,
            role_instance_id=role_instance_id,
            role_id=role_id,
            max_work_items=self.max_work_items,
            max_agents=self.max_agents,
            max_recent_completions=self.max_recent_completions,
        )


class NullWorkItemGovernanceContextProvider:
    def load_for_work_item(self, work_item_id: str) -> tuple[GovernanceContext | None, GovernanceChecklist | None]:
        del work_item_id
        return None, None


class NullWorkItemStateProvider:
    def state_for_work_item(self, work_item_id: str) -> str | None:
        del work_item_id
        return None

    def owner_for_work_item(self, work_item_id: str) -> str | None:
        del work_item_id
        return None


class NullMessageFreshnessProvider:
    def superseding_message_id(self, *, message_id: str, target_role: str, work_item_id: str) -> str | None:
        del message_id, target_role, work_item_id
        return None


class DatabaseWorkItemGovernanceContextProvider:
    def __init__(self, db: object) -> None:
        self.db = db

    def load_for_work_item(self, work_item_id: str) -> tuple[GovernanceContext | None, GovernanceChecklist | None]:
        context = self.db.work_item_governance_context(work_item_id)  # type: ignore[attr-defined]
        checklist = self.db.work_item_governance_checklist(work_item_id)  # type: ignore[attr-defined]
        return context, checklist


class DatabaseWorkItemStateProvider:
    def __init__(self, db: object) -> None:
        self.db = db

    def state_for_work_item(self, work_item_id: str) -> str | None:
        detail = self.db.work_item_detail(work_item_id)  # type: ignore[attr-defined]
        return None if detail is None else detail.state

    def owner_for_work_item(self, work_item_id: str) -> str | None:
        detail = self.db.work_item_detail(work_item_id)  # type: ignore[attr-defined]
        return None if detail is None else detail.owner_role


class DatabaseMessageFreshnessProvider:
    def __init__(self, db: object) -> None:
        self.db = db

    def superseding_message_id(self, *, message_id: str, target_role: str, work_item_id: str) -> str | None:
        current = self.db.connection.execute(  # type: ignore[attr-defined]
            """
            SELECT created_at
            FROM message_journal
            WHERE message_id=? AND stage='published'
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (message_id,),
        ).fetchone()
        if current is None:
            return None
        newer = self.db.connection.execute(  # type: ignore[attr-defined]
            """
            SELECT message_id
            FROM message_journal
            WHERE stage='published'
              AND target_role=?
              AND work_item_id=?
              AND message_id<>?
              AND created_at>?
            ORDER BY created_at DESC, journal_id DESC
            LIMIT 1
            """,
            (target_role, work_item_id, message_id, current["created_at"]),
        ).fetchone()
        return None if newer is None else str(newer["message_id"])


class EchoWorker:
    """Tiny worker for contract tests; production uses Codex or other adapters."""

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        if not prompt:
            raise ValueError("prompt is required")
        return [f"noop:{message.message_id}", f"status.complete:{message.message_id}"]


@dataclass
class RoleAgentService:
    config: RoleInstanceConfig
    broker: BrokerAdapter
    worker: AgentWorker
    memory: AgentMemory
    conversation_context: ConversationContext = field(default_factory=NullConversationContext)
    operational_context: OperationalContext = field(default_factory=NullOperationalContext)
    work_item_governance_context: WorkItemGovernanceContextProvider = field(
        default_factory=NullWorkItemGovernanceContextProvider
    )
    work_item_state_provider: WorkItemStateProvider = field(default_factory=NullWorkItemStateProvider)
    message_freshness_provider: MessageFreshnessProvider = field(default_factory=NullMessageFreshnessProvider)
    governance_instructions: GovernanceInstructionSet = field(default_factory=GovernanceInstructionSet)
    status_reporter: AgentStatusReporter = field(default_factory=NullAgentStatusReporter)
    terminal_tool_call_audit: TerminalToolCallAudit = field(default_factory=NullTerminalToolCallAudit)
    run_recorder: AgentRunRecorder = field(default_factory=NullAgentRunRecorder)
    prompt_recorder: AgentPromptRecorder = field(default_factory=NullAgentPromptRecorder)
    message_journal: MessageJournalRecorder = field(default_factory=NullMessageJournalRecorder)
    session_recorder: AgentSessionRecorder = field(default_factory=NullAgentSessionRecorder)
    failure_reporter: AgentFailureReporter = field(default_factory=NullAgentFailureReporter)
    max_delivery_attempts: int = 3

    def __post_init__(self) -> None:
        if self.max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")

    def run_until_idle(
        self,
        *,
        max_messages: int = 10,
        governance_context: GovernanceContext | None = None,
        governance_checklist: GovernanceChecklist | None = None,
    ) -> tuple[AgentRunResult, ...]:
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        results: list[AgentRunResult] = []
        for _ in range(max_messages):
            result = self.run_once(
                governance_context=governance_context,
                governance_checklist=governance_checklist,
            )
            if result is None:
                break
            results.append(result)
        return tuple(results)

    def run_once(
        self,
        *,
        governance_context: GovernanceContext | None = None,
        governance_checklist: GovernanceChecklist | None = None,
    ) -> AgentRunResult | None:
        self._report_status(container_state="running", current_work=None)
        claimed = self._fetch_next_message()
        if claimed is None:
            self._report_status(container_state="running", current_work=None)
            return None
        message = claimed.message
        self._journal_message(
            message,
            stage="claimed",
            direction="inbound",
            status="claimed",
            broker_consumer=claimed.consumer,
            delivery_attempt=message.delivery_count + 1,
            summary=f"Claimed by {self.config.role_instance_id}",
        )
        self._report_status(container_state="running", current_work=_message_work_ref(message.payload))
        terminal_skip_result = self._skip_if_terminal_work_item(message, claimed.consumer)
        if terminal_skip_result is not None:
            return terminal_skip_result
        superseded_skip_result = self._skip_if_superseded_work_message(message, claimed.consumer)
        if superseded_skip_result is not None:
            return superseded_skip_result
        inform_only_result = self._ack_if_inform_only_message(message, claimed.consumer)
        if inform_only_result is not None:
            return inform_only_result
        agent_message = AgentMessage(
            message_id=message.message_id,
            subject=message.subject,
            payload={
                **message.payload,
                "role_instance_id": self.config.role_instance_id,
                "role_id": self.config.role_id,
                "project_id": self.config.project_id,
            },
        )
        prompt_governance_context, prompt_governance_checklist = self._governance_for_message(
            agent_message,
            governance_context=governance_context,
            governance_checklist=governance_checklist,
        )
        run_id = f"run-{uuid4().hex}"
        prompt = self._build_prompt(
            agent_message,
            governance_context=prompt_governance_context,
            governance_checklist=prompt_governance_checklist,
        )
        self._record_session(status="active")
        run_started_at = datetime.now(timezone.utc).isoformat()
        self.run_recorder.interrupt_running(
            role_instance_id=self.config.role_instance_id,
            reason=f"Role service claimed {message.message_id}; prior running record no longer owns this role instance.",
            completed_at=run_started_at,
        )
        self.run_recorder.record(
            run_id=run_id,
            role_instance_id=self.config.role_instance_id,
            message_id=message.message_id,
            subject=message.subject,
            status="running",
            work_item_id=_message_work_item_id(message.payload),
            started_at=run_started_at,
            completed_at=run_started_at,
        )
        self.prompt_recorder.record(
            prompt_id=f"prompt-{run_id}",
            run_id=run_id,
            role_id=self.config.role_id,
            role_instance_id=self.config.role_instance_id,
            assignment_id=message.message_id,
            prompt_text=prompt,
            component_manifest={
                "project_id": self.config.project_id,
                "role_id": self.config.role_id,
                "role_instance_id": self.config.role_instance_id,
                "message_id": message.message_id,
                "subject": message.subject,
                "work_item_id": _message_work_item_id(message.payload),
                "has_governance_context": prompt_governance_context is not None,
                "has_conversation_context": bool(_payload_text(message.payload, "conversation_ref")),
                "prompt_version": "v3",
            },
        )
        try:
            self._journal_message(
                message,
                stage="worker_started",
                direction="inbound",
                status="running",
                broker_consumer=claimed.consumer,
                delivery_attempt=message.delivery_count + 1,
                summary=f"Worker started for {self.config.role_instance_id}",
            )
            terminal_audit_snapshot = self.terminal_tool_call_audit.snapshot(self.config.role_instance_id)
            tool_calls = self.worker.run(prompt, agent_message)
            if not tool_calls:
                raise ValueError("agent did not call any tool")
            audited_tool_calls = self.terminal_tool_call_audit.verify_terminal_call(
                self.config.role_instance_id,
                terminal_audit_snapshot,
                tool_calls,
            )
            self.run_recorder.record(
                run_id=run_id,
                role_instance_id=self.config.role_instance_id,
                message_id=message.message_id,
                subject=message.subject,
                status="completed",
                work_item_id=_message_work_item_id(message.payload),
                tool_calls=audited_tool_calls,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            self.broker.ack(self.config.inbox_stream, claimed.consumer, message.message_id)
            self._journal_message(
                message,
                stage="acked",
                direction="inbound",
                status="completed",
                broker_consumer=claimed.consumer,
                delivery_attempt=message.delivery_count + 1,
                summary=f"Processed by {self.config.role_instance_id}",
            )
            self._report_status(container_state="running", current_work=None)
            return AgentRunResult(message_id=message.message_id, status="completed", tool_calls=audited_tool_calls)
        except Exception as exc:
            try:
                audited_tool_calls = self.terminal_tool_call_audit.verify_terminal_call(
                    self.config.role_instance_id,
                    terminal_audit_snapshot,
                    [],
                )
            except Exception:
                try:
                    audited_tool_calls = self.terminal_tool_call_audit.verify_message_terminal_call(
                        self.config.role_instance_id,
                        message_id=message.message_id,
                        correlation_id=_message_correlation_id(agent_message.payload),
                    )
                except Exception:
                    audited_tool_calls = ()
            if audited_tool_calls:
                self.run_recorder.record(
                    run_id=run_id,
                    role_instance_id=self.config.role_instance_id,
                    message_id=message.message_id,
                    subject=message.subject,
                    status="completed",
                    work_item_id=_message_work_item_id(message.payload),
                    tool_calls=audited_tool_calls,
                    error=None,
                    started_at=run_started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                self.broker.ack(self.config.inbox_stream, claimed.consumer, message.message_id)
                self._journal_message(
                    message,
                    stage="acked",
                    direction="inbound",
                    status="completed_after_worker_error",
                    broker_consumer=claimed.consumer,
                    delivery_attempt=message.delivery_count + 1,
                    summary=f"Audited terminal tool calls for message after worker error: {exc}",
                )
                self._report_status(container_state="running", current_work=None)
                return AgentRunResult(message_id=message.message_id, status="completed", tool_calls=audited_tool_calls)
            status = "dead_lettered" if message.delivery_count + 1 >= self.max_delivery_attempts else "failed"
            completed_at = datetime.now(timezone.utc).isoformat()
            self.run_recorder.record(
                run_id=run_id,
                role_instance_id=self.config.role_instance_id,
                message_id=message.message_id,
                subject=message.subject,
                status=status,
                work_item_id=_message_work_item_id(message.payload),
                error=str(exc),
                started_at=run_started_at,
                completed_at=completed_at,
            )
            if message.delivery_count + 1 >= self.max_delivery_attempts:
                self.broker.dead_letter(
                    self.config.inbox_stream,
                    claimed.consumer,
                    message.message_id,
                    reason=str(exc),
                )
                self._journal_message(
                    message,
                    stage="dead_lettered",
                    direction="inbound",
                    status=status,
                    broker_consumer=claimed.consumer,
                    delivery_attempt=message.delivery_count + 1,
                    summary=str(exc),
                )
                try:
                    self.failure_reporter.report_dead_letter(
                        role_instance_id=self.config.role_instance_id,
                        role_id=self.config.role_id,
                        message_id=message.message_id,
                        work_item_id=_message_work_item_id(message.payload),
                        error=str(exc),
                        payload=message.payload,
                    )
                except Exception as report_exc:
                    self._journal_message(
                        message,
                        stage="failed",
                        direction="inbound",
                        status="failure_projection_failed",
                        broker_consumer=claimed.consumer,
                        delivery_attempt=message.delivery_count + 1,
                        summary=f"Failed to project dead-letter to work item: {report_exc}",
                    )
            else:
                self.broker.nack(
                    self.config.inbox_stream,
                    claimed.consumer,
                    message.message_id,
                    reason=str(exc),
                )
                self._journal_message(
                    message,
                    stage="nacked",
                    direction="inbound",
                    status=status,
                    broker_consumer=claimed.consumer,
                    delivery_attempt=message.delivery_count + 1,
                    summary=str(exc),
                )
            self._report_status(container_state="running", current_work=None, governance_waits=(str(exc),))
            return AgentRunResult(message_id=message.message_id, status=status, error=str(exc))

    def _skip_if_terminal_work_item(self, message: BrokerMessage, consumer: str) -> AgentRunResult | None:
        work_item_id = _message_work_item_id(message.payload)
        if work_item_id is None:
            return None
        state = self.work_item_state_provider.state_for_work_item(work_item_id)
        if state not in TERMINAL_STATES:
            return None
        owner_role = self.work_item_state_provider.owner_for_work_item(work_item_id)
        if owner_role == self.config.role_id:
            return None
        completed_at = datetime.now(timezone.utc).isoformat()
        summary = f"Skipped stale message for terminal work item {work_item_id} in state {state}."
        self.run_recorder.record(
            run_id=f"run-{uuid4().hex}",
            role_instance_id=self.config.role_instance_id,
            message_id=message.message_id,
            subject=message.subject,
            status="stale_terminal_work_skipped",
            work_item_id=work_item_id,
            tool_calls=(),
            started_at=completed_at,
            completed_at=completed_at,
        )
        self.broker.ack(self.config.inbox_stream, consumer, message.message_id)
        self._journal_message(
            message,
            stage="acked",
            direction="inbound",
            status="stale_terminal_work_skipped",
            broker_consumer=consumer,
            delivery_attempt=message.delivery_count + 1,
            summary=summary,
        )
        self._report_status(container_state="running", current_work=None)
        return AgentRunResult(message_id=message.message_id, status="stale_terminal_work_skipped")

    def _skip_if_superseded_work_message(self, message: BrokerMessage, consumer: str) -> AgentRunResult | None:
        work_item_id = _message_work_item_id(message.payload)
        if work_item_id is None:
            return None
        superseding_message_id = self.message_freshness_provider.superseding_message_id(
            message_id=message.message_id,
            target_role=self.config.role_id,
            work_item_id=work_item_id,
        )
        if superseding_message_id is None:
            return None
        completed_at = datetime.now(timezone.utc).isoformat()
        summary = (
            f"Skipped superseded message for work item {work_item_id}; "
            f"newer message {superseding_message_id} exists for {self.config.role_id}."
        )
        self.run_recorder.record(
            run_id=f"run-{uuid4().hex}",
            role_instance_id=self.config.role_instance_id,
            message_id=message.message_id,
            subject=message.subject,
            status="superseded_work_message_skipped",
            work_item_id=work_item_id,
            tool_calls=(),
            started_at=completed_at,
            completed_at=completed_at,
        )
        self.broker.ack(self.config.inbox_stream, consumer, message.message_id)
        self._journal_message(
            message,
            stage="acked",
            direction="inbound",
            status="superseded_work_message_skipped",
            broker_consumer=consumer,
            delivery_attempt=message.delivery_count + 1,
            summary=summary,
        )
        self._report_status(container_state="running", current_work=None)
        return AgentRunResult(message_id=message.message_id, status="superseded_work_message_skipped")

    def _ack_if_inform_only_message(self, message: BrokerMessage, consumer: str) -> AgentRunResult | None:
        if not _is_inform_only_message(
            message.payload,
            role_id=self.config.role_id,
            work_item_state_provider=self.work_item_state_provider,
        ):
            return None
        work_item_id = _message_work_item_id(message.payload)
        completed_at = datetime.now(timezone.utc).isoformat()
        summary = _payload_text(message.payload, "message") or "Informed update received."
        observation = f"Informed update for {work_item_id or 'unscoped work'}: {summary}"
        self.memory.record_observation(
            self.config.role_instance_id,
            observation,
            source_ref=message.message_id,
        )
        self.run_recorder.record(
            run_id=f"run-{uuid4().hex}",
            role_instance_id=self.config.role_instance_id,
            message_id=message.message_id,
            subject=message.subject,
            status="inform_only_acknowledged",
            work_item_id=work_item_id,
            tool_calls=(),
            started_at=completed_at,
            completed_at=completed_at,
        )
        self.broker.ack(self.config.inbox_stream, consumer, message.message_id)
        self._journal_message(
            message,
            stage="acked",
            direction="inbound",
            status="inform_only_acknowledged",
            broker_consumer=consumer,
            delivery_attempt=message.delivery_count + 1,
            summary="Acknowledged informed.update without worker run.",
        )
        self._report_status(container_state="running", current_work=None)
        return AgentRunResult(message_id=message.message_id, status="inform_only_acknowledged")

    def _fetch_next_message(self) -> ClaimedAgentMessage | None:
        for consumer, subject in self._consumer_subjects():
            self.broker.ensure_consumer(
                self.config.inbox_stream,
                consumer,
                filter_subject=subject,
            )
            messages = self.broker.fetch(self.config.inbox_stream, consumer, batch=1)
            if messages:
                return ClaimedAgentMessage(message=messages[0], consumer=consumer)
        return None

    def _record_session(self, *, status: str) -> None:
        hydrated_at = datetime.now(timezone.utc).isoformat()
        provider = str(getattr(self.worker, "provider", self.worker.__class__.__name__))
        mode = str(getattr(self.worker, "session_mode", "unknown"))
        session_status = str(getattr(self.worker, "session_status", status))
        session_id = f"session-{self.config.role_instance_id}"
        memory_summary = self.memory.load_summary(self.config.role_instance_id)
        self.session_recorder.upsert_agent_session(
            session_id=session_id,
            role_instance_id=self.config.role_instance_id,
            provider=provider,
            provider_session_ref=str(getattr(self.worker, "provider_session_ref", session_id)),
            status=session_status,
            mode=mode,
            last_hydrated_at=hydrated_at,
            prompt_version="v3",
            memory_version=_message_memory_version(memory_summary),
        )

    def _journal_message(
        self,
        message: BrokerMessage,
        *,
        stage: str,
        direction: str,
        status: str,
        broker_consumer: str | None = None,
        delivery_attempt: int = 0,
        summary: str = "",
    ) -> None:
        self.message_journal.record_message_journal(
            message_id=message.message_id,
            correlation_id=_message_correlation_id(message.payload),
            direction=direction,
            stage=stage,
            status=status,
            connector=_payload_text(message.payload, "connector"),
            conversation_ref=_payload_text(message.payload, "conversation_ref"),
            thread_ref=_payload_text(message.payload, "thread_ref") or _payload_text(message.payload, "reply_thread_ref"),
            source_ref=_payload_text(message.payload, "source_message_id"),
            target_role=self.config.role_id,
            role_instance_id=self.config.role_instance_id,
            work_item_id=_message_work_item_id(message.payload),
            queue_item_id=_payload_text(message.payload, "queue_item_id"),
            broker_subject=message.subject,
            broker_consumer=broker_consumer,
            delivery_attempt=delivery_attempt,
            summary=summary,
            payload=message.payload,
        )

    def _consumer_subjects(self) -> tuple[tuple[str, str], ...]:
        return (
            (f"{self.config.inbox_consumer}.priority", f"agent.{self.config.role_id}.priority"),
            (self.config.inbox_consumer, f"agent.{self.config.role_id}"),
            (f"{self.config.inbox_consumer}.relevance", f"agent.{self.config.role_id}.relevance"),
        )

    def _report_status(
        self,
        *,
        container_state: str,
        current_work: str | None,
        governance_waits: tuple[str, ...] = (),
    ) -> None:
        self.status_reporter.report(
            AgentStatus(
                role_instance_id=self.config.role_instance_id,
                container_state=container_state,
                heartbeat_at=datetime.now(timezone.utc).isoformat(),
                current_work=current_work,
                inbox_depth=self._role_inbox_depth(),
                dead_letter_depth=len(self.broker.dead_letters(self.config.inbox_stream)),
                governance_waits=governance_waits,
            )
        )

    def _role_inbox_depth(self) -> int:
        depth = 0
        for consumer, subject in self._consumer_subjects():
            self.broker.ensure_consumer(
                self.config.inbox_stream,
                consumer,
                filter_subject=subject,
            )
            depth += len(self.broker.pending(self.config.inbox_stream, consumer))
        return depth

    def _build_prompt(
        self,
        message: AgentMessage,
        *,
        governance_context: GovernanceContext | None = None,
        governance_checklist: GovernanceChecklist | None = None,
    ) -> str:
        role_prompt = self.config.role_prompt_path.read_text(encoding="utf-8")
        system_prompt = _read_optional(self.config.system_prompt_path)
        organisation_prompt = _read_optional(self.config.organisation_prompt_path)
        project_prompt = _read_optional(self.config.project_prompt_path)
        raci_prompt = _read_optional(self.config.raci_path)
        tools_prompt = _read_optional(self.config.tools_prompt_path)
        memory_summary = self.memory.load_summary(self.config.role_instance_id)
        conversation_summary = self._conversation_summary(message)
        operational_summary = self.operational_context.load(
            project_id=self.config.project_id,
            role_instance_id=self.config.role_instance_id,
            role_id=self.config.role_id,
        )
        reply_routing = _reply_routing_prompt_section(message.payload)
        relevance_guidance = _relevance_check_prompt_section(message)
        delegation_guidance = _agent_delegation_prompt_section(message)
        lines = [
            "<agentic-mesh-v3-agent>",
            f"<role-instance>{self.config.role_instance_id}</role-instance>",
            "<role>",
            role_prompt,
            "</role>",
        ]
        _append_optional_section(lines, "system", system_prompt)
        _append_optional_section(lines, "organisation", organisation_prompt)
        _append_optional_section(lines, "project", project_prompt)
        _append_optional_section(lines, "raci", raci_prompt)
        _append_optional_section(lines, "available-tools", tools_prompt)
        lines.append(self.governance_instructions.as_prompt_section())
        if governance_context is not None:
            lines.append(governance_context.as_prompt_section(role_id=self.config.role_id))
            checklist = governance_checklist or evaluate_governance_checklist(governance_context)
            lines.append(checklist.as_prompt_section())
        lines.extend(
            [
                "<memory>",
                memory_summary or "No prior role memory recorded.",
                "</memory>",
                "<conversation-context>",
                conversation_summary or "No recent conversation context recorded.",
                "</conversation-context>",
                "<mesh-operational-context>",
                operational_summary or "No live mesh operational context available.",
                "</mesh-operational-context>",
                reply_routing,
                relevance_guidance,
                delegation_guidance,
                "<message-metadata>",
                f"<message-id>{message.message_id}</message-id>",
                f"<subject>{message.subject}</subject>",
                "</message-metadata>",
                "<assignment>",
                json.dumps(message.payload, indent=2, sort_keys=True, default=str),
                "</assignment>",
                "</agentic-mesh-v3-agent>",
            ]
        )
        return "\n".join(lines)

    def _governance_for_message(
        self,
        message: AgentMessage,
        *,
        governance_context: GovernanceContext | None,
        governance_checklist: GovernanceChecklist | None,
    ) -> tuple[GovernanceContext | None, GovernanceChecklist | None]:
        if governance_context is not None:
            return governance_context, governance_checklist
        work_item_id = message.payload.get("work_item_id")
        if work_item_id is None or str(work_item_id) == "":
            return None, None
        return self.work_item_governance_context.load_for_work_item(str(work_item_id))

    def _conversation_summary(self, message: AgentMessage) -> str:
        conversation_ref = message.payload.get("conversation_ref")
        if conversation_ref is None or str(conversation_ref) == "":
            return ""
        return self.conversation_context.load_recent(str(conversation_ref))


def _operational_context_summary(
    snapshot: ReportingSnapshot,
    *,
    role_instance_id: str,
    role_id: str,
    max_work_items: int,
    max_agents: int,
    max_recent_completions: int,
) -> str:
    lines = [
        f"Project: {snapshot.project_id}",
        f"Current role: {role_id} ({role_instance_id})",
        (
            "Use this section as read-only operational evidence. "
            "If action is needed, call safe-output tools rather than claiming state changed."
        ),
    ]
    lines.append(
        "Counts: "
        f"backlog={len(snapshot.backlog)}, active_work={len(snapshot.work_items)}, "
        f"agents={len(snapshot.agents)}, recent_completions={len(snapshot.recent_completions)}"
    )
    if snapshot.backlog:
        lines.append("Backlog / queue:")
        for item in snapshot.backlog[:max_work_items]:
            linked = f", linked_work={item.linked_work_item_id}" if item.linked_work_item_id else ""
            lines.append(
                "- "
                f"{item.queue_item_id} [{item.status}] owner={item.owner_role}{linked}: "
                f"{_truncate(item.title, 140)}"
            )
    else:
        lines.append("Backlog / queue: no open items.")
    if snapshot.work_items:
        lines.append("Open work:")
        for item in snapshot.work_items[:max_work_items]:
            attention = f", attention={_truncate(item.attention_reason, 100)}" if item.attention_reason else ""
            lines.append(
                "- "
                f"{item.work_item_id} [{item.state}] owner={item.owner_role}{attention}: "
                f"{_truncate(item.title, 120)}; next={_truncate(item.next_action, 180)}"
            )
    else:
        lines.append("Open work: no active work items.")
    if snapshot.agents:
        lines.append("Agent health:")
        for agent in snapshot.agents[:max_agents]:
            lifecycle = ""
            if agent.last_lifecycle_error and agent_has_actionable_lifecycle_alert(agent):
                lifecycle = (
                    f", lifecycle={agent.last_lifecycle_action or 'unknown'}"
                    f"/{agent.last_lifecycle_exit_code}: {_truncate(agent.last_lifecycle_error, 120)}"
                )
            waits = f", waits={_truncate('; '.join(agent.governance_waits), 120)}" if agent.governance_waits else ""
            session = f", session={agent.session_mode}" if agent.session_mode else ""
            lines.append(
                "- "
                f"{agent.role_instance_id} state={agent.container_state} inbox={agent.inbox_depth} "
                f"dead={agent.dead_letter_depth} current={agent.current_work or 'none'}"
                f"{session}{waits}{lifecycle}"
            )
    else:
        lines.append("Agent health: no agent status rows recorded.")
    if snapshot.recent_completions:
        lines.append("Recent completions:")
        for item in snapshot.recent_completions[:max_recent_completions]:
            lines.append(
                "- "
                f"{item.work_item_id} [{item.state}] owner={item.owner_role}: "
                f"{_truncate(item.title, 120)}"
            )
    return "\n".join(lines)


def _message_work_ref(payload: dict[str, object]) -> str | None:
    value = payload.get("work_item_id") or payload.get("source_message_id")
    return None if value is None else str(value)


def _message_work_item_id(payload: dict[str, object]) -> str | None:
    value = payload.get("work_item_id")
    if value is None or str(value).strip() == "":
        return None
    return str(value)


def _payload_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        return None
    return str(value)


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _message_correlation_id(payload: dict[str, object]) -> str | None:
    return _payload_text(payload, "correlation_id") or _payload_text(payload, "source_message_id")


def _agent_failure_reply_text(*, role_id: str, work_item_id: str | None, message_id: str, error: str) -> str:
    work_line = f"\n\nWork item: `{work_item_id}`" if work_item_id else ""
    return (
        f"**{role_id} could not complete the requested action.**"
        f"{work_line}\n\n"
        f"Message: `{message_id}`\n\n"
        f"Problem: {error}\n\n"
        "The runtime recorded this as an agent-delivery failure. "
        "Retry after the worker/tool wiring issue is corrected, or route the item to Project Manager for recovery."
    )


def _message_memory_version(memory_summary: str) -> str:
    return hashlib.sha256(memory_summary.encode("utf-8")).hexdigest()


def _reply_routing_prompt_section(payload: dict[str, object]) -> str:
    target_ref = _optional_prompt_value(payload.get("reply_target_ref"))
    thread_ref = _optional_prompt_value(payload.get("reply_thread_ref"))
    connector = _optional_prompt_value(payload.get("connector"))
    if target_ref is None:
        return "<reply-routing>No source reply route was provided.</reply-routing>"
    lines = [
        "<reply-routing>",
        "Use this source reply route for conversational `status.reply` unless a deliberate override is required.",
        f"<connector>{connector or 'unspecified'}</connector>",
        f"<reply-target-ref>{target_ref}</reply-target-ref>",
    ]
    if thread_ref is not None:
        lines.append(f"<reply-thread-ref>{thread_ref}</reply-thread-ref>")
    lines.append("</reply-routing>")
    return "\n".join(lines)


def _relevance_check_prompt_section(message: AgentMessage) -> str:
    route_type = _optional_prompt_value(message.payload.get("route_type"))
    if route_type not in {"project_channel_relevance_check", "team_wide_relevance_check"}:
        return "<relevance-check>Not a relevance-check assignment.</relevance-check>"
    source_message_id = _optional_prompt_value(message.payload.get("source_message_id")) or message.message_id
    conversation_ref = _optional_prompt_value(message.payload.get("conversation_ref")) or "unspecified"
    return "\n".join(
        [
            "<relevance-check>",
            f"<route-type>{route_type}</route-type>",
            f"<source-message-id>{source_message_id}</source-message-id>",
            f"<conversation-ref>{conversation_ref}</conversation-ref>",
            "This is a lightweight project-channel relevance check, not automatic durable work.",
            "First decide whether your role has material specialist input for this message.",
            "Always call `relevance.record` with `source_message_id`, `conversation_ref`, `relevance_score` from 0 to 100, and `rationale`.",
            "If you have useful specialist input for the human/channel, call `status.reply` using the provided reply-routing context.",
            "If you have no useful specialist input, do not send a stakeholder/channel reply; finish with `status.complete` summarising that no response was needed.",
            "If the message reveals durable work, use the appropriate backlog/work/governance tools and then confirm what you did.",
            "</relevance-check>",
        ]
    )


def _agent_delegation_prompt_section(message: AgentMessage) -> str:
    if _optional_prompt_value(message.payload.get("message_type")) != "agent.delegate":
        return "<agent-delegation>Not an agent-delegation assignment.</agent-delegation>"
    source_role = _optional_prompt_value(message.payload.get("source_role")) or "unspecified"
    source_role_instance_id = _optional_prompt_value(message.payload.get("source_role_instance_id")) or "unspecified"
    task = _optional_prompt_value(message.payload.get("task")) or "unspecified"
    reason = _optional_prompt_value(message.payload.get("reason")) or "unspecified"
    expected_output = _optional_prompt_value(message.payload.get("expected_output")) or "unspecified"
    work_item_id = _optional_prompt_value(message.payload.get("work_item_id"))
    reply_target_ref = _optional_prompt_value(message.payload.get("reply_target_ref"))
    return_target = (
        "If a reply_target_ref is supplied, use status.reply to report the result to that route. "
        "If no stakeholder reply route is supplied, use informed.update to report concise results back "
        f"to `{source_role}` and handoff.require only when you must transfer formal lifecycle ownership."
    )
    lines = [
        "<agent-delegation>",
        "This is a focused role-to-role delegation, not automatic formal lifecycle ownership.",
        f"<source-role>{source_role}</source-role>",
        f"<source-role-instance>{source_role_instance_id}</source-role-instance>",
        f"<task>{task}</task>",
        f"<reason>{reason}</reason>",
        f"<expected-output>{expected_output}</expected-output>",
    ]
    if work_item_id:
        lines.append(f"<work-item-id>{work_item_id}</work-item-id>")
    if reply_target_ref:
        lines.append(f"<reply-target-ref>{reply_target_ref}</reply-target-ref>")
    lines.extend(
        [
            return_target,
            "Do the delegated task through safe-output tools first, then send the required reply/status.",
            "</agent-delegation>",
        ]
    )
    return "\n".join(lines)


def _optional_prompt_value(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value)


def _truncate(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _has_terminal_tool_call(tool_calls: list[str]) -> bool:
    for call in tool_calls:
        text = str(call)
        if text.startswith("terminal:"):
            return True
        if _tool_call_name(text) in TERMINAL_TOOL_NAMES:
            return True
    return False


def _tool_call_name(call: object) -> str:
    text = str(call)
    if text.startswith("terminal:"):
        text = text[len("terminal:") :]
    return text.split(":", 1)[0]


def _read_optional(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _append_optional_section(lines: list[str], name: str, content: str | None) -> None:
    if not content:
        return
    lines.extend([f"<{name}>", content, f"</{name}>"])


def _conversation_row_summary(row: dict[str, object]) -> str:
    thread = row.get("thread_ref")
    thread_text = f", thread: {thread}" if thread else ""
    mentioned_roles = row.get("mentioned_roles") or ()
    mentions_text = f", mentions: {', '.join(str(role) for role in mentioned_roles)}" if mentioned_roles else ""
    expired_text = ", expired=true" if row.get("raw_expired_at") else ""
    return (
        f"- [{row.get('connector')}/{row.get('source_type')}] {row.get('sender_ref')}: "
        f"{row.get('text')} (message: {row.get('message_id')}{thread_text}{mentions_text}{expired_text})"
    )


def _conversation_summary_row(row: dict[str, object]) -> str:
    source_ids = ", ".join(str(item) for item in row.get("source_message_ids") or ()) or "none"
    durable_refs = ", ".join(str(item) for item in row.get("durable_refs") or ()) or "none"
    return (
        f"- [{row.get('visibility')}] {row.get('summary')} "
        f"(summary: {row.get('summary_id')}, sources: {source_ids}, durable_refs: {durable_refs}, "
        f"by: {row.get('created_by_role')})"
    )
