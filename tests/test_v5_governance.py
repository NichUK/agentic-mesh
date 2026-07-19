from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.document_store import DocumentContent, DocumentMetadata, DocumentNotFound
from agentic_mesh_v5.document_store import DocumentPage
from agentic_mesh_v5.flow_definition import validate_flow
from agentic_mesh_v5.flow_engine import FlowEngine, FlowEngineConflict, TransitionSource
from agentic_mesh_v5.governance import GovernanceConflict, GovernanceStore
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.queues import RoleQueueStore


class _Documents:
    def __init__(self, paths=()):
        self.paths = set(paths)
        self.etags = {}

    def stat(self, path: str) -> DocumentMetadata:
        if path not in self.paths:
            raise DocumentNotFound("document was not found")
        return DocumentMetadata(
            item_id=path,
            name=path.rsplit("/", 1)[-1],
            path=path,
            size=256,
            etag=self.etags.get(path, f'"etag:{path}"'),
            is_folder=False,
            mime_type="text/markdown",
            created_at="2026-07-18T10:00:00Z",
            modified_at="2026-07-18T10:00:00Z",
        )

    def list(self, path="", *, cursor=None, page_size=200):
        return DocumentPage((), None)

    def read(self, path):
        return DocumentContent(self.stat(path), b"evidence")

    def create(self, path, content, *, content_type):
        self.paths.add(path)
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


