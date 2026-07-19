from __future__ import annotations

import os
import uuid
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.ado_adapter import AdoComment, AdoConflict, AdoWorkItem
from agentic_mesh_v5.ado_progress import AdoMilestonePublisher
from agentic_mesh_v5.ado_progress import AdoProgressBlocked
from agentic_mesh_v5.ado_progress import AdoProgressConflict
from agentic_mesh_v5.ado_progress import MilestoneEvidence
from agentic_mesh_v5.ado_progress import MilestoneRequest
from agentic_mesh_v5.ado_progress import milestone_target_state
from agentic_mesh_v5.database import MigrationRunner


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
def progress_database(postgres_database: str) -> str:
    assert MigrationRunner(postgres_database).migrate().current_version == 31
    with psycopg.connect(postgres_database) as connection:
        connection.execute(
            "INSERT INTO agentic_mesh_v5.projects(project_id,display_name) "
            "VALUES ('alpha','Alpha')"
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.roles(project_id,role_id,template_id)
            VALUES ('alpha','project-manager','project-manager')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_items
                (project_id,work_item_id,assigned_role_id,title,status)
            VALUES ('alpha','mesh-1','project-manager','Mesh work','active')
            """
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.project_manifest_snapshots
                (project_id,manifest_digest,source_revision,source_path,
                 snapshot,registered_by)
            VALUES ('alpha',%s,%s,'agentic-mesh/project.yaml','{}','pm')
            """,
            ("d" * 64, "a" * 40),
        )
        connection.execute(
            """
            INSERT INTO agentic_mesh_v5.work_item_ado_links
                (project_id,work_item_id,manifest_digest,organization_url,
                 ado_project,external_work_item_id,external_url,linked_by)
            VALUES ('alpha','mesh-1',%s,'https://dev.azure.com/example',
                    'alpha-ado',101,
                    'https://dev.azure.com/example/alpha-ado/_workitems/edit/101',
                    'pm')
            """,
            ("d" * 64,),
        )
    return postgres_database


class SimulatedCrash(BaseException):
    pass


class FakeProgressClient:
    def __init__(self, *, state: str = "New") -> None:
        self.state = state
        self.revision = 1
        self.comments: dict[str, AdoComment] = {}
        self.comment_calls = 0
        self.update_calls: list[tuple[str, str]] = []
        self.operation_ids: list[str] = []
        self.crash_after_comment = False
        self.crash_after_update = False
        self.fail_comment = False
        self.manual_state_on_update: str | None = None

    def read(self, *, project_id: str, work_item_id: str) -> AdoWorkItem:
        assert (project_id, work_item_id) == ("alpha", "mesh-1")
        return AdoWorkItem(
            101,
            self.revision,
            "alpha-ado",
            {"System.State": self.state, "System.Title": "Mesh work"},
        )

    def comment_once(
        self,
        *,
        project_id: str,
        work_item_id: str,
        marker: str,
        text: str,
    ) -> AdoComment:
        assert (project_id, work_item_id) == ("alpha", "mesh-1")
        self.comment_calls += 1
        if self.fail_comment:
            raise RuntimeError("ADO offline")
        comment = self.comments.get(marker)
        if comment is None:
            comment = AdoComment(len(self.comments) + 1, text)
            self.comments[marker] = comment
        if self.crash_after_comment:
            self.crash_after_comment = False
            raise SimulatedCrash()
        return comment

    def update_state(
        self,
        *,
        project_id: str,
        work_item_id: str,
        operation_id: str,
        expected_state: str,
        target_state: str,
        actor_id: str,
    ) -> int:
        assert project_id == "alpha" and work_item_id == "mesh-1"
        assert operation_id.startswith("milestone-state:")
        self.operation_ids.append(operation_id)
        assert actor_id
        if self.manual_state_on_update is not None:
            self.state = self.manual_state_on_update
            self.revision += 1
            self.manual_state_on_update = None
        if self.state != expected_state:
            raise AdoConflict("ADO fields changed outside the expected state")
        self.update_calls.append((expected_state, target_state))
        self.state = target_state
        self.revision += 1
        if self.crash_after_update:
            self.crash_after_update = False
            raise SimulatedCrash()
        return self.revision


