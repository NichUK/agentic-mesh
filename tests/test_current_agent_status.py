import json
from pathlib import Path

from agentic_mesh.activation_evidence import ActivationEvidence
from agentic_mesh.activation_evidence import FileActivationEvidenceStore
from agentic_mesh.agent_run_state import AgentRunState
from agentic_mesh.agent_run_state import FileAgentRunStateStore
from agentic_mesh.config import load_mesh_config
from agentic_mesh.current_agent_status import SCHEMA_VERSION
from agentic_mesh.current_agent_status import assert_current_agents_payload_safe
from agentic_mesh.current_agent_status import build_current_agent_status
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import Message
from agentic_mesh.problem_status import ProblemStatusStore
from agentic_mesh.problem_status import worker_problem_status
from agentic_mesh.storage import FileMessageStore


GENERATED_AT = "2026-06-05T15:00:00+00:00"


def _config():
    return load_mesh_config(
        Path.cwd(),
        project_file="examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
    )


def _instance(config, role_id: str, ordinal: int = 1):
    suffix = f".{role_id}.{ordinal}"
    return next(
        instance
        for instance in config.instances.values()
        if instance.instance_id.endswith(suffix)
    )


def _write_lifecycle(state_root: Path, project_id: str, instance, state: str) -> None:
    path = state_root / "projects" / project_id / "lifecycle" / f"{instance.instance_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project_id": project_id,
                "role_id": instance.role_id,
                "role_instance_id": instance.instance_id,
                "state": state,
                "updated_at": "2026-06-05T14:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _enqueue(
    store: FileMessageStore,
    role_id: str,
    work_item_id: str,
    lifecycle_state: str,
    *,
    source_anchor: dict | None = None,
) -> Message:
    return store.enqueue(
        Message.create(
            role_id=role_id,
            message_type=f"sdlc.{lifecycle_state}",
            payload={
                "work_item_id": work_item_id,
                "work_item_type": "slice",
                "queue_item_id": f"queue-{work_item_id}",
                "lifecycle_state": lifecycle_state,
                "source_anchor": source_anchor,
            },
            source="test",
            correlation_id=f"corr-{work_item_id}",
        )
    )


def _set_claimed_at(state_root: Path, project_id: str, role_id: str, instance_id: str, value: str) -> None:
    for path in (state_root / "projects" / project_id / "queues" / role_id / "claimed" / instance_id).glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["claimed_at"] = value
        path.write_text(json.dumps(data), encoding="utf-8")


def test_current_agents_payload_lists_all_configured_instances_without_evidence(
    tmp_path: Path,
) -> None:
    config = _config()

    payload = build_current_agent_status(
        mesh_config=config,
        state_root=tmp_path / "state",
        workspace_root=Path.cwd(),
        generated_at=GENERATED_AT,
    )

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["read_only"] is True
    assert payload["refresh_mode"] == "manual_browser_refresh"
    assert payload["counts"]["total_agents"] == 14
    assert len(payload["agents"]) == 14
    assert {row["condition"] for row in payload["agents"]} == {"not_observed"}
    assert payload["attention"] == []


