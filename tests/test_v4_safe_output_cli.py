from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh_v4.agent_config import materialize_agent_configs
from agentic_mesh_v4.cli import main
from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.decision_records import DecisionRequest
from agentic_mesh_v4.decision_records import DecisionRecordError
from agentic_mesh_v4.decision_records import record_card_delivery_attempt
from agentic_mesh_v4.decision_records import request_decision
from v4_postgres import make_v4_db_url


PROJECT_CONFIG = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")


def test_v4_safe_output_work_item_update_records_state_and_call(tmp_path: Path, capsys) -> None:
    db_path = make_v4_db_url()

    main(
        [
            "--project-config",
            str(PROJECT_CONFIG),
            "--db",
            str(db_path),
            "safe-output",
            "work-item-update",
            "--role-id",
            "product-manager",
            "--work-item-id",
            "work-1",
            "--title",
            "Test work",
            "--state",
            "product_definition",
            "--owner-role",
            "product-manager",
            "--next-action",
            "Define product scope.",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    db = V4Database(db_path)
    try:
        db.migrate()
        work = db.connection.execute("SELECT * FROM work_items WHERE work_item_id='work-1'").fetchone()
        call = db.connection.execute("SELECT * FROM safe_output_calls WHERE call_id=?", (output["call_id"],)).fetchone()
    finally:
        db.close()
    assert work["state"] == "product_definition"
    assert work["owner_role"] == "product-manager"
    assert call["role_instance_id"] == "agentic-mesh-dev.product-manager.1"
    assert call["tool_name"] == "work_item.update"


def test_v4_human_wait_rejects_dashboard_only_escalation(capsys) -> None:
    db_path = make_v4_db_url()

    with pytest.raises(DecisionRecordError, match="delivered through Teams"):
        main(
            [
                "--project-config",
                str(PROJECT_CONFIG),
                "--db",
                str(db_path),
                "safe-output",
                "work-item-update",
                "--role-id",
                "project-manager",
                "--work-item-id",
                "work-human-wait",
                "--title",
                "Human decision needed",
                "--state",
                "blocked_on_human",
                "--owner-role",
                "project-manager",
                "--next-action",
                "Await sponsor direction.",
            ]
        )

    db = V4Database(db_path)
    try:
        db.migrate()
        assert db.connection.execute(
            "SELECT 1 FROM work_items WHERE work_item_id=?",
            ("work-human-wait",),
        ).fetchone() is None
    finally:
        db.close()


def test_v4_human_wait_requires_blocking_role_teams_delivery(capsys) -> None:
    db_path = make_v4_db_url()
    db = V4Database(db_path)
    try:
        db.migrate()
        decision = request_decision(
            db=db,
            request=DecisionRequest(
                work_item_id="work-human-wait-delivered",
                requester_role="project-manager",
                owner_role="project-manager",
                authority_label="Sponsor",
                authorized_responders={"sponsor": "sponsor-aad-id"},
                decision_type="approval",
                title="Choose the remediation path",
                question="Approve the bounded remediation path so migration can continue?",
                options=("approved", "changes_requested", "deferred"),
                recommended_option="approved",
            ),
            deliver=False,
        )
        record_card_delivery_attempt(
            db=db,
            decision_id=decision["decision_id"],
            channel="teams",
            conversation_ref='{"conversation":{"id":"sponsor-chat"}}',
            activity_id="teams-activity-1",
            state="delivered",
        )
    finally:
        db.close()

    main(
        [
            "--project-config",
            str(PROJECT_CONFIG),
            "--db",
            str(db_path),
            "safe-output",
            "work-item-update",
            "--role-id",
            "project-manager",
            "--work-item-id",
            "work-human-wait-delivered",
            "--title",
            "Human decision needed",
            "--state",
            "blocked_on_human",
            "--owner-role",
            "project-manager",
            "--next-action",
            "Await sponsor direction.",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    db = V4Database(db_path)
    try:
        db.migrate()
        work = db.connection.execute(
            "SELECT state FROM work_items WHERE work_item_id=?",
            ("work-human-wait-delivered",),
        ).fetchone()
        call = db.connection.execute(
            "SELECT payload_json FROM safe_output_calls WHERE call_id=?",
            (output["call_id"],),
        ).fetchone()
    finally:
        db.close()
    assert work["state"] == "blocked_on_human"
    assert json.loads(call["payload_json"])["sponsor_notification"]["activity_id"] == "teams-activity-1"


def test_v4_safe_output_handoff_moves_work_item_and_queues_target_role(tmp_path: Path, capsys) -> None:
    db_path = make_v4_db_url()
    main(
        [
            "--project-config",
            str(PROJECT_CONFIG),
            "--db",
            str(db_path),
            "safe-output",
            "work-item-update",
            "--role-id",
            "product-manager",
            "--work-item-id",
            "work-1",
            "--title",
            "Test work",
            "--state",
            "product_definition",
            "--owner-role",
            "product-manager",
            "--next-action",
            "Define product scope.",
        ]
    )
    capsys.readouterr()

    main(
        [
            "--project-config",
            str(PROJECT_CONFIG),
            "--db",
            str(db_path),
            "safe-output",
            "architecture-impact",
            "--role-id",
            "product-manager",
            "--work-item-id",
            "work-1",
            "--classification",
            "none",
            "--rationale",
            "The isolated test work changes no enterprise architecture domain.",
        ]
    )
    impact_output = json.loads(capsys.readouterr().out)

    main(
        [
            "--project-config",
            str(PROJECT_CONFIG),
            "--db",
            str(db_path),
            "safe-output",
            "handoff",
            "--from-role",
            "product-manager",
            "--to-role",
            "ux-designer",
            "--work-item-id",
            "work-1",
            "--state",
            "experience_design",
            "--next-action",
            "Create UX design evidence.",
            "--reason",
            "Product definition is ready for UX.",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    db = V4Database(db_path)
    try:
        db.migrate()
        work = db.connection.execute("SELECT * FROM work_items WHERE work_item_id='work-1'").fetchone()
        message = db.connection.execute(
            "SELECT * FROM message_queue WHERE message_id=?",
            (output["message_id"],),
        ).fetchone()
        handoff = db.connection.execute(
            "SELECT * FROM handoffs WHERE handoff_id=?",
            (output["handoff_id"],),
        ).fetchone()
    finally:
        db.close()
    assert work["state"] == "experience_design"
    assert work["owner_role"] == "ux-designer"
    assert message["target_role"] == "ux-designer"
    assert message["state"] == "queued"
    assert handoff["from_role"] == "product-manager"
    assert handoff["to_role"] == "ux-designer"
    assert impact_output["architecture_impact"] == "none"


def test_v4_materialized_agents_md_names_safe_output_cli(tmp_path: Path) -> None:
    config = load_project_config(PROJECT_CONFIG)
    materialize_agent_configs(
        project_config=config,
        output_root=tmp_path / "agents",
        role_templates_dir=Path("config/roles"),
    )

    text = (tmp_path / "agents" / "product-manager" / "1" / "AGENTS.md").read_text(encoding="utf-8")

    assert "python -m agentic_mesh_v4.cli" in text
    assert "safe-output handoff" in text
    assert "safe-output artifact-link" in text
    assert "safe-output work-item-update" in text

    container = json.loads(
        (tmp_path / "agents" / "product-manager" / "1" / "container.json").read_text(encoding="utf-8")
    )
    assert container["model"] == "gpt-5.6-sol"
    assert container["reasoning_effort"] == "high"
    assert container["plan_mode_reasoning_effort"] == "xhigh"
    assert container["show_raw_agent_reasoning"] is False


def test_v4_safe_output_memory_record_can_write_role_and_institutional_memory(tmp_path: Path, capsys) -> None:
    db_path = make_v4_db_url()

    main(
        [
            "--project-config",
            str(PROJECT_CONFIG),
            "--db",
            str(db_path),
            "safe-output",
            "memory-record",
            "--role-id",
            "project-manager",
            "--scope",
            "both",
            "--summary",
            "Sponsor wants durable handoffs to be treated as mandatory.",
            "--source-ref",
            "conversation:test",
            "--tags",
            "governance",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    db = V4Database(db_path)
    try:
        db.migrate()
        role_memory = db.connection.execute(
            "SELECT * FROM role_memory WHERE memory_id=?",
            (output["role_memory_id"],),
        ).fetchone()
        project_memory = db.connection.execute(
            "SELECT * FROM project_memory WHERE memory_id=?",
            (output["project_memory_id"],),
        ).fetchone()
    finally:
        db.close()
    assert role_memory["project_id"] == "agentic-mesh-dev"
    assert role_memory["role_id"] == "project-manager"
    assert role_memory["scope"] == "role"
    assert project_memory["project_id"] == "agentic-mesh-dev"
    assert project_memory["created_by_role_id"] == "project-manager"

