from datetime import datetime
from datetime import timezone
from pathlib import Path

from agentic_mesh_v3.lifecycle import HibernationPolicy
from agentic_mesh_v3.lifecycle import RoleContainerSpec
from agentic_mesh_v3.lifecycle import plan_lifecycle_action
from agentic_mesh_v3.reporting import AgentStatus


def test_role_container_spec_declares_expected_mounts(tmp_path: Path) -> None:
    spec = RoleContainerSpec(
        role_instance_id="agentic-mesh-dev.engineering.1",
        image="agentic-mesh:local",
        source_repo=tmp_path / "source",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        agent_config_dir=tmp_path / "agent",
        runtime_state_dir=tmp_path / "state",
        document_library_root=tmp_path / "documents",
        environment={"PROJECT_ID": "agentic-mesh-dev"},
    )

    mounts = spec.volume_mounts()

    assert mounts[str(tmp_path / "source")] == "/mesh/source"
    assert mounts[str(tmp_path / "documents")] == "/documents"


def test_role_container_spec_generates_service_command(tmp_path: Path) -> None:
    spec = RoleContainerSpec(
        role_instance_id="agentic-mesh-dev.product-manager.1",
        image="agentic-mesh:local",
        source_repo=tmp_path / "source",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        agent_config_dir=tmp_path / "agent",
        runtime_state_dir=tmp_path / "state",
        document_library_root=tmp_path / "documents",
        environment={"PROJECT_ID": "agentic-mesh-dev"},
    )

    command = spec.service_command(poll_interval_seconds=2.5, idle_exit_seconds=600)

    assert command == [
        "agentic-mesh-v3",
        "--db",
        "/mesh/state/agentic-mesh-v3.sqlite3",
        "--project-config",
        "/mesh/project/agentic-mesh/project.yaml",
        "run-agent-service",
        "--role-id",
        "product-manager",
        "--instance-id",
        "1",
        "--agent-config-dir",
        "/mesh/agent",
        "--runtime-state-dir",
        "/mesh/state",
        "--poll-interval-seconds",
        "2.5",
        "--idle-exit-seconds",
        "600",
    ]


def test_lifecycle_wakes_hibernated_agent_with_inbox() -> None:
    decision = plan_lifecycle_action(
        status=AgentStatus(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            container_state="hibernated",
            heartbeat_at=None,
            inbox_depth=1,
        ),
        policy=HibernationPolicy(),
    )

    assert decision.action == "wake"
    assert "pending inbox" in decision.reason


def test_lifecycle_hibernates_idle_agent_when_warm_pool_allows() -> None:
    decision = plan_lifecycle_action(
        status=AgentStatus(
            role_instance_id="agentic-mesh-dev.engineering.2",
            container_state="running",
            heartbeat_at="2026-06-15T10:00:00+00:00",
            inbox_depth=0,
        ),
        policy=HibernationPolicy(idle_after_seconds=60, min_warm_instances_per_role=1),
        now=datetime(2026, 6, 15, 10, 5, tzinfo=timezone.utc),
        warm_instances_for_role=2,
    )

    assert decision.action == "hibernate"


def test_lifecycle_keeps_minimum_warm_instance_running() -> None:
    decision = plan_lifecycle_action(
        status=AgentStatus(
            role_instance_id="agentic-mesh-dev.engineering.1",
            container_state="running",
            heartbeat_at="2026-06-15T10:00:00+00:00",
            inbox_depth=0,
        ),
        policy=HibernationPolicy(idle_after_seconds=60, min_warm_instances_per_role=1),
        now=datetime(2026, 6, 15, 10, 5, tzinfo=timezone.utc),
        warm_instances_for_role=1,
    )

    assert decision.action == "none"
    assert "minimum warm pool" in decision.reason
