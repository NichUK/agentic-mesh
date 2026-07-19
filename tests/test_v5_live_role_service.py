from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from threading import Event
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.cli import build_parser
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.docker_fleet import DockerContainerFleetSupervisor
from agentic_mesh_v5.fleet import FleetAction, FleetSupervisorError
from agentic_mesh_v5.flow_definition import validate_flow
from agentic_mesh_v5.flow_engine import FlowEngine
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.progress import ProgressDraft, ProgressStore
from agentic_mesh_v5.prompt_renderer import RenderedRoleStatePrompt
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.role_service import RoleService, RoleServiceConfig
from agentic_mesh_v5.thread_affinity import ThreadAffinityKey, ThreadAffinityStore
from agentic_mesh_v5.worker_provider import (
    EngineMetadata,
    ProviderEvent,
    ProviderEventKind,
    TurnCompletionStatus,
)


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required")
    database_name = f"mesh_v5_role_service_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    parsed = urlsplit(base_url)
    database_url = urlunsplit(parsed._replace(path=f"/{database_name}"))
    try:
        yield database_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


def _flow():
    return validate_flow(
        {
            "schema_version": 1,
            "flow_id": "delivery",
            "leader_role": "engineering",
            "entry_state": "build",
            "terminal_states": ["build"],
            "states": {
                "build": {
                    "owner_role": "engineering",
                    "purpose": "Deliver the bounded change.",
                    "artifact": "projects/{project_id}/work/{work_item_id}/build.md",
                    "consults": [],
                    "gates": [],
                    "routes": [],
                    "terminal": True,
                }
            },
        },
        digest="b" * 64,
    )


def _seed(database_url: str, work_ids: tuple[str, ...]) -> None:
    MigrationRunner(database_url).migrate()
    lifecycle = LifecycleStore(database_url)
    lifecycle.create_project(
        project_id="alpha", display_name="Alpha", sponsor_ids=("sponsor",)
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'engineering', 'engineering')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status, provider_ref)
            VALUES ('alpha', 'engineering-1', 'engineering', 'running', 'fake')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_manifest_snapshots
                (project_id, manifest_digest, source_revision, source_path,
                 snapshot, registered_by)
            VALUES ('alpha', %s, %s, 'agentic-mesh/project.yaml', '{}'::jsonb, 'test')
            """,
            ("a" * 64, "1" * 40),
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_bindings
                (project_id, role_id, manifest_digest, role_reference, role_digest,
                 role_snapshot, tool_profile_reference, tool_profile_digest,
                 tool_profile_id, flow_reference, flow_digest,
                 prompt_configuration_digest, role_class, memory_scope,
                 collaboration_identity, minimum_instances, maximum_instances,
                 activated_by)
            VALUES
                ('alpha', 'engineering', %s, 'role/engineering@1', %s,
                 '{}'::jsonb, 'tool-profile/development@1', %s, 'development',
                 'flow/delivery@1', %s, %s, 'development', 'project-role',
                 'engineering', 1, 2, 'test')
            """,
            ("a" * 64, "d" * 64, "e" * 64, "b" * 64, "c" * 64),
        )
    queues = RoleQueueStore(database_url)
    queues.create_queue(project_id="alpha", queue_id="engineering", role_id="engineering")
    for work_id in work_ids:
        lifecycle.create_work_item(
            project_id="alpha",
            work_item_id=work_id,
            title=work_id,
            owner_role_id="engineering",
            actor_id="project-manager",
            correlation_id=f"create-{work_id}",
        )
        lifecycle.transition_work_item(
            project_id="alpha",
            work_item_id=work_id,
            target_status="active",
            actor_id="project-manager",
            correlation_id=f"activate-{work_id}",
            expected_version=1,
        )
        FlowEngine(database_url).start(
            project_id="alpha",
            work_item_id=work_id,
            flow=_flow(),
            fields={},
            actor_id="project-manager",
            operation_id=f"start-{work_id}",
        )
        queues.enqueue(
            project_id="alpha",
            queue_id="engineering",
            queue_item_id=f"queue-{work_id}",
            work_item_id=work_id,
            idempotency_key=f"queue-{work_id}",
            payload={"conversation_id": "delivery", "goal": f"process {work_id}"},
        )


class _Turn:
    def __init__(self, thread_id: str, effect) -> None:
        self.thread_id = thread_id
        self.turn_id = f"turn-{uuid.uuid4().hex}"
        self._effect = effect

    def events(self):
        self._effect(self.turn_id)
        yield ProviderEvent(
            ProviderEventKind.TURN_COMPLETED,
            self.thread_id,
            self.turn_id,
            completion=TurnCompletionStatus.COMPLETED,
        )

    def interrupt(self) -> None:
        pass


