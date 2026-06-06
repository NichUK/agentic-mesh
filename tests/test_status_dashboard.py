import json
from pathlib import Path

from agentic_mesh import status_dashboard
from agentic_mesh.activation_evidence import ActivationEvidence
from agentic_mesh.config import load_mesh_config
from agentic_mesh.journal import EventJournal
from agentic_mesh.threaded_context import FileThreadedContextStore
from agentic_mesh.threaded_context import ThreadRouteRecord
from agentic_mesh.work_item_recovery import FileRecoveryStatusStore
from agentic_mesh.work_item_recovery import RecoveryClassifier
from agentic_mesh.problem_status import worker_problem_status
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import SourceAnchor


def test_status_dashboard_grouping_and_labels() -> None:
    assert status_dashboard.SCHEMA_VERSION == "status-dashboard-v0"
    assert status_dashboard.work_item_status_group("blocked") == "attention_needed"
    assert status_dashboard.work_item_status_group("running") == "active"
    assert status_dashboard.work_item_status_group("completed") == "terminal"
    assert status_dashboard.queue_status_group("promoted") == "promoted"
    assert (
        status_dashboard.queue_status_group(
            "captured",
            notification_failure_reason="send failed",
        )
        == "attention_needed"
    )
    assert (
        status_dashboard.display_label("needs_runtime_recovery", item_type="work_item")
        == "Needs runtime recovery"
    )
    assert (
        status_dashboard.display_label("ready_for_promotion", item_type="queue")
        == "Ready to promote"
    )


