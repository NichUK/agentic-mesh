from pathlib import Path

from agentic_mesh_v3.agent import DatabaseAgentStatusReporter
from agentic_mesh_v3.agent import DatabaseAgentFailureReporter
from agentic_mesh_v3.agent import DatabaseAgentPromptRecorder
from agentic_mesh_v3.agent import DatabaseAgentRunRecorder
from agentic_mesh_v3.agent import DatabaseConversationContext
from agentic_mesh_v3.agent import DatabaseOperationalContext
from agentic_mesh_v3.agent import DatabaseTerminalToolCallAudit
from agentic_mesh_v3.agent import DatabaseWorkItemGovernanceContextProvider
from agentic_mesh_v3.agent import DatabaseWorkItemStateProvider
from agentic_mesh_v3.agent import EchoWorker
from agentic_mesh_v3.agent import InMemoryRoleMemory
from agentic_mesh_v3.agent import RoleAgentService
from agentic_mesh_v3.agent import RoleInstanceConfig
from agentic_mesh_v3.agent import build_role_memory
from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.connectors import DeliveryReceipt
from agentic_mesh_v3.connectors import OutboundMessage
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import evaluate_governance_checklist
from agentic_mesh_v3.memory import DatabaseRoleMemory
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.tools import V3ToolService


class NoToolWorker:
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        return []


class NonTerminalWorker:
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        return ["status.update"]


class ReplyOnlySymbolicWorker:
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        return [f"status.reply:{message.message_id}"]


class DoOnlySymbolicWorker:
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        return [f"noop:{message.message_id}"]


class CapturingWorker:
    def __init__(self) -> None:
        self.prompt = ""
        self.message_payload = {}

    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        self.prompt = prompt
        self.message_payload = dict(message.payload)
        return [f"noop:{message.message_id}", f"status.reply:{message.message_id}"]


class FailingIfCalledWorker:
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        del prompt, message
        raise AssertionError("worker should not be called")


class FakeStakeholderBridge:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> DeliveryReceipt:
        self.messages.append(message)
        return DeliveryReceipt(
            delivery_id=f"delivery-{len(self.messages)}",
            connector=message.connector,
            target_ref=message.target_ref,
            thread_ref=message.thread_ref,
        )


class RecordingTerminalWorker:
    def __init__(self, tools: V3ToolService, role_instance_id: str) -> None:
        self.tools = tools
        self.role_instance_id = role_instance_id

    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        del prompt
        do_result = self.tools.call(
            role_instance_id=self.role_instance_id,
            tool_name="noop",
            payload={"reason": "No durable state change needed for the test message."},
        )
        reply_result = self.tools.call(
            role_instance_id=self.role_instance_id,
            tool_name="status.reply",
            payload={"text_markdown": f"Processed {message.message_id}"},
        )
        return [do_result.call_id, reply_result.call_id]


class RecordingTerminalWorkerWithClaimMismatch(RecordingTerminalWorker):
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        super().run(prompt, message)
        return ["terminal:status.reply", "call-claimed-but-not-recorded"]


class RecordingTerminalThenFailWorker(RecordingTerminalWorker):
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        super().run(prompt, message)
        raise RuntimeError("worker failed after recording safe outputs")


class RecordingTerminalThenMalformedStdoutWorker(RecordingTerminalWorker):
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        super().run(prompt, message)
        raise ValueError("worker subprocess stdout must be JSON")


class RecordingReplyOnlyWorker:
    def __init__(self, tools: V3ToolService, role_instance_id: str) -> None:
        self.tools = tools
        self.role_instance_id = role_instance_id

    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        del prompt
        result = self.tools.call(
            role_instance_id=self.role_instance_id,
            tool_name="status.reply",
            payload={"text_markdown": f"Processed {message.message_id}"},
        )
        return [result.call_id]


class RecordingDoOnlyWorker:
    def __init__(self, tools: V3ToolService, role_instance_id: str) -> None:
        self.tools = tools
        self.role_instance_id = role_instance_id

    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        del prompt, message
        result = self.tools.call(
            role_instance_id=self.role_instance_id,
            tool_name="noop",
            payload={"reason": "Nothing durable to do."},
        )
        return [result.call_id]


class RecordingIncompleteOnlyWorker:
    def __init__(self, tools: V3ToolService, role_instance_id: str) -> None:
        self.tools = tools
        self.role_instance_id = role_instance_id

    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        del prompt
        result = self.tools.call(
            role_instance_id=self.role_instance_id,
            tool_name="report.incomplete",
            payload={"reason": f"Cannot complete {message.message_id} without missing context."},
        )
        return [result.call_id]


class FakeStatusReporter:
    def __init__(self) -> None:
        self.statuses = []

    def report(self, status):  # type: ignore[no-untyped-def]
        self.statuses.append(status)


class FakeOperationalContext:
    def load(self, *, project_id: str, role_instance_id: str, role_id: str) -> str:
        return (
            f"Project: {project_id}\n"
            f"Current role: {role_id} ({role_instance_id})\n"
            "Open work:\n"
            "- work-ops [waiting_agent] owner=delivery-manager: proceed with implementation."
        )


def _config(tmp_path: Path) -> RoleInstanceConfig:
    role_prompt = tmp_path / "role.md"
    role_prompt.write_text("You are Product Manager.", encoding="utf-8")
    return RoleInstanceConfig(
        project_id="agentic-mesh-dev",
        role_id="product-manager",
        instance_id="1",
        role_prompt_path=role_prompt,
        memory_db_path=tmp_path / "memory.sqlite3",
        inbox_stream="agent-inbox",
        inbox_consumer="pm-1",
    )


