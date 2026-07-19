from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.document_store import DocumentContent
from agentic_mesh_v5.document_store import DocumentMetadata
from agentic_mesh_v5.document_store import DocumentPage
from agentic_mesh_v5.flow_definition import validate_flow
from agentic_mesh_v5.flow_engine import FlowEngine
from agentic_mesh_v5.flow_engine import FlowEngineConflict
from agentic_mesh_v5.flow_engine import FlowEngineError
from agentic_mesh_v5.flow_engine import TransitionSource
from agentic_mesh_v5.governance import GovernanceConflict
from agentic_mesh_v5.governance import GovernanceStore
from agentic_mesh_v5.handoffs import HandoffStore
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.queues import RoleQueueStore
from agentic_mesh_v5.routing import Router


class _Documents:
    def stat(self, path: str) -> DocumentMetadata:
        return DocumentMetadata(
            item_id=path,
            name=path.rsplit("/", 1)[-1],
            path=path,
            size=100,
            etag=f'"{path}"',
            is_folder=False,
            mime_type="text/markdown",
            created_at=None,
            modified_at=None,
        )

    def list(self, path="", *, cursor=None, page_size=200):
        return DocumentPage((), None)

    def read(self, path):
        return DocumentContent(self.stat(path), b"")

    def create(self, path, content, *, content_type):
        return self.stat(path)

    def update(self, path, content, *, content_type, expected_etag):
        return self.stat(path)


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for Postgres tests")
    database_name = f"mesh_v5_{uuid.uuid4().hex}"
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


def _flow_value():
    return {
        "schema_version": 1,
        "flow_id": "delivery",
        "leader_role": "project-manager",
        "entry_state": "analysis",
        "terminal_states": ["release"],
        "states": {
            "analysis": {
                "owner_role": "business-analyst",
                "purpose": "Clarify the delivery.",
                "artifact": "projects/{project_id}/work/{work_item_id}/analysis.md",
                "consults": [{"id": "review", "role": "reviewer"}],
                "gates": [{"id": "owner-gate", "reviewer_role": "reviewer"}],
                "informs": [{"id": "notice", "role": "reviewer"}],
                "routes": [
                    {
                        "id": "analysis-complete",
                        "outcome": "completed",
                        "target_state": "release",
                        "target_role": "release-manager",
                    }
                ],
                "terminal": False,
            },
            "release": {
                "owner_role": "release-manager",
                "purpose": "Confirm the delivery.",
                "artifact": "projects/{project_id}/work/{work_item_id}/release.md",
                "consults": [],
                "gates": [],
                "routes": [],
                "terminal": True,
            },
        },
    }