def test_status_dashboard_includes_redacted_notification_attempt_summary(
    tmp_path: Path,
) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    attempt_root = (
        tmp_path
        / "state"
        / "projects"
        / mesh_config.project.project_id
        / "notification_attempts"
    )
    attempt_root.mkdir(parents=True)
    (attempt_root / "notift-one.json").write_text(
        json.dumps(
            {
                "attempt_id": "notift-one",
                "event_id": "notifevt-one",
                "status": "dead_lettered",
                "work_item_id": "work-hm",
                "connector_id": "teams",
                "connector_type": "teams",
                "route_label": "project status fallback",
                "fallback_used": True,
                "fallback_reason": "source_anchor_unavailable",
                "redacted_error_class": "TimeoutError",
                "updated_at": "2026-06-05T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    dead_root = attempt_root / "dead_letters"
    dead_root.mkdir()
    (dead_root / "notift-one.json").write_text("{}", encoding="utf-8")

    aggregate = status_dashboard.build_status_dashboard(
        mesh_config=mesh_config,
        state_root=tmp_path / "state",
        workspace_root=Path.cwd(),
        work_item_status=lambda work_item_id: {"status": "not_found"},
        artifact_exists=lambda path: False,
    )

    notifications = aggregate["notifications"]
    assert notifications["schema_version"] == "notification-ops-summary-v0"
    assert notifications["total_attempts"] == 1
    assert notifications["dead_letters"] == 1
    assert notifications["fallback_count"] == 1
    assert notifications["attempts"][0]["route_label"] == "project status fallback"
    serialized = json.dumps(notifications)
    assert "teams_conversation_id" not in serialized
    assert "service_url" not in serialized


def test_status_dashboard_queue_rows_include_notification_summary(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    queue = FileWorkQueueStore(state_root, mesh_config.project.project_id, journal)
    item = queue.capture(
        title="Receipt state",
        summary="Expose notification fields.",
        owner_role="product-manager",
        source_anchor=SourceAnchor(
            connector_type="teams",
            connector_id="teams",
            source_scope="all-agents",
            source_message_id="raw-source",
            actor="raw-user",
            received_at="2026-06-05T12:00:00+00:00",
            display_label="Sponsor request",
        ),
        recommended_work_item_type="slice",
    )
    queue.record_notification(
        item.queue_item_id,
        status="blocked_unroutable",
        reason="no_explicit_notification_surface",
        event_kind="queue.captured",
        attempt_id="notift-dashboard",
        dedupe_ref="dedupe-dashboard",
        route_kind="configured_surface",
        selected_surface_key="status_fallback",
        route_label="project status fallback",
        fallback_reason="source_route_missing",
        failure_class="MissingSurface",
        correlation_id="corr-dashboard",
    )

    aggregate = status_dashboard.build_status_dashboard(
        mesh_config=mesh_config,
        state_root=state_root,
        workspace_root=Path.cwd(),
        work_item_status=lambda work_item_id: {"status": "not_found"},
        artifact_exists=lambda path: False,
    )

    row = aggregate["queue_items"][0]
    assert row["status_group"] == "attention_needed"
    assert row["notification_state"] == "blocked_unroutable"
    assert row["notification_event_kind"] == "queue.captured"
    assert row["notification_attempt_id"] == "notift-dashboard"
    assert row["notification_selected_surface_key"] == "status_fallback"
    assert row["notification_fallback_reason"] == "source_route_missing"
    assert row["notification_failure_class"] == "MissingSurface"


def test_status_dashboard_includes_safe_threaded_context_rows(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    journal = EventJournal(state_root, mesh_config.project.project_id)
    store = FileThreadedContextStore(state_root, mesh_config.project.project_id, journal)
    route = ThreadRouteRecord.create(
        connector_type="teams",
        connector_id="teams-shared",
        source_scope="engineering",
        root_message_ref="activity-parent",
        parent_work_item_id="work-parent-1",
        parent_work_item_type="slice",
        lifecycle_state="implementation",
        owner_role="engineering",
        source_anchor_ref="source:parent",
    )
    store.upsert_route(route)
    store.capture(
        route=route,
        source_message_ref="reply-1",
        actor_label="Nich",
        text="Please include this context.",
        mentioned_roles=[],
        source_anchor_ref="source:reply",
        correlation_id="corr-context",
    )

    aggregate = status_dashboard.build_status_dashboard(
        mesh_config=mesh_config,
        state_root=state_root,
        workspace_root=Path.cwd(),
        work_item_status=lambda work_item_id: {
            "status": "running",
            "current": {
                "status": "running",
                "title": "Parent work",
                "role_id": "engineering",
                "lifecycle_state": "implementation",
            },
        }
        if work_item_id == "work-parent-1"
        else {"status": "not_found"},
        artifact_exists=lambda path: False,
    )

    row = [
        item
        for item in aggregate["work_items"]
        if item["work_item_id"] == "work-parent-1"
    ][0]
    assert row["threaded_context"]["count"] == 1
    assert row["threaded_context"]["pending_owner_attention"] == 1
    serialized = json.dumps(row["threaded_context"])
    assert "reply-1" not in serialized
    assert "activity-parent" not in serialized
    assert "service_url" not in serialized


def test_status_dashboard_composes_recovery_above_problem_status(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    state_root = tmp_path / "state"
    problem = worker_problem_status(
        failure_class="malformed_handoff",
        reason="Invalid handoff shape",
        recovery_action="operator_review",
        retryable=True,
        role_id="engineering",
        role_instance_id="agentic-mesh-dev.engineering.1",
        message_payload={
            "work_item_id": "work-recovery-status",
            "work_item_type": "slice",
            "queue_item_id": "queue-recovery-status",
        },
        source_message_id="msg-recovery-status",
        correlation_id="corr-recovery-status",
        lifecycle_state="implementation",
        worker_adapter="codex-cli",
        worker_model="codex",
    )
    recovery = RecoveryClassifier().classify_problem(
        project_id=mesh_config.project.project_id,
        problem_status=problem,
    )
    FileRecoveryStatusStore(
        state_root,
        mesh_config.project.project_id,
    ).write_current(recovery)

    aggregate = status_dashboard.build_status_dashboard(
        mesh_config=mesh_config,
        state_root=state_root,
        workspace_root=Path.cwd(),
        work_item_status=lambda work_item_id: {
            "status": "needs_runtime_recovery",
            "current": problem.to_dict(),
        }
        if work_item_id == "work-recovery-status"
        else {"status": "not_found"},
        artifact_exists=lambda path: False,
    )

    row = [
        item
        for item in aggregate["work_items"]
        if item["work_item_id"] == "work-recovery-status"
    ][0]
    assert row["status_group"] == "attention_needed"
    assert row["display_label"] == "Runtime fix required"
    assert row["current_recovery"]["recovery_reason_class"] == "malformed_handoff"
    assert row["current_problem"]["status"] == "needs_runtime_recovery"


def test_status_dashboard_rows_surface_activation_attention(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    evidence = ActivationEvidence(
        project_id=mesh_config.project.project_id,
        work_item_id="work-activation-status",
        work_item_type="slice",
        lifecycle_state="implementation",
        impact_categories=("runtime_code", "route_or_ingress"),
        live_smoke_required=True,
        target_labels=("dogfood_compose",),
        activation_paths=("rebuild_image",),
        source_status="source_ready",
        activation_status="blocked",
        smoke_status="failed",
        failure_class="source_changed_running_service_not_updated",
        action_owner="runtime/operator",
        next_action="Rebuild the image and smoke /agents/current.json.",
        retryable=True,
        notification_state="failed",
        updated_at="2026-06-05T16:00:00+00:00",
    )

    row = status_dashboard.work_item_row_from_status(
        work_item_id="work-activation-status",
        payload={
            "status": "completed",
            "current": {"status": "completed", "lifecycle_state": "implementation"},
            "activation_summary": evidence.to_summary(),
            "timeline": [],
            "queue_entries": [],
        },
        queue_by_id={},
        queue_by_work_item={},
        title_hints={},
        workspace_root=Path.cwd(),
        mesh_config=mesh_config,
        artifact_exists=lambda path: False,
        state_root=tmp_path / "state",
    )

    assert row["status_group"] == "attention_needed"
    assert row["display_label"] == "Stale runtime detected"
    assert row["source_status"] == "source_ready"
    assert row["activation_status"] == "blocked"
    assert row["smoke_status"] == "failed"
    assert row["notification_state"] == "failed"
    assert row["failure_class"] == "source_changed_running_service_not_updated"
    assert row["target_labels"] == ["dogfood_compose"]
