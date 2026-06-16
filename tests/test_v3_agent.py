from pathlib import Path

from agentic_mesh_v3.agent import DatabaseAgentStatusReporter
from agentic_mesh_v3.agent import DatabaseConversationContext
from agentic_mesh_v3.agent import DatabaseTerminalToolCallAudit
from agentic_mesh_v3.agent import DatabaseWorkItemGovernanceContextProvider
from agentic_mesh_v3.agent import EchoWorker
from agentic_mesh_v3.agent import InMemoryRoleMemory
from agentic_mesh_v3.agent import RoleAgentService
from agentic_mesh_v3.agent import RoleInstanceConfig
from agentic_mesh_v3.agent import build_role_memory
from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.governance import GovernanceContext
from agentic_mesh_v3.governance import evaluate_governance_checklist
from agentic_mesh_v3.memory import DatabaseRoleMemory
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

    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        self.prompt = prompt
        return [f"noop:{message.message_id}", f"status.reply:{message.message_id}"]


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


class FakeStatusReporter:
    def __init__(self) -> None:
        self.statuses = []

    def report(self, status):  # type: ignore[no-untyped-def]
        self.statuses.append(status)


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


def test_role_agent_processes_inbox_and_records_memory(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
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
    assert "processed" in memory.load_summary("agentic-mesh-dev.product-manager.1")


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
    assert broker.depth("agent-inbox").pending == 0


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


def test_role_agent_can_use_configured_sqlite_memory(tmp_path: Path) -> None:
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
    assert "processed" in summary
    assert "source: work-item:work-123" in summary


def test_role_agent_records_run_memory_with_conversation_source(tmp_path: Path) -> None:
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
        assert len(records) == 1
        assert records[0]["source_ref"] == "conversation:dm:product-manager"
    finally:
        db.close()


def test_role_agent_records_run_memory_with_broker_source_when_payload_has_no_context(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
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
        assert len(records) == 1
        assert records[0]["source_ref"] == f"broker-message:{published.message_id}"
    finally:
        db.close()


def test_role_agent_prompt_includes_mounted_context_components(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish("agent-inbox", "agent.product-manager", {"request": "shape this"})
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
    assert '{\n  "request": "shape this"\n}' in worker.prompt


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
    assert "terminal safe-output tool" in (result.error or "")
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
    assert "did not record a terminal safe-output tool call" in (result.error or "")
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
