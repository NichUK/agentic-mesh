from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path

from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.cli import main
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.sweeps import ProjectSweepService


def test_project_sweep_flags_blocked_and_waiting_items(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-blocked",
            title="Blocked work",
            description="Blocked.",
            state="blocked",
            owner_role="project-manager",
        )
        db.upsert_work_item(
            work_item_id="work-waiting",
            title="Waiting work",
            description="Waiting.",
            state="waiting_human",
            owner_role="product-manager",
        )

        findings = ProjectSweepService(db).sweep(now=datetime(2026, 6, 15, tzinfo=timezone.utc))

        assert [(finding.work_item_id, finding.reason) for finding in findings] == [
            ("work-blocked", "work item is in blocked"),
            ("work-waiting", "work item is in waiting_human"),
        ]
    finally:
        db.close()


def test_project_sweep_flags_stale_non_terminal_work(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-active",
            title="Active work",
            description="Active.",
            state="active",
            owner_role="engineering",
        )
        db.connection.execute(
            "UPDATE work_items SET updated_at='2026-06-15 10:00:00' WHERE work_item_id='work-active'"
        )

        findings = ProjectSweepService(db).sweep(
            stale_after_seconds=3600,
            now=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
        )

        assert len(findings) == 1
        assert findings[0].work_item_id == "work-active"
        assert findings[0].reason == "work item has not changed for 7200 seconds"
    finally:
        db.close()


def test_project_sweep_flags_unresolved_governance_on_active_work(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-governance",
            title="Governed work",
            description="Needs governance evidence.",
            state="active",
            owner_role="engineering",
            current_phase="development",
            governance={
                "phase": "development",
                "accountable_role": "engineering",
                "responsible_roles": ["engineering"],
                "consulted_roles": ["qa-engineer"],
                "informed_roles": ["project-manager"],
                "sponsor_decision_points": ["product-signoff"],
            },
        )

        findings = ProjectSweepService(db).sweep(
            stale_after_seconds=3600,
            now=datetime(2026, 6, 15, tzinfo=timezone.utc),
        )
    finally:
        db.close()

    assert len(findings) == 1
    assert findings[0].work_item_id == "work-governance"
    assert findings[0].reason == (
        "governance checklist has unresolved items: "
        "consultations=qa-engineer; informed_updates=project-manager; sponsor_decisions=product-signoff"
    )


def test_project_sweep_prioritizes_waiting_state_over_governance_reason(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-waiting",
            title="Waiting work",
            description="Waiting.",
            state="waiting_agent",
            owner_role="engineering",
            governance={
                "phase": "development",
                "accountable_role": "engineering",
                "responsible_roles": ["engineering"],
                "consulted_roles": ["qa-engineer"],
            },
        )

        findings = ProjectSweepService(db).sweep(now=datetime(2026, 6, 15, tzinfo=timezone.utc))
    finally:
        db.close()

    assert len(findings) == 1
    assert findings[0].reason == "work item is in waiting_agent"


def test_cli_sweep_project_outputs_findings(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-blocked",
            title="Blocked work",
            description="Blocked.",
            state="blocked",
            owner_role="project-manager",
        )
    finally:
        db.close()

    result = main(["--db", str(db_path), "sweep-project"])

    assert result == 0
    output = capsys.readouterr().out
    assert "work-blocked" in output
    assert "work item is in blocked" in output