def test_role_agent_processes_inbox_without_polluting_role_memory(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    memory = InMemoryRoleMemory()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=EchoWorker(),
        memory=memory,
    )
    context = GovernanceContext.from_assignment(
        work_item_id="work-123",
        assignment=DEFAULT_SDLC_RACI.for_phase("requirements"),
    )

    result = service.run_once(governance_context=context)

    assert result is not None
    assert result.status == "completed"
    assert result.tool_calls
    assert memory.load_summary("agentic-mesh-dev.product-manager.1") == ""


def test_role_agent_reports_current_work_and_idle_status(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-123"})
    reporter = FakeStatusReporter()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=EchoWorker(),
        memory=InMemoryRoleMemory(),
        status_reporter=reporter,
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    assert [status.current_work for status in reporter.statuses] == [None, "work-123", None]
    assert reporter.statuses[-1].inbox_depth == 0


def test_role_agent_reports_only_role_specific_inbox_depth(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "agent.engineering"])
    broker.publish("agent-inbox", "agent.engineering", {"work_item_id": "work-eng"})
    reporter = FakeStatusReporter()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=EchoWorker(),
        memory=InMemoryRoleMemory(),
        status_reporter=reporter,
    )

    result = service.run_once()

    assert result is None
    assert reporter.statuses[-1].inbox_depth == 0


def test_role_agent_counts_direct_and_relevance_inbox_depth(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "agent.product-manager.relevance"])
    broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-1"})
    broker.publish("agent-inbox", "agent.product-manager.relevance", {"source_message_id": "msg-1"})
    reporter = FakeStatusReporter()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=EchoWorker(),
        memory=InMemoryRoleMemory(),
        status_reporter=reporter,
    )

    service.run_once()

    assert reporter.statuses[0].inbox_depth == 2
    assert reporter.statuses[-1].inbox_depth == 1


def test_role_agent_run_until_idle_processes_available_messages(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-1"})
    broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-2"})
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=EchoWorker(),
        memory=InMemoryRoleMemory(),
    )

    results = service.run_until_idle(max_messages=5)

    assert [result.status for result in results] == ["completed", "completed"]
    assert broker.depth("agent-inbox").pending == 0


def test_role_agent_processes_relevance_inbox_messages(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "agent.product-manager.relevance"])
    published = broker.publish(
        "agent-inbox",
        "agent.product-manager.relevance",
        {"source_message_id": "msg-1", "route_type": "project_channel_relevance_check"},
    )
    worker = CapturingWorker()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    assert result.message_id == published.message_id
    assert "<subject>agent.product-manager.relevance</subject>" in worker.prompt
    assert "project_channel_relevance_check" in worker.prompt
    assert "<relevance-check>" in worker.prompt
    assert "Always call `relevance.record`" in worker.prompt
    assert "do not send a stakeholder/channel reply" in worker.prompt
    assert broker.depth("agent-inbox").pending == 0