class _BlockingTurn:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.turn_id = f"turn-{uuid.uuid4().hex}"
        self.interrupted = Event()

    def events(self):
        self.interrupted.wait(timeout=10)
        yield ProviderEvent(
            ProviderEventKind.TURN_COMPLETED,
            self.thread_id,
            self.turn_id,
            completion=TurnCompletionStatus.INTERRUPTED,
        )

    def interrupt(self) -> None:
        self.interrupted.set()


class _BlockingThread:
    def __init__(self, thread_id: str, turn: _BlockingTurn) -> None:
        self.thread_id = thread_id
        self._turn = turn

    def start_turn(self, _request):
        return self._turn


class _Thread:
    def __init__(self, thread_id: str, effect) -> None:
        self.thread_id = thread_id
        self._effect = effect

    def start_turn(self, _request):
        return _Turn(self.thread_id, self._effect)


class _Engine:
    def __init__(self, effect) -> None:
        self.metadata = EngineMetadata("fake", "fake", "1", "test", "test")
        self._effect = effect
        self.created_threads: list[str] = []
        self.resumed_threads: list[str] = []
        self.closed = 0

    def start_thread(self, _request):
        thread_id = f"thread-{len(self.created_threads) + 1}"
        self.created_threads.append(thread_id)
        return _Thread(thread_id, self._effect)

    def resume_thread(self, thread_id, _request):
        self.resumed_threads.append(thread_id)
        return _Thread(thread_id, self._effect)

    def close(self) -> None:
        self.closed += 1


class _BlockingEngine(_Engine):
    def __init__(self) -> None:
        super().__init__(lambda _turn: None)
        self.turn = _BlockingTurn("thread-1")

    def start_thread(self, _request):
        self.created_threads.append("thread-1")
        return _BlockingThread("thread-1", self.turn)


class _CloseOnlyTurn(_BlockingTurn):
    def interrupt(self) -> None:
        pass


class _CloseOnlyEngine(_BlockingEngine):
    def __init__(self) -> None:
        super().__init__()
        self.turn = _CloseOnlyTurn("thread-1")

    def close(self) -> None:
        super().close()
        self.turn.interrupted.set()


class _Provider:
    provider_id = "fake"

    def __init__(self, engine: _Engine) -> None:
        self._engine = engine
        self.opens = 0

    def open(self):
        self.opens += 1
        return self._engine


def _service(
    tmp_path: Path,
    database_url: str,
    provider: _Provider,
    *,
    lease_seconds: int = 120,
    heartbeat_seconds: int = 30,
    turn_timeout_seconds: int = 900,
) -> RoleService:
    sources = tmp_path / "sources.json"
    sources.write_text(
        json.dumps(
            {"projects": {"alpha": {"repositories": {"primary": str(tmp_path)}}}}
        ),
        encoding="utf-8",
    )
    prompt = RenderedRoleStatePrompt("prompt", "c" * 64, (), ())
    return RoleService(
        database_url,
        RoleServiceConfig(
            project_id="alpha",
            role_id="engineering",
            instance_id="engineering-1",
            configuration_root=tmp_path,
            source_repositories_file=sources,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            turn_timeout_seconds=turn_timeout_seconds,
        ),
        provider_factory=lambda _key: provider,
        workspace_resolver=lambda _project, _work, _actor: tmp_path,
        prompt_resolver=lambda _role, _flow, _state: prompt,
    )


def test_role_service_reuses_one_engine_and_separates_work_threads(
    postgres_database: str, tmp_path: Path
) -> None:
    _seed(postgres_database, ("work-1", "work-2"))
    pending = ["work-1", "work-2"]

    def effect(turn_id: str) -> None:
        work_id = pending.pop(0)
        ProgressStore(postgres_database).record(
            ProgressDraft(
                project_id="alpha",
                work_item_id=work_id,
                role_instance_id="engineering-1",
                checkpoint_id=turn_id,
                expected_previous_sequence=0,
                status="in_progress",
                goal="Process durable work",
                step="Record the completed role turn",
                completed_action="Completed the bounded queue action",
                activity=None,
                blocker=None,
                next_action="Continue the configured flow",
                safe_summary="The role turn produced a durable effect.",
            )
        )

    engine = _Engine(effect)
    provider = _Provider(engine)
    service = _service(tmp_path, postgres_database, provider)
    try:
        assert service.run_once().status == "completed"
        assert service.run_once().status == "completed"
        assert service.run_once().status == "empty"
    finally:
        service.close()
    assert provider.opens == 1
    assert engine.created_threads == ["thread-1", "thread-2"]
    assert engine.resumed_threads == []
    assert engine.closed == 1