def test_cli_sweep_project_can_publish_findings_to_project_manager(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "v3.sqlite3"
    project_config = tmp_path / "project.yaml"
    project_config.write_text(
        f"""
project_id: agentic-mesh-dev
broker:
  adapter: in-memory
  stream: agent-inbox
document_library:
  adapter: filesystem
  root: {(tmp_path / "documents").as_posix()}
roles:
  project-manager:
    instances: 1
""".strip(),
        encoding="utf-8",
    )
    db = V3Database(db_path)
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-blocked",
            title="Blocked work",
            description="Blocked.",
            state="blocked",
            owner_role="project-manager",
        )
    finally:
        db.close()

    result = main(
        [
            "--db",
            str(db_path),
            "--project-config",
            str(project_config),
            "sweep-project",
            "--publish-to-project-manager",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "work-blocked" in output
    assert "published_message_ids" in output
    assert "msg-" in output


def test_cli_sweep_project_publish_requires_project_config(tmp_path: Path) -> None:
    db_path = tmp_path / "v3.sqlite3"
    db = V3Database(db_path)
    try:
        db.migrate()
    finally:
        db.close()

    try:
        main(["--db", str(db_path), "sweep-project", "--publish-to-project-manager"])
    except ValueError as exc:
        assert "--project-config is required" in str(exc)
    else:
        raise AssertionError("publishing sweep findings should require project config")


def test_project_sweep_publishes_findings_to_project_manager_inbox(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-blocked",
            title="Blocked work",
            description="Blocked.",
            state="blocked",
            owner_role="engineering",
            next_action="Chase blocker.",
        )
        db.add_artifact(
            artifact_id="artifact-1",
            work_item_id="work-blocked",
            filename="index.md",
            title="Work item index",
            relative_path="work-items/work-blocked/index.md",
            document_type="work-item-index",
            status="accepted",
            created_by_role="project-manager",
        )
        service = ProjectSweepService(db)
        findings = service.sweep(now=datetime(2026, 6, 15, tzinfo=timezone.utc))

        message_ids = service.publish_findings(
            broker,
            stream="agent-inbox",
            findings=findings,
            project_manager_role_id="project-manager",
        )
        broker.ensure_consumer("agent-inbox", "pm-sweep", filter_subject="agent.project-manager")
        messages = broker.fetch("agent-inbox", "pm-sweep")
    finally:
        db.close()

    assert len(message_ids) == 1
    assert messages[0].payload["message_type"] == "project_sweep.finding"
    assert messages[0].payload["work_item_id"] == "work-blocked"
    assert messages[0].payload["artifact_count"] == 1
    assert messages[0].payload["work_item_url"] == "/work-item/work-blocked"
    assert messages[0].payload["required_action"] == (
        "Review the finding and use normal tools to chase, unblock, rescope, or close the work."
    )


def test_project_sweep_does_not_republish_unchanged_findings(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-blocked",
            title="Blocked work",
            description="Blocked.",
            state="blocked",
            owner_role="engineering",
            next_action="Chase blocker.",
        )
        service = ProjectSweepService(db)
        findings = service.sweep(now=datetime(2026, 6, 15, tzinfo=timezone.utc))

        first_message_ids = service.publish_findings(
            broker,
            stream="agent-inbox",
            findings=findings,
            project_manager_role_id="project-manager",
        )
        second_message_ids = service.publish_findings(
            broker,
            stream="agent-inbox",
            findings=findings,
            project_manager_role_id="project-manager",
        )
        event_count = db.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='project_sweep.finding_published'"
        ).fetchone()[0]
    finally:
        db.close()

    assert len(first_message_ids) == 1
    assert second_message_ids == ()
    assert event_count == 1


def test_project_sweep_republishes_when_finding_changes(tmp_path: Path) -> None:
    broker = InMemoryBrokerAdapter()
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-blocked",
            title="Blocked work",
            description="Blocked.",
            state="blocked",
            owner_role="engineering",
            next_action="Chase blocker.",
        )
        service = ProjectSweepService(db)
        first_findings = service.sweep(now=datetime(2026, 6, 15, tzinfo=timezone.utc))
        first_message_ids = service.publish_findings(
            broker,
            stream="agent-inbox",
            findings=first_findings,
            project_manager_role_id="project-manager",
        )

        db.update_work_item_state(
            work_item_id="work-blocked",
            state="blocked",
            next_action="Escalate blocker to sponsor.",
        )
        second_findings = service.sweep(now=datetime(2026, 6, 15, tzinfo=timezone.utc))
        second_message_ids = service.publish_findings(
            broker,
            stream="agent-inbox",
            findings=second_findings,
            project_manager_role_id="project-manager",
        )
    finally:
        db.close()

    assert len(first_message_ids) == 1
    assert len(second_message_ids) == 1
    assert second_message_ids != first_message_ids


def test_project_sweep_recovers_stale_agent_wake_blocker_when_agent_is_running(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-stale-agent-blocker",
            title="Stale agent blocker",
            description="Blocked because a role looked unavailable.",
            state="blocked",
            owner_role="delivery-manager",
            current_phase="delivery_planning",
            next_action=(
                "Platform/runtime owner must restore or wake "
                "agentic-mesh-dev.platform-engineer.1, then complete Compose ps."
            ),
        )
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.platform-engineer.1",
                container_state="running",
                heartbeat_at="2026-06-19T20:49:01+00:00",
                current_work="work-stale-agent-blocker",
            )
        )

        recoveries = ProjectSweepService(db).recover_stale_agent_blockers()
        row = db.connection.execute(
            """
            SELECT state, owner_role, current_phase, next_action
            FROM work_items
            WHERE work_item_id='work-stale-agent-blocker'
            """
        ).fetchone()
        event = db.connection.execute(
            """
            SELECT payload_json
            FROM events
            WHERE event_type='work_item.stale_agent_blocker_recovered'
              AND aggregate_id='work-stale-agent-blocker'
            """
        ).fetchone()
    finally:
        db.close()

    assert len(recoveries) == 1
    assert recoveries[0].role_instance_id == "agentic-mesh-dev.platform-engineer.1"
    assert recoveries[0].owner_role == "platform-engineer"
    assert row["state"] == "waiting_agent"
    assert row["owner_role"] == "platform-engineer"
    assert row["current_phase"] == "delivery_planning"
    assert "Recovered stale agent wake blocker" in row["next_action"]
    assert event is not None


def test_project_sweep_does_not_recover_normal_or_unhealthy_agent_blockers(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-normal-blocker",
            title="Normal blocker",
            description="Blocked for a real reason.",
            state="blocked",
            owner_role="delivery-manager",
            next_action="Ask agentic-mesh-dev.platform-engineer.1 to review deployment risk.",
        )
        db.upsert_work_item(
            work_item_id="work-hibernated-agent-blocker",
            title="Hibernated blocker",
            description="Blocked because a role looked unavailable.",
            state="blocked",
            owner_role="delivery-manager",
            next_action="Wake agentic-mesh-dev.solution-architect.1 and continue design review.",
        )
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.platform-engineer.1",
                container_state="running",
                heartbeat_at="2026-06-19T20:49:01+00:00",
            )
        )
        db.upsert_agent_status(
            AgentStatus(
                role_instance_id="agentic-mesh-dev.solution-architect.1",
                container_state="hibernated",
                heartbeat_at=None,
            )
        )

        recoveries = ProjectSweepService(db).recover_stale_agent_blockers()
        states = {
            row["work_item_id"]: row["state"]
            for row in db.connection.execute("SELECT work_item_id, state FROM work_items")
        }
    finally:
        db.close()

    assert recoveries == ()
    assert states == {
        "work-hibernated-agent-blocker": "blocked",
        "work-normal-blocker": "blocked",
    }