def test_role_agent_prompt_marks_non_relevance_assignments(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    worker = CapturingWorker()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    assert "<relevance-check>Not a relevance-check assignment.</relevance-check>" in worker.prompt


def test_role_agent_passes_role_identity_to_worker_message(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    worker = CapturingWorker()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    assert worker.message_payload["project_id"] == "agentic-mesh-dev"
    assert worker.message_payload["role_id"] == "product-manager"
    assert worker.message_payload["role_instance_id"] == "agentic-mesh-dev.product-manager.1"


def test_role_agent_prompt_explains_agent_delegation_assignments(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish(
        "agent-inbox",
        "agent.product-manager",
        {
            "message_type": "agent.delegate",
            "source_role": "project-manager",
            "source_role_instance_id": "agentic-mesh-dev.project-manager.1",
            "task": "Inspect delivery-manager inbox health.",
            "reason": "Project Manager is diagnosing a stalled sponsor request.",
            "expected_output": "Concise finding and recommended next action.",
            "work_item_id": "work-ops",
        },
    )
    worker = CapturingWorker()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    assert "<agent-delegation>" in worker.prompt
    assert "This is a focused role-to-role delegation" in worker.prompt
    assert "<source-role>project-manager</source-role>" in worker.prompt
    assert "<source-role-instance>agentic-mesh-dev.project-manager.1</source-role-instance>" in worker.prompt
    assert "<task>Inspect delivery-manager inbox health.</task>" in worker.prompt
    assert "<expected-output>Concise finding and recommended next action.</expected-output>" in worker.prompt
    assert "<work-item-id>work-ops</work-item-id>" in worker.prompt
    assert "use informed.update to report concise results back to `project-manager`" in worker.prompt
    assert "handoff.require only when you must transfer formal lifecycle ownership" in worker.prompt


def test_role_agent_prompt_marks_non_delegation_assignments(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    worker = CapturingWorker()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    assert "<agent-delegation>Not an agent-delegation assignment.</agent-delegation>" in worker.prompt


def test_role_agent_prioritizes_direct_messages_over_relevance_checks(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "agent.product-manager.relevance"])
    relevance = broker.publish("agent-inbox", "agent.product-manager.relevance", {"source_message_id": "msg-1"})
    direct = broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-1"})
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=EchoWorker(),
        memory=InMemoryRoleMemory(),
    )

    first = service.run_once()
    second = service.run_once()

    assert first is not None
    assert first.message_id == direct.message_id
    assert second is not None
    assert second.message_id == relevance.message_id
    assert broker.depth("agent-inbox").pending == 0


def test_role_agent_prioritizes_stakeholder_dm_over_normal_inbox_work(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager", "agent.product-manager.priority"])
    normal = broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-1"})
    priority = broker.publish(
        "agent-inbox",
        "agent.product-manager.priority",
        {"source_type": "dm", "text": "Give me a status update."},
    )
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=EchoWorker(),
        memory=InMemoryRoleMemory(),
    )

    first = service.run_once()
    second = service.run_once()

    assert first is not None
    assert first.message_id == priority.message_id
    assert second is not None
    assert second.message_id == normal.message_id
    assert broker.depth("agent-inbox").pending == 0


def test_database_status_reporter_updates_agent_read_model(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-123"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=EchoWorker(),
            memory=InMemoryRoleMemory(),
            status_reporter=DatabaseAgentStatusReporter(db),
        )

        service.run_once()

        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
        assert snapshot.agents[0].role_instance_id == "agentic-mesh-dev.product-manager.1"
        assert snapshot.agents[0].container_state == "running"
    finally:
        db.close()


def test_role_agent_can_use_configured_sqlite_memory_without_auto_run_logs(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status", "work_item_id": "work-123"})
    config = _config(tmp_path)
    memory = build_role_memory(config)
    service = RoleAgentService(
        config=config,
        broker=broker,
        worker=EchoWorker(),
        memory=memory,
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    summary = memory.load_summary("agentic-mesh-dev.product-manager.1")
    assert summary == ""


def test_role_agent_does_not_copy_conversation_run_logs_into_role_memory(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish(
        "agent-inbox",
        "agent.product-manager",
        {"request": "status", "conversation_ref": "dm:product-manager"},
    )
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=EchoWorker(),
            memory=DatabaseRoleMemory(db),
        )

        result = service.run_once()
        records = db.list_role_memory("agentic-mesh-dev.product-manager.1")

        assert result is not None
        assert result.status == "completed"
        assert records == []
    finally:
        db.close()


def test_role_agent_does_not_copy_broker_run_logs_into_role_memory(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=EchoWorker(),
            memory=DatabaseRoleMemory(db),
        )

        result = service.run_once()
        records = db.list_role_memory("agentic-mesh-dev.product-manager.1")

        assert result is not None
        assert result.status == "completed"
        assert records == []
    finally:
        db.close()


def test_role_agent_prompt_includes_mounted_context_components(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish(
        "agent-inbox",
        "agent.product-manager",
        {
            "request": "shape this",
            "connector": "teams",
            "reply_target_ref": "team:team-1/channel:channel-1",
            "reply_thread_ref": "root-message-1",
        },
    )
    config = _config(tmp_path)
    organisation = tmp_path / "organisation.md"
    project = tmp_path / "project.md"
    system = tmp_path / "system.md"
    raci = tmp_path / "raci.json"
    tools = tmp_path / "tools.md"
    system.write_text("System instruction.", encoding="utf-8")
    organisation.write_text("Organisation instruction.", encoding="utf-8")
    project.write_text("Project instruction.", encoding="utf-8")
    raci.write_text('[{"phase":"requirements"}]', encoding="utf-8")
    tools.write_text("Use safe-output tools.", encoding="utf-8")
    config = RoleInstanceConfig(
        project_id=config.project_id,
        role_id=config.role_id,
        instance_id=config.instance_id,
        role_prompt_path=config.role_prompt_path,
        memory_db_path=config.memory_db_path,
        inbox_stream=config.inbox_stream,
        inbox_consumer=config.inbox_consumer,
        system_prompt_path=system,
        organisation_prompt_path=organisation,
        project_prompt_path=project,
        raci_path=raci,
        tools_prompt_path=tools,
    )
    worker = CapturingWorker()
    service = RoleAgentService(
        config=config,
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    assert "<system>" in worker.prompt
    assert "System instruction." in worker.prompt
    assert "<organisation>" in worker.prompt
    assert "Organisation instruction." in worker.prompt
    assert "<project>" in worker.prompt
    assert "Project instruction." in worker.prompt
    assert "<raci>" in worker.prompt
    assert '"phase":"requirements"' in worker.prompt
    assert "<available-tools>" in worker.prompt
    assert "Use safe-output tools." in worker.prompt
    assert "<message-metadata>" in worker.prompt
    assert f"<message-id>{published.message_id}</message-id>" in worker.prompt
    assert "<subject>agent.product-manager</subject>" in worker.prompt
    assert "<reply-routing>" in worker.prompt
    assert "Use this source reply route for conversational `status.reply`" in worker.prompt
    assert "<connector>teams</connector>" in worker.prompt
    assert "<reply-target-ref>team:team-1/channel:channel-1</reply-target-ref>" in worker.prompt
    assert "<reply-thread-ref>root-message-1</reply-thread-ref>" in worker.prompt
    assert '"request": "shape this"' in worker.prompt


def test_role_agent_prompt_marks_missing_reply_route(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "shape this"})
    worker = CapturingWorker()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    assert "<reply-routing>No source reply route was provided.</reply-routing>" in worker.prompt


def test_role_agent_prompt_includes_mesh_operational_context(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "what is happening?"})
    worker = CapturingWorker()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
        operational_context=FakeOperationalContext(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "completed"
    assert "<mesh-operational-context>" in worker.prompt
    assert "work-ops [waiting_agent] owner=delivery-manager" in worker.prompt
    assert "Current role: product-manager (agentic-mesh-dev.product-manager.1)" in worker.prompt


def test_database_operational_context_summarises_agent_lifecycle_alerts(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.release-manager.1",
                container_state="lifecycle_failed",
                heartbeat_at=None,
                inbox_depth=3,
                dead_letter_depth=1,
            )
        )
        db.record_agent_lifecycle_result(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            action="wake",
            service_name="agentic-mesh-dev-release-manager-1",
            command=("docker", "compose", "up", "-d", "--no-deps", "--no-recreate", "agentic-mesh-dev-release-manager-1"),
            working_directory=None,
            reason="pending inbox messages",
            exit_code=1,
            stdout="",
            stderr='Error response from daemon: Conflict. The container name "/runtime" is already in use.',
            executed=True,
        )

        summary = DatabaseOperationalContext(db).load(
            project_id="agentic-mesh-dev",
            role_instance_id="agentic-mesh-dev.product-manager.1",
            role_id="product-manager",
        )

        assert "Agent health:" in summary
        assert "agentic-mesh-dev.release-manager.1 state=lifecycle_failed inbox=3 dead=1" in summary
        assert "lifecycle=wake/1: Error response from daemon: Conflict" in summary
    finally:
        db.close()


def test_database_operational_context_suppresses_stale_lifecycle_alerts(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.business-analyst.1",
                container_state="lifecycle_failed",
                heartbeat_at=None,
                inbox_depth=0,
                dead_letter_depth=0,
            )
        )
        db.record_agent_lifecycle_result(
            role_instance_id="agentic-mesh-dev.business-analyst.1",
            action="wake",
            service_name="agentic-mesh-dev-business-analyst-1",
            command=("docker", "compose", "up", "-d", "--no-deps", "--no-recreate", "agentic-mesh-dev-business-analyst-1"),
            working_directory=None,
            reason="old pending inbox messages",
            exit_code=1,
            stdout="",
            stderr="Bind for 0.0.0.0:8100 failed: port is already allocated",
            executed=True,
        )

        summary = DatabaseOperationalContext(db).load(
            project_id="agentic-mesh-dev",
            role_instance_id="agentic-mesh-dev.project-manager.1",
            role_id="project-manager",
        )

        assert "agentic-mesh-dev.business-analyst.1 state=lifecycle_failed inbox=0 dead=0" in summary
        assert "Bind for 0.0.0.0:8100 failed" not in summary
        assert "lifecycle=wake/1" not in summary
    finally:
        db.close()


def test_role_agent_prompt_loads_runtime_database_role_memory(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.record_role_memory(
            memory_id="memory-preference",
            role_instance_id="agentic-mesh-dev.product-manager.1",
            summary="Sponsor prefers compact dashboard rows.",
            source_ref="work-123/index.md",
        )
        worker = CapturingWorker()
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=worker,
            memory=DatabaseRoleMemory(db),
        )

        result = service.run_once()

        assert result is not None
        assert result.status == "completed"
        assert "Sponsor prefers compact dashboard rows." in worker.prompt
        assert "source: work-123/index.md" in worker.prompt
    finally:
        db.close()


def test_role_agent_prompt_loads_recent_conversation_context(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish(
        "agent-inbox",
        "agent.product-manager",
        {"request": "status", "conversation_ref": "dm:product-manager"},
    )
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.record_conversation_message(
            message_id="msg-1",
            connector="teams",
            conversation_ref="dm:product-manager",
            source_type="dm",
            sender_ref="sponsor",
            text="Give me a status update.",
            mentioned_roles=("product-manager",),
        )
        db.compact_conversation_context(
            summary_id="summary-1",
            conversation_ref="dm:product-manager",
            visibility="private",
            summary="Sponsor prefers compact dashboard rows.",
            source_message_ids=("msg-1",),
            created_by_role="product-manager",
        )
        db.expire_conversation_raw_text(message_id="msg-1", expired_at="2026-06-15T12:00:00+00:00")
        worker = CapturingWorker()
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=worker,
            memory=InMemoryRoleMemory(),
            conversation_context=DatabaseConversationContext(db),
        )

        result = service.run_once()

        assert result is not None
        assert result.status == "completed"
        assert "<conversation-context>" in worker.prompt
        assert "<conversation-summaries>" in worker.prompt
        assert "Sponsor prefers compact dashboard rows." in worker.prompt
        assert "[expired raw conversation]" in worker.prompt
        assert "expired=true" in worker.prompt
        assert "message: msg-1" in worker.prompt
        assert "mentions: product-manager" in worker.prompt
    finally:
        db.close()


def test_role_agent_prompt_includes_governance_checklist(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-123"})
    worker = CapturingWorker()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
    )
    context = GovernanceContext(
        work_item_id="work-123",
        phase="requirements",
        accountable_role="project-manager",
        responsible_roles=("business-analyst",),
        consulted_roles=("product-manager",),
        informed_roles=("delivery-manager",),
        sponsor_decision_points=("requirements-signoff",),
    )

    result = service.run_once(governance_context=context)

    assert result is not None
    assert result.status == "completed"
    assert "<governance-checklist>" in worker.prompt
    assert "Missing consultation evidence for `product-manager`" in worker.prompt
    assert "Missing informed-update evidence for `delivery-manager`" in worker.prompt
    assert "Pending sponsor/stakeholder decision `requirements-signoff`" in worker.prompt


def test_role_agent_prompt_loads_database_work_item_governance_context(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-123"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-123",
            title="Shape product",
            description="Shape the product scope.",
            state="shaping",
            owner_role="product-manager",
            current_phase="requirements",
            governance={
                "phase": "requirements",
                "accountable_role": "project-manager",
                "responsible_roles": ["business-analyst"],
                "consulted_roles": ["product-manager"],
                "informed_roles": ["delivery-manager"],
                "sponsor_decision_points": ["requirements-signoff"],
            },
        )
        worker = CapturingWorker()
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=worker,
            memory=InMemoryRoleMemory(),
            work_item_governance_context=DatabaseWorkItemGovernanceContextProvider(db),
        )

        result = service.run_once()

        assert result is not None
        assert result.status == "completed"
        assert "<governance-context>" in worker.prompt
        assert "Work item: `work-123`" in worker.prompt
        assert "This role's RACI position: `consulted`" in worker.prompt
        assert "Missing consultation evidence for `product-manager`" in worker.prompt
        assert "Missing informed-update evidence for `delivery-manager`" in worker.prompt
        assert "Pending sponsor/stakeholder decision `requirements-signoff`" in worker.prompt
    finally:
        db.close()


def test_role_agent_prompt_uses_supplied_governance_checklist(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"work_item_id": "work-123"})
    worker = CapturingWorker()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=worker,
        memory=InMemoryRoleMemory(),
    )
    context = GovernanceContext(
        work_item_id="work-123",
        phase="requirements",
        accountable_role="project-manager",
        responsible_roles=("business-analyst",),
        consulted_roles=("product-manager",),
    )
    checklist = evaluate_governance_checklist(
        context,
        governance_records=(
            {
                "record_type": "consult.request",
                "target_ref": "product-manager",
                "status": "requested",
            },
        ),
    )

    result = service.run_once(governance_context=context, governance_checklist=checklist)

    assert result is not None
    assert result.status == "completed"
    assert "Governance checklist is currently satisfied." in worker.prompt