def test_current_agents_classifies_runtime_conditions_and_preserves_redaction(
    tmp_path: Path,
) -> None:
    config = _config()
    state_root = tmp_path / "state"
    project_id = config.project.project_id
    journal = EventJournal(state_root, project_id)
    store = FileMessageStore(state_root, project_id, journal)
    run_store = FileAgentRunStateStore(state_root, project_id)

    ba = _instance(config, "business-analyst")
    platform = _instance(config, "platform-engineer")
    pm = _instance(config, "product-manager")
    eng1 = _instance(config, "engineering", 1)
    eng2 = _instance(config, "engineering", 2)
    security = _instance(config, "security-architect")
    solution = _instance(config, "solution-architect")

    _write_lifecycle(state_root, project_id, ba, "idle")
    _write_lifecycle(state_root, project_id, platform, "hibernated")
    _enqueue(store, pm.role_id, "work-pending", "product_definition")

    safe_anchor = {
        "connector_type": "teams",
        "connector_id": "teams",
        "source_scope": "channel",
        "source_anchor_ref": "source:abc",
        "display_label": "Codex queued from sponsor operations check",
        "received_at": "2026-06-05T14:37:55+00:00",
        "activity_id": "raw-activity-id-must-not-appear",
        "service_url": "https://graph.microsoft.com/must-not-appear",
    }
    running_message = _enqueue(
        store,
        eng1.role_id,
        "work-running",
        "implementation",
        source_anchor=safe_anchor,
    )
    claimed_running = store.claim_next(eng1.role_id, eng1.instance_id)
    assert claimed_running is not None
    _set_claimed_at(
        state_root,
        project_id,
        eng1.role_id,
        eng1.instance_id,
        "2026-06-05T14:55:00+00:00",
    )
    run_store.write_current(
        AgentRunState.from_claim(
            instance=eng1,
            message=running_message.claimed(eng1.instance_id),
            run_state="running",
            timeout_seconds=3600,
            progress_window_seconds=600,
            evidence_source="worker_run_started",
            observed_at="2026-06-05T14:59:00+00:00",
        )
    )

    _enqueue(store, eng2.role_id, "work-stale", "implementation")
    claimed_stale = store.claim_next(eng2.role_id, eng2.instance_id)
    assert claimed_stale is not None
    _set_claimed_at(
        state_root,
        project_id,
        eng2.role_id,
        eng2.instance_id,
        "2026-06-05T13:00:00+00:00",
    )

    security_message = _enqueue(
        store,
        security.role_id,
        "work-provider-limit",
        "security_review",
    )
    ProblemStatusStore(state_root, project_id).write_current(
        worker_problem_status(
            failure_class="usage_limit",
            reason="Provider usage limit was reached.",
            recovery_action="operator_review",
            retryable=True,
            role_id=security.role_id,
            role_instance_id=security.instance_id,
            message_payload=security_message.payload,
            source_message_id=security_message.message_id,
            correlation_id=security_message.correlation_id,
            lifecycle_state="security_review",
            worker_adapter=security.override.worker.adapter,
            worker_model=security.override.worker.model,
        )
    )

    solution_message = _enqueue(
        store,
        solution.role_id,
        "work-notify",
        "solution_design",
    )
    claimed_solution = store.claim_next(solution.role_id, solution.instance_id)
    assert claimed_solution is not None
    run_store.write_current(
        AgentRunState.from_claim(
            instance=solution,
            message=solution_message.claimed(solution.instance_id),
            run_state="running",
            timeout_seconds=1800,
            progress_window_seconds=300,
            evidence_source="worker_run_started",
            observed_at="2026-06-05T14:59:00+00:00",
        )
    )
    attempt_root = state_root / "projects" / project_id / "notification_attempts"
    attempt_root.mkdir(parents=True)
    (attempt_root / "notift-one.json").write_text(
        json.dumps(
            {
                "attempt_id": "notift-one",
                "event_id": "notifevt-one",
                "status": "failed",
                "work_item_id": "work-notify",
                "connector_id": "teams",
                "connector_type": "teams",
                "route_label": "status fallback",
                "redacted_error_class": "TimeoutError",
                "service_url": "https://graph.microsoft.com/hidden",
                "updated_at": "2026-06-05T14:59:30+00:00",
            }
        ),
        encoding="utf-8",
    )

    payload = build_current_agent_status(
        mesh_config=config,
        state_root=state_root,
        workspace_root=Path.cwd(),
        generated_at=GENERATED_AT,
    )
    rows = {row["role_instance_id"]: row for row in payload["agents"]}

    assert rows[ba.instance_id]["condition"] == "idle"
    assert rows[platform.instance_id]["condition"] == "hibernated"
    assert rows[pm.instance_id]["condition"] == "pending_queue"
    assert rows[eng1.instance_id]["condition"] == "running"
    assert rows[eng1.instance_id]["source_anchor_summary"] == {
        "connector_type": "teams",
        "connector_id": "teams",
        "source_scope": "channel",
        "source_anchor_ref": "source:abc",
        "display_label": "Codex queued from sponsor operations check",
        "received_at": "2026-06-05T14:37:55+00:00",
    }
    assert rows[eng2.instance_id]["condition"] == "stale_active"
    assert rows[eng2.instance_id]["stale_threshold_seconds"] == 1800
    assert rows[security.instance_id]["condition"] == "provider_limited"
    assert rows[security.instance_id]["provider_recovery_class"] == "usage_limit"
    assert rows[solution.instance_id]["condition"] == "connector_notification_failed"
    assert "running" in rows[solution.instance_id]["secondary_conditions"]
    assert payload["counts"]["attention_count"] >= 3

    serialized = json.dumps(payload)
    for forbidden in [
        "raw-activity-id-must-not-appear",
        "graph.microsoft.com",
        "service_url",
        "activity_id",
        "secret_ref",
        "mount_ref",
        "stdout",
        "stderr",
        "prompt",
    ]:
        assert forbidden not in serialized
    assert_current_agents_payload_safe(payload)


