from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.hibernation import HibernationPolicy
from agentic_mesh_v2.hibernation import HibernationService
from agentic_mesh_v2.project_config import load_role_hibernation_config
from agentic_mesh_v2.server import V2StatusHandler


def _render(snapshot: dict[str, object]) -> str:
    handler = object.__new__(V2StatusHandler)
    handler._snapshot = lambda: snapshot  # type: ignore[method-assign]
    return handler._render_status()


def test_hibernation_policy_loads_project_defaults_and_role_overrides(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
hibernation:
  enabled: true
  idle_after_seconds: 900
  min_warm_instances: 1
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
    hibernation:
      idle_after_seconds: 60
  engineering:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    product_policy = HibernationPolicy.from_mapping(
        load_role_hibernation_config(project_file, role_id="product-manager")
    )
    engineering_policy = HibernationPolicy.from_mapping(
        load_role_hibernation_config(project_file, role_id="engineering")
    )

    assert product_policy.enabled is True
    assert product_policy.idle_after_seconds == 60
    assert product_policy.min_warm_instances == 1
    assert engineering_policy.idle_after_seconds == 900
    assert engineering_policy.min_warm_instances == 1


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"enabled": "yes"}, "enabled must be a boolean"),
        ({"idle_after_seconds": 0}, "idle_after_seconds must be a positive integer"),
        ({"min_warm_instances": -1}, "min_warm_instances must be zero or greater"),
    ],
)
def test_hibernation_policy_rejects_invalid_values(config: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        HibernationPolicy.from_mapping(config)


def test_hibernation_service_marks_idle_instance_hibernated_after_grace(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.update_role_instance_status(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="idle",
        detail="Idle and safe.",
    )
    db.connection.execute(
        """
        UPDATE role_instance_status
        SET heartbeat_at = datetime('now', '-120 seconds')
        WHERE role_instance_id = 'test-project.product-manager.1'
        """
    )
    service = HibernationService(
        db,
        HibernationPolicy(enabled=True, idle_after_seconds=60, min_warm_instances=0),
    )

    decision = service.mark_hibernating(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        reason="Idle grace elapsed.",
    )
    service.mark_hibernated(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        reason="Container stopped after safe hibernation.",
    )

    instance = db.status_snapshot()["role_instance_statuses"][0]
    assert decision.can_hibernate is True
    assert decision.idle_seconds is not None
    assert decision.idle_seconds >= 60
    assert instance["status"] == "hibernated"
    assert instance["hibernation_reason"] == "Container stopped after safe hibernation."
    assert instance["hibernated_at"]
    html = _render(db.status_snapshot())
    assert "Heartbeat / Hibernated" in html
    assert "Hibernate / Wake Reason" in html
    assert "Container stopped after safe hibernation." in html


def test_hibernation_service_refuses_active_assignment_and_queued_role_work(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.update_role_instance_status(
        role_id="engineering",
        role_instance_id="test-project.engineering.1",
        status="active",
        current_assignment_id="assignment-active",
        detail="Running a tool call.",
    )
    service = HibernationService(db, HibernationPolicy(idle_after_seconds=1))

    active = service.evaluate(role_id="engineering", role_instance_id="test-project.engineering.1")

    assert active.can_hibernate is False
    assert "not idle" in active.reason

    db.update_role_instance_status(
        role_id="engineering",
        role_instance_id="test-project.engineering.1",
        status="idle",
        current_assignment_id=None,
        detail="Idle again.",
    )
    db.connection.execute(
        """
        UPDATE role_instance_status
        SET heartbeat_at = datetime('now', '-120 seconds')
        WHERE role_instance_id = 'test-project.engineering.1'
        """
    )
    db.create_role_assignment(
        assignment_id="assignment-queued",
        role_id="engineering",
        source_ref="msg-queued",
        title="Queued engineering work",
        summary="Queued work should keep the role awake.",
        assignment_type="work_item_handoff",
        visibility_scope="project",
        payload={},
    )

    queued = service.evaluate(role_id="engineering", role_instance_id="test-project.engineering.1")

    assert queued.can_hibernate is False
    assert queued.reason == "Role has queued work waiting."


def test_hibernation_service_respects_minimum_warm_instances(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    for index in (1, 2):
        db.update_role_instance_status(
            role_id="qa-engineer",
            role_instance_id=f"test-project.qa-engineer.{index}",
            status="idle",
            detail="Idle.",
        )
    db.connection.execute(
        """
        UPDATE role_instance_status
        SET heartbeat_at = datetime('now', '-120 seconds')
        WHERE role_id = 'qa-engineer'
        """
    )
    service = HibernationService(db, HibernationPolicy(idle_after_seconds=1, min_warm_instances=2))

    decision = service.evaluate(role_id="qa-engineer", role_instance_id="test-project.qa-engineer.1")

    assert decision.can_hibernate is False
    assert decision.reason == "Hibernation would violate the configured warm-instance floor."


def test_hydration_marks_instance_hydrating_and_records_wake_reason(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.update_role_instance_hibernation(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )
    service = HibernationService(db)

    service.mark_hydrating(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        reason="New direct message arrived.",
    )

    instance = db.status_snapshot()["role_instance_statuses"][0]
    assert instance["status"] == "hydrating"
    assert instance["hibernation_reason"] == "Idle grace elapsed."
    assert instance["wake_reason"] == "New direct message arrived."
