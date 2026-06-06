from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agentic_mesh.activation_evidence import FileActivationEvidenceStore
from agentic_mesh.capabilities import CapabilityReadinessStore
from agentic_mesh.capabilities import unknown_summary
from agentic_mesh.agent_run_state import FileAgentRunStateStore
from agentic_mesh.agent_run_state import RunStateReadError
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.human_gates import STATUS_LABELS
from agentic_mesh.models import MeshConfig
from agentic_mesh.models import RoleInstanceConfig
from agentic_mesh.models import utc_now_iso
from agentic_mesh.worker_runs import FileWorkerRunStore
from agentic_mesh.worker_runs import WorkerRun
from agentic_mesh.worker_runs import WorkerRunReadError


SCHEMA_VERSION = "current-agents-v0"
REFRESH_MODE = "manual_browser_refresh"

CONDITION_PRECEDENCE = [
    "connector_notification_failed",
    "provider_limited",
    "provider_auth_failed",
    "timed_out",
    "needs_runtime_recovery",
    "failed_terminal",
    "blocked_terminal",
    "waiting_for_human",
    "stale_active",
    "running_long",
    "running",
    "pending_queue",
    "hibernated",
    "idle",
    "not_observed",
    "unknown",
]

ATTENTION_CONDITIONS = {
    "connector_notification_failed",
    "provider_limited",
    "provider_auth_failed",
    "timed_out",
    "needs_runtime_recovery",
    "failed_terminal",
    "blocked_terminal",
    "waiting_for_human",
    "stale_active",
}

CONDITION_LABELS = {
    "connector_notification_failed": "Connector notification failed",
    "provider_limited": "Provider limited",
    "provider_auth_failed": "Provider auth failed",
    "timed_out": "Timed out",
    "needs_runtime_recovery": "Needs runtime recovery",
    "failed_terminal": "Failed terminal",
    "blocked_terminal": "Blocked terminal",
    "waiting_for_human": "Waiting for human",
    "stale_active": "Stale active",
    "running_long": "Running long",
    "running": "Running",
    "pending_queue": "Pending queue",
    "hibernated": "Hibernated",
    "idle": "Idle",
    "not_observed": "Not observed",
    "unknown": "Unknown",
}

CONDITION_GROUPS = {
    "connector_notification_failed": "attention",
    "provider_limited": "attention",
    "provider_auth_failed": "attention",
    "timed_out": "attention",
    "needs_runtime_recovery": "attention",
    "failed_terminal": "attention",
    "blocked_terminal": "attention",
    "waiting_for_human": "attention",
    "stale_active": "attention",
    "running_long": "running",
    "running": "running",
    "pending_queue": "pending",
    "hibernated": "inactive",
    "idle": "inactive",
    "not_observed": "unknown",
    "unknown": "unknown",
}

PROVIDER_LIMIT_CLASSES = {
    "usage_limit",
    "credit_exhausted",
    "quota_exceeded",
    "rate_limited",
}
PROVIDER_CLASS_LABELS = {
    "usage_limit": "Usage limit",
    "credit_exhausted": "Credit exhausted",
    "quota_exceeded": "Quota exceeded",
    "rate_limited": "Rate limited",
    "auth_failed": "Auth failed",
    "timeout": "Timeout",
    "process_failed": "Process failed",
    "invalid_result": "Invalid result",
    "missing_executable": "Missing executable",
    "unsupported_adapter": "Unsupported adapter",
    "auth_missing": "Auth failed",
    "schema_failed": "Invalid result",
    "publication_failed": "Publication failed",
    "malformed_route": "Malformed route",
    "malformed_handoff": "Malformed handoff",
    "unknown_provider_failure": "Unknown provider failure",
}

NOTIFICATION_ATTENTION_STATUSES = {
    "failed",
    "dead_lettered",
    "blocked_unroutable",
    "retry_scheduled",
}

SAFE_SOURCE_ANCHOR_KEYS = {
    "connector_type",
    "connector_id",
    "source_scope",
    "source_anchor_ref",
    "display_label",
    "received_at",
}

