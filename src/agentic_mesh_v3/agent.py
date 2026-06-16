from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Protocol

from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.governance import GovernanceChecklist
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import GovernanceInstructionSet
from agentic_mesh_v3.governance import evaluate_governance_checklist
from agentic_mesh_v3.memory import SQLiteRoleMemory
from agentic_mesh_v3.reporting import AgentStatus


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

    def record_observation(self, role_instance_id: str, observation: str) -> None:
        """Record a source-linked memory observation."""


class ConversationContext(Protocol):
    def load_recent(self, conversation_ref: str) -> str:
        """Load recent conversation context for a connector conversation."""


class WorkItemGovernanceContextProvider(Protocol):
    def load_for_work_item(self, work_item_id: str) -> tuple[GovernanceContext | None, GovernanceChecklist | None]:
        """Load governance context/checklist for a work item."""


class AgentStatusReporter(Protocol):
    def report(self, status: AgentStatus) -> None:
        """Publish current agent status to the runtime read model."""


class NullAgentStatusReporter:
    def report(self, status: AgentStatus) -> None:
        return


class DatabaseAgentStatusReporter:
    def __init__(self, db: object) -> None:
        self.db = db

    def report(self, status: AgentStatus) -> None:
        self.db.upsert_agent_status(status)  # type: ignore[attr-defined]


class InMemoryRoleMemory:
    def __init__(self) -> None:
        self._memory: dict[str, list[str]] = {}

    def load_summary(self, role_instance_id: str) -> str:
        return "\n".join(self._memory.get(role_instance_id, []))

    def record_observation(self, role_instance_id: str, observation: str) -> None:
        self._memory.setdefault(role_instance_id, []).append(observation)


def build_role_memory(config: RoleInstanceConfig) -> AgentMemory:
    return SQLiteRoleMemory(config.memory_db_path)


class NullConversationContext:
    def load_recent(self, conversation_ref: str) -> str:
        return ""


class DatabaseConversationContext:
    def __init__(self, db: object, *, limit: int = 10) -> None:
        self.db = db
        self.limit = limit

    def load_recent(self, conversation_ref: str) -> str:
        rows = self.db.list_conversation_messages(conversation_ref, limit=self.limit)  # type: ignore[attr-defined]
        return "\n".join(_conversation_row_summary(row) for row in rows)


class NullWorkItemGovernanceContextProvider:
    def load_for_work_item(self, work_item_id: str) -> tuple[GovernanceContext | None, GovernanceChecklist | None]:
        del work_item_id
        return None, None


class DatabaseWorkItemGovernanceContextProvider:
    def __init__(self, db: object) -> None:
        self.db = db

    def load_for_work_item(self, work_item_id: str) -> tuple[GovernanceContext | None, GovernanceChecklist | None]:
        context = self.db.work_item_governance_context(work_item_id)  # type: ignore[attr-defined]
        checklist = self.db.work_item_governance_checklist(work_item_id)  # type: ignore[attr-defined]
        return context, checklist


class EchoWorker:
    """Tiny worker for contract tests; production uses Codex or other adapters."""

    def run(self, prompt: str, message: AgentMessage) -> list[str]:
        if not prompt:
            raise ValueError("prompt is required")
        return [f"status.report_progress:{message.message_id}"]


@dataclass
class RoleAgentService:
    config: RoleInstanceConfig
    broker: BrokerAdapter
    worker: AgentWorker
    memory: AgentMemory
    conversation_context: ConversationContext = field(default_factory=NullConversationContext)
    work_item_governance_context: WorkItemGovernanceContextProvider = field(
        default_factory=NullWorkItemGovernanceContextProvider
    )
    governance_instructions: GovernanceInstructionSet = field(default_factory=GovernanceInstructionSet)
    status_reporter: AgentStatusReporter = field(default_factory=NullAgentStatusReporter)
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
        self._report_status(container_state="running", current_work=_message_work_ref(message.payload))
        agent_message = AgentMessage(
            message_id=message.message_id,
            subject=message.subject,
            payload=message.payload,
        )
        prompt_governance_context, prompt_governance_checklist = self._governance_for_message(
            agent_message,
            governance_context=governance_context,
            governance_checklist=governance_checklist,
        )
        prompt = self._build_prompt(
            agent_message,
            governance_context=prompt_governance_context,
            governance_checklist=prompt_governance_checklist,
        )
        try:
            tool_calls = self.worker.run(prompt, agent_message)
            if not tool_calls:
                raise ValueError("agent did not call any tool")
            self.memory.record_observation(
                self.config.role_instance_id,
                f"{datetime.now(timezone.utc).isoformat()} processed {message.message_id} with {len(tool_calls)} tool calls",
            )
            self.broker.ack(self.config.inbox_stream, claimed.consumer, message.message_id)
            self._report_status(container_state="running", current_work=None)
            return AgentRunResult(message_id=message.message_id, status="completed", tool_calls=tuple(tool_calls))
        except Exception as exc:
            if message.delivery_count + 1 >= self.max_delivery_attempts:
                self.broker.dead_letter(
                    self.config.inbox_stream,
                    claimed.consumer,
                    message.message_id,
                    reason=str(exc),
                )
                status = "dead_lettered"
            else:
                self.broker.nack(
                    self.config.inbox_stream,
                    claimed.consumer,
                    message.message_id,
                    reason=str(exc),
                )
                status = "failed"
            self._report_status(container_state="running", current_work=None, governance_waits=(str(exc),))
            return AgentRunResult(message_id=message.message_id, status=status, error=str(exc))

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

    def _consumer_subjects(self) -> tuple[tuple[str, str], ...]:
        return (
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
                inbox_depth=self.broker.depth(self.config.inbox_stream).pending,
                dead_letter_depth=len(self.broker.dead_letters(self.config.inbox_stream)),
                governance_waits=governance_waits,
            )
        )

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
            lines.extend(
                [
                    "<governance-context>",
                    str(governance_context.handoff_requirements()),
                    "</governance-context>",
                ]
            )
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


def _message_work_ref(payload: dict[str, object]) -> str | None:
    value = payload.get("work_item_id") or payload.get("source_message_id")
    return None if value is None else str(value)


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
    return (
        f"- [{row.get('connector')}/{row.get('source_type')}] {row.get('sender_ref')}: "
        f"{row.get('text')} (message: {row.get('message_id')}{thread_text})"
    )