@pytest.fixture
def flow_database(postgres_database: str):
    assert MigrationRunner(postgres_database).migrate().current_version == 31
    lifecycle = LifecycleStore(postgres_database)
    lifecycle.create_project(
        project_id="alpha", display_name="Alpha", sponsor_ids=("sponsor-1",)
    )
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
            VALUES ('alpha', 'business-analyst', 'business-analyst'),
                   ('alpha', 'release-manager', 'release-manager'),
                   ('alpha', 'reviewer', 'reviewer')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.role_instances
                (project_id, instance_id, role_id, status)
            VALUES ('alpha', 'analyst-1', 'business-analyst', 'running'),
                   ('alpha', 'release-1', 'release-manager', 'running'),
                   ('alpha', 'reviewer-1', 'reviewer', 'running')
            """
        )
    lifecycle.create_work_item(
        project_id="alpha",
        work_item_id="work-1",
        title="Work 1",
        owner_role_id="business-analyst",
        actor_id="project-manager",
        correlation_id="create-work-1",
    )
    lifecycle.transition_work_item(
        project_id="alpha",
        work_item_id="work-1",
        target_status="active",
        actor_id="project-manager",
        correlation_id="activate-work-1",
        expected_version=1,
    )
    queues = RoleQueueStore(postgres_database)
    for role in ("business-analyst", "release-manager", "reviewer"):
        queues.create_queue(project_id="alpha", queue_id=role, role_id=role)
    queues.enqueue(
        project_id="alpha",
        queue_id="business-analyst",
        queue_item_id="source-item",
        work_item_id="work-1",
        idempotency_key="source-item",
        payload={},
    )
    source = queues.claim(
        project_id="alpha",
        queue_id="business-analyst",
        owner_instance_id="analyst-1",
        lease_seconds=3600,
    )
    return postgres_database, lifecycle, queues, source


def _verify_artifact(database_url: str, engine: FlowEngine) -> None:
    run = engine.get("alpha", "work-1")
    path = f"projects/alpha/work/work-1/{run.current_state}.md"
    GovernanceStore(database_url, _Documents()).verify_artifact(
        project_id="alpha",
        work_item_id="work-1",
        path=path,
        actor_role_id=run.owner_role_id,
        record_id=f"{run.current_state}-artifact",
    )


def _approve_gate(database_url: str, engine: FlowEngine, obligation_id: str) -> None:
    obligation = next(
        item for item in engine.obligations("alpha", "work-1")
        if item.kind == "gate" and item.obligation_id == obligation_id
    )
    GovernanceStore(database_url, _Documents()).decide_gate(
        project_id="alpha",
        work_item_id="work-1",
        obligation_id=obligation_id,
        decision="approved",
        actor_role_id=obligation.accountable_role_id,
        record_id=f"{obligation.state}-{obligation_id}",
        evidence={"uri": f"evidence://{obligation_id}"},
    )


def _respond_to_consult(database_url: str, obligation_id: str) -> None:
    GovernanceStore(database_url, _Documents()).record_consultation(
        project_id="alpha",
        work_item_id="work-1",
        obligation_id=obligation_id,
        decision="responded",
        actor_role_id="reviewer",
        record_id=f"consult-{obligation_id}",
        evidence={"response": "reviewed"},
    )


def _ready_flow(flow_database):
    database_url, _lifecycle, _queues, source = flow_database
    engine = FlowEngine(database_url)
    engine.start(
        project_id="alpha",
        work_item_id="work-1",
        flow=validate_flow(_flow_value(), digest="a" * 64),
        fields={},
        actor_id="project-manager",
        operation_id="start-flow",
    )
    for kind, obligation_id in (("consult", "review"), ("inform", "notice")):
        engine.dispatch(
            project_id="alpha",
            work_item_id="work-1",
            kind=kind,
            obligation_id=obligation_id,
            actor_id="business-analyst",
        )
    _respond_to_consult(database_url, "review")
    _verify_artifact(database_url, engine)
    _approve_gate(database_url, engine, "owner-gate")
    with pytest.raises(GovernanceConflict, match="record id conflicts"):
        GovernanceStore(database_url, _Documents()).decide_gate(
            project_id="alpha",
            work_item_id="work-1",
            obligation_id="owner-gate",
            decision="rejected",
            actor_role_id="reviewer",
            record_id="analysis-owner-gate",
            reason="changed request",
        )
    return database_url, engine, source


def _prepare(engine: FlowEngine, source):
    return engine.prepare_transition(
        project_id="alpha",
        work_item_id="work-1",
        outcome="completed",
        fields={},
        source=TransitionSource(source.lease_id, source.lease_token),
        expected_version=1,
        actor_id="business-analyst",
        operation_id="prepare-release",
    )


def test_flow_run_requires_obligations_and_accepted_handoff(flow_database) -> None:
    database_url, lifecycle, queues, source = flow_database
    engine = FlowEngine(database_url)
    flow = validate_flow(_flow_value(), digest="a" * 64)
    started = engine.start(
        project_id="alpha",
        work_item_id="work-1",
        flow=flow,
        fields={"architecture_impact": "no-material"},
        actor_id="project-manager",
        operation_id="start-flow",
    )
    assert started.current_state == "analysis"
    assert started.owner_role_id == "business-analyst"
    assert engine.start(
        project_id="alpha",
        work_item_id="work-1",
        flow=flow,
        fields={"architecture_impact": "no-material"},
        actor_id="project-manager",
        operation_id="start-flow",
    ) == started
    with pytest.raises(FlowEngineConflict, match="operation id conflicts"):
        engine.start(
            project_id="alpha",
            work_item_id="work-1",
            flow=flow,
            fields={"architecture_impact": "material"},
            actor_id="project-manager",
            operation_id="start-flow",
        )
    obligations = engine.obligations("alpha", "work-1")
    assert {(item.kind, item.obligation_id) for item in obligations} == {
        ("artifact", "artifact"),
        ("consult", "review"),
        ("gate", "owner-gate"),
        ("inform", "notice"),
    }
    with pytest.raises(FlowEngineConflict, match="not satisfied"):
        engine.prepare_transition(
            project_id="alpha",
            work_item_id="work-1",
            outcome="completed",
            fields={},
            source=TransitionSource(source.lease_id, source.lease_token),
            expected_version=1,
            actor_id="business-analyst",
            operation_id="prepare-release",
        )

    for kind, obligation_id in (("consult", "review"), ("inform", "notice")):
        assert engine.dispatch(
            project_id="alpha",
            work_item_id="work-1",
            kind=kind,
            obligation_id=obligation_id,
            actor_id="business-analyst",
        ).status == "dispatched"
    _respond_to_consult(database_url, "review")
    _verify_artifact(database_url, engine)
    _approve_gate(database_url, engine, "owner-gate")

    changed_snapshot = flow.snapshot
    changed_snapshot["states"]["analysis"]["routes"][0]["target_state"] = "changed"
    assert flow.snapshot["states"]["analysis"]["routes"][0]["target_state"] == (
        "release"
    )
    pending = engine.prepare_transition(
        project_id="alpha",
        work_item_id="work-1",
        outcome="completed",
        fields={},
        source=TransitionSource(source.lease_id, source.lease_token),
        expected_version=1,
        actor_id="business-analyst",
        operation_id="prepare-release",
    )
    assert pending.status == "handoff_pending"
    assert pending.pending_target_state == "release"
    assert pending.version == 2
    assert len(engine.obligations("alpha", "work-1")) == 4
    assert engine.prepare_transition(
        project_id="alpha",
        work_item_id="work-1",
        outcome="completed",
        fields={},
        source=TransitionSource(source.lease_id, source.lease_token),
        expected_version=1,
        actor_id="business-analyst",
        operation_id="prepare-release",
    ) == pending
    with pytest.raises(FlowEngineConflict, match="not accepted"):
        engine.pickup_transition(
            project_id="alpha",
            work_item_id="work-1",
            expected_version=2,
            actor_id="release-manager",
            operation_id="pickup-release",
        )

    target = queues.claim(
        project_id="alpha",
        queue_id="release-manager",
        owner_instance_id="release-1",
        lease_seconds=3600,
    )
    handoffs = HandoffStore(database_url)
    handoffs.claim(
        project_id="alpha",
        handoff_id=pending.pending_handoff_id,
        lease_id=target.lease_id,
        lease_token=target.lease_token,
    )
    handoffs.accept(
        project_id="alpha",
        handoff_id=pending.pending_handoff_id,
        lease_id=target.lease_id,
        lease_token=target.lease_token,
    )
    picked_up = engine.pickup_transition(
        project_id="alpha",
        work_item_id="work-1",
        expected_version=2,
        actor_id="release-manager",
        operation_id="pickup-release",
    )
    assert picked_up.current_state == "release"
    assert picked_up.owner_role_id == "release-manager"
    assert picked_up.version == 3
    assert engine.pickup_transition(
        project_id="alpha",
        work_item_id="work-1",
        expected_version=2,
        actor_id="release-manager",
        operation_id="pickup-release",
    ) == picked_up
    assert lifecycle.get_work_item("alpha", "work-1").owner_role_id == (
        "release-manager"
    )

    _verify_artifact(database_url, engine)
    completed = engine.complete(
        project_id="alpha",
        work_item_id="work-1",
        expected_version=3,
        actor_id="release-manager",
        operation_id="complete-flow",
        evidence={"acceptance": "evidence://accepted"},
    )
    assert completed.status == "completed"
    assert engine.complete(
        project_id="alpha",
        work_item_id="work-1",
        expected_version=3,
        actor_id="release-manager",
        operation_id="complete-flow",
        evidence={"acceptance": "evidence://accepted"},
    ) == completed
    assert lifecycle.get_work_item("alpha", "work-1").status == "completed"
    with psycopg.connect(database_url) as connection:
        actions = connection.execute(
            """
            SELECT action FROM agentic_mesh_v5.flow_transition_journal
            WHERE project_id = 'alpha' AND work_item_id = 'work-1'
            ORDER BY sequence
            """
        ).fetchall()
    assert actions == [("start",), ("prepare",), ("pickup",), ("complete",)]
    with psycopg.connect(database_url) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            connection.execute(
                """
                UPDATE agentic_mesh_v5.flow_transition_journal SET action = 'start'
                WHERE project_id = 'alpha' AND work_item_id = 'work-1'
                  AND action = 'complete'
                """
            )


def test_flow_start_rejects_wrong_owner_and_operation_conflict(flow_database) -> None:
    database_url, _lifecycle, _queues, _source = flow_database
    engine = FlowEngine(database_url)
    value = deepcopy(_flow_value())
    value["states"]["analysis"]["owner_role"] = "release-manager"
    flow = validate_flow(value, digest="b" * 64)
    with pytest.raises(FlowEngineConflict, match="entry owner"):
        engine.start(
            project_id="alpha",
            work_item_id="work-1",
            flow=flow,
            fields={},
            actor_id="project-manager",
            operation_id="start-flow",
        )


def test_dispatch_rolls_back_route_and_obligation_together(
    flow_database, monkeypatch
) -> None:
    database_url, _lifecycle, _queues, _source = flow_database
    engine = FlowEngine(database_url)
    engine.start(
        project_id="alpha",
        work_item_id="work-1",
        flow=validate_flow(_flow_value(), digest="a" * 64),
        fields={},
        actor_id="project-manager",
        operation_id="start-flow",
    )
    original = Router.route_in_transaction

    def route_then_fail(router, connection, draft):
        original(router, connection, draft)
        raise RuntimeError("injected dispatch failure")

    monkeypatch.setattr(Router, "route_in_transaction", route_then_fail)
    with pytest.raises(FlowEngineError, match="dispatch failed"):
        engine.dispatch(
            project_id="alpha",
            work_item_id="work-1",
            kind="consult",
            obligation_id="review",
            actor_id="business-analyst",
        )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.queue_items
            WHERE project_id = 'alpha' AND queue_id = 'reviewer'
            """
        ).fetchone()[0] == 0
        assert connection.execute(
            """
            SELECT status FROM agentic_mesh_v5.flow_obligations
            WHERE project_id = 'alpha' AND work_item_id = 'work-1'
              AND obligation_kind = 'consult' AND obligation_id = 'review'
            """
        ).fetchone() == ("pending",)


def test_prepare_recovers_from_crash_after_reservation(
    flow_database, monkeypatch
) -> None:
    database_url, engine, source = _ready_flow(flow_database)
    original_offer = engine._handoffs.offer
    attempts = 0

    def fail_once(draft):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FlowEngineConflict("injected handoff outage")
        return original_offer(draft)

    monkeypatch.setattr(engine._handoffs, "offer", fail_once)
    with pytest.raises(FlowEngineConflict, match="injected"):
        _prepare(engine, source)
    assert engine.get("alpha", "work-1").status == "handoff_preparing"
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.handoffs"
        ).fetchone()[0] == 0

    recovered = _prepare(engine, source)
    assert recovered.status == "handoff_pending"
    assert recovered.version == 2


def test_concurrent_exact_prepare_has_one_handoff_and_journal_entry(
    flow_database,
) -> None:
    database_url, engine, source = _ready_flow(flow_database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: _prepare(engine, source), range(2)))

    assert {item.pending_handoff_id for item in results} == {
        results[0].pending_handoff_id
    }
    assert {item.version for item in results} == {2}
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.handoffs"
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT count(*) FROM agentic_mesh_v5.flow_transition_journal
            WHERE action = 'prepare'
            """
        ).fetchone()[0] == 1