FORBIDDEN_KEYS = {
    "tenant_id",
    "team_id",
    "channel_id",
    "conversation_id",
    "activity_id",
    "actor_id",
    "user_id",
    "bot_id",
    "service_url",
    "graph_url",
    "external_url",
    "raw_source_message_id",
    "raw_payload_ref",
    "secret_ref",
    "credential_ref",
    "mount_ref",
    "oauth_path",
    "private_key_path",
    "stdout",
    "stderr",
    "command",
    "command_line",
    "prompt",
    "model_response",
    "provider_error_body",
    "container_id",
    "process_id",
    "pid",
    "hostname",
    "log_path",
    "support_diagnostics",
}


def route_links() -> dict[str, str]:
    return {
        "current_agents_html": "/agents/current",
        "current_agents_json": "/agents/current.json",
        "status_html": "/status",
        "status_json": "/status.json",
        "status_agents_html": "/status/agents",
        "status_agents_json": "/status/agents.json",
    }


def build_current_agent_status(
    *,
    mesh_config: MeshConfig,
    state_root: Path,
    workspace_root: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    del workspace_root
    project_id = mesh_config.project.project_id
    generated = generated_at or utc_now_iso()
    run_store = FileAgentRunStateStore(state_root, project_id)
    worker_run_store = FileWorkerRunStore(state_root, project_id)
    pending_counts = _pending_counts(state_root, project_id)
    active_claims = _active_claims(state_root, project_id)
    lifecycle_records = _lifecycle_records(state_root, project_id)
    problem_statuses = _problem_statuses(state_root, project_id)
    activation_summaries = _activation_summaries(state_root, project_id)
    notifications = _notification_attempts(state_root, project_id)
    human_gates = _human_gate_summaries(state_root, project_id)
    readiness_store = CapabilityReadinessStore(state_root, project_id, create_dirs=False)

    rows = []
    for instance in sorted(
        mesh_config.instances.values(),
        key=lambda candidate: candidate.instance_id,
    ):
        row = _build_agent_row(
            instance=instance,
            generated_at=generated,
            pending_count=pending_counts.get(instance.role_id, 0),
            active_claim=active_claims.get(instance.instance_id),
            lifecycle_record=lifecycle_records.get(instance.instance_id),
            problem_statuses=problem_statuses,
            activation_summaries=activation_summaries,
            notifications=notifications,
            human_gates=human_gates,
            run_state_result=run_store.read_current_with_error(instance.instance_id),
            worker_run_result=worker_run_store.read_current_with_error(
                instance.instance_id,
            ),
            capability_readiness=(
                readiness_store.read_current(instance.instance_id) or {}
            ).get("summary")
            or unknown_summary(
                project_id=project_id,
                role_id=instance.role_id,
                role_instance_id=instance.instance_id,
                generated_at=generated,
                diagnostic_class="missing_evidence",
            ),
        )
        rows.append(row)

    counts = _counts(rows)
    attention = _attention(rows)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "project": {
            "project_id": mesh_config.project.project_id,
            "name": mesh_config.project.name,
            "default_repository": mesh_config.project.workspace.default_repository,
            "workspace_label": mesh_config.project.workspace.root,
        },
        "links": route_links(),
        "counts": counts,
        "attention": attention,
        "agents": rows,
        "read_only": True,
        "refresh_mode": REFRESH_MODE,
    }
    assert_current_agents_payload_safe(payload)
    return payload