def test_role_service_releases_completed_turn_without_durable_effect(
    postgres_database: str, tmp_path: Path
) -> None:
    _seed(postgres_database, ("work-1",))
    service = _service(tmp_path, postgres_database, _Provider(_Engine(lambda _turn: None)))
    try:
        result = service.run_once()
    finally:
        service.close()
    assert result.status == "released"
    assert result.reason == "no-durable-effect"
    assert (
        RoleQueueStore(postgres_database)
        .get_item("alpha", "queue-work-1")
        .status
        == "ready"
    )


def test_role_service_interrupts_a_provider_turn_at_the_configured_timeout(
    postgres_database: str, tmp_path: Path
) -> None:
    _seed(postgres_database, ("work-1",))
    engine = _BlockingEngine()
    service = _service(
        tmp_path,
        postgres_database,
        _Provider(engine),
        lease_seconds=30,
        heartbeat_seconds=5,
        turn_timeout_seconds=1,
    )
    started = time.monotonic()
    try:
        result = service.run_once()
    finally:
        service.close()
    assert time.monotonic() - started < 5
    assert result.status == "released"
    assert result.reason == "provider-turn-timeout"
    assert engine.turn.interrupted.is_set()
    binding = ThreadAffinityStore(postgres_database).read(
        ThreadAffinityKey("alpha", "work-1", "engineering", "delivery")
    )
    assert binding is not None and binding.active_operation_id is None


def test_role_service_closes_an_engine_when_interrupt_does_not_end_the_stream(
    postgres_database: str, tmp_path: Path
) -> None:
    _seed(postgres_database, ("work-1",))
    engine = _CloseOnlyEngine()
    provider = _Provider(engine)
    service = _service(
        tmp_path,
        postgres_database,
        provider,
        lease_seconds=30,
        heartbeat_seconds=5,
        turn_timeout_seconds=1,
    )
    started = time.monotonic()
    try:
        result = service.run_once()
    finally:
        service.close()
    assert time.monotonic() - started < 5
    assert result.status == "released"
    assert result.reason == "provider-turn-timeout"
    assert engine.closed == 1
    assert engine.turn.interrupted.is_set()
    binding = ThreadAffinityStore(postgres_database).read(
        ThreadAffinityKey("alpha", "work-1", "engineering", "delivery")
    )
    assert binding is not None and binding.active_operation_id is None


def test_role_service_reclaims_only_an_operation_superseded_by_its_new_lease(
    postgres_database: str, tmp_path: Path
) -> None:
    _seed(postgres_database, ("work-1",))
    key = ThreadAffinityKey("alpha", "work-1", "engineering", "delivery")
    affinity = ThreadAffinityStore(postgres_database)
    affinity.bind_or_read(
        key,
        instance_id="engineering-1",
        provider_id="fake",
        prompt_digest="c" * 64,
        create_thread=lambda: "old-thread",
    )
    affinity.claim_operation(
        key, instance_id="engineering-1", prompt_digest="c" * 64
    )

    def effect(turn_id: str) -> None:
        ProgressStore(postgres_database).record(
            ProgressDraft(
                project_id="alpha",
                work_item_id="work-1",
                role_instance_id="engineering-1",
                checkpoint_id=turn_id,
                expected_previous_sequence=0,
                status="in_progress",
                goal="Recover the superseded turn",
                step="Resume the durable thread",
                completed_action="Released the operation owned by the expired lease",
                activity=None,
                blocker=None,
                next_action="Continue the flow",
                safe_summary="The newer lease resumed the existing thread safely.",
            )
        )

    engine = _Engine(effect)
    service = _service(tmp_path, postgres_database, _Provider(engine))
    try:
        result = service.run_once()
    finally:
        service.close()
    assert result.status == "completed"
    assert engine.resumed_threads == ["old-thread"]
    binding = affinity.read(key)
    assert binding is not None and binding.active_operation_id is None


