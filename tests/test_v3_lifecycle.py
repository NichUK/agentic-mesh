from datetime import datetime
from datetime import timezone
from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.lifecycle import HibernationPolicy
from agentic_mesh_v3.lifecycle import ComposeLifecycleConfig
from agentic_mesh_v3.lifecycle import ComposeLifecycleExecutor
from agentic_mesh_v3.lifecycle import CommandExecutionResult
from agentic_mesh_v3.lifecycle import LifecycleDecision
from agentic_mesh_v3.lifecycle import RoleContainerSpec
from agentic_mesh_v3.lifecycle import compose_lifecycle_command
from agentic_mesh_v3.lifecycle import plan_lifecycle_action
from agentic_mesh_v3.lifecycle import plan_lifecycle_actions
from agentic_mesh_v3.lifecycle import refresh_agent_statuses_from_broker
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


def test_role_container_spec_mounts_target_repositories(tmp_path: Path) -> None:
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
        target_repositories={"app": tmp_path / "app"},
    )

    mounts = spec.volume_mounts()

    assert mounts[str(tmp_path / "app")] == "/mesh/workspaces/app"


def test_role_container_spec_rejects_unsafe_target_repository_mount_names(tmp_path: Path) -> None:
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
        target_repositories={"../app": tmp_path / "app"},
    )

    try:
        spec.volume_mounts()
    except ValueError as exc:
        assert "invalid target repository id" in str(exc)
    else:
        raise AssertionError("unsafe target repository mount names should be rejected")


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
        "--project-id",
        "agentic-mesh-dev",
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


def test_lifecycle_keeps_missing_agent_asleep_without_inbox_or_warm_pool() -> None:
    decision = plan_lifecycle_action(
        status=AgentStatus(
            role_instance_id="agentic-mesh-dev.solution-architect.1",
            container_state="missing",
            heartbeat_at=None,
            inbox_depth=0,
        ),
        policy=HibernationPolicy(min_warm_instances_per_role=0),
        warm_instances_for_role=0,
    )

    assert decision.action == "none"
    assert "configured but asleep" in decision.reason


def test_lifecycle_starts_missing_agent_with_pending_inbox() -> None:
    decision = plan_lifecycle_action(
        status=AgentStatus(
            role_instance_id="agentic-mesh-dev.solution-architect.1",
            container_state="missing",
            heartbeat_at=None,
            inbox_depth=1,
        ),
        policy=HibernationPolicy(min_warm_instances_per_role=0),
        warm_instances_for_role=0,
    )

    assert decision.action == "start"
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


def test_lifecycle_retries_failed_agent_with_pending_inbox() -> None:
    decision = plan_lifecycle_action(
        status=AgentStatus(
            role_instance_id="agentic-mesh-dev.delivery-manager.1",
            container_state="lifecycle_failed",
            heartbeat_at=None,
            inbox_depth=1,
        ),
        policy=HibernationPolicy(min_warm_instances_per_role=0),
    )

    assert decision.action == "wake"
    assert decision.reason == "retry failed lifecycle action for pending inbox messages"


def test_lifecycle_wakes_running_agent_with_stale_heartbeat_and_pending_inbox() -> None:
    now = datetime(2026, 6, 18, 12, 5, tzinfo=timezone.utc)

    decision = plan_lifecycle_action(
        status=AgentStatus(
            role_instance_id="agentic-mesh-dev.delivery-manager.1",
            container_state="running",
            heartbeat_at="2026-06-18T12:00:00+00:00",
            inbox_depth=1,
        ),
        policy=HibernationPolicy(
            min_warm_instances_per_role=0,
            pending_inbox_stale_after_seconds=120,
        ),
        now=now,
    )

    assert decision.action == "wake"
    assert decision.reason == "pending inbox messages and stale heartbeat for 300 seconds"


def test_lifecycle_keeps_running_agent_with_fresh_heartbeat_and_pending_inbox() -> None:
    now = datetime(2026, 6, 18, 12, 0, 30, tzinfo=timezone.utc)

    decision = plan_lifecycle_action(
        status=AgentStatus(
            role_instance_id="agentic-mesh-dev.delivery-manager.1",
            container_state="running",
            heartbeat_at="2026-06-18T12:00:00+00:00",
            inbox_depth=1,
        ),
        policy=HibernationPolicy(
            min_warm_instances_per_role=0,
            pending_inbox_stale_after_seconds=120,
        ),
        now=now,
    )

    assert decision.action == "none"
    assert decision.reason == "agent has pending inbox messages"