def _build_agent_row(
    *,
    instance: RoleInstanceConfig,
    generated_at: str,
    pending_count: int,
    active_claim: dict[str, Any] | None,
    lifecycle_record: dict[str, Any] | None,
    problem_statuses: list[dict[str, Any]],
    activation_summaries: dict[str, dict[str, Any]],
    notifications: list[dict[str, Any]],
    human_gates: dict[str, dict[str, Any]],
    run_state_result: Any,
    worker_run_result: Any,
    capability_readiness: dict[str, Any],
) -> dict[str, Any]:
    extraction_error = None
    run_state = None
    worker_run = None
    if isinstance(run_state_result, RunStateReadError):
        extraction_error = run_state_result.error_class
    else:
        run_state = run_state_result
    if isinstance(worker_run_result, WorkerRunReadError):
        extraction_error = worker_run_result.error_class
    elif isinstance(worker_run_result, WorkerRun):
        worker_run = worker_run_result

    claim_payload = (active_claim or {}).get("payload") or {}
    if run_state is not None and active_claim is None and worker_run is None:
        # Legacy agent_run_state/current.json files predate WorkerRun and can be
        # left behind after a worker exits. Treat them as historical diagnostics
        # unless a live queue claim or WorkerRun confirms the execution is active.
        run_state = None
    work_item_id = claim_payload.get("work_item_id") or (
        worker_run.work_item_id if worker_run is not None else (
            run_state.work_item_id if run_state is not None else None
        )
    )
    queue_item_id = claim_payload.get("queue_item_id") or (
        worker_run.queue_item_id if worker_run is not None else (
            run_state.queue_item_id if run_state is not None else None
        )
    )
    matched_problem = _matched_problem(
        instance=instance,
        work_item_id=work_item_id,
        statuses=problem_statuses,
    )
    if not work_item_id and matched_problem is not None:
        work_item_id = matched_problem.get("work_item_id")
    matched_activation = activation_summaries.get(str(work_item_id)) if work_item_id else None
    matched_notification = _matched_notification(
        work_item_id=work_item_id,
        queue_item_id=queue_item_id,
        notifications=notifications,
    )
    human_gate = human_gates.get(str(work_item_id)) if work_item_id else None
    thresholds = _thresholds(instance)

    candidates: list[tuple[str, str, str | None]] = []
    if extraction_error:
        candidates.append(("unknown", "run_state", "Run-state record is incomplete."))
    if matched_notification is not None:
        candidates.append(
            (
                "connector_notification_failed",
                "notification_attempt",
                "Connector notification needs operator attention.",
            )
        )
    if matched_problem is not None:
        candidates.append(_problem_candidate(matched_problem))
    if human_gate and human_gate.get("status") in {"waiting_for_response", "pending"}:
        candidates.append(
            (
                "waiting_for_human",
                "human_gate_summary",
                "Current human response gate is waiting for a response.",
            )
        )
    worker_candidate = _worker_run_candidate(worker_run)
    if worker_candidate is not None:
        candidates.append(worker_candidate)

    running_candidate = _running_candidate(
        active_claim=active_claim,
        run_state=run_state,
        worker_run=worker_run,
        thresholds=thresholds,
        generated_at=generated_at,
    )
    if running_candidate is not None:
        candidates.append(running_candidate)
    elif run_state is not None:
        candidates.append(
            (
                "unknown",
                run_state.last_safe_evidence_source,
                "Run-state evidence exists without an active claim.",
            )
        )

    if pending_count > 0 and active_claim is None:
        candidates.append(
            ("pending_queue", "pending_queue", "Role has pending queue work.")
        )

    lifecycle_state = str((lifecycle_record or {}).get("state") or "not_observed")
    if lifecycle_state == "hibernated":
        candidates.append(("hibernated", "lifecycle_record", "Role instance is hibernated."))
    elif lifecycle_state == "idle":
        candidates.append(("idle", "lifecycle_record", "Role instance is idle."))

    if not candidates:
        candidates.append(
            (
                "not_observed",
                "configured_instance",
                "Configured role instance has no current runtime evidence.",
            )
        )

    condition, evidence_source, reason = _primary_candidate(candidates)
    secondary = [
        candidate
        for candidate, _, _ in candidates
        if candidate != condition
    ]
    claim_age = _elapsed(active_claim.get("claimed_at"), generated_at) if active_claim else None
    last_heartbeat = (
        worker_run.last_heartbeat_at if worker_run is not None else (
            run_state.last_heartbeat_at if run_state is not None else None
        )
    )
    last_progress = (
        worker_run.last_progress_at if worker_run is not None else (
            run_state.last_progress_at if run_state is not None else None
        )
    )
    heartbeat_age = _elapsed(last_heartbeat, generated_at)
    elapsed = None
    if active_claim is not None:
        elapsed = claim_age
    elif run_state is not None:
        elapsed = _elapsed(run_state.started_at, generated_at)
    elif worker_run is not None:
        elapsed = _elapsed(worker_run.started_at, generated_at)

    provider_class = _provider_recovery_class(matched_problem)
    if worker_run is not None and worker_run.provider_recovery_class:
        provider_class = worker_run.provider_recovery_class
    status_url = _status_url(work_item_id)
    row = {
        "project_id": instance.project_id,
        "role_id": instance.role_id,
        "role_instance_id": instance.instance_id,
        "role_template_id": instance.template.role_id,
        "service_label": _safe_service_label(instance),
        "lifecycle_record_state": lifecycle_state,
        "condition": condition,
        "condition_label": CONDITION_LABELS[condition],
        "condition_group": CONDITION_GROUPS[condition],
        "secondary_conditions": secondary,
        "condition_reason": reason,
        "evidence_source": evidence_source,
        "evidence_observed_at": _evidence_observed_at(
            active_claim=active_claim,
            run_state=run_state,
            lifecycle_record=lifecycle_record,
            problem_status=matched_problem,
            notification=matched_notification,
            human_gate=human_gate,
        ),
        "current_work_item_id": work_item_id,
        "current_work_item_type": claim_payload.get("work_item_type")
        or (worker_run.work_item_type if worker_run is not None else None)
        or (run_state.work_item_type if run_state is not None else None),
        "queue_item_id": queue_item_id,
        "source_anchor_summary": _source_anchor_summary(claim_payload.get("source_anchor")),
        "message_id": active_claim.get("message_id") if active_claim else (
            worker_run.message_id if worker_run is not None else (
                run_state.message_id if run_state is not None else None
            )
        ),
        "lifecycle_state": claim_payload.get("lifecycle_state")
        or (worker_run.lifecycle_state if worker_run is not None else None)
        or (run_state.lifecycle_state if run_state is not None else None)
        or (matched_problem or {}).get("lifecycle_state"),
        "owner_role": (matched_problem or {}).get("affected_role") or instance.role_id,
        "worker_adapter": instance.override.worker.adapter,
        "worker_model": instance.override.worker.model,
        "claim_started_at": active_claim.get("claimed_at") if active_claim else None,
        "elapsed_seconds": _rounded(elapsed),
        "last_heartbeat_at": last_heartbeat,
        "last_progress_at": last_progress,
        "heartbeat_age_seconds": _rounded(heartbeat_age),
        "pending_queue_count": pending_count,
        "claim_age_seconds": _rounded(claim_age),
        "timeout_seconds": (
            worker_run.hard_timeout_seconds if worker_run is not None else None
        )
        or (run_state.timeout_seconds if run_state is not None else None)
        or instance.override.worker.timeout_seconds,
        "progress_window_seconds": (
            worker_run.stale_output_seconds if worker_run is not None else None
        )
        or (
            run_state.progress_window_seconds if run_state is not None else None
        )
        or instance.override.worker.progress_window_seconds,
        "worker_run": (
            worker_run.safe_summary(generated_at=generated_at)
            if worker_run is not None
            else None
        ),
        "capability_readiness": capability_readiness,
        "long_running_threshold_seconds": thresholds["long_running"],
        "stale_threshold_seconds": thresholds["stale"],
        "provider_recovery_class": provider_class,
        "provider_recovery_label": (
            PROVIDER_CLASS_LABELS.get(provider_class) if provider_class else None
        ),
        "retryable": (matched_problem or {}).get("retryable"),
        "recovery_action": (matched_problem or {}).get("recovery_action"),
        "problem_kind": (matched_problem or {}).get("problem_kind"),
        "failure_class": (matched_problem or {}).get("failure_class"),
        "activation_summary": matched_activation,
        "approval_status": (human_gate or {}).get("status")
        or _approval_context_from_problem(matched_problem),
        "current_human_gate_id": (human_gate or {}).get("gate_id"),
        "next_human_gate_id": (human_gate or {}).get("next_gate_id"),
        "approval_attention_reason": (human_gate or {}).get("attention_reason"),
        "notification_status": (matched_notification or {}).get("status"),
        "notification_failure_reason": (
            matched_notification or {}
        ).get("redacted_error_class"),
        "notification_fallback_used": (
            matched_notification or {}
        ).get("fallback_used"),
        "next_action": _next_action(condition, matched_problem, matched_notification),
        "action_owner": _action_owner(condition, matched_problem),
        "status_url": status_url,
        "work_item_json_url": f"{status_url}.json" if status_url else None,
        "extraction_error": extraction_error,
    }
    return row


