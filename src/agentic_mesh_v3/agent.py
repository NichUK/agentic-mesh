from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Protocol

from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import GovernanceInstructionSet
from agentic_mesh_v3.memory import SQLiteRoleMemory


@dataclass(frozen=True)
class RoleInstanceConfig:
    project_id: str
    role_id: str
    instance_id: str
    role_prompt_path: Path
    memory_db_path: Path
    inbox_stream: str
    inbox_consumer: str

    @property
    def role_instance_id(self) -> str:
        return f"{self.project_id}.{self.role_id}.{self.instance_id}"


@dataclass(frozen=True)
class AgentMessage:
    message_id: str
    subject: str
    payload: dict[str, object]


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


class InMemoryRoleMemory:
    def __init__(self) -> None:
        self._memory: dict[str, list[str]] = {}

    def load_summary(self, role_instance_id: str) -> str:
        return "\n".join(self._memory.get(role_instance_id, []))

    def record_observation(self, role_instance_id: str, observation: str) -> None:
        self._memory.setdefault(role_instance_id, []).append(observation)


def build_role_memory(config: RoleInstanceConfig) -> AgentMemory:
    return SQLiteRoleMemory(config.memory_db_path)


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
    governance_instructions: GovernanceInstructionSet = field(default_factory=GovernanceInstructionSet)

    def run_once(self, *, governance_context: GovernanceContext | None = None) -> AgentRunResult | None:
        self.broker.ensure_consumer(
            self.config.inbox_stream,
            self.config.inbox_consumer,
            filter_subject=f"agent.{self.config.role_id}",
        )
        messages = self.broker.fetch(self.config.inbox_stream, self.config.inbox_consumer, batch=1)
        if not messages:
            return None
        message = messages[0]
        agent_message = AgentMessage(
            message_id=message.message_id,
            subject=message.subject,
            payload=message.payload,
        )
        prompt = self._build_prompt(agent_message, governance_context=governance_context)
        try:
            tool_calls = self.worker.run(prompt, agent_message)
            if not tool_calls:
                raise ValueError("agent did not call any tool")
            self.memory.record_observation(
                self.config.role_instance_id,
                f"{datetime.now(timezone.utc).isoformat()} processed {message.message_id} with {len(tool_calls)} tool calls",
            )
            self.broker.ack(self.config.inbox_stream, self.config.inbox_consumer, message.message_id)
            return AgentRunResult(message_id=message.message_id, status="completed", tool_calls=tuple(tool_calls))
        except Exception as exc:
            self.broker.nack(
                self.config.inbox_stream,
                self.config.inbox_consumer,
                message.message_id,
                reason=str(exc),
            )
            return AgentRunResult(message_id=message.message_id, status="failed", error=str(exc))

    def _build_prompt(
        self, message: AgentMessage, *, governance_context: GovernanceContext | None = None
    ) -> str:
        role_prompt = self.config.role_prompt_path.read_text(encoding="utf-8")
        memory_summary = self.memory.load_summary(self.config.role_instance_id)
        lines = [
            "<agentic-mesh-v3-agent>",
            f"<role-instance>{self.config.role_instance_id}</role-instance>",
            "<role>",
            role_prompt,
            "</role>",
            self.governance_instructions.as_prompt_section(),
        ]
        if governance_context is not None:
            lines.extend(
                [
                    "<governance-context>",
                    str(governance_context.handoff_requirements()),
                    "</governance-context>",
                ]
            )
        lines.extend(
            [
                "<memory>",
                memory_summary or "No prior role memory recorded.",
                "</memory>",
                "<assignment>",
                str(message.payload),
                "</assignment>",
                "</agentic-mesh-v3-agent>",
            ]
        )
        return "\n".join(lines)
