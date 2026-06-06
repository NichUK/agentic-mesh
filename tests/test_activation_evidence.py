import json
from pathlib import Path

import pytest

from agentic_mesh.activation_evidence import ActivationEvidence
from agentic_mesh.activation_evidence import ActivationReadError
from agentic_mesh.activation_evidence import FileActivationEvidenceStore
from agentic_mesh.activation_evidence import SmokeEvidence
from agentic_mesh.activation_evidence import activation_problem_status
from agentic_mesh.activation_evidence import assert_activation_payload_safe


def _evidence(**overrides):
    values = {
        "project_id": "agentic-mesh-dev",
        "work_item_id": "work-activation",
        "work_item_type": "slice",
        "queue_item_id": "queue-activation",
        "source_message_id": "msg-activation",
        "source_anchor_ref": "source:activation",
        "correlation_id": "corr-activation",
        "lifecycle_state": "implementation",
        "impact_categories": ("runtime_code", "container_image", "route_or_ingress"),
        "live_smoke_required": True,
        "target_labels": ("dogfood_compose",),
        "activation_paths": ("rebuild_image",),
        "source_status": "source_ready",
        "activation_status": "blocked",
        "smoke_status": "blocked",
        "failure_class": "activation_target_unavailable",
        "action_owner": "runtime/operator",
        "next_action": "Run live-safe control-plane smoke in a Docker-capable target.",
        "retryable": True,
        "rollback_summary": "Rebuild the previous local image and recreate affected services.",
        "evidence_refs": ("work-items/work-activation/100-implementation-log.md",),
        "notification_state": "pending",
        "status_url": "/work-items/work-activation",
        "updated_at": "2026-06-05T16:00:00+00:00",
    }
    values.update(overrides)
    return ActivationEvidence(**values)


def test_activation_evidence_contract_preserves_enums_and_labels() -> None:
    smoke = SmokeEvidence(
        smoke_id="smoke-current",
        target_label="dogfood_compose",
        route_label="/agents/current.json",
        observed_at="2026-06-05T16:01:00+00:00",
        result="failed",
        status_code=404,
        expected_status_code=200,
        schema_expectation="current-agents-v0",
        actual_summary="404 returned",
        failure_class="source_changed_running_service_not_updated",
        evidence_ref="work-items/work-activation/100-implementation-log.md",
    )
    evidence = _evidence(
        smoke_status="failed",
        failure_class="source_changed_running_service_not_updated",
        smoke_evidence=(smoke,),
    )

    payload = evidence.to_dict()

    assert payload["schema_version"] == "activation-evidence-v0"
    assert payload["impact_categories"] == [
        "runtime_code",
        "container_image",
        "route_or_ingress",
    ]
    assert "Runtime code" in payload["impact_category_labels"]
    assert payload["activation_paths"] == ["rebuild_image"]
    assert payload["activation_path_labels"] == ["Rebuild image"]
    assert payload["smoke_evidence"][0]["route_label"] == "/agents/current.json"
    assert payload["smoke_evidence"][0]["schema_expectation"] == "current-agents-v0"
    assert_activation_payload_safe(payload)


def test_none_impact_requires_rationale_and_exclusive_category() -> None:
    with pytest.raises(ValueError, match="none_rationale"):
        _evidence(
            impact_categories=("none",),
            activation_paths=("not_required",),
            target_labels=(),
            activation_status="not_required",
            smoke_status="not_required",
            live_smoke_required=False,
            failure_class=None,
            next_action=None,
            none_rationale=None,
        )

    with pytest.raises(ValueError, match="only impact category"):
        _evidence(
            impact_categories=("none", "runtime_code"),
            none_rationale="Documentation-only change.",
        )


def test_invalid_enums_and_unsafe_labels_are_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported impact_categories"):
        _evidence(impact_categories=("docker_only",))
    with pytest.raises(ValueError, match="safe logical label|unsafe environment detail"):
        _evidence(target_labels=("linuxch.example.internal",))
    with pytest.raises(ValueError, match="safe route label"):
        SmokeEvidence(
            smoke_id="smoke-url",
            target_label="dogfood_compose",
            route_label="https://internal.example/agents/current.json",
            observed_at="2026-06-05T16:01:00+00:00",
            result="failed",
        )