def test_lifecycle_batch_hibernates_only_idle_instances_above_warm_pool() -> None:
    decisions = plan_lifecycle_actions(
        [
            AgentStatus(
                role_instance_id="agentic-mesh-dev.engineering.1",
                container_state="running",
                heartbeat_at="2026-06-15T10:00:00+00:00",
                inbox_depth=0,
            ),
            AgentStatus(
                role_instance_id="agentic-mesh-dev.engineering.2",
                container_state="running",
                heartbeat_at="2026-06-15T10:00:00+00:00",
                inbox_depth=0,
            ),
            AgentStatus(
                role_instance_id="agentic-mesh-dev.engineering.3",
                container_state="running",
                heartbeat_at="2026-06-15T10:00:00+00:00",
                inbox_depth=0,
            ),
        ],
        policy=HibernationPolicy(idle_after_seconds=60, min_warm_instances_per_role=1),
        now=datetime(2026, 6, 15, 10, 5, tzinfo=timezone.utc),
    )

    assert [decision.action for decision in decisions].count("hibernate") == 2
    assert decisions[-1].action == "none"
    assert "minimum warm pool" in decisions[-1].reason


def test_lifecycle_batch_accounts_for_wakes_before_hibernating_idle_instances() -> None:
    decisions = plan_lifecycle_actions(
        [
            AgentStatus(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                container_state="running",
                heartbeat_at="2026-06-15T10:00:00+00:00",
                inbox_depth=0,
            ),
            AgentStatus(
                role_instance_id="agentic-mesh-dev.product-manager.2",
                container_state="hibernated",
                heartbeat_at="2026-06-15T10:00:00+00:00",
                inbox_depth=1,
            ),
        ],
        policy=HibernationPolicy(idle_after_seconds=60, min_warm_instances_per_role=1),
        now=datetime(2026, 6, 15, 10, 5, tzinfo=timezone.utc),
    )

    actions_by_agent = {decision.role_instance_id: decision.action for decision in decisions}
    assert actions_by_agent == {
        "agentic-mesh-dev.product-manager.2": "wake",
        "agentic-mesh-dev.product-manager.1": "hibernate",
    }


def test_lifecycle_refreshes_stopped_agent_inbox_depth_from_broker() -> None:
    class FakeBroker:
        def __init__(self) -> None:
            self.consumers: list[tuple[str, str, str | None]] = []

        def ensure_consumer(self, stream: str, consumer: str, *, filter_subject: str | None = None):  # type: ignore[no-untyped-def]
            self.consumers.append((stream, consumer, filter_subject))

        def pending(self, stream: str, consumer: str | None = None, *, limit: int = 20):  # type: ignore[no-untyped-def]
            del stream, limit
            return [object(), object()] if consumer == "product-manager.1" else []

        def dead_letters(self, stream: str, *, limit: int = 20):  # type: ignore[no-untyped-def]
            del stream, limit
            return []

    broker = FakeBroker()
    statuses = refresh_agent_statuses_from_broker(
        [
            AgentStatus(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                container_state="hibernated",
                heartbeat_at=None,
                inbox_depth=0,
            )
        ],
        role_instance_ids=("agentic-mesh-dev.product-manager.1",),
        broker=broker,  # type: ignore[arg-type]
        stream="agent-inbox",
    )

    assert statuses[0].inbox_depth == 2
    assert ("agent-inbox", "product-manager.1", "agent.product-manager") in broker.consumers
    decision = plan_lifecycle_action(status=statuses[0], policy=HibernationPolicy())
    assert decision.action == "wake"


def test_compose_lifecycle_command_maps_wake_and_hibernate_to_compose(tmp_path: Path) -> None:
    config = ComposeLifecycleConfig(
        compose_files=(tmp_path / "compose.yml", tmp_path / "override.yml"),
        working_directory=tmp_path,
        project_name="agentic-mesh",
    )

    wake = compose_lifecycle_command(
        LifecycleDecision("wake", "agentic-mesh-dev.product-manager.1", "pending inbox messages"),
        config=config,
    )
    hibernate = compose_lifecycle_command(
        LifecycleDecision("hibernate", "agentic-mesh-dev.product-manager.1", "idle"),
        config=config,
    )

    assert wake is not None
    assert wake.service_name == "agentic-mesh-dev-product-manager-1"
    assert wake.command == (
        "docker",
        "compose",
        "--project-name",
        "agentic-mesh",
        "-f",
        str(tmp_path / "compose.yml"),
        "-f",
        str(tmp_path / "override.yml"),
        "up",
        "-d",
        "--no-deps",
        "--no-recreate",
        "agentic-mesh-dev-product-manager-1",
    )
    assert wake.working_directory == tmp_path
    assert hibernate is not None
    assert hibernate.command[-2:] == ("stop", "agentic-mesh-dev-product-manager-1")