def test_role_service_heartbeats_a_long_turn(
    postgres_database: str, tmp_path: Path
) -> None:
    _seed(postgres_database, ("work-1",))

    def effect(turn_id: str) -> None:
        time.sleep(6)
        ProgressStore(postgres_database).record(
            ProgressDraft(
                project_id="alpha",
                work_item_id="work-1",
                role_instance_id="engineering-1",
                checkpoint_id=turn_id,
                expected_previous_sequence=0,
                status="in_progress",
                goal="Keep the lease alive",
                step="Complete the long role turn",
                completed_action="Finished after one heartbeat interval",
                activity=None,
                blocker=None,
                next_action="Continue the flow",
                safe_summary="The long turn retained its lease.",
            )
        )

    service = _service(
        tmp_path,
        postgres_database,
        _Provider(_Engine(effect)),
        lease_seconds=30,
        heartbeat_seconds=5,
    )
    try:
        assert service.run_once().status == "completed"
    finally:
        service.close()
    with psycopg.connect(postgres_database, autocommit=True) as connection:
        heartbeat_advanced = connection.execute(
            """
            SELECT heartbeat_at > acquired_at
            FROM agentic_mesh_v5.leases
            WHERE project_id = 'alpha'
            """
        ).fetchone()[0]
    assert heartbeat_advanced is True


def test_role_service_rejects_sensitive_queue_payload_without_echoing_it(
    postgres_database: str, tmp_path: Path
) -> None:
    _seed(postgres_database, ("work-1",))
    secret = "sk-" + "x" * 32
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.queue_items
            SET payload = jsonb_build_object(
                'conversation_id', 'delivery', 'goal', %s::text
            )
            WHERE project_id = 'alpha' AND queue_item_id = 'queue-work-1'
            """,
            (secret,),
        )
    provider = _Provider(_Engine(lambda _turn: None))
    service = _service(tmp_path, postgres_database, provider)
    try:
        result = service.run_once()
    finally:
        service.close()
    assert result.status == "released"
    assert result.reason == "turn-failed"
    assert secret not in json.dumps(result.to_dict())
    assert provider.opens == 0


def test_docker_fleet_uses_exact_mapping_and_idempotent_commands(tmp_path: Path) -> None:
    mapping = tmp_path / "fleet.json"
    mapping.write_text(
        json.dumps(
            {
                "projects": {
                    "alpha": {
                        "instances": {
                            "engineering-1": {
                                "role_id": "engineering",
                                "container": "mesh-alpha-engineering-1",
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    timeouts: list[int] = []
    states = iter(("false\n", "true\n"))

    def runner(command, **kwargs):
        commands.append(command)
        timeouts.append(kwargs["timeout"])
        stdout = next(states) if command[1] == "inspect" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    supervisor = DockerContainerFleetSupervisor(mapping, runner=runner)
    wake = FleetAction("a1", "alpha", "engineering", "engineering-1", "wake", "queue")
    supervisor.apply(wake)
    supervisor.apply(wake)
    assert commands == [
        ["docker", "inspect", "--format", "{{.State.Running}}", "mesh-alpha-engineering-1"],
        ["docker", "start", "mesh-alpha-engineering-1"],
        ["docker", "inspect", "--format", "{{.State.Running}}", "mesh-alpha-engineering-1"],
    ]
    assert timeouts == [120, 120, 120]
    with pytest.raises(FleetSupervisorError, match="not provisioned"):
        supervisor.apply(
            FleetAction("a2", "bravo", "engineering", "engineering-1", "wake", "queue")
        )


def test_docker_fleet_stop_timeout_covers_the_configured_grace(tmp_path: Path) -> None:
    mapping = tmp_path / "fleet.json"
    mapping.write_text(
        json.dumps(
            {
                "projects": {
                    "alpha": {
                        "instances": {
                            "engineering-1": {
                                "role_id": "engineering",
                                "container": "mesh-alpha-engineering-1",
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[list[str], int]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        stdout = "true\n" if command[1] == "inspect" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    DockerContainerFleetSupervisor(
        mapping, runner=runner, stop_seconds=180
    ).apply(
        FleetAction(
            "a1",
            "alpha",
            "engineering",
            "engineering-1",
            "hibernate",
            "idle",
        )
    )
    assert calls[-1] == (
        [
            "docker",
            "stop",
            "--time",
            "180",
            "mesh-alpha-engineering-1",
        ],
        210,
    )


def test_role_service_cli_is_explicit() -> None:
    args = build_parser().parse_args(
        [
            "role-service",
            "--config-root",
            "config",
            "--project",
            "alpha",
            "--role",
            "engineering",
            "--instance",
            "engineering-1",
            "--source-repositories",
            "sources.json",
            "--once",
        ]
    )
    assert args.command == "role-service"
    assert args.once is True