def _evidence(*kinds: str) -> tuple[MilestoneEvidence, ...]:
    return tuple(MilestoneEvidence(kind, f"evidence://{kind}") for kind in kinds)


def _request(
    sequence: int,
    kind: str,
    evidence: tuple[MilestoneEvidence, ...],
    *,
    milestone_id: str | None = None,
    actor_id: str = "project-manager",
) -> MilestoneRequest:
    return MilestoneRequest(
        project_id="alpha",
        work_item_id="mesh-1",
        milestone_id=milestone_id or f"milestone-{sequence}",
        sequence=sequence,
        kind=kind,
        summary=f"Summary for {kind}",
        evidence=evidence,
        next_action=f"Continue after {kind}",
        actor_id=actor_id,
    )


@pytest.mark.parametrize(
    ("kind", "evidence", "target"),
    [
        ("start", _evidence("source"), "Active"),
        ("handoff", _evidence("handoff"), None),
        ("blocker", _evidence("blocker"), None),
        ("recovery", _evidence("recovery"), None),
        ("pull-request", _evidence("pull-request"), None),
        (
            "pull-request",
            _evidence("pull-request", "implementation", "automated-test"),
            "Resolved",
        ),
        (
            "deployment",
            _evidence("deployment", "implementation", "automated-test"),
            "Resolved",
        ),
        ("acceptance", _evidence("acceptance", "owner-review"), "Closed"),
    ],
)
def test_milestone_state_mapping(kind, evidence, target) -> None:
    assert milestone_target_state(kind, evidence) == target


def test_complete_milestone_sequence_publishes_useful_comments_and_states(
    progress_database: str,
) -> None:
    client = FakeProgressClient()
    publisher = AdoMilestonePublisher(progress_database, client=client)
    requests = (
        _request(1, "start", _evidence("source")),
        _request(2, "handoff", _evidence("handoff")),
        _request(3, "blocker", _evidence("blocker")),
        _request(4, "recovery", _evidence("recovery")),
        _request(5, "pull-request", _evidence("pull-request")),
        _request(
            6,
            "deployment",
            _evidence("deployment", "implementation", "automated-test"),
        ),
        _request(7, "acceptance", _evidence("acceptance", "owner-review")),
    )

    results = tuple(publisher.publish(item) for item in requests)

    assert all(item.status == "published" for item in results)
    assert client.state == "Closed"
    assert client.update_calls == [
        ("New", "Active"),
        ("Active", "Resolved"),
        ("Resolved", "Closed"),
    ]
    assert len(client.comments) == 7
    assert all("Status:" in item.text for item in client.comments.values())
    assert all("Evidence:" in item.text for item in client.comments.values())
    assert all("Next action:" in item.text for item in client.comments.values())
    assert results[4].state_disposition == "not-requested"
    with psycopg.connect(progress_database) as connection:
        assert connection.execute(
            """
            SELECT status,version FROM agentic_mesh_v5.work_items
            WHERE project_id='alpha' AND work_item_id='mesh-1'
            """
        ).fetchone() == ("active", 1)


def test_crash_after_comment_resumes_without_duplicate_and_allows_pickup(
    progress_database: str,
) -> None:
    client = FakeProgressClient()
    client.crash_after_comment = True
    publisher = AdoMilestonePublisher(progress_database, client=client)
    request = _request(1, "start", _evidence("source"))

    with pytest.raises(SimulatedCrash):
        publisher.publish(request)
    assert publisher.get(project_id="alpha", milestone_id="milestone-1").status == (
        "pending"
    )

    resumed = publisher.publish(
        _request(
            1,
            "start",
            _evidence("source"),
            actor_id="replacement-worker",
        )
    )
    assert resumed.status == "published"
    assert len(client.comments) == 1
    assert client.state == "Active"