def test_role_agent_requeues_if_worker_calls_no_tools(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=NoToolWorker(),
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "failed"
    assert "did not call any tool" in (result.error or "")
    assert broker.depth("agent-inbox").pending == 1


def test_role_agent_blocks_linked_work_item_on_dead_letter(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish(
        "agent-inbox",
        "agent.product-manager",
        {
            "request": "status",
            "work_item_id": "work-123",
            "correlation_id": "corr-123",
            "source_message_id": "msg-123",
        },
    )
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-123",
            title="Shape product",
            description="Shape the product scope.",
            state="waiting_agent",
            owner_role="product-manager",
        )
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=NoToolWorker(),
            memory=InMemoryRoleMemory(),
            failure_reporter=DatabaseAgentFailureReporter(db),
            max_delivery_attempts=1,
        )

        result = service.run_once()
        detail = db.work_item_detail("work-123")
    finally:
        db.close()

    assert result is not None
    assert result.status == "dead_lettered"
    assert detail is not None
    assert detail.state == "blocked"
    assert detail.owner_role == "product-manager"
    assert "Agent delivery dead-lettered" in detail.next_action
    assert published.message_id in detail.next_action


def test_role_agent_acks_informed_update_without_worker_run_or_work_block(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish(
        "agent-inbox",
        "agent.product-manager",
        {
            "message_type": "informed.update",
            "message": "Engineering sent FYI evidence to Product Manager.",
            "work_item_id": "work-123",
            "correlation_id": "corr-123",
            "source_message_id": "agent.engineering:1073",
        },
    )
    db = V3Database(tmp_path / "v3.sqlite3")
    memory = InMemoryRoleMemory()
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-123",
            title="Shape product",
            description="Shape the product scope.",
            state="deploying",
            owner_role="release-manager",
            next_action="Deployment target is running.",
        )
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=FailingIfCalledWorker(),
            memory=memory,
            failure_reporter=DatabaseAgentFailureReporter(db),
            run_recorder=DatabaseAgentRunRecorder(db),
            message_journal=db,
            max_delivery_attempts=1,
        )

        result = service.run_once()
        detail = db.work_item_detail("work-123")
        runs = [
            tuple(row)
            for row in db.connection.execute(
                "SELECT status, work_item_id FROM agent_runs WHERE message_id=?",
                (published.message_id,),
            ).fetchall()
        ]
        journal = db.connection.execute(
            """
            SELECT stage, status, summary
            FROM message_journal
            WHERE message_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (published.message_id,),
        ).fetchone()
        events = [
            dict(row)
            for row in db.connection.execute(
                """
                SELECT event_type, payload_json
                FROM events
                WHERE aggregate_id=?
                ORDER BY event_id
                """,
                ("work-123",),
            ).fetchall()
        ]
    finally:
        db.close()

    assert result is not None
    assert result.status == "inform_only_acknowledged"
    assert detail is not None
    assert detail.state == "deploying"
    assert detail.owner_role == "release-manager"
    assert detail.next_action == "Deployment target is running."
    assert runs == [("inform_only_acknowledged", "work-123")]
    assert journal is not None
    assert journal["stage"] == "acked"
    assert journal["status"] == "inform_only_acknowledged"
    assert "without worker run" in journal["summary"]
    assert memory.load_summary("agentic-mesh-dev.product-manager.1").startswith("Informed update for work-123")
    assert not any(event["event_type"] == "agent.delivery_failure_recorded" for event in events)
    assert not broker.pending("agent-inbox", "pm-1")


def test_role_agent_acks_stale_message_for_terminal_work_item_without_worker_run(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish(
        "agent-inbox",
        "agent.product-manager",
        {
            "message_type": "handoff.request",
            "message": "Continue old work.",
            "work_item_id": "work-closed",
        },
    )
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-closed",
            title="Closed work",
            description="Already closed.",
            state="closed",
            owner_role="project-manager",
            next_action="Already closed.",
        )
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=FailingIfCalledWorker(),
            memory=InMemoryRoleMemory(),
            work_item_state_provider=DatabaseWorkItemStateProvider(db),
            run_recorder=DatabaseAgentRunRecorder(db),
            message_journal=db,
            max_delivery_attempts=1,
        )

        result = service.run_once()
        runs = db.connection.execute(
            "SELECT status, work_item_id FROM agent_runs WHERE message_id=?",
            (published.message_id,),
        ).fetchall()
        journal = db.connection.execute(
            """
            SELECT stage, status, summary
            FROM message_journal
            WHERE message_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (published.message_id,),
        ).fetchone()
    finally:
        db.close()

    assert result is not None
    assert result.status == "stale_terminal_work_skipped"
    assert broker.depth("agent-inbox").pending == 0
    assert [dict(row) for row in runs] == [
        {"status": "stale_terminal_work_skipped", "work_item_id": "work-closed"}
    ]
    assert journal["stage"] == "acked"
    assert journal["status"] == "stale_terminal_work_skipped"
    assert "terminal work item work-closed" in journal["summary"]


