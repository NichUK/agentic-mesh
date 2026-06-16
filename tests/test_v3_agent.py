from pathlib import Path

from agentic_mesh_v3.agent import DatabaseAgentStatusReporter
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


class NoToolWorker:
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        return []


class CapturingWorker:
    def __init__(self) -> None:
        self.prompt = ""

    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        self.prompt = prompt
        return [f"status.reply:{message.message_id}"]


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
    broker.publish("agent-inbox", "agent.product-manager", {"request": "status"})
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
    assert "processed" in memory.load_summary("agentic-mesh-dev.product-manager.1")


def test_role_agent_prompt_includes_mounted_context_components(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    broker.ensure_stream("agent-inbox", ["agent.product-manager"])
    published = broker.publish("agent-inbox", "agent.product-manager", {"request": "shape this"})
    config = _config(tmp_path)
    organisation = tmp_path / "organisation.md"
    project = tmp_path / "project.md"
    raci = tmp_path / "raci.json"
    tools = tmp_path / "tools.md"
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