def _running_candidate(
    *,
    active_claim: dict[str, Any] | None,
    run_state: Any,
    worker_run: WorkerRun | None,
    thresholds: dict[str, int],
    generated_at: str,
) -> tuple[str, str, str | None] | None:
    if active_claim is None:
        return None
    claim_age = _elapsed(active_claim.get("claimed_at"), generated_at) or 0
    if worker_run is not None and worker_run.run_status == "timed_out":
        return "timed_out", "worker_run", "Worker execution timed out."
    if worker_run is not None and worker_run.run_condition == "stale_output":
        return "stale_active", "worker_run", "Worker output is stale."
    if run_state is not None and run_state.run_state == "timed_out":
        return "timed_out", "run_state", "Worker execution timed out."
    freshness = None
    evidence = None
    if worker_run is not None:
        freshness = (
            worker_run.last_progress_at
            or worker_run.last_heartbeat_at
            or worker_run.started_at
        )
        evidence = "worker_run"
    elif run_state is not None:
        freshness = run_state.last_progress_at or run_state.last_heartbeat_at or run_state.started_at
        evidence = run_state.last_safe_evidence_source
    if freshness is None:
        if claim_age >= thresholds["stale"]:
            return (
                "stale_active",
                "active_claim",
                "Active claim has no recent runtime progress evidence.",
            )
        return (
            "unknown",
            "active_claim",
            "Active claim exists but runtime progress evidence is not available yet.",
        )
    freshness_age = _elapsed(freshness, generated_at)
    if freshness_age is None or freshness_age >= thresholds["stale"]:
        return (
            "stale_active",
            evidence or "active_claim",
            "Runtime progress evidence is older than the stale threshold.",
        )
    if claim_age >= thresholds["long_running"]:
        return (
            "running_long",
            evidence or "run_state",
            "Active claim is still progressing beyond the long-running threshold.",
        )
    return ("running", evidence or "run_state", "Active claim has fresh runtime evidence.")