def test_role_agent_dead_letter_notifies_source_conversation(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.project-manager"])
    published = broker.publish(
        "agent-inbox",
        "agent.project-manager",
        {
            "request": "status",
            "connector": "teams",
            "conversation_ref": "dm:project-manager",
            "reply_target_ref": "conversation:pm-dm",
            "reply_thread_ref": "thread-1",
            "work_item_id": "work-123",
        },
    )
    db = V3Database(tmp_path / "v3.sqlite3")
    bridge = FakeStakeholderBridge()
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-123",
            title="Recover runtime",
            description="Recover the runtime.",
            state="waiting_agent",
            owner_role="project-manager",
        )
        service = RoleAgentService(
            config=RoleInstanceConfig(
                project_id="agentic-mesh-dev",
                role_id="project-manager",
                instance_id="1",
                role_prompt_path=_config(tmp_path).role_prompt_path,
                memory_db_path=tmp_path / "memory.sqlite3",
                inbox_stream="agent-inbox",
                inbox_consumer="project-manager-1",
            ),
            broker=broker,
            worker=NoToolWorker(),
            memory=InMemoryRoleMemory(),
            failure_reporter=DatabaseAgentFailureReporter(db, stakeholder_bridge=bridge),
            max_delivery_attempts=1,
        )

        result = service.run_once()
        deliveries = db.list_outbound_deliveries("work-123")
        journal_rows = [
            dict(row)
            for row in db.connection.execute(
                "SELECT stage, status, summary FROM message_journal WHERE message_id=? ORDER BY created_at",
                (published.message_id,),
            ).fetchall()
        ]
    finally:
        db.close()

    assert result is not None
    assert result.status == "dead_lettered"
    assert len(bridge.messages) == 1
    assert bridge.messages[0].connector == "teams"
    assert bridge.messages[0].target_ref == "conversation:pm-dm"
    assert bridge.messages[0].thread_ref == "thread-1"
    assert "project-manager could not complete" in bridge.messages[0].text_markdown
    assert "agent did not call any tool" in bridge.messages[0].text_markdown
    assert deliveries[-1]["purpose"] == "agent.failure"
    assert deliveries[-1]["status"] == "sent"
    assert any(row["stage"] == "reply_delivered" and row["status"] == "sent" for row in journal_rows)


