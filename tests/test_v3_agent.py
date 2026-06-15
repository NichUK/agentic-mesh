from pathlib import Path

from agentic_mesh_v3.agent import EchoWorker
from agentic_mesh_v3.agent import InMemoryRoleMemory
from agentic_mesh_v3.agent import RoleAgentService
from agentic_mesh_v3.agent import RoleInstanceConfig
from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.governance import DEFAULT_SDLC_RACI
from agentic_mesh_v3.governance import GovernanceContext


class NoToolWorker:
    def run(self, prompt, message):  # type: ignore[no-untyped-def]
        return []


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
