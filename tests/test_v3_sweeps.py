from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path

from agentic_mesh_v3.broker import InMemoryBrokerAdapter
from agentic_mesh_v3.cli import main
from agentic_mesh_v3.db import V3Database
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
    assert messages[0].payload["required_action"] == (
        "Review the finding and use normal tools to chase, unblock, rescope, or close the work."
    )