def test_crash_after_state_update_resumes_without_duplicate_effects(
    progress_database: str,
) -> None:
    client = FakeProgressClient()
    client.crash_after_update = True
    publisher = AdoMilestonePublisher(progress_database, client=client)
    request = _request(1, "start", _evidence("source"))

    with pytest.raises(SimulatedCrash):
        publisher.publish(request)

    resumed = publisher.publish(request)
    assert resumed.status == "published"
    assert resumed.state_disposition == "already-current"
    assert len(client.comments) == 1
    assert client.update_calls == [("New", "Active")]
    assert client.state == "Active"


def test_maximum_length_milestone_id_uses_bounded_delivery_keys(
    progress_database: str,
) -> None:
    client = FakeProgressClient()
    publisher = AdoMilestonePublisher(progress_database, client=client)

    result = publisher.publish(
        _request(
            1,
            "start",
            _evidence("source"),
            milestone_id="m" * 256,
        )
    )

    assert result.status == "published"
    assert len(next(iter(client.comments))) <= 256
    assert len(client.operation_ids) == 1
    assert len(client.operation_ids[0]) <= 256


def test_pending_blocks_later_work_and_late_sequence_is_suppressed(
    progress_database: str,
) -> None:
    client = FakeProgressClient()
    client.fail_comment = True
    publisher = AdoMilestonePublisher(progress_database, client=client)

    with pytest.raises(RuntimeError, match="offline"):
        publisher.publish(_request(1, "start", _evidence("source")))
    with pytest.raises(AdoProgressBlocked, match="milestone-1"):
        publisher.publish(_request(3, "handoff", _evidence("handoff")))

    client.fail_comment = False
    publisher.publish(_request(1, "start", _evidence("source")))
    publisher.publish(_request(3, "handoff", _evidence("handoff")))
    stale = publisher.publish(
        _request(2, "recovery", _evidence("recovery"), milestone_id="late-2")
    )

    assert stale.status == "suppressed"
    assert stale.state_disposition == "suppressed"
    assert stale.suppression_reason == "stale milestone sequence"
    assert len(client.comments) == 2


def test_manual_state_is_preserved_before_or_during_conditional_update(
    progress_database: str,
) -> None:
    manual = FakeProgressClient(state="Removed")
    first = AdoMilestonePublisher(progress_database, client=manual).publish(
        _request(1, "start", _evidence("source"))
    )
    assert first.state_disposition == "preserved-manual"
    assert manual.state == "Removed"
    assert manual.update_calls == []

    with psycopg.connect(progress_database) as connection:
        connection.execute(
            "DELETE FROM agentic_mesh_v5.work_item_ado_milestones"
        )
    raced = FakeProgressClient(state="New")
    raced.manual_state_on_update = "Removed"
    second = AdoMilestonePublisher(progress_database, client=raced).publish(
        _request(1, "start", _evidence("source"))
    )
    assert second.state_disposition == "preserved-manual"
    assert second.external_state == "Removed"
    assert raced.update_calls == []


def test_validation_and_payload_conflicts_fail_before_duplicate_delivery(
    progress_database: str,
) -> None:
    client = FakeProgressClient()
    publisher = AdoMilestonePublisher(progress_database, client=client)
    with pytest.raises(ValueError, match="owner-review"):
        publisher.publish(_request(1, "acceptance", _evidence("acceptance")))
    with pytest.raises(ValueError, match="handoff.*handoff evidence"):
        publisher.publish(_request(1, "handoff", _evidence("source")))

    first = publisher.publish(_request(1, "start", _evidence("source")))
    replay = publisher.publish(
        _request(1, "start", _evidence("source"), actor_id="replacement-worker")
    )
    assert first == replay
    with pytest.raises(AdoProgressConflict, match="another payload"):
        publisher.publish(
            replace(
                _request(1, "start", _evidence("source")),
                summary="Changed summary",
            )
        )
    assert len(client.comments) == 1