def _worker_run_candidate(
    worker_run: WorkerRun | None,
) -> tuple[str, str, str | None] | None:
    if worker_run is None:
        return None
    if worker_run.run_condition == "provider_limited":
        return "provider_limited", "worker_run", worker_run.next_action
    if worker_run.run_condition == "provider_auth_failed":
        return "provider_auth_failed", "worker_run", worker_run.next_action
    if worker_run.run_condition == "timed_out":
        return "timed_out", "worker_run", worker_run.next_action
    if worker_run.run_condition in {
        "invalid_result",
        "process_failed",
        "missing_executable",
        "unsupported_adapter",
        "runtime_publication_failed",
        "unknown_provider_failure",
    }:
        return "needs_runtime_recovery", "worker_run", worker_run.next_action
    if worker_run.run_condition == "stale_output":
        return "stale_active", "worker_run", "Worker output is stale."
    return None


def _problem_candidate(problem: dict[str, Any]) -> tuple[str, str, str | None]:
    failure_class = str(problem.get("failure_class") or "")
    recovery_action = str(problem.get("recovery_action") or "")
    problem_kind = str(problem.get("problem_kind") or "")
    if failure_class in PROVIDER_LIMIT_CLASSES:
        return "provider_limited", "problem_status", problem.get("reason_summary")
    if failure_class in {"auth_failed", "auth_missing"} or recovery_action == "repair_auth":
        return "provider_auth_failed", "problem_status", problem.get("reason_summary")
    if failure_class == "timeout":
        return "timed_out", "problem_status", problem.get("reason_summary")
    if problem.get("status") == "needs_runtime_recovery":
        return "needs_runtime_recovery", "problem_status", problem.get("reason_summary")
    if problem.get("status") == "failed" and problem_kind == "role_failure":
        return "failed_terminal", "problem_status", problem.get("reason_summary")
    if problem.get("status") == "blocked" and problem_kind == "role_blocker":
        return "blocked_terminal", "problem_status", problem.get("reason_summary")
    return "unknown", "problem_status", "Problem status was present but not classifiable."


def _primary_candidate(candidates: list[tuple[str, str, str | None]]) -> tuple[str, str, str | None]:
    ranked = sorted(
        candidates,
        key=lambda candidate: CONDITION_PRECEDENCE.index(candidate[0]),
    )
    return ranked[0]


def _thresholds(instance: RoleInstanceConfig) -> dict[str, int]:
    worker = instance.override.worker
    long_running = worker.timeout_seconds or (
        3600 if instance.role_id == "engineering" else 1800
    )
    stale = worker.progress_window_seconds or (
        1800 if instance.role_id == "engineering" else 900
    )
    return {"long_running": int(long_running), "stale": int(stale)}


