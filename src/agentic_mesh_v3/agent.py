from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.governance import GovernanceChecklist
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import GovernanceInstructionSet
from agentic_mesh_v3.governance import evaluate_governance_checklist
from agentic_mesh_v3.memory import SQLiteRoleMemory
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.tool_contracts import DO_TOOLS
from agentic_mesh_v3.tool_contracts import REPLY_TOOLS


TERMINAL_TOOL_NAMES = {"status.reply", "status.complete", "noop", "report.incomplete"}


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


class WorkItemGovernanceContextProvider(Protocol):
    def load_for_work_item(self, work_item_id: str) -> tuple[GovernanceContext | None, GovernanceChecklist | None]:
        """Load governance context/checklist for a work item."""


class AgentStatusReporter(Protocol):
    def report(self, status: AgentStatus) -> None:
        """Publish current agent status to the runtime read model."""


class TerminalToolCallAudit(Protocol):
    def snapshot(self, role_instance_id: str) -> object:
        """Capture terminal tool-call state before a worker run."""

    def verify_terminal_call(self, role_instance_id: str, before: object, tool_calls: list[str]) -> tuple[str, ...]:
        """Verify the run recorded terminal calls and return audited call ids/names."""


class AgentRunRecorder(Protocol):
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


class NullAgentStatusReporter:
    def report(self, status: AgentStatus) -> None:
        return


class NullAgentRunRecorder:
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


class NullTerminalToolCallAudit:
    def snapshot(self, role_instance_id: str) -> object:
        del role_instance_id
        return None

    def verify_terminal_call(self, role_instance_id: str, before: object, tool_calls: list[str]) -> tuple[str, ...]:
        del role_instance_id, before
        if not _has_terminal_tool_call(tool_calls):
            raise ValueError("agent did not call a terminal safe-output tool")
        if not any(_tool_call_name(call) in DO_TOOLS for call in tool_calls):
            raise ValueError("agent did not call a DO safe-output tool")
        if not any(_tool_call_name(call) in REPLY_TOOLS for call in tool_calls):
            raise ValueError("agent did not call a REPLY safe-output tool")
        return tuple(tool_calls)


class DatabaseTerminalToolCallAudit:
    def __init__(self, db: object) -> None:
        self.db = db

    def snapshot(self, role_instance_id: str) -> frozenset[str]:
        return frozenset(self._tool_call_ids(role_instance_id))

    def verify_terminal_call(self, role_instance_id: str, before: object, tool_calls: list[str]) -> tuple[str, ...]:
        del tool_calls
        before_ids = set(before) if isinstance(before, (frozenset, set)) else set()
        new_calls = self._new_tool_calls(role_instance_id, before_ids)
        if not any(bool(row.get("terminal")) for row in new_calls):
            raise ValueError("agent did not record a terminal safe-output tool call")
        if not any(str(row.get("tool_name")) in DO_TOOLS for row in new_calls):
            raise ValueError("agent did not record a DO safe-output tool call")
        if not any(str(row.get("tool_name")) in REPLY_TOOLS for row in new_calls):
            raise ValueError("agent did not record a REPLY safe-output tool call")
        return tuple(str(row["call_id"]) for row in new_calls)

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


class DatabaseAgentStatusReporter:
    def __init__(self, db: object) -> None:
        self.db = db

    def report(self, status: AgentStatus) -> None:
        self.db.upsert_agent_status(status)  # type: ignore[attr-defined]


class DatabaseAgentRunRecorder:
    def __init__(self, db: object) -> None:
        self.db = db

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
        return [f"noop:{message.message_id}", f"status.complete:{message.message_id}"]


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
    terminal_tool_call_audit: TerminalToolCallAudit = field(default_factory=NullTerminalToolCallAudit)
    run_recorder: AgentRunRecorder = field(default_factory=NullAgentRunRecorder)
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
        run_id = f"run-{uuid4().hex}"
        run_started_at = datetime.now(timezone.utc).isoformat()
        try:
            terminal_audit_snapshot = self.terminal_tool_call_audit.snapshot(self.config.role_instance_id)
            tool_calls = self.worker.run(prompt, agent_message)
            if not tool_calls:
                raise ValueError("agent did not call any tool")
            audited_tool_calls = self.terminal_tool_call_audit.verify_terminal_call(
                self.config.role_instance_id,
                terminal_audit_snapshot,
                tool_calls,
            )
            self.memory.record_observation(
                self.config.role_instance_id,
                f"{datetime.now(timezone.utc).isoformat()} processed {message.message_id} with {len(audited_tool_calls)} audited tool calls",
                source_ref=_message_memory_source_ref(message),
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
                audited_tool_calls = ()
            if audited_tool_calls:
                self.memory.record_observation(
                    self.config.role_instance_id,
                    (
                        f"{datetime.now(timezone.utc).isoformat()} processed {message.message_id} "
                        f"with {len(audited_tool_calls)} audited tool calls after worker error: {exc}"
                    ),
                    source_ref=_message_memory_source_ref(message),
                )
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
            else:
                self.broker.nack(
                    self.config.inbox_stream,
                    claimed.consumer,
                    message.message_id,
                    reason=str(exc),
                )
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
        reply_routing = _reply_routing_prompt_section(message.payload)
        relevance_guidance = _relevance_check_prompt_section(message)
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
                reply_routing,
                relevance_guidance,
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


def _message_work_item_id(payload: dict[str, object]) -> str | None:
    value = payload.get("work_item_id")
    if value is None or str(value).strip() == "":
        return None
    return str(value)


def _message_memory_source_ref(message: BrokerMessage) -> str:
    for key, prefix in (
        ("work_item_id", "work-item"),
        ("conversation_ref", "conversation"),
        ("source_message_id", "source-message"),
    ):
        value = message.payload.get(key)
        if value is not None and str(value).strip():
            return f"{prefix}:{value}"
    return f"broker-message:{message.message_id}"


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


def _optional_prompt_value(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value)


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