@pytest.fixture
def governance_database(postgres_database: str) -> str:
    assert MigrationRunner(postgres_database).migrate().current_version == 29
    lifecycle = LifecycleStore(postgres_database)
    lifecycle.create_project(
        project_id="alpha", display_name="Alpha", sponsor_ids=("sponsor-1",)
    )
    roles = (
        "product-manager",
        "enterprise-architect",
        "solution-architect",
        "project-manager",
        "sponsor",
    )
    with psycopg.connect(postgres_database) as connection:
        for role in roles:
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.roles(project_id, role_id, template_id)
                VALUES ('alpha', %s, %s)
                """,
                (role, role),
            )
            connection.execute(
                """
                INSERT INTO agentic_mesh_v5.role_instances
                    (project_id, instance_id, role_id, status)
                VALUES ('alpha', %s, %s, 'running')
                """,
                (f"{role}-1", role),
            )
    queues = RoleQueueStore(postgres_database)
    for role in roles:
        queues.create_queue(project_id="alpha", queue_id=role, role_id=role)
    return postgres_database


def _impact_flow():
    return {
        "schema_version": 1,
        "flow_id": "architecture-impact",
        "leader_role": "project-manager",
        "entry_state": "product-definition",
        "terminal_states": ["enterprise-alignment", "solution-design"],
        "states": {
            "product-definition": {
                "owner_role": "product-manager",
                "purpose": "Classify enterprise architecture impact.",
                "artifact": "work-items/{work_item_id}/product.md",
                "consults": [{"id": "enterprise-impact", "role": "enterprise-architect"}],
                "gates": [
                    {
                        "id": "architecture-impact",
                        "type": "architecture_impact",
                        "reviewer_role": "product-manager",
                    }
                ],
                "informs": [],
                "routes": [
                    {
                        "id": "no-material-route",
                        "outcome": "completed",
                        "target_state": "solution-design",
                        "target_role": "solution-architect",
                        "when": {"architecture_impact": ["no-material"]},
                    },
                    {
                        "id": "alignment-route",
                        "outcome": "completed",
                        "target_state": "enterprise-alignment",
                        "target_role": "enterprise-architect",
                        "when": {"architecture_impact": ["material", "uncertain"]},
                    },
                ],
                "terminal": False,
            },
            "enterprise-alignment": {
                "owner_role": "enterprise-architect",
                "purpose": "Align material work.",
                "artifact": "work-items/{work_item_id}/alignment.md",
                "consults": [],
                "gates": [],
                "informs": [],
                "routes": [],
                "terminal": True,
            },
            "solution-design": {
                "owner_role": "solution-architect",
                "purpose": "Design the solution.",
                "artifact": "work-items/{work_item_id}/solution.md",
                "consults": [],
                "gates": [],
                "informs": [],
                "routes": [],
                "terminal": True,
            },
        },
    }


def _review_flow(*, sponsor_gate=False):
    gates = [
        {
            "id": "owner-review",
            "type": "document_owner_review",
            "reviewer_role": "solution-architect",
        },
        {
            "id": "conformance",
            "type": "architecture_conformance",
            "reviewer_role": "enterprise-architect",
        },
    ]
    if sponsor_gate:
        gates = [
            {
                "id": "sponsor-signoff",
                "type": "human_response",
                "requested_from": "sponsor",
            }
        ]
    return {
        "schema_version": 1,
        "flow_id": "reviews",
        "leader_role": "project-manager",
        "entry_state": "solution-design",
        "terminal_states": ["release"],
        "states": {
            "solution-design": {
                "owner_role": "solution-architect",
                "purpose": "Review the solution.",
                "artifact": "work-items/{work_item_id}/solution.md",
                "consults": [],
                "gates": gates,
                "informs": [],
                "routes": [
                    {
                        "id": "release-route",
                        "outcome": "completed",
                        "target_state": "release",
                        "target_role": "product-manager",
                    }
                ],
                "terminal": False,
            },
            "release": {
                "owner_role": "product-manager",
                "purpose": "Release.",
                "artifact": "work-items/{work_item_id}/release.md",
                "consults": [],
                "gates": [],
                "informs": [],
                "routes": [],
                "terminal": True,
            },
        },
    }


def _consult_flow():
    value = _review_flow()
    value["flow_id"] = "consult-exception"
    value["states"]["solution-design"]["gates"] = [
        {
            "id": "owner-review",
            "type": "document_owner_review",
            "reviewer_role": "solution-architect",
        }
    ]
    value["states"]["solution-design"]["consults"] = [
        {"id": "enterprise-input", "role": "enterprise-architect"}
    ]
    return value


def _start(database_url: str, *, work_item_id: str, owner: str, flow_value):
    lifecycle = LifecycleStore(database_url)
    lifecycle.create_work_item(
        project_id="alpha",
        work_item_id=work_item_id,
        title=work_item_id,
        owner_role_id=owner,
        actor_id="project-manager",
        correlation_id=f"create-{work_item_id}",
    )
    lifecycle.transition_work_item(
        project_id="alpha",
        work_item_id=work_item_id,
        target_status="active",
        actor_id="project-manager",
        correlation_id=f"activate-{work_item_id}",
        expected_version=1,
    )
    queues = RoleQueueStore(database_url)
    queues.enqueue(
        project_id="alpha",
        queue_id=owner,
        queue_item_id=f"source-{work_item_id}",
        work_item_id=work_item_id,
        idempotency_key=f"source-{work_item_id}",
        payload={},
    )
    source = queues.claim(
        project_id="alpha",
        queue_id=owner,
        owner_instance_id=f"{owner}-1",
        lease_seconds=3600,
    )
    engine = FlowEngine(database_url)
    engine.start(
        project_id="alpha",
        work_item_id=work_item_id,
        flow=validate_flow(flow_value, digest="a" * 64),
        fields={},
        actor_id="project-manager",
        operation_id=f"start-{work_item_id}",
    )
    return engine, source


@pytest.mark.parametrize(
    ("impact", "target"),
    [
        ("no-material", "solution-design"),
        ("material", "enterprise-alignment"),
        ("uncertain", "enterprise-alignment"),
    ],
)
def test_architecture_impact_is_pinned_into_route_selection(
    governance_database: str, impact: str, target: str
) -> None:
    database_url = governance_database
    work_item_id = f"impact-{impact}"
    engine, source = _start(
        database_url,
        work_item_id=work_item_id,
        owner="product-manager",
        flow_value=_impact_flow(),
    )
    path = f"work-items/{work_item_id}/product.md"
    governance = GovernanceStore(database_url, _Documents((path,)))
    artifact = governance.verify_artifact(
        project_id="alpha",
        work_item_id=work_item_id,
        path=path,
        actor_role_id="product-manager",
        record_id="product-artifact",
    )
    assert governance.verify_artifact(
        project_id="alpha",
        work_item_id=work_item_id,
        path=path,
        actor_role_id="product-manager",
        record_id="product-artifact",
    ) == artifact
    engine.dispatch(
        project_id="alpha",
        work_item_id=work_item_id,
        kind="consult",
        obligation_id="enterprise-impact",
        actor_id="product-manager",
    )
    governance.record_consultation(
        project_id="alpha",
        work_item_id=work_item_id,
        obligation_id="enterprise-impact",
        decision="responded",
        actor_role_id="enterprise-architect",
        record_id="enterprise-response",
        evidence={"affected_domains": ["application"]},
    )
    decision = governance.decide_gate(
        project_id="alpha",
        work_item_id=work_item_id,
        obligation_id="architecture-impact",
        decision="approved",
        actor_role_id="product-manager",
        record_id="impact-decision",
        reason="Evidence-based classification.",
        evidence={"rationale": "Scoped assessment completed."},
        architecture_impact=impact,
    )
    assert governance.decide_gate(
        project_id="alpha",
        work_item_id=work_item_id,
        obligation_id="architecture-impact",
        decision="approved",
        actor_role_id="product-manager",
        record_id="impact-decision",
        reason="Evidence-based classification.",
        evidence={"rationale": "Scoped assessment completed."},
        architecture_impact=impact,
    ) == decision

    conflicting = "material" if impact == "no-material" else "no-material"
    with pytest.raises(FlowEngineConflict, match="conflicts with governance"):
        engine.prepare_transition(
            project_id="alpha",
            work_item_id=work_item_id,
            outcome="completed",
            fields={"architecture_impact": conflicting},
            source=TransitionSource(source.lease_id, source.lease_token),
            expected_version=1,
            actor_id="product-manager",
            operation_id="conflicting-route",
        )

    pending = engine.prepare_transition(
        project_id="alpha",
        work_item_id=work_item_id,
        outcome="completed",
        fields={},
        source=TransitionSource(source.lease_id, source.lease_token),
        expected_version=1,
        actor_id="product-manager",
        operation_id="governed-route",
    )
    assert pending.pending_target_state == target
    assert pending.fields["architecture_impact"] == impact
    assert artifact.document_path == path
    assert artifact.document_etag == f'"etag:{path}"'


def test_missing_documents_and_rejected_reviews_do_not_satisfy_flow(
    governance_database: str,
) -> None:
    database_url = governance_database
    engine, source = _start(
        database_url,
        work_item_id="review-work",
        owner="solution-architect",
        flow_value=_review_flow(),
    )
    path = "work-items/review-work/solution.md"
    documents = _Documents()
    governance = GovernanceStore(database_url, documents)
    with pytest.raises(DocumentNotFound):
        governance.verify_artifact(
            project_id="alpha",
            work_item_id="review-work",
            path=path,
            actor_role_id="solution-architect",
            record_id="missing-artifact",
        )
    assert governance.records("alpha", "review-work") == ()

    documents.paths.add(path)
    def verify():
        return governance.verify_artifact(
            project_id="alpha",
            work_item_id="review-work",
            path=path,
            actor_role_id="solution-architect",
            record_id="solution-artifact",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        artifacts = list(pool.map(lambda _index: verify(), range(2)))
    assert artifacts[0] == artifacts[1]
    assert len(governance.records("alpha", "review-work")) == 1
    documents.etags[path] = '"changed"'
    with pytest.raises(GovernanceConflict, match="artifact has changed"):
        governance.decide_gate(
            project_id="alpha",
            work_item_id="review-work",
            obligation_id="owner-review",
            decision="approved",
            actor_role_id="solution-architect",
            record_id="stale-owner-review",
        )
    documents.etags[path] = f'"etag:{path}"'
    for gate_id, actor in (
        ("owner-review", "solution-architect"),
        ("conformance", "enterprise-architect"),
    ):
        governance.decide_gate(
            project_id="alpha",
            work_item_id="review-work",
            obligation_id=gate_id,
            decision="rejected",
            actor_role_id=actor,
            record_id=f"{gate_id}-rejected",
            reason="Changes are required.",
        )
    assert {
        item.obligation_id: item.status for item in engine.obligations("alpha", "review-work")
        if item.kind == "gate"
    } == {"conformance": "pending", "owner-review": "pending"}

    governance.decide_gate(
        project_id="alpha",
        work_item_id="review-work",
        obligation_id="owner-review",
        decision="approved",
        actor_role_id="solution-architect",
        record_id="owner-review-approved",
    )
    documents.etags[path] = '"changed-after-review"'
    with pytest.raises(GovernanceConflict, match="reviewed state artifact has changed"):
        governance.decide_gate(
            project_id="alpha",
            work_item_id="review-work",
            obligation_id="owner-review",
            decision="approved",
            actor_role_id="solution-architect",
            record_id="owner-review-approved",
        )
    documents.etags[path] = f'"etag:{path}"'
    with pytest.raises(FlowEngineConflict, match="not satisfied"):
        engine.prepare_transition(
            project_id="alpha",
            work_item_id="review-work",
            outcome="completed",
            fields={},
            source=TransitionSource(source.lease_id, source.lease_token),
            expected_version=1,
            actor_id="solution-architect",
            operation_id="blocked-release",
        )
    governance.decide_gate(
        project_id="alpha",
        work_item_id="review-work",
        obligation_id="conformance",
        decision="approved",
        actor_role_id="enterprise-architect",
        record_id="conformance-approved",
        evidence={"requirements_checked": True},
    )
    assert engine.prepare_transition(
        project_id="alpha",
        work_item_id="review-work",
        outcome="completed",
        fields={},
        source=TransitionSource(source.lease_id, source.lease_token),
        expected_version=1,
        actor_id="solution-architect",
        operation_id="approved-release",
    ).pending_target_state == "release"

    with psycopg.connect(database_url) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            connection.execute(
                """
                UPDATE agentic_mesh_v5.governance_records SET reason = 'changed'
                WHERE project_id = 'alpha' AND record_id = 'conformance-approved'
                """
            )


def test_consultation_exception_requires_reason_and_accountable_actor(
    governance_database: str,
) -> None:
    database_url = governance_database
    engine, source = _start(
        database_url,
        work_item_id="consult-work",
        owner="solution-architect",
        flow_value=_consult_flow(),
    )
    path = "work-items/consult-work/solution.md"
    governance = GovernanceStore(database_url, _Documents((path,)))
    governance.verify_artifact(
        project_id="alpha",
        work_item_id="consult-work",
        path=path,
        actor_role_id="solution-architect",
        record_id="solution-artifact",
    )
    with pytest.raises(ValueError, match="reason is required"):
        governance.record_consultation(
            project_id="alpha",
            work_item_id="consult-work",
            obligation_id="enterprise-input",
            decision="exception",
            actor_role_id="solution-architect",
            record_id="blank-exception",
        )
    with pytest.raises(GovernanceConflict, match="owner or flow leader"):
        governance.record_consultation(
            project_id="alpha",
            work_item_id="consult-work",
            obligation_id="enterprise-input",
            decision="exception",
            actor_role_id="product-manager",
            record_id="wrong-actor",
            reason="Unavailable during incident.",
        )
    governance.record_consultation(
        project_id="alpha",
        work_item_id="consult-work",
        obligation_id="enterprise-input",
        decision="exception",
        actor_role_id="solution-architect",
        record_id="consult-exception",
        reason="Enterprise input is not relevant to this isolated correction.",
    )
    governance.decide_gate(
        project_id="alpha",
        work_item_id="consult-work",
        obligation_id="owner-review",
        decision="approved",
        actor_role_id="solution-architect",
        record_id="owner-review",
    )
    assert engine.prepare_transition(
        project_id="alpha",
        work_item_id="consult-work",
        outcome="completed",
        fields={},
        source=TransitionSource(source.lease_id, source.lease_token),
        expected_version=1,
        actor_id="solution-architect",
        operation_id="exception-route",
    ).pending_target_state == "release"


def test_sponsor_gate_is_reserved_for_sponsor_approval(governance_database: str) -> None:
    database_url = governance_database
    _engine, _source = _start(
        database_url,
        work_item_id="sponsor-work",
        owner="solution-architect",
        flow_value=_review_flow(sponsor_gate=True),
    )
    path = "work-items/sponsor-work/solution.md"
    governance = GovernanceStore(database_url, _Documents((path,)))
    governance.verify_artifact(
        project_id="alpha",
        work_item_id="sponsor-work",
        path=path,
        actor_role_id="solution-architect",
        record_id="solution-artifact",
    )
    with pytest.raises(GovernanceConflict, match="reserved for sponsor approval"):
        governance.decide_gate(
            project_id="alpha",
            work_item_id="sponsor-work",
            obligation_id="sponsor-signoff",
            decision="approved",
            actor_role_id="sponsor",
            record_id="attempted-sponsor-approval",
        )
