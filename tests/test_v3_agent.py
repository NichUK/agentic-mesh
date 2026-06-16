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


class NoToolWorker:
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        return []


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