def test_compose_lifecycle_executor_dry_runs_only_actionable_decisions(tmp_path: Path) -> None:
    executor = ComposeLifecycleExecutor(
        ComposeLifecycleConfig(compose_files=(tmp_path / "compose.yml",), working_directory=tmp_path)
    )

    results = executor.apply(
        [
            LifecycleDecision("none", "agentic-mesh-dev.product-manager.1", "already running"),
            LifecycleDecision("start", "agentic-mesh-dev.engineering.1", "missing"),
        ]
    )

    assert len(results) == 1
    assert results[0].executed is False
    assert results[0].exit_code is None
    assert results[0].command[-3:] == ("--no-deps", "--no-recreate", "agentic-mesh-dev-engineering-1")


def test_compose_lifecycle_executor_executes_with_injected_runner(tmp_path: Path) -> None:
    calls = []

    def runner(command, *, cwd, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append((tuple(command), cwd, timeout_seconds))
        return CommandExecutionResult(
            exit_code=0,
            stdout="started",
        )

    executor = ComposeLifecycleExecutor(
        ComposeLifecycleConfig(
            compose_files=(tmp_path / "compose.yml",),
            working_directory=tmp_path,
            timeout_seconds=7,
        ),
        runner=runner,
    )

    results = executor.apply(
        [LifecycleDecision("wake", "agentic-mesh-dev.engineering.1", "pending inbox")],
        execute=True,
    )

    assert len(results) == 1
    assert results[0].executed is True
    assert results[0].exit_code == 0
    assert results[0].stdout == "started"
    assert calls == [
        (
            (
                "docker",
                "compose",
                "-f",
                str(tmp_path / "compose.yml"),
                "up",
                "-d",
                "--no-deps",
                "--no-recreate",
                "agentic-mesh-dev-engineering-1",
            ),
            tmp_path,
            7,
        )
    ]


def test_lifecycle_results_update_agent_status_projection(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.engineering.1",
                container_state="running",
                heartbeat_at="2026-06-15T10:00:00+00:00",
            )
        )

        db.record_agent_lifecycle_result(
            role_instance_id="agentic-mesh-dev.engineering.1",
            action="hibernate",
            reason="idle",
            service_name="agentic-mesh-dev-engineering-1",
            command=("docker", "compose", "stop", "agentic-mesh-dev-engineering-1"),
            working_directory=None,
            exit_code=0,
            executed=True,
        )

        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
        assert snapshot.agents[0].container_state == "hibernated"

        db.record_agent_lifecycle_result(
            role_instance_id="agentic-mesh-dev.engineering.1",
            action="wake",
            reason="pending inbox messages",
            service_name="agentic-mesh-dev-engineering-1",
            command=("docker", "compose", "up", "-d", "agentic-mesh-dev-engineering-1"),
            working_directory=None,
            exit_code=1,
            stderr="compose failed",
            executed=True,
        )

        snapshot = db.status_snapshot(project_id="agentic-mesh-dev")
        events = db.connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE event_type='agent.lifecycle_action_recorded'"
        ).fetchone()
    finally:
        db.close()

    assert snapshot.agents[0].container_state == "lifecycle_failed"
    assert snapshot.agents[0].last_lifecycle_action == "wake"
    assert snapshot.agents[0].last_lifecycle_reason == "pending inbox messages"
    assert snapshot.agents[0].last_lifecycle_service == "agentic-mesh-dev-engineering-1"
    assert snapshot.agents[0].last_lifecycle_exit_code == 1
    assert snapshot.agents[0].last_lifecycle_executed is True
    assert snapshot.agents[0].last_lifecycle_error == "compose failed"
    assert snapshot.agents[0].last_lifecycle_at is not None
    assert snapshot.agents[0].governance_waits == ()
    assert events["count"] == 2