def test_file_activation_store_writes_current_and_append_only_history(tmp_path: Path) -> None:
    store = FileActivationEvidenceStore(tmp_path / "state", "agentic-mesh-dev")
    evidence = _evidence()

    current_path = store.write_current(evidence, event_type="activation_blocked")
    loaded = store.read_current("work-activation")
    history = current_path.with_name("activation-evidence-history.jsonl")

    assert loaded == evidence
    assert current_path == (
        tmp_path
        / "state"
        / "projects"
        / "agentic-mesh-dev"
        / "work_items"
        / "work-activation"
        / "activation-evidence.json"
    )
    lines = history.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "activation_blocked"
    assert event["status_url_present"] is True
    assert "status_url" not in event


def test_file_activation_store_rejects_invalid_ids_and_symlink_escape(tmp_path: Path) -> None:
    store = FileActivationEvidenceStore(tmp_path / "state", "agentic-mesh-dev")
    with pytest.raises(ValueError, match="contained logical id"):
        store.current_path("../escape")

    outside = tmp_path / "outside"
    outside.mkdir()
    work_items = tmp_path / "state" / "projects" / "agentic-mesh-dev" / "work_items"
    work_items.mkdir(parents=True, exist_ok=True)
    (work_items / "work-activation").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes configured root|symlink"):
        store.write_current(_evidence())


def test_corrupt_current_record_returns_row_level_read_error(tmp_path: Path) -> None:
    store = FileActivationEvidenceStore(tmp_path / "state", "agentic-mesh-dev")
    current = store.current_path("work-activation", create_dirs=True)
    current.write_text("{not-json", encoding="utf-8")

    result = store.read_current_with_error("work-activation")

    assert isinstance(result, ActivationReadError)
    assert result.to_summary()["activation_status"] == "unknown"
    assert result.to_summary()["attention_needed"] is True


def test_activation_problem_status_maps_stale_runtime_to_runtime_recovery() -> None:
    problem = activation_problem_status(
        evidence=_evidence(
            smoke_status="failed",
            failure_class="source_changed_running_service_not_updated",
            next_action="Rebuild image and recreate the target service.",
        ),
        affected_role="engineering",
        role_instance_id="agentic-mesh-dev.engineering.1",
    )
    payload = problem.to_dict()

    assert payload["status"] == "needs_runtime_recovery"
    assert payload["problem_kind"] == "runtime_failed"
    assert payload["failure_class"] == "source_changed_running_service_not_updated"
    assert payload["recovery_action"] == "operator_review"
    assert payload["action_owner"] == "runtime/operator"


def test_journal_and_telemetry_fields_are_allowlisted() -> None:
    evidence = _evidence()

    journal = evidence.journal_fields(
        event_type="activation_blocked",
        role_id="engineering",
        role_instance_id="agentic-mesh-dev.engineering.1",
    )
    telemetry = evidence.telemetry_attributes(event_type="activation_blocked")

    assert set(journal) <= {
        "project_id",
        "work_item_id",
        "work_item_type",
        "queue_item_id",
        "lifecycle_state",
        "role_id",
        "role_instance_id",
        "correlation_id",
        "source_anchor_ref",
        "schema_version",
        "impact_categories",
        "activation_paths",
        "target_labels",
        "source_status",
        "activation_status",
        "smoke_status",
        "failure_class",
        "action_owner",
        "retryable",
        "event_type",
        "status_url_present",
    }
    assert all(key.startswith("agentic_mesh.activation.") for key in telemetry)
    serialized = json.dumps({"journal": journal, "telemetry": telemetry})
    assert "service_url" not in serialized
    assert "tenant_id" not in serialized
    assert "secret_ref" not in serialized
    assert "command" not in serialized