def test_role_agent_requeues_if_worker_calls_no_terminal_tool(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=NonTerminalWorker(),
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "failed"
    assert "REPLY safe-output tool" in (result.error or "")
    assert broker.depth("agent-inbox").pending == 1


def test_role_agent_requeues_if_symbolic_worker_calls_reply_without_do_tool(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=ReplyOnlySymbolicWorker(),
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "failed"
    assert "DO safe-output tool" in (result.error or "")
    assert broker.depth("agent-inbox").pending == 1


def test_role_agent_requeues_if_symbolic_worker_calls_do_without_reply_tool(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=DoOnlySymbolicWorker(),
        memory=InMemoryRoleMemory(),
    )

    result = service.run_once()

    assert result is not None
    assert result.status == "failed"
    assert "REPLY safe-output tool" in (result.error or "")
    assert broker.depth("agent-inbox").pending == 1


def test_role_agent_requeues_if_terminal_call_was_not_recorded_in_audit(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=CapturingWorker(),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
        )

        result = service.run_once()
    finally:
        db.close()

    assert result is not None
    assert result.status == "failed"
    assert "did not record any safe-output tool call" in (result.error or "")
    assert broker.depth("agent-inbox").pending == 1


def test_role_agent_requeues_if_database_audit_records_reply_without_do_tool(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=RecordingReplyOnlyWorker(V3ToolService(db), role_instance_id),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
        )

        result = service.run_once()
    finally:
        db.close()

    assert result is not None
    assert result.status == "failed"
    assert "did not record a DO safe-output tool call" in (result.error or "")
    assert broker.depth("agent-inbox").pending == 1


def test_role_agent_requeues_if_database_audit_records_do_without_reply_tool(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=RecordingDoOnlyWorker(V3ToolService(db), role_instance_id),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
        )

        result = service.run_once()
    finally:
        db.close()

    assert result is not None
    assert result.status == "failed"
    assert "did not record a REPLY safe-output tool call" in (result.error or "")
    assert broker.depth("agent-inbox").pending == 1


def test_role_agent_accepts_terminal_call_recorded_through_tool_service(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=RecordingTerminalWorker(V3ToolService(db), role_instance_id),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
        )

        result = service.run_once()
        tool_calls = db.list_tool_calls()
    finally:
        db.close()

    assert result is not None
    assert result.status == "completed"
    assert set(result.tool_calls) == {call["call_id"] for call in tool_calls}
    calls_by_name = {call["tool_name"]: call for call in tool_calls}
    assert set(calls_by_name) == {"noop", "status.reply"}
    assert calls_by_name["noop"]["terminal"] is True
    assert calls_by_name["status.reply"]["terminal"] is True
    assert broker.depth("agent-inbox").pending == 0


def test_role_agent_accepts_report_incomplete_as_do_reply_terminal(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=RecordingIncompleteOnlyWorker(V3ToolService(db), role_instance_id),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
        )

        result = service.run_once()
        tool_calls = db.list_tool_calls()
    finally:
        db.close()

    assert result is not None
    assert result.status == "completed"
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_name"] == "report.incomplete"
    assert tool_calls[0]["terminal"] is True
    assert result.tool_calls == (tool_calls[0]["call_id"],)
    assert broker.depth("agent-inbox").pending == 0


def test_role_agent_uses_audited_tool_calls_not_worker_stdout_claims(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status", "work_item_id": "work-123"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-123",
            title="Shape product",
            description="Shape the product scope.",
            state="shaping",
            owner_role="product-manager",
        )
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=RecordingTerminalWorkerWithClaimMismatch(V3ToolService(db), role_instance_id),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
            run_recorder=DatabaseAgentRunRecorder(db),
        )

        result = service.run_once()
        recorded_tool_calls = tuple(str(call["call_id"]) for call in db.list_tool_calls())
        runs = db.list_agent_runs(role_instance_id)
    finally:
        db.close()

    assert result is not None
    assert result.status == "completed"
    assert result.tool_calls == recorded_tool_calls
    assert runs[0]["tool_calls"] == recorded_tool_calls
    assert "call-claimed-but-not-recorded" not in result.tool_calls


def test_role_agent_completes_when_worker_fails_after_terminal_safe_outputs(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=RecordingTerminalThenFailWorker(V3ToolService(db), role_instance_id),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
            run_recorder=DatabaseAgentRunRecorder(db),
        )

        result = service.run_once()
        recorded_tool_calls = tuple(str(call["call_id"]) for call in db.list_tool_calls())
        runs = db.list_agent_runs(role_instance_id)
    finally:
        db.close()

    assert result is not None
    assert result.status == "completed"
    assert result.tool_calls == recorded_tool_calls
    assert runs[0]["status"] == "completed"
    assert runs[0]["error"] is None
    broker.ensure_consumer("agent-inbox", "pm-1", filter_subject="agent.product-manager")
    assert broker.fetch("agent-inbox", "pm-1") == []


def test_role_agent_completes_when_worker_stdout_is_malformed_after_terminal_safe_outputs(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=RecordingTerminalThenMalformedStdoutWorker(V3ToolService(db), role_instance_id),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
            run_recorder=DatabaseAgentRunRecorder(db),
        )

        result = service.run_once()
        recorded_tool_calls = tuple(str(call["call_id"]) for call in db.list_tool_calls())
        runs = db.list_agent_runs(role_instance_id)
    finally:
        db.close()

    assert result is not None
    assert result.status == "completed"
    assert result.tool_calls == recorded_tool_calls
    assert runs[0]["status"] == "completed"
    assert runs[0]["error"] is None
    assert broker.depth("agent-inbox").pending == 0


def test_role_agent_acknowledges_retry_with_prior_message_linked_safe_outputs(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish(
        "agent-inbox",
        "agent.product-manager",
        {"request": "status", "correlation_id": "corr-prior-safe-output"},
        message_id="agent.product-manager:retry-1",
    )
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        tools = V3ToolService(db)
        do_call = tools.call(
            role_instance_id=role_instance_id,
            tool_name="noop",
            payload={
                "reason": "Prior delivery attempt already completed the durable action.",
                "source_message_id": published.message_id,
                "correlation_id": "corr-prior-safe-output",
            },
        )
        reply_call = tools.call(
            role_instance_id=role_instance_id,
            tool_name="status.reply",
            payload={
                "text_markdown": "Prior delivery attempt already replied.",
                "source_message_id": published.message_id,
                "correlation_id": "corr-prior-safe-output",
            },
        )
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=NoToolWorker(),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
            run_recorder=DatabaseAgentRunRecorder(db),
        )

        result = service.run_once()
        runs = db.list_agent_runs(role_instance_id)
    finally:
        db.close()

    assert result is not None
    assert result.status == "completed"
    assert set(result.tool_calls) == {do_call.call_id, reply_call.call_id}
    assert runs[0]["status"] == "completed"
    assert runs[0]["error"] is None
    assert broker.depth("agent-inbox").pending == 0


def test_role_agent_does_not_acknowledge_retry_with_unrelated_prior_safe_outputs(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish(
        "agent-inbox",
        "agent.product-manager",
        {"request": "status", "correlation_id": "corr-current"},
        message_id="agent.product-manager:retry-2",
    )
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        tools = V3ToolService(db)
        tools.call(
            role_instance_id=role_instance_id,
            tool_name="noop",
            payload={
                "reason": "Prior unrelated delivery attempt completed.",
                "source_message_id": "agent.product-manager:other",
                "correlation_id": "corr-other",
            },
        )
        tools.call(
            role_instance_id=role_instance_id,
            tool_name="status.reply",
            payload={
                "text_markdown": "Prior unrelated delivery attempt replied.",
                "source_message_id": "agent.product-manager:other",
                "correlation_id": "corr-other",
            },
        )
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=NoToolWorker(),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
            run_recorder=DatabaseAgentRunRecorder(db),
        )

        result = service.run_once()
        runs = db.list_agent_runs(role_instance_id)
    finally:
        db.close()

    assert result is not None
    assert result.status == "failed"
    assert result.tool_calls == ()
    assert "agent did not call any tool" in (result.error or "")
    assert runs[0]["status"] == "failed"
    assert broker.depth("agent-inbox").pending == 1


def test_role_agent_records_successful_run_to_database(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish("agent-inbox", "agent.product-manager", {"request": "status", "work_item_id": "work-123"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-123",
            title="Shape product",
            description="Shape the product scope.",
            state="shaping",
            owner_role="product-manager",
        )
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=RecordingTerminalWorker(V3ToolService(db), role_instance_id),
            memory=InMemoryRoleMemory(),
            status_reporter=DatabaseAgentStatusReporter(db),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
            run_recorder=DatabaseAgentRunRecorder(db),
        )

        result = service.run_once()
        runs = db.list_agent_runs(role_instance_id)
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
        detail = db.work_item_detail("work-123")
    finally:
        db.close()

    assert result is not None
    assert result.status == "completed"
    assert len(runs) == 1
    assert runs[0]["message_id"] == published.message_id
    assert runs[0]["work_item_id"] == "work-123"
    assert runs[0]["subject"] == "agent.product-manager"
    assert runs[0]["status"] == "completed"
    assert len(runs[0]["tool_calls"]) == 2
    assert runs[0]["error"] is None
    assert snapshot.agents[0].last_run_status == "completed"
    assert snapshot.agents[0].last_run_error is None
    assert detail is not None
    assert len(detail.agent_runs) == 1
    assert detail.agent_runs[0].message_id == published.message_id
    assert detail.agent_runs[0].work_item_id == "work-123"


def test_role_agent_interrupts_stale_running_runs_for_same_role(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish("agent-inbox", "agent.product-manager", {"request": "status", "work_item_id": "work-123"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        db.record_agent_run(
            run_id="run-stale",
            role_instance_id=role_instance_id,
            message_id="agent.product-manager:old",
            subject="agent.product-manager",
            status="running",
            work_item_id="work-123",
            started_at="2026-06-19T09:00:00+00:00",
            completed_at="2026-06-19T09:00:00+00:00",
        )
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=RecordingTerminalWorker(V3ToolService(db), role_instance_id),
            memory=InMemoryRoleMemory(),
            terminal_tool_call_audit=DatabaseTerminalToolCallAudit(db),
            run_recorder=DatabaseAgentRunRecorder(db),
        )

        result = service.run_once()
        runs = db.list_agent_runs(role_instance_id)
    finally:
        db.close()

    assert result is not None
    assert result.status == "completed"
    stale = next(run for run in runs if run["run_id"] == "run-stale")
    current = next(run for run in runs if run["message_id"] == published.message_id)
    assert stale["status"] == "interrupted"
    assert "prior running record no longer owns this role instance" in (stale["error"] or "")
    assert current["status"] == "completed"


def test_status_snapshot_prefers_active_running_run_over_same_timestamp_interruption(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.release-manager.1"
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id=role_instance_id,
                container_state="running",
                heartbeat_at="2026-06-19T13:08:15+00:00",
                current_work="work-release",
                inbox_depth=12,
            )
        )
        db.record_agent_run(
            run_id="run-active",
            role_instance_id=role_instance_id,
            message_id="agent.release-manager:314",
            subject="agent.release-manager",
            status="running",
            work_item_id="work-release",
            started_at="2026-06-19T13:08:15+00:00",
            completed_at="2026-06-19T13:08:15+00:00",
        )
        db.record_agent_run(
            run_id="run-stale-interrupted",
            role_instance_id=role_instance_id,
            message_id="agent.release-manager:318",
            subject="agent.release-manager",
            status="interrupted",
            work_item_id="work-release",
            error="Role service claimed agent.release-manager:314; prior running record no longer owns this role instance.",
            started_at="2026-06-19T13:05:23+00:00",
            completed_at="2026-06-19T13:08:15+00:00",
        )

        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert snapshot.agents[0].last_run_status == "running"
    assert snapshot.agents[0].last_run_error is None


def test_role_agent_records_failed_run_to_database(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=NoToolWorker(),
            memory=InMemoryRoleMemory(),
            status_reporter=DatabaseAgentStatusReporter(db),
            run_recorder=DatabaseAgentRunRecorder(db),
            prompt_recorder=DatabaseAgentPromptRecorder(db),
        )

        result = service.run_once()
        runs = db.list_agent_runs(role_instance_id)
        prompts = tuple(
            dict(row)
            for row in db.connection.execute(
                "SELECT run_id, role_id, role_instance_id, assignment_id, prompt_text, component_manifest_json FROM agent_prompts"
            )
        )
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert result is not None
    assert result.status == "failed"
    assert len(runs) == 1
    assert runs[0]["message_id"] == published.message_id
    assert runs[0]["status"] == "failed"
    assert runs[0]["tool_calls"] == ()
    assert "did not call any tool" in runs[0]["error"]
    assert len(prompts) == 1
    assert prompts[0]["run_id"] == runs[0]["run_id"]
    assert prompts[0]["role_id"] == "product-manager"
    assert prompts[0]["role_instance_id"] == role_instance_id
    assert prompts[0]["assignment_id"] == published.message_id
    assert "<agentic-mesh-v3-agent>" in prompts[0]["prompt_text"]
    assert "You are Product Manager." in prompts[0]["prompt_text"]
    assert '"prompt_version": "v3"' in prompts[0]["component_manifest_json"]
    assert snapshot.agents[0].last_run_status == "failed"
    assert "did not call any tool" in (snapshot.agents[0].last_run_error or "")


def test_role_agent_dead_letters_after_delivery_limit(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    reporter = FakeStatusReporter()
    service = RoleAgentService(
        config=_config(tmp_path),
        broker=broker,
        worker=NoToolWorker(),
        memory=InMemoryRoleMemory(),
        status_reporter=reporter,
        max_delivery_attempts=2,
    )

    first = service.run_once()
    second = service.run_once()

    assert first is not None
    assert first.status == "failed"
    assert second is not None
    assert second.status == "dead_lettered"
    assert broker.depth("agent-inbox").pending == 0
    dead = broker.dead_letters("agent-inbox")[0]
    assert dead.payload["dead_letter_reason"] == "agent did not call any tool"
    assert reporter.statuses[-1].dead_letter_depth == 1


def test_role_agent_records_dead_lettered_run_to_database(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        role_instance_id = "agentic-mesh-dev.product-manager.1"
        service = RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=NoToolWorker(),
            memory=InMemoryRoleMemory(),
            status_reporter=DatabaseAgentStatusReporter(db),
            run_recorder=DatabaseAgentRunRecorder(db),
            max_delivery_attempts=1,
        )

        result = service.run_once()
        runs = db.list_agent_runs(role_instance_id)
        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
    finally:
        db.close()

    assert result is not None
    assert result.status == "dead_lettered"
    assert len(runs) == 1
    assert runs[0]["status"] == "dead_lettered"
    assert "did not call any tool" in runs[0]["error"]
    assert snapshot.agents[0].last_run_status == "dead_lettered"


def test_role_agent_rejects_invalid_delivery_limit(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()

    try:
        RoleAgentService(
            config=_config(tmp_path),
            broker=broker,
            worker=EchoWorker(),
            memory=InMemoryRoleMemory(),
            max_delivery_attempts=0,
        )
    except ValueError as exc:
        assert "max_delivery_attempts must be positive" in str(exc)
    else:
        raise AssertionError("invalid delivery limit should fail validation")