def test_current_agents_includes_safe_activation_summary_for_runtime_recovery(
    tmp_path: Path,
) -> None:
    config = _config()
    state_root = tmp_path / "state"
    project_id = config.project.project_id
    eng = _instance(config, "engineering", 1)
    evidence = ActivationEvidence(
        project_id=project_id,
        work_item_id="work-current-activation",
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
        next_action="Rebuild image and smoke /agents/current.json.",
        retryable=True,
        updated_at="2026-06-05T16:20:00+00:00",
    )
    FileActivationEvidenceStore(state_root, project_id).write_current(evidence)
    ProblemStatusStore(state_root, project_id).write_current(
        worker_problem_status(
            failure_class="source_changed_running_service_not_updated",
            reason="Source-ready current-agent route is not active on target.",
            recovery_action="operator_review",
            retryable=True,
            role_id=eng.role_id,
            role_instance_id=eng.instance_id,
            message_payload={
                "work_item_id": "work-current-activation",
                "work_item_type": "slice",
                "queue_item_id": "queue-current-activation",
            },
            source_message_id="msg-current-activation",
            correlation_id="corr-current-activation",
            lifecycle_state="implementation",
            worker_adapter=eng.override.worker.adapter,
            worker_model=eng.override.worker.model,
        )
    )

    payload = build_current_agent_status(
        mesh_config=config,
        state_root=state_root,
        workspace_root=Path.cwd(),
        generated_at=GENERATED_AT,
    )
    row = next(
        item for item in payload["agents"] if item["role_instance_id"] == eng.instance_id
    )

    assert row["condition"] == "needs_runtime_recovery"
    assert row["failure_class"] == "source_changed_running_service_not_updated"
    assert row["activation_summary"]["target_labels"] == ["dogfood_compose"]
    assert row["activation_summary"]["smoke_status"] == "failed"
    assert payload["read_only"] is True
    assert_current_agents_payload_safe(payload)


def test_current_agent_corrupt_run_state_is_row_local(tmp_path: Path) -> None:
    config = _config()
    state_root = tmp_path / "state"
    instance = _instance(config, "engineering", 1)
    run_store = FileAgentRunStateStore(state_root, config.project.project_id)
    run_store.current_path(instance.instance_id).write_text("{bad-json", encoding="utf-8")

    payload = build_current_agent_status(
        mesh_config=config,
        state_root=state_root,
        workspace_root=Path.cwd(),
        generated_at=GENERATED_AT,
    )
    row = next(
        item for item in payload["agents"] if item["role_instance_id"] == instance.instance_id
    )

    assert row["condition"] == "unknown"
    assert row["extraction_error"] == "JSONDecodeError"
    assert len(payload["agents"]) == 14
