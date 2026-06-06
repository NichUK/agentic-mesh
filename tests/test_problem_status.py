from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh.problem_status import ProblemStatus
from agentic_mesh.problem_status import ProblemStatusStore
from agentic_mesh.problem_status import worker_problem_status


def test_problem_status_serializes_labels_and_redacts_forbidden_terms() -> None:
    status = worker_problem_status(
        failure_class="timeout",
        reason=(
            "Codex timed out; tenant_id=t-123 service_url=https://example.invalid "
            "secret_ref=codex-secret /tmp/runtime/path"
        ),
        recovery_action="retry_same_state",
        retryable=True,
        role_id="engineering",
        role_instance_id="agentic-mesh-dev.engineering.1",
        message_payload={
            "work_item_id": "work-123",
            "work_item_type": "slice",
            "queue_item_id": "queue-123",
            "lifecycle_state": "implementation",
            "source_anchor": {"summary": "Teams thread channel_id=19:secret"},
        },
        source_message_id="msg-123",
        correlation_id="corr-123",
        lifecycle_state="implementation",
        worker_adapter="codex-cli",
        worker_model="codex",
        timeout_seconds=3600,
        artifact_paths=["work-items/work-123/100-implementation-log.md"],
    )

    payload = status.to_dict()
    encoded = json.dumps(payload)

    assert payload["schema_version"] == "problem-status-v0"
    assert payload["status"] == "needs_runtime_recovery"
    assert payload["status_label"] == "Needs runtime recovery"
    assert payload["problem_label"] == "Worker failed: timeout"
    assert payload["retryability_label"] == "Retryable after the listed action"
    assert payload["artifact_verification"][0]["verification"] == "unverified_partial"
    assert "tenant_id" not in encoded
    assert "service_url" not in encoded
    assert "secret_ref" not in encoded
    assert "/tmp/runtime/path" not in encoded
    assert "channel_id" not in encoded


def test_problem_status_rejects_role_problem_as_runtime_recovery() -> None:
    with pytest.raises(ValueError, match="role-owned problems"):
        ProblemStatus(
            work_item_id="work-123",
            work_item_type="slice",
            queue_item_id=None,
            source_message_id=None,
            source_anchor_ref=None,
            source_anchor_summary=None,
            correlation_id="corr-123",
            status="needs_runtime_recovery",
            problem_kind="role_blocker",
            lifecycle_state="implementation",
            affected_role="engineering",
            role_instance_id=None,
            reason="Blocked by product scope.",
            next_action="Clarify scope.",
            action_owner="product-manager",
            retryable=True,
        )


def test_problem_status_store_writes_snapshot_and_history(tmp_path: Path) -> None:
    store = ProblemStatusStore(tmp_path / "state", "agentic-mesh-dev")
    status = worker_problem_status(
        failure_class="invalid_result",
        reason="Worker returned invalid result JSON.",
        recovery_action="retry_same_state",
        retryable=True,
        role_id="engineering",
        role_instance_id="agentic-mesh-dev.engineering.1",
        message_payload={
            "work_item_id": "work-123",
            "work_item_type": "slice",
            "lifecycle_state": "implementation",
        },
        source_message_id="msg-123",
        correlation_id="corr-123",
        lifecycle_state="implementation",
        worker_adapter="codex-cli",
        worker_model="codex",
    )

    snapshot_path = store.write_current(status)
    current = store.read_current("work-123")

    assert snapshot_path.name == "problem-status.json"
    assert current is not None
    assert current["failure_class"] == "invalid_result"
    assert store.history_path("work-123").exists()
    assert store.clear_current("work-123") is True
    assert store.read_current("work-123") is None