def _pending_counts(state_root: Path, project_id: str) -> dict[str, int]:
    queue_root = state_root / "projects" / project_id / "queues"
    counts: dict[str, int] = {}
    if not queue_root.exists():
        return counts
    for role_dir in sorted(path for path in queue_root.iterdir() if path.is_dir()):
        counts[role_dir.name] = len(list((role_dir / "pending").glob("*.json")))
    return counts


def _active_claims(state_root: Path, project_id: str) -> dict[str, dict[str, Any]]:
    queue_root = state_root / "projects" / project_id / "queues"
    claims: dict[str, dict[str, Any]] = {}
    if not queue_root.exists():
        return claims
    for path in sorted(queue_root.glob("*/claimed/*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            instance_id = str(data.get("claimed_by") or path.parent.name)
            row = {
                "role_id": str(data.get("role_id") or path.parents[2].name),
                "message_id": data.get("message_id"),
                "claimed_at": data.get("claimed_at"),
                "payload": data.get("payload") or {},
            }
        except Exception:
            continue
        previous = claims.get(instance_id)
        if previous is None or str(row.get("claimed_at") or "") > str(previous.get("claimed_at") or ""):
            claims[instance_id] = row
    return claims


def _lifecycle_records(state_root: Path, project_id: str) -> dict[str, dict[str, Any]]:
    root = state_root / "projects" / project_id / "lifecycle"
    records = {}
    if not root.exists():
        return records
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        instance_id = str(data.get("role_instance_id") or path.stem)
        records[instance_id] = data
    return records


def _problem_statuses(state_root: Path, project_id: str) -> list[dict[str, Any]]:
    root = state_root / "projects" / project_id / "work_items"
    if not root.exists():
        return []
    statuses = []
    for path in sorted(root.glob("*/problem-status.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        statuses.append(data)
    return statuses


def _activation_summaries(state_root: Path, project_id: str) -> dict[str, dict[str, Any]]:
    store = FileActivationEvidenceStore(state_root, project_id, create_dirs=False)
    records, errors = store.list_current()
    summaries = {record.work_item_id: record.to_summary() for record in records}
    for error in errors:
        summaries[error.work_item_id] = error.to_summary()
    return summaries


def _notification_attempts(state_root: Path, project_id: str) -> list[dict[str, Any]]:
    root = state_root / "projects" / project_id / "notification_attempts"
    if not root.exists():
        return []
    attempts = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("status") or "") in NOTIFICATION_ATTENTION_STATUSES:
            attempts.append(_safe_notification_attempt(data))
    return attempts


def _human_gate_summaries(state_root: Path, project_id: str) -> dict[str, dict[str, Any]]:
    store = FileHumanGateRequestStore(state_root, project_id)
    from_store: dict[str, dict[str, Any]] = {}
    for request in store.read_all():
        status = request.status
        if status == "request_pending_delivery":
            status = "request_failed"
        if status == "waiting_for_response":
            status = "pending"
        from_store[request.work_item_id] = {
            "schema_version": "human-gate-summary-v0",
            "status": status,
            "display_label": STATUS_LABELS.get(status, status),
            "gate_id": request.gate_id,
            "next_gate_id": None,
            "approval_request_id": request.approval_request_id
            or request.response_request_id,
            "response_request_id": request.response_request_id,
            "notification_attempt_id": request.current_notification_attempt_id,
            "attention_reason": request.status_reason
            or ("Human response is required." if status == "pending" else None),
            "updated_at": request.updated_at,
        }
    if from_store:
        return from_store
    path = state_root / "projects" / project_id / "journal" / "events.jsonl"
    if not path.exists():
        return {}
    requested: dict[str, dict[str, Any]] = {}
    completed: set[tuple[str, str | None]] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            work_item_id = event.get("work_item_id")
            gate_id = event.get("gate_id")
            if not work_item_id:
                continue
            if event.get("event_type") == "human_response_requested":
                requested[str(work_item_id)] = {
                    "status": "waiting_for_response",
                    "gate_id": gate_id,
                    "attention_reason": "Human response is required.",
                    "updated_at": event.get("timestamp"),
                }
            if event.get("event_type") in {
                "human_response_recorded",
                "human_response_received_from_teams",
            }:
                completed.add((str(work_item_id), gate_id))
    for work_item_id, gate in list(requested.items()):
        if (work_item_id, gate.get("gate_id")) in completed:
            requested[work_item_id] = {
                **gate,
                "status": "completed",
                "attention_reason": None,
            }
    return requested


def _matched_problem(
    *,
    instance: RoleInstanceConfig,
    work_item_id: Any,
    statuses: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [
        status
        for status in statuses
        if status.get("role_instance_id") == instance.instance_id
        or (
            status.get("affected_role") == instance.role_id
            and work_item_id
            and status.get("work_item_id") == work_item_id
        )
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda status: str(status.get("occurred_at") or ""))[-1]


def _matched_notification(
    *,
    work_item_id: Any,
    queue_item_id: Any,
    notifications: list[dict[str, Any]],
) -> dict[str, Any] | None:
    matches = [
        item
        for item in notifications
        if (work_item_id and item.get("work_item_id") == work_item_id)
        or (queue_item_id and item.get("queue_item_id") == queue_item_id)
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: str(item.get("updated_at") or ""))[-1]


def _safe_notification_attempt(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": data.get("attempt_id"),
        "event_id": data.get("event_id"),
        "status": data.get("status"),
        "work_item_id": data.get("work_item_id"),
        "queue_item_id": data.get("queue_item_id"),
        "connector_id": data.get("connector_id"),
        "connector_type": data.get("connector_type"),
        "route_label": data.get("route_label"),
        "retry_count": int(data.get("retry_count", 0)),
        "fallback_used": bool(data.get("fallback_used", False)),
        "fallback_reason": data.get("fallback_reason"),
        "redacted_error_class": data.get("redacted_error_class"),
        "updated_at": data.get("updated_at") or data.get("created_at"),
    }


def _source_anchor_summary(source_anchor: Any) -> dict[str, Any] | None:
    if not isinstance(source_anchor, dict):
        return None
    return {
        key: str(source_anchor[key])
        for key in SAFE_SOURCE_ANCHOR_KEYS
        if source_anchor.get(key) is not None
    }


def _safe_service_label(instance: RoleInstanceConfig) -> str:
    label = instance.service_name or instance.telemetry_service_name
    if not label:
        return "unknown"
    if "/" in label or "\\" in label or "://" in label or ".." in label:
        return "unknown"
    return label


def _provider_recovery_class(problem: dict[str, Any] | None) -> str | None:
    if not problem:
        return None
    failure_class = str(problem.get("failure_class") or "")
    if failure_class == "auth_missing":
        return "auth_failed"
    if failure_class == "schema_failed":
        return "invalid_result"
    if failure_class in PROVIDER_CLASS_LABELS:
        return failure_class
    if problem.get("status") == "needs_runtime_recovery":
        return "unknown_provider_failure"
    return None


def _approval_context_from_problem(problem: dict[str, Any] | None) -> str | None:
    if not problem:
        return None
    status = problem.get("status")
    if status == "blocked":
        return "blocked_before_gate"
    if status == "failed":
        return "failed_before_gate"
    return None


def _next_action(
    condition: str,
    problem: dict[str, Any] | None,
    notification: dict[str, Any] | None,
) -> str:
    if problem and problem.get("next_action"):
        return str(problem["next_action"])
    if condition == "connector_notification_failed":
        status = str((notification or {}).get("status") or "failed")
        if status == "blocked_unroutable":
            return "Configure a safe notification surface or inspect connector routing."
        return "Inspect connector health and retry policy."
    if condition == "waiting_for_human":
        return "Wait for the requested human response."
    if condition == "stale_active":
        return "Operator should inspect or reclaim the stale active claim."
    if condition in {"idle", "hibernated", "not_observed"}:
        return "No action"
    if condition == "pending_queue":
        return "Runtime should claim the next pending message."
    return "Next action unknown"


def _action_owner(condition: str, problem: dict[str, Any] | None) -> str:
    if problem and problem.get("action_owner"):
        return str(problem["action_owner"])
    if condition in {
        "connector_notification_failed",
        "provider_limited",
        "provider_auth_failed",
        "timed_out",
        "needs_runtime_recovery",
        "stale_active",
    }:
        return "runtime/operator"
    if condition == "waiting_for_human":
        return "human"
    return "none"


def _evidence_observed_at(
    *,
    active_claim: dict[str, Any] | None,
    run_state: Any,
    lifecycle_record: dict[str, Any] | None,
    problem_status: dict[str, Any] | None,
    notification: dict[str, Any] | None,
    human_gate: dict[str, Any] | None,
) -> str | None:
    for candidate in [
        (notification or {}).get("updated_at"),
        (problem_status or {}).get("occurred_at"),
        (human_gate or {}).get("updated_at"),
        run_state.last_progress_at if run_state is not None else None,
        run_state.last_heartbeat_at if run_state is not None else None,
        run_state.started_at if run_state is not None else None,
        active_claim.get("claimed_at") if active_claim else None,
        (lifecycle_record or {}).get("updated_at"),
    ]:
        if candidate:
            return str(candidate)
    return None


def _status_url(work_item_id: Any) -> str | None:
    if not isinstance(work_item_id, str) or not work_item_id:
        return None
    if "/" in work_item_id or "\\" in work_item_id or ".." in work_item_id:
        return None
    return f"/work-items/{quote(work_item_id, safe='')}"


def _counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_condition = Counter(row["condition"] for row in rows)
    by_group = Counter(row["condition_group"] for row in rows)
    return {
        "total_agents": len(rows),
        "by_condition": dict(sorted(by_condition.items())),
        "by_condition_group": dict(sorted(by_group.items())),
        "attention_count": sum(
            1
            for row in rows
            if row["condition"] in ATTENTION_CONDITIONS
            or set(row.get("secondary_conditions") or []) & ATTENTION_CONDITIONS
        ),
        "running_count": int(by_condition.get("running", 0))
        + int(by_condition.get("running_long", 0)),
        "pending_queue_count": sum(int(row.get("pending_queue_count") or 0) for row in rows),
        "unknown_count": int(by_condition.get("unknown", 0))
        + int(by_condition.get("not_observed", 0)),
        "extraction_error_count": sum(1 for row in rows if row.get("extraction_error")),
    }


def _attention(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in rows
        if row["condition"] in ATTENTION_CONDITIONS
        or set(row.get("secondary_conditions") or []) & ATTENTION_CONDITIONS
    ]
    candidates.sort(
        key=lambda row: (
            CONDITION_PRECEDENCE.index(row["condition"]),
            str(row.get("evidence_observed_at") or ""),
            row["role_instance_id"],
        )
    )
    return [
        {
            "role_instance_id": row["role_instance_id"],
            "condition": row["condition"],
            "condition_label": row["condition_label"],
            "current_work_item_id": row["current_work_item_id"],
            "lifecycle_state": row["lifecycle_state"],
            "elapsed_seconds": row["elapsed_seconds"],
            "claim_age_seconds": row["claim_age_seconds"],
            "evidence_source": row["evidence_source"],
            "attention_reason": row["condition_reason"],
            "next_action": row["next_action"],
            "action_owner": row["action_owner"],
            "status_url": row["status_url"],
            "work_item_json_url": row["work_item_json_url"],
        }
        for row in candidates
    ]


def _elapsed(iso_timestamp: Any, generated_at: str) -> float | None:
    if not iso_timestamp:
        return None
    try:
        start = datetime.fromisoformat(str(iso_timestamp))
        end = datetime.fromisoformat(generated_at)
        return max(0.0, (end - start).total_seconds())
    except Exception:
        return None


def _rounded(value: float | None) -> int | None:
    if value is None:
        return None
    return int(value)


def assert_current_agents_payload_safe(payload: Any) -> None:
    def walk(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            forbidden = set(value) & FORBIDDEN_KEYS
            if forbidden:
                raise ValueError(f"current-agent payload contains forbidden keys: {sorted(forbidden)}")
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, str):
            _assert_safe_value(value, path)

    walk(payload)


def _assert_safe_value(value: str, path: str) -> None:
    lowered = value.lower()
    forbidden_markers = [
        "graph.microsoft.com",
        "serviceurl",
        "service_url",
        "raw_payload",
        "bearer ",
        "private key",
        "stdout",
        "stderr",
        "prompt:",
        "model response",
        "container_id",
        "support-mode",
    ]
    if any(marker in lowered for marker in forbidden_markers):
        raise ValueError(f"unsafe current-agent value at {path}")
    if re.search(r"[A-Za-z]:\\", value):
        raise ValueError(f"unsafe path value at {path}")
    if value.startswith("/mesh/") or value.startswith("/tmp/"):
        raise ValueError(f"unsafe absolute path value at {path}")
