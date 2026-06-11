from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.request import Request
from urllib.request import urlopen

from agentic_mesh import telemetry
from agentic_mesh.approval_requests import ApprovalStatusService
from agentic_mesh.auth import AuthResolver
from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.capabilities import CapabilityReadinessStore
from agentic_mesh.capabilities import validate_and_store_all
from agentic_mesh.config import load_mesh_config
from agentic_mesh.connectors import BotFrameworkTeamsConnectorAdapter
from agentic_mesh.connectors import FileSecretResolver
from agentic_mesh.connectors import GraphTeamsChannelIngressAdapter
from agentic_mesh.connectors import GraphTeamsConnectorAdapter
from agentic_mesh.connectors import LocalTeamsConnectorAdapter
from agentic_mesh.connectors import load_graph_token
from agentic_mesh.controller_auth import ControllerAuthService
from agentic_mesh.controller_auth import serve_controller_auth
from agentic_mesh.document_library import build_document_manifest
from agentic_mesh.document_library import migration_dry_run
from agentic_mesh.document_library import document_library_context
from agentic_mesh.document_library import render_migration_dry_run
from agentic_mesh.document_library import render_validation_findings
from agentic_mesh.document_library import render_flow_markdown_export
from agentic_mesh.document_library import render_flow_mermaid
from agentic_mesh.document_library import resolve_document_library_root
from agentic_mesh.document_library import validate_document_library
from agentic_mesh.document_library import write_document_manifest
from agentic_mesh.external_actions import ACTION_CAPTURE_WORK
from agentic_mesh.external_actions import ACTION_MARK_READY
from agentic_mesh.external_actions import ACTION_PROMOTE
from agentic_mesh.external_actions import ACTION_PURGE_RAW
from agentic_mesh.external_actions import ACTION_READ_RAW_REFERENCE
from agentic_mesh.external_actions import ACTION_SHOW_STATUS
from agentic_mesh.external_actions import ACTION_TRANSITION
from agentic_mesh.external_actions import OUTCOME_ACTION_DENIED
from agentic_mesh.external_actions import OUTCOME_ACTION_FAILED
from agentic_mesh.external_actions import OUTCOME_NOT_CAPTURED
from agentic_mesh.external_actions import ExternalActionTarget
from agentic_mesh.external_actions import ExternalActor
from agentic_mesh.external_actions import ExternalActionPolicy
from agentic_mesh.external_actions import ExternalActionRequest
from agentic_mesh.external_actions import ExternalActionService
from agentic_mesh.external_actions import FileExternalActionStore
from agentic_mesh.external_actions import QueueReceiptNotificationService
from agentic_mesh.external_actions import receipt_output
from agentic_mesh.journal import EventJournal
from agentic_mesh.lifecycle import LifecycleStore
from agentic_mesh.messaging import build_problem_status_connector_message
from agentic_mesh.messaging import build_human_response_received_message
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import Message
from agentic_mesh.models import new_id
from agentic_mesh.models import utc_now_iso
from agentic_mesh.notifications import FileNotificationAttemptStore
from agentic_mesh.notifications import FileSourceRouteStore
from agentic_mesh.provider_conditions import CodexProviderConditionProfile
from agentic_mesh.prompt_audit import write_startup_prompt_audit
from agentic_mesh.recovery_alerts import FileRecoveryAlertStore
from agentic_mesh.recovery_alerts import RecoveryAlertState
from agentic_mesh.recovery_observability import build_recovery_observability_view
from agentic_mesh.recovery_poller import RecoveryPoller
from agentic_mesh.recovery_poller import RecoveryPollerPolicy
from agentic_mesh.problem_status import ProblemStatusStore
from agentic_mesh.problem_status import role_problem_status
from agentic_mesh.problem_status import worker_problem_status
from agentic_mesh.route_status import CurrentRouteStore
from agentic_mesh.runtime import AgentRuntime
from agentic_mesh.safe_outputs import append_safe_output_record
from agentic_mesh.safe_outputs import safe_output_context_from_env
from agentic_mesh.safe_outputs import safe_output_file_from_env
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.teams_ingress import ReloadableTeamsBotIngress
from agentic_mesh.teams_ingress import serve_teams_bot_ingress
from agentic_mesh.url_roots import configured_url_root
from agentic_mesh.work_item_indexes import backfill_work_item_indexes
from agentic_mesh.work_item_indexes import validation_errors
from agentic_mesh.work_item_recovery import DuplicateActiveWorkGuard
from agentic_mesh.work_item_recovery import FileRecoveryStatusStore
from agentic_mesh.work_item_recovery import RECOVERY_READ_ACTION
from agentic_mesh.work_item_recovery import RecoveryActionRequest
from agentic_mesh.work_item_recovery import RecoveryActionService
from agentic_mesh.work_item_recovery import RecoveryClassifier
from agentic_mesh.work_queue import FileWorkQueueStore
from agentic_mesh.work_queue import SourceAnchor
from agentic_mesh.workers import ConfiguredWorkerAdapter
from agentic_mesh.worker_runs import FileWorkerRunStore
from agentic_mesh.worker_runs import WorkerRun
from agentic_mesh.worker_runs import WorkerRunReadError


DEFAULT_CONTROL_PLANE_URL = "http://10.0.0.65:8100"


def build_runtime(
    config_root: Path,
    project_file: str,
    workspace_root: Path,
    state_root: Path,
):
    mesh_config = load_mesh_config(config_root, project_file=project_file)
    effective_workspace_root = project_workspace_root(workspace_root, mesh_config)
    journal = EventJournal(state_root, mesh_config.project.project_id)
    message_store = FileMessageStore(state_root, mesh_config.project.project_id, journal)
    connector_outbox = FileConnectorOutbox(
        state_root,
        mesh_config.project.project_id,
        journal,
    )
    artifact_store = ArtifactStore(
        effective_workspace_root,
        mesh_config.project.project_id,
        journal,
        document_library_root=project_document_library_root(
            effective_workspace_root,
            mesh_config,
        ),
    )
    lifecycle = LifecycleStore(state_root, mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store=message_store,
        artifact_store=artifact_store,
        journal=journal,
        project=mesh_config.project,
        worker=ConfiguredWorkerAdapter(
            mesh_config=mesh_config,
            project=mesh_config.project,
            auth_methods=mesh_config.auth_methods,
            workspace_root=effective_workspace_root,
            state_root=state_root,
        ),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )
    return mesh_config, journal, message_store, connector_outbox, lifecycle, runtime


def project_workspace_root(base_workspace_root: Path, mesh_config) -> Path:
    configured_root = Path(mesh_config.project.workspace.root)
    if configured_root.is_absolute():
        return configured_root
    return (base_workspace_root / configured_root).resolve()


def project_document_library_root(effective_workspace_root: Path, mesh_config) -> Path:
    return resolve_document_library_root(
        effective_workspace_root,
        mesh_config.project.document_library,
    )


def configure_component_telemetry(mesh_config, component: str) -> None:
    telemetry.configure_process_telemetry(
        mesh_config,
        service_name=telemetry.service_name_for_component(mesh_config, component),
        component=component,
    )


def configure_instance_telemetry(mesh_config, instance_id: str) -> None:
    instance = mesh_config.instances[instance_id]
    telemetry.configure_process_telemetry(
        mesh_config,
        service_name=telemetry.service_name_for_instance(instance),
        role_instance=instance,
    )


def cmd_validate(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    print(
        json.dumps(
            {
                "organization_id": mesh_config.organization.organization_id,
                "global_language": mesh_config.organization.global_language,
                "global_locale": mesh_config.organization.global_locale,
                "auth_methods": sorted(mesh_config.auth_methods),
                "response_types": sorted(mesh_config.response_types),
                "project_id": mesh_config.project.project_id,
                "workspace": {
                    "root": mesh_config.project.workspace.root,
                    "default_repository": mesh_config.project.workspace.default_repository,
                    "repositories": {
                        repository_id: asdict(repository)
                        for repository_id, repository in sorted(
                            mesh_config.project.workspace.repositories.items()
                        )
                    },
                },
                "document_library": {
                    **document_library_context(
                        project_workspace_root(args.workspace_root, mesh_config),
                        mesh_config.project,
                    ),
                },
                "meshes": sorted(mesh_config.project.meshes),
                "connectors": {
                    connector_id: {
                        "adapter": connector.adapter,
                        "identity_model": connector.identity_model,
                        "team_id": connector.team_id,
                        "team_name": connector.team_name,
                        "ingress": (
                            asdict(connector.ingress)
                            if connector.ingress is not None
                            else None
                        ),
                        "channels": sorted(connector.channels),
                        "role_bots": sorted(connector.role_bots),
                    }
                    for connector_id, connector in sorted(
                        mesh_config.project.connectors.items()
                    )
                },
                "roles": sorted(mesh_config.project.roles),
                "auth_credentials": sorted(mesh_config.project.auth_credentials),
                "instances": sorted(mesh_config.instances),
                "role_auth": {
                    role_id: (
                        {
                            "credential_ref": role.worker.auth.credential_ref,
                            "method": role.worker.auth.method,
                        }
                        if role.worker.auth
                        else None
                    )
                    for role_id, role in sorted(mesh_config.project.roles.items())
                },
                "document_accountabilities": sorted(
                    mesh_config.project.document_accountabilities
                ),
                "flow_id": mesh_config.project.flow.flow_id,
                "entry_state": mesh_config.project.flow.entry_state,
                "flow_states": sorted(mesh_config.project.flow.states),
            },
            indent=2,
        )
    )
    return 0


def cmd_enqueue(args) -> int:
    mesh_config, _, message_store, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "cli")
    lifecycle_state = args.lifecycle_state or mesh_config.project.flow.entry_state
    flow_state = mesh_config.project.flow.states[lifecycle_state]
    role_id = args.role or flow_state.owner_role
    work_item_id = args.work_item_id or new_id("work")
    message = Message.create(
        role_id=role_id,
        message_type=args.type,
        payload={
            "title": args.title,
            "summary": args.summary,
            "work_item_id": work_item_id,
            "work_item_type": args.work_item_type,
            "lifecycle_state": lifecycle_state,
        },
        source=args.source,
    )
    message_store.enqueue(message)
    print(json.dumps({"message_id": message.message_id, "correlation_id": message.correlation_id}))
    return 0


def cmd_run_agent(args) -> int:
    mesh_config, _, message_store, _, lifecycle, runtime = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_instance_telemetry(mesh_config, args.instance)
    instance = mesh_config.instances[args.instance]
    lifecycle.set_state(instance, "active", reason="run_once")
    startup_reclaimed = []
    reclaim_claimed_before = getattr(args, "reclaim_claimed_before", None)
    if reclaim_claimed_before:
        startup_reclaimed = message_store.reclaim_claims_before(
            instance.role_id,
            args.instance,
            reclaim_claimed_before,
        )
    reclaimed = message_store.reclaim_stale_claims(
        instance.role_id,
        args.instance,
        getattr(args, "claim_lease_seconds", None)
        or int(os.environ.get("AGENTIC_MESH_CLAIM_LEASE_SECONDS", "21600")),
    )
    did_work = runtime.run_once(args.instance, instance)
    lifecycle.set_state(instance, "idle", reason="run_once_complete")
    print(
        json.dumps(
            {
                "instance": args.instance,
                "did_work": did_work,
                "reclaimed": len(startup_reclaimed) + len(reclaimed),
            }
        )
    )
    return 0


def cmd_control_plane_tick(args) -> int:
    mesh_config, journal, message_store, connector_outbox, lifecycle, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "control-plane")
    transitions = lifecycle.control_plane_tick(
        mesh_config,
        message_store,
        idle_grace_seconds=args.idle_grace_seconds,
    )
    recovery_reconciliation = reconcile_missing_recovery_records(
        mesh_config=mesh_config,
        state_root=args.state_root,
        journal=journal,
        connector_outbox=connector_outbox,
        document_library_root=project_document_library_root(
            project_workspace_root(args.workspace_root, mesh_config),
            mesh_config,
        ),
    )
    work_queue = FileWorkQueueStore(args.state_root, mesh_config.project.project_id, journal)
    queue_closed = work_queue.reconcile_promoted_closures(
        message_store=message_store,
        actor_role="control-plane",
    )
    terminal_runs = reconcile_terminal_worker_runs(
        state_root=args.state_root,
        project_id=mesh_config.project.project_id,
        journal=journal,
    )
    print(
        json.dumps(
            {
                "transitions": transitions,
                "recovery_reconciliation": recovery_reconciliation,
                "work_queue_reconciliation": {
                    "closed_count": len(queue_closed),
                    "closed_queue_item_ids": [
                        item.queue_item_id for item in queue_closed
                    ],
                },
                "worker_run_reconciliation": terminal_runs,
            },
            indent=2,
        )
    )
    return 0


STOPPED_WORK_STATUSES = {"blocked", "failed", "needs_runtime_recovery"}
RECONCILIATION_SKIP_RECOVERY_STATES = {
    "duplicate_active_work",
    "not_recoverable",
    "recovery_queued",
    "recovery_running",
    "recovery_succeeded",
    "superseded",
}


def reconcile_terminal_worker_runs(
    *,
    state_root: Path,
    project_id: str,
    journal: EventJournal,
) -> dict[str, Any]:
    store = FileWorkerRunStore(state_root, project_id)
    cleared: list[dict[str, Any]] = []
    for run in store.list_current():
        if not isinstance(run, WorkerRun):
            continue
        if run.run_status not in {"completed", "failed", "timed_out", "canceled"}:
            continue
        if not store.clear_current(run.role_instance_id, expected_run_id=run.run_id):
            continue
        cleared.append(
            {
                "role_instance_id": run.role_instance_id,
                "run_id": run.run_id,
                "work_item_id": run.work_item_id,
                "run_status": run.run_status,
            }
        )
        journal.append(
            "worker_run_current_cleared",
            project_id=project_id,
            role_id=run.role_id,
            role_instance_id=run.role_instance_id,
            run_id=run.run_id,
            message_id=run.message_id,
            work_item_id=run.work_item_id,
            lifecycle_state=run.lifecycle_state,
            run_status=run.run_status,
            reason="terminal_run_not_current_work",
        )
    return {
        "cleared_count": len(cleared),
        "cleared": cleared,
    }


def reconcile_missing_recovery_records(
    *,
    mesh_config,
    state_root: Path,
    journal: EventJournal,
    connector_outbox: FileConnectorOutbox | None = None,
    document_library_root: Path | None = None,
) -> dict[str, Any]:
    """Backfill durable problem/recovery records for stopped work.

    Older or partially deployed runtimes can leave work stopped only as a
    `work_completed` journal event. That strands the item because the recovery
    service has no durable source record to act on. Reconciliation is
    intentionally conservative: it writes missing problem/recovery state, but it
    never retries or enqueues work by itself.
    """

    project_id = mesh_config.project.project_id
    problem_store = ProblemStatusStore(state_root, project_id)
    recovery_store = FileRecoveryStatusStore(state_root, project_id)
    classifier = RecoveryClassifier()
    events = journal.read_all()
    latest_completed: dict[str, dict[str, Any]] = {}
    failures_by_message: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") == "agent_run_failed" and event.get("message_id"):
            failures_by_message[str(event["message_id"])] = event
            continue
        if event.get("event_type") != "work_completed":
            continue
        work_item_id = event.get("work_item_id")
        if not isinstance(work_item_id, str) or not work_item_id:
            continue
        latest_completed[work_item_id] = event

    active_work_items = _active_work_item_ids(state_root, project_id)
    messages_by_id = _messages_by_id(state_root, project_id)
    created_problem_count = 0
    created_recovery_count = 0
    updated_problem_count = 0
    updated_recovery_count = 0
    queued_notification_count = 0
    skipped_active_count = 0
    skipped_terminal_count = 0
    errors: list[dict[str, str]] = []

    for work_item_id, event in sorted(latest_completed.items()):
        status = str(event.get("status") or "")
        if status not in STOPPED_WORK_STATUSES:
            skipped_terminal_count += 1
            continue
        if work_item_id in active_work_items:
            skipped_active_count += 1
            continue
        current_recovery = recovery_store.get_current(work_item_id)
        if (
            current_recovery is not None
            and current_recovery.recovery_state in RECONCILIATION_SKIP_RECOVERY_STATES
        ):
            skipped_terminal_count += 1
            continue

        message_id = str(event.get("message_id") or "")
        message_data = messages_by_id.get(message_id, {})
        message_payload = message_data.get("payload") or {}
        evidence = _historical_blocker_evidence(document_library_root, work_item_id)
        problem = problem_store.read_current(work_item_id)
        problem_changed = False
        if problem is None:
            try:
                problem_status = _problem_status_from_stopped_event(
                    project_id=project_id,
                    event=event,
                    message_payload=message_payload,
                    failure_event=failures_by_message.get(message_id),
                    evidence=evidence,
                )
                problem_store.write_current(problem_status)
                journal.append(
                    "problem_status_recorded",
                    project_id=project_id,
                    source="control_plane_reconciliation",
                    **problem_status.journal_fields(),
                )
                problem = problem_status.to_dict()
                created_problem_count += 1
                problem_changed = True
            except Exception as exc:
                errors.append(
                    {
                        "work_item_id": work_item_id,
                        "stage": "problem_status",
                        "error_class": exc.__class__.__name__,
                    }
                )
                continue
        else:
            try:
                problem_status = _problem_status_from_stopped_event(
                    project_id=project_id,
                    event=event,
                    message_payload=message_payload,
                    failure_event=failures_by_message.get(message_id),
                    evidence=evidence,
                )
                if _problem_status_should_replace(problem, problem_status.to_dict()):
                    problem_store.write_current(problem_status)
                    journal.append(
                        "problem_status_recorded",
                        project_id=project_id,
                        source="control_plane_reclassification",
                        **problem_status.journal_fields(),
                    )
                    problem = problem_status.to_dict()
                    updated_problem_count += 1
                    problem_changed = True
            except Exception as exc:
                errors.append(
                    {
                        "work_item_id": work_item_id,
                        "stage": "problem_status_reclassification",
                        "error_class": exc.__class__.__name__,
                    }
                )
                continue

        try:
            recovery = classifier.classify_problem(
                project_id=project_id,
                problem_status=problem,
                current=current_recovery,
            )
            if current_recovery is None or _recovery_status_should_replace(
                current_recovery.to_dict(),
                recovery.to_dict(),
            ):
                recovery_store.write_current(recovery)
                recovery_fields = recovery.journal_fields()
                recovery_fields.pop("project_id", None)
                journal.append(
                    "recovery_status_recorded",
                    project_id=project_id,
                    source=(
                        "control_plane_reclassification"
                        if current_recovery is not None
                        else "control_plane_reconciliation"
                    ),
                    **recovery_fields,
                )
                if current_recovery is None:
                    created_recovery_count += 1
                else:
                    updated_recovery_count += 1
        except Exception as exc:
            errors.append(
                {
                    "work_item_id": work_item_id,
                    "stage": "recovery_status",
                    "error_class": exc.__class__.__name__,
                }
            )
            continue

        if (
            problem_changed
            and connector_outbox is not None
            and _should_queue_reconciled_problem_notification(problem)
        ):
            try:
                if _queue_reconciled_problem_notification(
                    mesh_config=mesh_config,
                    connector_outbox=connector_outbox,
                    problem_status=problem,
                    source_message_data=message_data,
                    correlation_id=str(event.get("correlation_id") or new_id("corr")),
                ):
                    journal.append(
                        "problem_status_notification_queued",
                        project_id=project_id,
                        source="control_plane_reconciliation",
                        work_item_id=work_item_id,
                        work_item_type=problem.get("work_item_type"),
                        queue_item_id=problem.get("queue_item_id"),
                        role_id=problem.get("affected_role"),
                        lifecycle_state=problem.get("lifecycle_state"),
                        status=problem.get("status"),
                        problem_kind=problem.get("problem_kind"),
                        failure_class=problem.get("failure_class"),
                        correlation_id=str(event.get("correlation_id") or ""),
                        notification_result="queued",
                    )
                    queued_notification_count += 1
            except Exception as exc:
                errors.append(
                    {
                        "work_item_id": work_item_id,
                        "stage": "problem_status_notification",
                        "error_class": exc.__class__.__name__,
                    }
                )

    return {
        "created_problem_count": created_problem_count,
        "created_recovery_count": created_recovery_count,
        "updated_problem_count": updated_problem_count,
        "updated_recovery_count": updated_recovery_count,
        "queued_notification_count": queued_notification_count,
        "skipped_active_count": skipped_active_count,
        "skipped_terminal_count": skipped_terminal_count,
        "error_count": len(errors),
        "errors": errors[:20],
    }


def _problem_status_from_stopped_event(
    *,
    project_id: str,
    event: dict[str, Any],
    message_payload: dict[str, Any],
    failure_event: dict[str, Any] | None,
    evidence: dict[str, Any] | None = None,
):
    work_item_id = str(event.get("work_item_id") or "")
    status = str(event.get("status") or "")
    role_id = str(event.get("role_id") or message_payload.get("role_id") or "unknown")
    lifecycle_state = str(
        event.get("lifecycle_state")
        or message_payload.get("lifecycle_state")
        or "unknown"
    )
    message_id = str(event.get("message_id") or "")
    correlation_id = str(event.get("correlation_id") or new_id("corr"))
    evidence = evidence or {}
    evidence_failure_class = evidence.get("failure_class")
    if (
        status == "needs_runtime_recovery"
        or failure_event is not None
        or evidence_failure_class
    ):
        failure_class = str(evidence_failure_class or _failure_class_from_event(failure_event))
        return worker_problem_status(
            failure_class=failure_class,
            reason=str(
                evidence.get("reason")
                or _historical_runtime_problem_reason(failure_event)
            ),
            recovery_action=(
                "repair_configuration"
                if failure_class in {"malformed_handoff", "schema_failed"}
                else "operator_review"
            ),
            retryable=True,
            role_id=role_id,
            role_instance_id=event.get("role_instance_id"),
            message_payload={
                **message_payload,
                "work_item_id": work_item_id,
                "work_item_type": event.get("work_item_type")
                or message_payload.get("work_item_type"),
                "queue_item_id": message_payload.get("queue_item_id"),
                "source_anchor": message_payload.get("source_anchor"),
            },
            source_message_id=message_id,
            correlation_id=correlation_id,
            lifecycle_state=lifecycle_state,
            worker_adapter=None,
            worker_model=None,
            artifact_paths=list(evidence.get("artifact_paths") or []),
            status_url=f"/work-items/{work_item_id}",
        )
    return role_problem_status(
        status=status,
        message=(
            "Historical stopped work was found without a durable problem "
            f"record. Latest recorded status was `{status}`. Review this item, "
            "then either provide the requested role input, supersede it, or "
            "retry through work-item recovery if the blocker was caused by a "
            "repaired runtime/provider issue."
        ),
        project_id=project_id,
        role_id=role_id,
        role_instance_id=event.get("role_instance_id"),
        work_item_id=work_item_id,
        work_item_type=event.get("work_item_type") or message_payload.get("work_item_type"),
        lifecycle_state=lifecycle_state,
        queue_item_id=message_payload.get("queue_item_id"),
        source_message_id=message_id,
        source_anchor=message_payload.get("source_anchor"),
        correlation_id=correlation_id,
        status_url=f"/work-items/{work_item_id}",
    )


def _failure_class_from_event(event: dict[str, Any] | None) -> str:
    if not event:
        return "unknown_provider_failure"
    text = " ".join(
        str(event.get(key) or "")
        for key in ["error_type", "error_message"]
    ).lower()
    if "out-of-flow handoff" in text or "handoff" in text:
        return "malformed_handoff"
    if "timeout" in text:
        return "timeout"
    if "schema" in text:
        return "schema_failed"
    return "unknown_provider_failure"


def _historical_runtime_problem_reason(event: dict[str, Any] | None) -> str:
    if not event:
        return (
            "Historical runtime recovery state was found without a durable "
            "problem record. Operator review is required before retry."
        )
    message = str(event.get("error_message") or event.get("error_type") or "")
    if not message:
        return "Historical runtime failure had no safe error message."
    return f"Historical runtime failure: {message[:500]}"


def _historical_blocker_evidence(
    document_library_root: Path | None,
    work_item_id: str,
) -> dict[str, Any]:
    if document_library_root is None:
        return {}
    item_root = document_library_root / "work-items" / work_item_id
    if not item_root.exists():
        return {}
    docs = sorted(item_root.glob("*.md"))
    texts: list[tuple[str, str]] = []
    for path in docs:
        try:
            texts.append((path.name, path.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            continue
    if not texts:
        return {}
    combined = "\n".join(text for _, text in texts)
    lower = combined.lower()
    failure_class: str | None = None
    if "usage limit" in lower or "purchase more credits" in lower:
        failure_class = "usage_limit"
    elif "codex cli timed out" in lower or "timed out after" in lower:
        failure_class = "timeout"
    elif "bwrap: no permissions to create a new namespace" in lower:
        failure_class = "process_failed"
    elif "out-of-flow handoff" in lower or "out_of_flow_reason" in lower:
        failure_class = "malformed_handoff"

    blocked_section = _first_blocked_section(texts)
    reason = blocked_section or _first_failure_line(combined)
    if not failure_class and not reason:
        return {}
    artifact_paths = [
        f"work-items/{work_item_id}/{name}"
        for name, text in texts
        if "## Blocked:" in text or "ERROR:" in text or "timed out" in text.lower()
    ][:5]
    return {
        "failure_class": failure_class,
        "reason": reason
        or "Historical artifact indicates stopped work, but no concise reason was found.",
        "artifact_paths": artifact_paths,
    }


def _first_blocked_section(texts: list[tuple[str, str]]) -> str | None:
    for _, text in texts:
        marker = text.find("## Blocked:")
        if marker < 0:
            continue
        section = text[marker:]
        next_heading = section.find("\n## ", 1)
        if next_heading >= 0:
            section = section[:next_heading]
        return _compact_text(section, limit=700)
    return None


def _first_failure_line(text: str) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if (
            line.startswith("ERROR:")
            or "usage limit" in lowered
            or "timed out" in lowered
            or "bwrap:" in lowered
        ):
            return _compact_text(line, limit=700)
    return None


def _compact_text(value: str, *, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _problem_status_should_replace(
    current: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    current_reason = str(current.get("reason") or "")
    candidate_reason = str(candidate.get("reason") or "")
    if (
        current_reason.startswith("Historical stopped work was found without")
        and not candidate_reason.startswith("Historical stopped work was found without")
    ):
        return True
    for key in [
        "status",
        "problem_kind",
        "failure_class",
        "reason",
        "next_action",
        "action_owner",
    ]:
        if current.get(key) != candidate.get(key):
            return True
    return False


def _should_queue_reconciled_problem_notification(problem: dict[str, Any]) -> bool:
    """Reconciliation backfill updates durable state; chat is for live events."""
    return False


def _recovery_status_should_replace(
    current: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    for key in [
        "recovery_reason_class",
        "recoverability_class",
        "recovery_state",
        "next_action",
        "action_owner",
    ]:
        if current.get(key) != candidate.get(key):
            return True
    return False


def _queue_reconciled_problem_notification(
    *,
    mesh_config,
    connector_outbox: FileConnectorOutbox,
    problem_status: dict[str, Any],
    source_message_data: dict[str, Any],
    correlation_id: str,
) -> bool:
    affected_role = str(problem_status.get("affected_role") or "")
    source_instance = _source_instance_for_problem(mesh_config, affected_role)
    if source_instance is None:
        return False
    source_message = _source_message_for_problem(
        problem_status=problem_status,
        source_message_data=source_message_data,
        affected_role=affected_role,
        correlation_id=correlation_id,
    )
    source_anchor = source_message.payload.get("source_anchor") or {}
    source_scope = (
        source_anchor.get("source_scope") if isinstance(source_anchor, dict) else None
    )
    internal_scopes = {"api", "cli", "codex", "controller", "local", "system"}
    source_scope_text = _single_line(source_scope).lower()
    if source_scope_text and source_scope_text not in internal_scopes:
        channel = str(source_scope)
        fallback = False
    else:
        channel = "all-agents"
        fallback = True
    connector_outbox.enqueue(
        build_problem_status_connector_message(
            channel=channel,
            source_instance=source_instance,
            source_message=source_message,
            problem_status=problem_status,
            fallback=fallback,
        )
    )
    return True


def _source_instance_for_problem(mesh_config, affected_role: str):
    for instance in mesh_config.instances.values():
        if instance.role_id == affected_role:
            return instance
    return next(iter(mesh_config.instances.values()), None)


def _source_message_for_problem(
    *,
    problem_status: dict[str, Any],
    source_message_data: dict[str, Any],
    affected_role: str,
    correlation_id: str,
) -> Message:
    payload = dict(source_message_data.get("payload") or {})
    payload.setdefault("title", _single_line(problem_status.get("work_item_id")))
    payload.setdefault("summary", problem_status.get("reason_summary") or problem_status.get("reason"))
    payload.setdefault("work_item_id", problem_status.get("work_item_id"))
    payload.setdefault("work_item_type", problem_status.get("work_item_type"))
    payload.setdefault("queue_item_id", problem_status.get("queue_item_id"))
    return Message(
        message_id=str(
            source_message_data.get("message_id")
            or problem_status.get("source_message_id")
            or new_id("msg")
        ),
        role_id=affected_role,
        type=str(source_message_data.get("type") or "problem_status.reconciled"),
        payload=payload,
        correlation_id=str(source_message_data.get("correlation_id") or correlation_id),
        created_at=str(source_message_data.get("created_at") or utc_now_iso()),
        source=str(source_message_data.get("source") or "control_plane_reconciliation"),
        claimed_by=source_message_data.get("claimed_by"),
        claimed_at=source_message_data.get("claimed_at"),
        trace_context=dict(source_message_data.get("trace_context") or {}),
    )


def _single_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _active_work_item_ids(state_root: Path, project_id: str) -> set[str]:
    root = state_root / "projects" / project_id / "queues"
    active: set[str] = set()
    if not root.exists():
        return active
    for path in list(root.glob("*/pending/*.json")) + list(root.glob("*/claimed/*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        work_item_id = (data.get("payload") or {}).get("work_item_id")
        if isinstance(work_item_id, str) and work_item_id:
            active.add(work_item_id)
    return active


def _messages_by_id(state_root: Path, project_id: str) -> dict[str, dict[str, Any]]:
    root = state_root / "projects" / project_id / "queues"
    messages: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return messages
    for path in root.glob("*/*/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        message_id = data.get("message_id")
        if isinstance(message_id, str) and message_id:
            messages[message_id] = data
    for path in root.glob("*/claimed/*/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        message_id = data.get("message_id")
        if isinstance(message_id, str) and message_id:
            messages[message_id] = data
    return messages


def cmd_status(args) -> int:
    mesh_config, journal, message_store, connector_outbox, lifecycle, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    lifecycle.ensure_instances(mesh_config)
    configure_component_telemetry(mesh_config, "cli")
    for role_id in sorted(mesh_config.project.roles):
        telemetry.set_gauge(
            "agentic_mesh.role_queue.pending",
            message_store.pending_count(role_id),
            {"project_id": mesh_config.project.project_id, "role_id": role_id},
        )
    for channel in ["approvals"]:
        telemetry.set_gauge(
            "agentic_mesh.connector_outbox.pending",
            connector_outbox.pending_count(channel),
            {"project_id": mesh_config.project.project_id, "channel": channel},
        )
    work_queue = FileWorkQueueStore(args.state_root, mesh_config.project.project_id, journal)
    work_queue_counts = work_queue.status_counts()
    for owner_role, statuses in work_queue_counts.items():
        for queue_status, count in statuses.items():
            telemetry.set_gauge(
                "agentic_mesh.work_queue.depth",
                count,
                {
                    "project_id": mesh_config.project.project_id,
                    "owner_role": owner_role,
                    "queue_status": queue_status,
                },
            )
    status = {
        "organization_id": mesh_config.organization.organization_id,
        "global_language": mesh_config.organization.global_language,
        "project_id": mesh_config.project.project_id,
        "workspace_root": str(project_workspace_root(args.workspace_root, mesh_config)),
        "document_library_root": str(
            project_document_library_root(
                project_workspace_root(args.workspace_root, mesh_config),
                mesh_config,
            )
        ),
        "default_repository": mesh_config.project.workspace.default_repository,
        "instances": {
            instance_id: lifecycle.get_state(instance_id)
            for instance_id in sorted(mesh_config.instances)
        },
        "pending": {
            role_id: message_store.pending_count(role_id)
            for role_id in sorted(mesh_config.project.roles)
        },
        "connector_pending": {
            channel: connector_outbox.pending_count(channel)
            for channel in ["approvals"]
        },
        "work_queue": work_queue_counts,
        "problem_statuses": _current_problem_statuses(
            args.state_root,
            mesh_config.project.project_id,
        ),
        "current_routes": _current_routes(
            args.state_root,
            mesh_config.project.project_id,
        ),
        "worker_runs": _worker_runs(
            args.state_root,
            mesh_config.project.project_id,
        ),
        "approval_requests": _approval_requests(
            args.state_root,
            mesh_config.project.project_id,
        ),
        "capability_readiness": _capability_readiness(
            args.state_root,
            mesh_config,
        ),
        "journal_path": str(journal.path),
    }
    print(json.dumps(status, indent=2))
    return 0


def cmd_capability_readiness(args) -> int:
    mesh_config = load_mesh_config(args.config_root, args.project_file)
    report = validate_and_store_all(
        mesh_config=mesh_config,
        workspace_root=project_workspace_root(args.workspace_root, mesh_config),
        state_root=args.state_root,
    )
    print(json.dumps(report, indent=2))
    return 0


def _capability_readiness(
    state_root: Path,
    mesh_config,
) -> dict[str, Any]:
    store = CapabilityReadinessStore(
        state_root,
        mesh_config.project.project_id,
        create_dirs=False,
    )
    summaries = []
    for instance in sorted(mesh_config.instances.values(), key=lambda item: item.instance_id):
        current = store.read_current(instance.instance_id)
        if current is None:
            continue
        summary = current.get("summary")
        if isinstance(summary, dict):
            summaries.append(summary)
    return {
        "schema_version": "capability-readiness-status-v0",
        "roles": summaries,
        "redaction_applied": True,
    }


def _current_problem_statuses(state_root: Path, project_id: str) -> list[dict[str, Any]]:
    store = ProblemStatusStore(state_root, project_id)
    if not store.root.exists():
        return []
    problems: list[dict[str, Any]] = []
    for path in sorted(store.root.glob("*/problem-status.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        problems.append(
            {
                "work_item_id": payload.get("work_item_id"),
                "status": payload.get("status"),
                "status_label": payload.get("status_label"),
                "problem_kind": payload.get("problem_kind"),
                "failure_class": payload.get("failure_class"),
                "affected_role": payload.get("affected_role"),
                "lifecycle_state": payload.get("lifecycle_state"),
                "reason_summary": payload.get("reason_summary"),
                "next_action": payload.get("next_action"),
                "action_owner": payload.get("action_owner"),
                "retryable": payload.get("retryable"),
                "updated_at": payload.get("occurred_at"),
            }
        )
    return problems


def _current_routes(state_root: Path, project_id: str) -> list[dict[str, Any]]:
    store = CurrentRouteStore(state_root, project_id)
    if not store.root.exists():
        return []
    routes: list[dict[str, Any]] = []
    for path in sorted(store.root.glob("*/current-route.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        routes.append(
            {
                "work_item_id": payload.get("work_item_id"),
                "route_id": payload.get("route_id"),
                "route_kind": payload.get("route_kind"),
                "route_status": payload.get("route_status"),
                "source_role": payload.get("source_role"),
                "target_role": payload.get("target_role"),
                "source_lifecycle_state": payload.get("source_lifecycle_state"),
                "target_lifecycle_state": payload.get("target_lifecycle_state"),
                "defect_id": payload.get("defect_id"),
                "required_change": payload.get("required_change"),
                "evidence_required": payload.get("evidence_required"),
                "updated_at": payload.get("requested_at"),
            }
        )
    return routes


def _worker_runs(state_root: Path, project_id: str) -> dict[str, Any]:
    store = FileWorkerRunStore(state_root, project_id)
    current = []
    for item in store.list_current():
        if isinstance(item, WorkerRunReadError):
            current.append(item.safe_summary())
        elif isinstance(item, WorkerRun):
            current.append(item.safe_summary())
    recent = []
    for item in store.list_recent(limit=25):
        if isinstance(item, WorkerRunReadError):
            recent.append(item.safe_summary())
        elif isinstance(item, WorkerRun):
            recent.append(item.safe_summary())
    return {
        "schema_version": "worker-run-list-v0",
        "current": current,
        "recent": recent,
        "read_only": True,
    }


def _approval_requests(state_root: Path, project_id: str) -> list[dict[str, Any]]:
    from agentic_mesh.human_gates import FileHumanGateRequestStore

    store = FileHumanGateRequestStore(state_root, project_id)
    rows = []
    for request in store.read_all():
        status = request.status
        if status == "waiting_for_response":
            status = "pending"
        if status == "request_pending_delivery":
            status = "request_failed"
        latest_attempt = (
            request.notification_attempts[-1] if request.notification_attempts else {}
        )
        rows.append(
            {
                "work_item_id": request.work_item_id,
                "lifecycle_state": request.lifecycle_state,
                "gate_id": request.gate_id,
                "approval_request_id": request.approval_request_id
                or request.response_request_id,
                "response_request_id": request.response_request_id,
                "approval_request_status": status,
                "approval_request_status_label": (
                    "Waiting for approval"
                    if status == "pending"
                    else status.replace("_", " ").title()
                ),
                "notification_attempt_id": latest_attempt.get(
                    "notification_attempt_id"
                ),
                "notification_attempt_status": latest_attempt.get("status"),
                "updated_at": request.updated_at,
            }
        )
    return rows


def cmd_document_manifest(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    workspace = project_workspace_root(args.workspace_root, mesh_config)
    configure_component_telemetry(mesh_config, "cli")
    if args.write:
        manifest_path = write_document_manifest(workspace, mesh_config.project)
        print(json.dumps({"manifest_path": str(manifest_path)}, indent=2))
    else:
        print(json.dumps(build_document_manifest(mesh_config.project), indent=2))
    return 0


def cmd_document_library_validate(args) -> int:
    mesh_config, _, _, _, _, runtime = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "document-library")
    workspace = project_workspace_root(args.workspace_root, mesh_config)
    root = project_document_library_root(workspace, mesh_config)
    findings = validate_document_library(root, work_item_id=args.work_item_id)
    output = render_validation_findings(findings, output_format=args.format)
    if args.write_report:
        if not args.work_item_id:
            print("--write-report requires --work-item-id", file=sys.stderr)
            return 2
        path = runtime.artifact_store.write_generated_artifact(
            relative_path=args.write_report,
            content=output,
            work_item_id=args.work_item_id,
            component_id="document-library-validation",
            lifecycle_state="implementation",
            correlation_id=args.correlation_id,
        )
        print(json.dumps({"report_path": str(path)}, indent=2))
    else:
        print(output, end="")
    return 1 if any(finding.severity == "error" for finding in findings) else 0


def cmd_document_library_migration_dry_run(args) -> int:
    mesh_config, _, _, _, _, runtime = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "document-library")
    workspace = project_workspace_root(args.workspace_root, mesh_config)
    root = project_document_library_root(workspace, mesh_config)
    report = migration_dry_run(
        root,
        work_item_id=args.work_item_id,
        migration_slice_id=args.migration_slice_id,
    )
    output = render_migration_dry_run(report, output_format=args.format)
    if args.write_report:
        path = runtime.artifact_store.write_generated_artifact(
            relative_path=args.write_report,
            content=output,
            work_item_id=args.work_item_id,
            component_id="document-library-migration",
            lifecycle_state="implementation",
            correlation_id=args.correlation_id,
        )
        print(json.dumps({"report_path": str(path)}, indent=2))
    else:
        print(output, end="")
    return 1 if report["findings"] else 0


def cmd_flow_mermaid(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    configure_component_telemetry(mesh_config, "cli")
    output = render_flow_mermaid(mesh_config.project.flow)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(json.dumps({"output": str(args.output)}, indent=2))
    else:
        print(output, end="")
    return 0


def cmd_flow_export(args) -> int:
    mesh_config, journal, _, _, _, runtime = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "lifecycle-export")
    output = str(args.output or "")
    attrs = telemetry.span_attributes(
        project_id=mesh_config.project.project_id,
        flow_id=mesh_config.project.flow.flow_id,
        format=args.format,
        output=output,
        work_item_id=args.work_item_id,
        queue_item_id=args.queue_item_id,
        component_id="lifecycle-export",
        result="started",
    )
    with telemetry.start_span(
        "flow_export.generate",
        correlation_id=args.correlation_id,
        attributes=attrs,
    ) as trace_context:
        if args.format != "markdown":
            attrs["result"] = "rejected"
            attrs["reason"] = "invalid_format"
            _journal_flow_export_rejected(
                journal,
                mesh_config.project.project_id,
                mesh_config.project.flow.flow_id,
                args,
                output=output,
                reason="invalid_format",
            )
            print(
                json.dumps({"error": "unsupported format", "format": args.format}),
                file=sys.stderr,
            )
            return 1
        command = _flow_export_command(args)
        try:
            content = render_flow_markdown_export(
                mesh_config.project.flow,
                project_id=mesh_config.project.project_id,
                project_file=_safe_relative_path(args.project_file, args.config_root),
                flow_source=_flow_source_reference(args.config_root, args.project_file),
                work_item_id=args.work_item_id,
                queue_item_id=args.queue_item_id,
                generated_at=_now_for_cli(),
                command=command,
            )
            runtime.artifact_store.write_generated_artifact(
                relative_path=output,
                content=content,
                work_item_id=args.work_item_id,
                queue_item_id=args.queue_item_id,
                work_item_type=args.work_item_type,
                lifecycle_state=args.lifecycle_state,
                component_id="lifecycle-export",
                correlation_id=args.correlation_id,
                trace_context=trace_context,
            )
            attrs["result"] = "success"
        except ValueError as exc:
            attrs["result"] = "rejected"
            attrs["reason"] = "invalid_output_path"
            _journal_flow_export_rejected(
                journal,
                mesh_config.project.project_id,
                mesh_config.project.flow.flow_id,
                args,
                output=output,
                reason="invalid_output_path",
            )
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1
        except Exception as exc:
            attrs["result"] = "failed"
            attrs["reason"] = exc.__class__.__name__
            journal.append(
                "flow_export_failed",
                project_id=mesh_config.project.project_id,
                flow_id=mesh_config.project.flow.flow_id,
                format=args.format,
                output=output,
                work_item_id=args.work_item_id,
                queue_item_id=args.queue_item_id,
                lifecycle_state=args.lifecycle_state,
                component_id="lifecycle-export",
                correlation_id=args.correlation_id,
                reason=exc.__class__.__name__,
            )
            print(
                json.dumps(
                    {
                        "error": "flow export failed",
                        "reason": exc.__class__.__name__,
                    }
                ),
                file=sys.stderr,
            )
            return 1
    print(
        json.dumps(
            {
                "format": args.format,
                "output": output,
                "project_id": mesh_config.project.project_id,
                "flow_id": mesh_config.project.flow.flow_id,
                "work_item_id": args.work_item_id,
                "queue_item_id": args.queue_item_id,
                "registered": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_work_item_index_validate(args) -> int:
    mesh_config, journal, _, _, _, runtime = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "work-item-index")
    document_root = project_document_library_root(
        project_workspace_root(args.workspace_root, mesh_config),
        mesh_config,
    )
    attrs = telemetry.span_attributes(
        project_id=mesh_config.project.project_id,
        component_id="work-item-index",
        operation="validate",
        work_item_id=args.work_item_id,
        result="started",
    )
    with telemetry.start_span(
        "work_item_index.validate",
        correlation_id=args.correlation_id,
        attributes=attrs,
    ):
        errors = validation_errors(document_root, work_item_id=args.work_item_id)
        if errors:
            attrs["result"] = "failed"
            for error in errors:
                journal.append(
                    "work_item_index_validation_failed",
                    project_id=mesh_config.project.project_id,
                    component_id="work-item-index",
                    work_item_id=error.get("work_item_id") or args.work_item_id,
                    path=error.get("path"),
                    reason=error.get("reason"),
                    correlation_id=args.correlation_id,
                    operation="validate",
                )
        else:
            attrs["result"] = "success"
    print(
        json.dumps(
            {
                "project_id": mesh_config.project.project_id,
                "work_item_id": args.work_item_id,
                "valid": not errors,
                "validation_errors": len(errors),
                "errors": errors,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if errors else 0


def cmd_work_item_index_backfill(args) -> int:
    mesh_config, journal, _, _, _, runtime = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "work-item-index")
    document_root = project_document_library_root(
        project_workspace_root(args.workspace_root, mesh_config),
        mesh_config,
    )
    execute = bool(args.execute)
    attrs = telemetry.span_attributes(
        project_id=mesh_config.project.project_id,
        component_id="work-item-index",
        operation="backfill",
        work_item_id=args.work_item_id,
        dry_run=not execute,
        result="started",
    )
    with telemetry.start_span(
        "work_item_index.backfill",
        correlation_id=args.correlation_id,
        attributes=attrs,
    ):
        try:
            report = backfill_work_item_indexes(
                document_root,
                execute=execute,
                artifact_writer=runtime.artifact_store.write_maintained_index,
                work_item_id=args.work_item_id,
                project_id=mesh_config.project.project_id,
                correlation_id=args.correlation_id,
            )
            event_type = (
                "work_item_index_backfill_completed"
                if report["failed"] == 0 and report["validation_errors"] == 0
                else "work_item_index_backfill_failed"
            )
            attrs["result"] = "success" if event_type.endswith("completed") else "failed"
            journal.append(
                event_type,
                project_id=mesh_config.project.project_id,
                component_id="work-item-index",
                work_item_id=args.work_item_id,
                dry_run=not execute,
                created=report["created"],
                updated=report["updated"],
                skipped=report["skipped"],
                repaired=report["repaired"],
                failed=report["failed"],
                validation_errors=report["validation_errors"],
                correlation_id=args.correlation_id,
                operation="backfill",
            )
        except Exception as exc:
            attrs["result"] = "failed"
            attrs["reason"] = exc.__class__.__name__
            journal.append(
                "work_item_index_backfill_failed",
                project_id=mesh_config.project.project_id,
                component_id="work-item-index",
                work_item_id=args.work_item_id,
                dry_run=not execute,
                failed=1,
                validation_errors=0,
                correlation_id=args.correlation_id,
                operation="backfill",
                reason=exc.__class__.__name__,
            )
            print(
                json.dumps(
                    {"error": "work item index backfill failed", "reason": exc.__class__.__name__}
                ),
                file=sys.stderr,
            )
            return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["failed"] or report["validation_errors"] else 0


def _journal_flow_export_rejected(
    journal: EventJournal,
    project_id: str,
    flow_id: str,
    args,
    *,
    output: str,
    reason: str,
) -> None:
    journal.append(
        "flow_export_rejected",
        project_id=project_id,
        flow_id=flow_id,
        format=args.format,
        output=output,
        work_item_id=args.work_item_id,
        queue_item_id=args.queue_item_id,
        lifecycle_state=args.lifecycle_state,
        component_id="lifecycle-export",
        correlation_id=args.correlation_id,
        reason=reason,
    )


def _safe_relative_path(path_value: str | Path, root: Path) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        return str(path).replace("\\", "/")
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return path.name


def _flow_source_reference(config_root: Path, project_file: str) -> str:
    project_path = Path(project_file)
    if not project_path.is_absolute():
        project_path = config_root / project_path
    try:
        import yaml

        data = yaml.safe_load(project_path.read_text(encoding="utf-8")) or {}
        template = ((data.get("flow") or {}) if isinstance(data, dict) else {}).get(
            "template"
        )
        if template:
            return f"config/flows/{template}.yaml"
    except Exception:
        pass
    return _safe_relative_path(project_file, config_root)


def _flow_export_command(args) -> str:
    parts = [
        "python -m agentic_mesh.cli flow-export",
        f"--format {args.format}",
        f"--work-item-id {args.work_item_id}",
    ]
    if args.queue_item_id:
        parts.append(f"--queue-item-id {args.queue_item_id}")
    parts.append(f"--output {args.output}")
    return " ".join(parts)


def cmd_auth_plan(args) -> int:
    mesh_config, _, _, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "cli")
    resolver = AuthResolver(mesh_config.auth_methods)
    plans = [
        asdict(resolver.plan_for_instance(instance))
        for instance in mesh_config.instances.values()
        if args.instance is None or instance.instance_id == args.instance
    ]
    print(json.dumps({"auth_plans": plans}, indent=2))
    return 0


def _credential_for_cli(args):
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    credential = mesh_config.project.auth_credentials.get(args.credential)
    if credential is None:
        raise SystemExit(
            f"Unknown auth credential `{args.credential}` in project "
            f"`{mesh_config.project.project_id}`."
        )
    method = mesh_config.auth_methods[credential.method]
    return mesh_config, credential, method


def _state_secret_path(state_root: Path, secret_ref: str) -> Path:
    return state_root / "secrets" / secret_ref


def _state_mount_path(state_root: Path, mount_ref: str) -> Path:
    return state_root / "worker_mounts" / mount_ref


def cmd_auth_store_secret(args) -> int:
    _, credential, method = _credential_for_cli(args)
    if not method.requires_secret_ref or not credential.secret_ref:
        raise SystemExit(
            f"Auth credential `{credential.credential_id}` does not use a secret_ref."
        )

    if args.value_env:
        secret_value = os.getenv(args.value_env, "")
    elif not sys.stdin.isatty():
        secret_value = sys.stdin.read()
    else:
        secret_value = getpass.getpass(
            f"Secret value for {credential.credential_id}: "
        )
    secret_value = secret_value.strip()
    if not secret_value:
        raise SystemExit("No secret value was provided.")

    secret_path = _state_secret_path(args.state_root, credential.secret_ref)
    if secret_path.exists() and not args.overwrite:
        raise SystemExit(
            f"Secret `{credential.secret_ref}` already exists. Use --overwrite to replace it."
        )
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(secret_value, encoding="utf-8")
    try:
        secret_path.chmod(0o600)
    except OSError:
        pass
    print(
        json.dumps(
            {
                "credential": credential.credential_id,
                "method": credential.method,
                "secret_ref": credential.secret_ref,
                "path": str(secret_path),
                "redacted": True,
            },
            indent=2,
        )
    )
    return 0


def cmd_codex_auth_login(args) -> int:
    _, credential, method = _credential_for_cli(args)
    if method.method_id != "codex_oauth_cache" or not credential.mount_ref:
        raise SystemExit(
            "codex-auth-login requires a credential using method "
            "`codex_oauth_cache` with a mount_ref."
        )
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise SystemExit("Codex CLI is not installed or not on PATH.")

    mount_path = _state_mount_path(args.state_root, credential.mount_ref)
    mount_path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(credential.env)
    env["CODEX_HOME"] = str(mount_path)
    command = [codex_bin, "login"]
    if args.device_auth:
        command.append("--device-auth")
    completed = subprocess.run(command, env=env, check=False)
    if completed.returncode == 0:
        print(
            json.dumps(
                {
                    "credential": credential.credential_id,
                    "method": credential.method,
                    "mount_ref": credential.mount_ref,
                    "codex_home": str(mount_path),
                    "redacted": True,
                },
                indent=2,
            )
        )
    return completed.returncode


def cmd_codex_auth_status(args) -> int:
    _, credential, method = _credential_for_cli(args)
    if method.method_id != "codex_oauth_cache" or not credential.mount_ref:
        raise SystemExit(
            "codex-auth-status requires a credential using method "
            "`codex_oauth_cache` with a mount_ref."
        )
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise SystemExit("Codex CLI is not installed or not on PATH.")

    mount_path = _state_mount_path(args.state_root, credential.mount_ref)
    env = os.environ.copy()
    env.update(credential.env)
    env["CODEX_HOME"] = str(mount_path)
    return subprocess.run(
        [codex_bin, "login", "status"],
        env=env,
        check=False,
    ).returncode


def _parse_response_value(value: str):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _normalize_response_value(value: Any, response_type: str | None) -> Any:
    if response_type != "approve_not_approve" or not isinstance(value, str):
        return value
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "approve": "approved",
        "approved": "approved",
        "not_approve": "not_approved",
        "not_approved": "not_approved",
        "reject": "not_approved",
        "rejected": "not_approved",
    }.get(normalized, value)


def _control_plane_url(args) -> str:
    configured = (
        getattr(args, "server_url", None)
        or os.environ.get("AGENTIC_MESH_CONTROL_PLANE_URL")
        or configured_url_root()
        or os.environ.get("AGENTIC_MESH_STATUS_BASE_URL")
        or _base_url_from_auth_admin(os.environ.get("AGENTIC_MESH_AUTH_ADMIN_URL"))
        or DEFAULT_CONTROL_PLANE_URL
    )
    return configured.rstrip("/")


def _base_url_from_auth_admin(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _post_human_response_to_server(args) -> int:
    payload = {
        "work_item_id": args.work_item_id,
        "work_item_type": args.work_item_type,
        "lifecycle_state": args.lifecycle_state,
        "gate_id": args.gate_id,
        "response_request_id": args.response_request_id,
        "responder": args.responder,
        "value": args.value,
        "source": args.source,
    }
    optional = {
        "approval_request_id": args.approval_request_id,
        "role": args.role,
        "correlation_id": args.correlation_id,
    }
    payload.update({key: value for key, value in optional.items() if value})
    data = urlencode(payload).encode("utf-8")
    url = f"{_control_plane_url(args)}/human-responses"
    request = Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=getattr(args, "server_timeout", 30)) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(body or str(exc), file=sys.stderr)
        return 1
    except URLError as exc:
        print(
            json.dumps(
                {
                    "error": "control_plane_unavailable",
                    "server_url": _control_plane_url(args),
                    "detail": str(exc.reason),
                }
            ),
            file=sys.stderr,
        )
        return 1
    print(body)
    return 0


def cmd_record_human_response(args) -> int:
    if not args.local_state:
        return _post_human_response_to_server(args)

    mesh_config, _, message_store, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "cli")
    flow_state = mesh_config.project.flow.states[args.lifecycle_state]
    gate = next(
        (
            candidate
            for candidate in flow_state.gates
            if candidate.gate_id == args.gate_id
        ),
        None,
    )
    message = build_human_response_received_message(
        target_role=args.role or flow_state.owner_role,
        work_item_id=args.work_item_id,
        work_item_type=args.work_item_type,
        lifecycle_state=args.lifecycle_state,
        gate_id=args.gate_id,
        response_type=gate.response_type if gate is not None else None,
        approval_request_id=args.approval_request_id,
        response_request_id=args.response_request_id,
        responder=args.responder,
        response_value=_normalize_response_value(
            _parse_response_value(args.value),
            gate.response_type if gate is not None else None,
        ),
        source=args.source,
        correlation_id=args.correlation_id,
    )
    message_store.enqueue(message)
    print(
        json.dumps(
            {
                "message_id": message.message_id,
                "target_role": message.role_id,
                "correlation_id": message.correlation_id,
            }
        )
    )
    return 0


def cmd_safe_output(args) -> int:
    if args.stdin_marker != ".":
        print("safe-output requires `.` and a JSON object on stdin", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(f"invalid safe-output JSON payload: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("safe-output payload must be a JSON object", file=sys.stderr)
        return 2
    try:
        record = append_safe_output_record(
            output_file=safe_output_file_from_env(),
            tool=args.tool,
            payload=payload,
            context=safe_output_context_from_env(),
        )
    except Exception as exc:
        print(f"safe-output failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "schema_version": record.to_dict()["schema_version"],
                "tool": record.tool,
                "recorded_at": record.recorded_at,
                "validation": record.validation,
            },
            sort_keys=True,
        )
    )
    return 0


def cmd_agent_loop(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    instance = mesh_config.instances[args.instance]
    workspace_root = project_workspace_root(args.workspace_root, mesh_config)
    document_library_root = project_document_library_root(workspace_root, mesh_config)
    try:
        startup_audit = write_startup_prompt_audit(
            document_library_root=document_library_root,
            project=mesh_config.project,
            instance=instance,
            workspace_root=workspace_root,
            reason="agent_loop_start",
            argv=sys.argv,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": "startup_prompt_audit_failed",
                    "instance": args.instance,
                    "reason": exc.__class__.__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "instance": args.instance,
                "startup_prompt_audit": startup_audit,
            },
            sort_keys=True,
        )
    )
    reload_requested = False
    if getattr(args, "reclaim_existing_claims_on_start", True):
        args.reclaim_claimed_before = utc_now_iso()
    while True:
        cmd_run_agent(args)
        args.reclaim_claimed_before = None
        if _consume_agent_reload_request(
            args.state_root,
            mesh_config.project.project_id,
            args.instance,
        ):
            print(
                json.dumps(
                    {
                        "instance": args.instance,
                        "reload_requested": True,
                        "action": "exiting_after_current_iteration",
                    }
                )
            )
            reload_requested = True
            break
        time.sleep(args.poll_seconds)
    return 0 if reload_requested else 0


def cmd_request_agent_reload(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    targets = (
        sorted(mesh_config.instances)
        if args.instance == "all"
        else [args.instance]
    )
    unknown = [target for target in targets if target not in mesh_config.instances]
    if unknown:
        print(
            json.dumps(
                {
                    "error": "unknown_instance",
                    "unknown_instances": unknown,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    request_dir = _agent_reload_request_dir(
        args.state_root,
        mesh_config.project.project_id,
    )
    request_dir.mkdir(parents=True, exist_ok=True)
    requested_at = utc_now_iso()
    for target in targets:
        path = request_dir / f"{target}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "agent-reload-request-v0",
                    "project_id": mesh_config.project.project_id,
                    "role_instance_id": target,
                    "requested_at": requested_at,
                    "reason": args.reason,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "project_id": mesh_config.project.project_id,
                "requested_instances": targets,
                "requested_at": requested_at,
                "request_dir": str(request_dir),
            },
            indent=2,
        )
    )
    return 0


def _agent_reload_request_dir(state_root: Path, project_id: str) -> Path:
    return state_root / "projects" / project_id / "runtime" / "agent-reload-requests"


def _consume_agent_reload_request(
    state_root: Path,
    project_id: str,
    instance_id: str,
) -> bool:
    path = _agent_reload_request_dir(state_root, project_id) / f"{instance_id}.json"
    if not path.exists():
        return False
    path.unlink(missing_ok=True)
    return True


def cmd_control_plane_loop(args) -> int:
    if getattr(args, "auth_admin_port", 0):
        service = ControllerAuthService(
            config_root=args.config_root,
            project_file=args.project_file,
            state_root=args.state_root,
            workspace_root=args.workspace_root,
        )
        thread = Thread(
            target=serve_controller_auth,
            kwargs={
                "host": args.auth_admin_host,
                "port": args.auth_admin_port,
                "service": service,
            },
            daemon=True,
        )
        thread.start()
    while True:
        cmd_control_plane_tick(args)
        time.sleep(args.poll_seconds)


def cmd_auth_admin_server(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    configure_component_telemetry(mesh_config, "auth-admin")
    service = ControllerAuthService(
        config_root=args.config_root,
        project_file=args.project_file,
        state_root=args.state_root,
        workspace_root=args.workspace_root,
    )
    serve_controller_auth(host=args.host, port=args.port, service=service)
    return 0


def cmd_router_loop(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    configure_component_telemetry(mesh_config, "router")
    while True:
        print(json.dumps({"service": "router", "status": "idle", "note": "handoffs route in runtime v0"}))
        time.sleep(args.poll_seconds)


def cmd_teams_connector_once(args) -> int:
    mesh_config, journal, _, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "teams-connector")
    connector = LocalTeamsConnectorAdapter(
        connector_id=args.connector_id,
        project_id=mesh_config.project.project_id,
        state_root=args.state_root,
        outbox=connector_outbox,
        journal=journal,
    )
    did_work = connector.process_once(args.channel)
    print(
        json.dumps(
            {
                "connector_id": args.connector_id,
                "channel": args.channel,
                "did_work": did_work,
            }
        )
    )
    return 0


def cmd_teams_connector_loop(args) -> int:
    while True:
        cmd_teams_connector_once(args)
        time.sleep(args.poll_seconds)


def _connector_channels(mesh_config, connector_id: str, channel: str) -> list[str]:
    connector_config = mesh_config.project.connectors[connector_id]
    if channel == "all":
        return sorted(connector_config.channels)
    return [channel]


def _bot_connector_channels(mesh_config, connector_id: str, channel: str) -> list[str]:
    channels = _connector_channels(mesh_config, connector_id, channel)
    if channel == "all":
        dm_enabled = any(
            gateway.enabled
            and gateway.teams is not None
            and gateway.teams.connector == connector_id
            and gateway.teams.dm_enabled
            for gateway in mesh_config.project.gateways.values()
        )
        if dm_enabled and "dm" not in channels:
            channels.append("dm")
    return sorted(channels)


def cmd_teams_graph_connector_once(args) -> int:
    mesh_config, journal, _, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "teams-connector")
    connector_config = mesh_config.project.connectors[args.connector]
    token = load_graph_token(
        args.token,
        args.token_file,
        tenant_id=args.tenant_id,
        client_id=args.client_id,
        client_secret=args.client_secret,
        client_secret_file=args.client_secret_file,
    )
    connector = GraphTeamsConnectorAdapter(
        connector_id=args.connector_id,
        project_id=mesh_config.project.project_id,
        connector_config=connector_config,
        outbox=connector_outbox,
        journal=journal,
        token=token,
    )
    results = {}
    for channel in _connector_channels(mesh_config, args.connector, args.channel):
        results[channel] = connector.process_once(channel)
    print(json.dumps({"connector_id": args.connector_id, "results": results}))
    return 0


def cmd_teams_graph_connector_loop(args) -> int:
    while True:
        cmd_teams_graph_connector_once(args)
        time.sleep(args.poll_seconds)


def cmd_teams_graph_ingress_once(args) -> int:
    mesh_config, journal, message_store, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "teams-ingress")
    connector_config = mesh_config.project.connectors[args.connector]
    token = load_graph_token(
        args.token,
        args.token_file,
        tenant_id=args.tenant_id,
        client_id=args.client_id,
        client_secret=args.client_secret,
        client_secret_file=args.client_secret_file,
    )
    ingress = GraphTeamsChannelIngressAdapter(
        connector_id=args.connector_id,
        project_id=mesh_config.project.project_id,
        state_root=args.state_root,
        connector_config=connector_config,
        message_store=message_store,
        journal=journal,
        project_config=mesh_config.project,
        token=token,
        connector_outbox=connector_outbox,
    )
    results = {}
    exit_code = 0
    for channel in _connector_channels(mesh_config, args.connector, args.channel):
        try:
            results[channel] = ingress.process_once(
                channel,
                max_messages=args.max_messages,
            )
        except Exception as exc:
            journal.append(
                "teams_graph_ingress_failed",
                project_id=mesh_config.project.project_id,
                connector_id=args.connector_id,
                channel=channel,
                error=str(exc),
            )
            results[channel] = {"error": str(exc)}
            exit_code = 1
    print(json.dumps({"connector_id": args.connector_id, "results": results}))
    return exit_code


def cmd_teams_graph_ingress_loop(args) -> int:
    while True:
        cmd_teams_graph_ingress_once(args)
        time.sleep(args.poll_seconds)


def cmd_teams_bot_connector_once(args) -> int:
    mesh_config, journal, _, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "teams-connector")
    connector_config = mesh_config.project.connectors[args.connector]
    connector = BotFrameworkTeamsConnectorAdapter(
        connector_id=args.connector_id,
        project_id=mesh_config.project.project_id,
        connector_config=connector_config,
        outbox=connector_outbox,
        journal=journal,
        secrets=FileSecretResolver(args.secret_root),
        service_url=args.service_url,
    )
    results = {}
    for channel in _bot_connector_channels(mesh_config, args.connector, args.channel):
        results[channel] = connector.process_once(channel)
    print(json.dumps({"connector_id": args.connector_id, "results": results}))
    return 0


def cmd_teams_bot_connector_loop(args) -> int:
    while True:
        cmd_teams_bot_connector_once(args)
        time.sleep(args.poll_seconds)


def cmd_teams_bot_listener(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    configure_component_telemetry(mesh_config, "teams-bot-listener")
    ingress = ReloadableTeamsBotIngress(
        config_root=args.config_root,
        project_file=args.project_file,
        state_root=args.state_root,
        connector=args.connector,
        connector_id=args.connector_id,
        secret_root=args.secret_root,
    )
    serve_teams_bot_ingress(host=args.host, port=args.port, ingress=ingress)
    return 0


def _work_queue_store(
    args,
) -> tuple[Any, EventJournal, FileMessageStore, FileConnectorOutbox, FileWorkQueueStore]:
    mesh_config, journal, message_store, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "cli")
    return (
        mesh_config,
        journal,
        message_store,
        connector_outbox,
        FileWorkQueueStore(args.state_root, mesh_config.project.project_id, journal),
    )


def _external_action_service(
    args,
    *,
    allow_cli_promotion: bool = False,
) -> tuple[Any, ExternalActionService]:
    mesh_config, journal, message_store, connector_outbox, work_queue = _work_queue_store(args)
    notification_service = QueueReceiptNotificationService(
        project_id=mesh_config.project.project_id,
        policy=mesh_config.project.notification_policy,
        work_queue=work_queue,
        attempt_store=FileNotificationAttemptStore(
            args.state_root,
            mesh_config.project.project_id,
        ),
        connector_outbox=connector_outbox,
        source_route_store=FileSourceRouteStore(
            args.state_root,
            mesh_config.project.project_id,
        ),
    )
    return (
        mesh_config,
        ExternalActionService(
            project_id=mesh_config.project.project_id,
            work_queue=work_queue,
            store=FileExternalActionStore(args.state_root, mesh_config.project.project_id),
            journal=journal,
            policy=ExternalActionPolicy(allow_cli_promotion=allow_cli_promotion),
            message_store=message_store,
            notification_service=notification_service,
        ),
    )


def _cli_actor(actor: str | None, *, role_id: str | None = None) -> ExternalActor:
    label = role_id or ("local-cli-actor" if actor else getpass.getuser() or "local-cli")
    return ExternalActor(
        actor_type="local_operator",
        display_label=label,
        role_id=role_id,
    )


def _cli_anchor(args) -> SourceAnchor:
    return SourceAnchor(
        connector_type=getattr(args, "connector_type", "cli"),
        connector_id=getattr(args, "connector_id", "local-cli"),
        source_scope=getattr(args, "source_scope", "cli"),
        source_message_id=getattr(args, "source_message_id", None),
        actor=getattr(args, "actor", None),
        received_at=getattr(args, "received_at", None) or _now_for_cli(),
        display_label=getattr(args, "display_label", None)
        or getattr(args, "source_scope", "cli"),
        external_url=getattr(args, "external_url", None),
    )


def _print_receipt(receipt, *, stderr: bool = False, **extra: Any) -> None:
    print(
        json.dumps(receipt_output(receipt, **extra), indent=2, sort_keys=True),
        file=sys.stderr if stderr else sys.stdout,
    )


def cmd_work_queue_capture(args) -> int:
    _, service = _external_action_service(args)
    raw_payload = json.loads(args.raw_payload) if args.raw_payload else None
    request = ExternalActionRequest(
        action_type=ACTION_CAPTURE_WORK,
        source_type="cli",
        source_anchor=_cli_anchor(args),
        actor=_cli_actor(args.actor),
        payload={
            "title": args.title,
            "summary": args.summary,
            "owner_role": args.owner_role,
            "recommended_work_item_type": args.work_item_type,
            "raw_payload": raw_payload,
            "retain_raw_payload": args.retain_raw_payload,
        },
        idempotency_key=args.idempotency_key,
    )
    receipt = service.execute(request)
    _print_receipt(receipt)
    return 1 if receipt.outcome in {OUTCOME_NOT_CAPTURED, OUTCOME_ACTION_FAILED} else 0


def cmd_work_queue_list(args) -> int:
    _, _, _, _, work_queue = _work_queue_store(args)
    items = [
        item.redacted_summary()
        for item in work_queue.list_items()
        if (args.status is None or item.status == args.status)
        and (args.owner_role is None or item.owner_role == args.owner_role)
    ]
    print(json.dumps({"items": items}, indent=2, sort_keys=True))
    return 0


def cmd_work_queue_show(args) -> int:
    _, service = _external_action_service(args)
    action_type = ACTION_READ_RAW_REFERENCE if args.support else ACTION_SHOW_STATUS
    request = ExternalActionRequest(
        action_type=action_type,
        source_type="cli",
        source_anchor=_cli_anchor(args),
        actor=_cli_actor(args.actor),
        target=ExternalActionTarget(queue_item_id=args.queue_item_id),
        reason=args.reason,
        correlation_id=args.correlation_id or new_id("corr"),
    )
    receipt = service.execute(request)
    if args.support:
        if receipt.outcome in {OUTCOME_ACTION_DENIED, OUTCOME_ACTION_FAILED}:
            error = receipt.safe_reason or "support mode requires actor, reason, and correlation id"
            if error in {
                "sensitive_action_requires_reason",
                "support_action_requires_actor_target_correlation",
            }:
                error = "support mode requires actor, reason, and correlation id"
            _print_receipt(receipt, stderr=True, error=error)
            return 1
        _print_receipt(receipt)
        return 0
    if receipt.outcome in {OUTCOME_ACTION_DENIED, OUTCOME_ACTION_FAILED}:
        _print_receipt(receipt, stderr=True, error=receipt.safe_reason or receipt.outcome)
        return 1
    _print_receipt(receipt)
    return 0


def cmd_work_queue_transition(args) -> int:
    _, service = _external_action_service(args)
    request = ExternalActionRequest(
        action_type=ACTION_TRANSITION,
        source_type="cli",
        source_anchor=_cli_anchor(args),
        actor=_cli_actor(args.actor_role, role_id=args.actor_role),
        target=ExternalActionTarget(queue_item_id=args.queue_item_id),
        payload={"status": args.status},
        reason=args.reason,
        correlation_id=args.correlation_id or new_id("corr"),
    )
    receipt = service.execute(request)
    if receipt.outcome in {OUTCOME_ACTION_DENIED, OUTCOME_ACTION_FAILED}:
        _print_receipt(receipt, stderr=True, error=receipt.safe_reason or receipt.outcome)
        return 1
    _print_receipt(receipt)
    return 0


def cmd_work_queue_readiness(args) -> int:
    _, service = _external_action_service(args)
    evidence = json.loads(args.evidence)
    request = ExternalActionRequest(
        action_type=ACTION_MARK_READY,
        source_type="cli",
        source_anchor=_cli_anchor(args),
        actor=_cli_actor(args.actor_role, role_id=args.actor_role),
        target=ExternalActionTarget(queue_item_id=args.queue_item_id),
        payload={"evidence": evidence},
        correlation_id=args.correlation_id or new_id("corr"),
    )
    receipt = service.execute(request)
    if receipt.outcome in {OUTCOME_ACTION_DENIED, OUTCOME_ACTION_FAILED}:
        _print_receipt(receipt, stderr=True, error=receipt.safe_reason or receipt.outcome)
        return 1
    _print_receipt(receipt)
    return 0


def cmd_work_queue_promote(args) -> int:
    mesh_config, service = _external_action_service(
        args,
        allow_cli_promotion=bool(args.allow),
    )
    lifecycle_state = args.lifecycle_state or mesh_config.project.flow.entry_state
    target_role = args.target_role or mesh_config.project.flow.states[lifecycle_state].owner_role
    request = ExternalActionRequest(
        action_type=ACTION_PROMOTE,
        source_type="cli",
        source_anchor=_cli_anchor(args),
        actor=_cli_actor(args.actor_role, role_id=args.actor_role),
        target=ExternalActionTarget(queue_item_id=args.queue_item_id),
        payload={
            "target_role": target_role,
            "lifecycle_state": lifecycle_state,
            "work_item_id": args.work_item_id,
            "work_item_type": args.work_item_type,
            "message_type": args.message_type,
        },
        reason=args.reason,
        idempotency_key=args.idempotency_key,
    )
    receipt = service.execute(request)
    if receipt.outcome in {OUTCOME_ACTION_DENIED, OUTCOME_ACTION_FAILED}:
        _print_receipt(receipt, stderr=True, error=receipt.safe_reason or receipt.outcome)
        return 1
    _print_receipt(receipt)
    return 0


def cmd_work_queue_purge(args) -> int:
    _, service = _external_action_service(args)
    request = ExternalActionRequest(
        action_type=ACTION_PURGE_RAW,
        source_type="cli",
        source_anchor=_cli_anchor(args),
        actor=_cli_actor(args.actor),
        target=ExternalActionTarget(queue_item_id=args.queue_item_id),
        payload={"execute": args.execute},
        reason=args.reason,
        correlation_id=args.correlation_id or new_id("corr"),
    )
    receipt = service.execute(request)
    if receipt.outcome in {OUTCOME_ACTION_DENIED, OUTCOME_ACTION_FAILED}:
        _print_receipt(receipt, stderr=True, error=receipt.safe_reason or receipt.outcome)
        return 1
    _print_receipt(receipt)
    return 0


def _recovery_service(args) -> tuple[Any, RecoveryActionService]:
    mesh_config, journal, message_store, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "work-item-recovery")
    project_id = mesh_config.project.project_id
    store = FileRecoveryStatusStore(args.state_root, project_id)
    return (
        mesh_config,
        RecoveryActionService(
            project_id=project_id,
            store=store,
            message_store=message_store,
            journal=journal,
            duplicate_guard=DuplicateActiveWorkGuard(
                state_root=args.state_root,
                project_id=project_id,
            ),
        ),
    )


def _approval_service(args) -> ApprovalStatusService:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    configure_component_telemetry(mesh_config, "approval-requests")
    return ApprovalStatusService(mesh_config=mesh_config, state_root=args.state_root)


def cmd_approval_status(args) -> int:
    service = _approval_service(args)
    dto = service.status_for_work_item(
        args.work_item_id,
        lifecycle_state=args.lifecycle_state,
        gate_id=args.gate_id,
        current_status=args.current_status,
        status_url=args.status_url,
    )
    print(json.dumps(dto.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_approval_reconcile(args) -> int:
    service = _approval_service(args)
    try:
        result = service.reconcile(
            work_item_id=args.work_item_id,
            lifecycle_state=args.lifecycle_state,
            gate_id=args.gate_id,
            dry_run=not args.execute,
            actor=args.actor,
            reason=args.reason,
            current_status=args.current_status,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_approval_resend(args) -> int:
    if not args.actor or not args.reason:
        print(
            "Actor and reason are required before this recovery action can run.",
            file=sys.stderr,
        )
        return 2
    mesh_config, journal, _, connector_outbox, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "approval-requests")
    service = ApprovalStatusService(mesh_config=mesh_config, state_root=args.state_root)
    try:
        dto = service.resend(
            work_item_id=args.work_item_id,
            lifecycle_state=args.lifecycle_state,
            gate_id=args.gate_id,
            actor=args.actor,
            reason=args.reason,
        )
        request_record = (
            service.store.read(dto.response_request_id)
            if dto.response_request_id
            else None
        )
        if request_record is not None:
            connector_message = _approval_resend_connector_message(
                mesh_config=mesh_config,
                request_record=request_record,
                actor=args.actor,
                reason=args.reason,
            )
            connector_message = connector_outbox.enqueue(connector_message)
            service.store.mark_enqueue_succeeded(
                request_record.response_request_id,
                connector_message_id=connector_message.message_id,
            )
            journal.append(
                "approval_request_resend_queued",
                project_id=mesh_config.project.project_id,
                work_item_id=request_record.work_item_id,
                lifecycle_state=request_record.lifecycle_state,
                gate_id=request_record.gate_id,
                approval_request_id=request_record.approval_request_id
                or request_record.response_request_id,
                response_request_id=request_record.response_request_id,
                notification_attempt_id=request_record.current_notification_attempt_id,
                connector_message_id=connector_message.message_id,
                actor=args.actor,
                reason=args.reason[:160],
                correlation_id=connector_message.correlation_id,
            )
            dto = service.status_for_work_item(
                args.work_item_id,
                lifecycle_state=args.lifecycle_state,
                gate_id=args.gate_id,
            )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(dto.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_approval_mark_superseded(args) -> int:
    if not args.actor or not args.reason:
        print(
            "Actor and reason are required before this recovery action can run.",
            file=sys.stderr,
        )
        return 2
    service = _approval_service(args)
    try:
        dto = service.mark_superseded(
            work_item_id=args.work_item_id,
            lifecycle_state=args.lifecycle_state,
            gate_id=args.gate_id,
            actor=args.actor,
            reason=args.reason,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(dto.to_dict(), indent=2, sort_keys=True))
    return 0


def _approval_resend_connector_message(
    *,
    mesh_config,
    request_record,
    actor: str,
    reason: str,
) -> ConnectorMessage:
    response_template = mesh_config.response_types.get(request_record.response_type)
    payload: dict[str, Any] = {
        "response_request_id": request_record.response_request_id,
        "approval_request_id": request_record.approval_request_id
        or request_record.response_request_id,
        "notification_attempt_id": request_record.current_notification_attempt_id,
        "gate_id": request_record.gate_id,
        "response_type": request_record.response_type,
        "prompt": request_record.prompt,
        "requested_from": request_record.requested_from,
        "completion_criteria": {"accepted_values": request_record.accepted_values},
        "project_id": mesh_config.project.project_id,
        "role_id": mesh_config.project.flow.states[
            request_record.lifecycle_state
        ].owner_role,
        "work_item_id": request_record.work_item_id,
        "work_item_type": request_record.work_item_type,
        "lifecycle_state": request_record.lifecycle_state,
        "requested_at": request_record.requested_at,
        "correlation_id": new_id("corr"),
        "approval_context": {
            "work_performed_summary": (
                "Original source thread unavailable; this approval update was "
                f"posted to {request_record.channel or 'approvals'}."
            ),
            "status_url": f"/work-items/{request_record.work_item_id}",
            "resend_reason": reason[:160],
            "resend_actor": actor[:120],
        },
    }
    if response_template is not None:
        payload["response_template"] = asdict(response_template)
    return ConnectorMessage.create(
        channel=str(request_record.channel or "approvals"),
        message_type="human_response.requested",
        payload=payload,
        source="approval-recovery-cli",
        correlation_id=str(payload["correlation_id"]),
    )


def _recovery_request(args, action_type: str) -> RecoveryActionRequest:
    return RecoveryActionRequest(
        action_type=action_type,
        project_id=getattr(args, "project_id", None) or "agentic-mesh-dev",
        work_item_id=args.work_item_id,
        work_item_type=getattr(args, "work_item_type", None),
        queue_item_id=getattr(args, "queue_item_id", None),
        lifecycle_state=getattr(args, "lifecycle_state", None),
        affected_role=getattr(args, "affected_role", None),
        actor=getattr(args, "actor", None),
        reason=getattr(args, "reason", None),
        idempotency_key=getattr(args, "idempotency_key", None),
        expected_revision=getattr(args, "expected_revision", None),
        correlation_id=getattr(args, "correlation_id", None) or new_id("corr"),
        source_type=getattr(args, "source_type", "cli"),
        repair_confirmed=bool(getattr(args, "repair_confirmed", False)),
        replacement_work_item_id=getattr(args, "replacement_work_item_id", None),
        decision_owner=getattr(args, "decision_owner", None),
        retry_after=getattr(args, "retry_after", None),
    )


def cmd_work_item_recovery_status(args) -> int:
    mesh_config, service = _recovery_service(args)
    request = _recovery_request(args, RECOVERY_READ_ACTION)
    request = replace(request, project_id=mesh_config.project.project_id)
    receipt = service.execute(request)
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_work_item_recovery_observability(args) -> int:
    mesh_config, service = _recovery_service(args)
    request = _recovery_request(args, RECOVERY_READ_ACTION)
    request = replace(request, project_id=mesh_config.project.project_id)
    receipt = service.execute(request)
    recovery_status = receipt.recovery_status
    from agentic_mesh.work_item_recovery import RecoveryStatus

    view = build_recovery_observability_view(
        RecoveryStatus.from_dict(recovery_status),
        alert_state=FileRecoveryAlertStore(
            args.state_root,
            mesh_config.project.project_id,
            create_dirs=False,
        ).get_current(args.work_item_id),
    )
    print(json.dumps(view.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_work_item_recovery_provider_probe(args) -> int:
    profile = CodexProviderConditionProfile()
    evidence = {
        "safe_non_prompt_evidence": not args.unsafe,
        "failure_class": args.failure_class,
        "confidence": args.confidence,
        "retry_after": args.retry_after,
        "next_check_at": args.next_check_at,
    }
    result = profile.classify_safe_evidence(evidence)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_work_item_recovery_poller_once(args) -> int:
    mesh_config, journal, message_store, _, _, _ = build_runtime(
        args.config_root,
        args.project_file,
        args.workspace_root,
        args.state_root,
    )
    configure_component_telemetry(mesh_config, "recovery-poller")
    project_id = mesh_config.project.project_id
    store = FileRecoveryStatusStore(args.state_root, project_id)
    service = RecoveryActionService(
        project_id=project_id,
        store=store,
        message_store=message_store,
        journal=journal,
        duplicate_guard=DuplicateActiveWorkGuard(
            state_root=args.state_root,
            project_id=project_id,
        ),
    )
    poller = RecoveryPoller(
        project_id=project_id,
        state_root=args.state_root,
        store=store,
        action_service=service,
        policy=RecoveryPollerPolicy(enabled=not args.disabled),
    )
    print(json.dumps(poller.run_once(), indent=2, sort_keys=True))
    return 0


def cmd_work_item_recovery_alert_acknowledge(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    store = FileRecoveryAlertStore(args.state_root, mesh_config.project.project_id)
    receipt = store.acknowledge(
        work_item_id=args.work_item_id,
        actor=args.actor,
        reason=args.reason,
        scope=args.scope,
        expected_revision=args.expected_revision,
        correlation_id=args.correlation_id or new_id("corr"),
    )
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    return 0 if receipt.outcome == "accepted" else 1


def cmd_work_item_recovery_alert_silence(args) -> int:
    mesh_config = load_mesh_config(args.config_root, project_file=args.project_file)
    store = FileRecoveryAlertStore(args.state_root, mesh_config.project.project_id)
    receipt = store.silence(
        work_item_id=args.work_item_id,
        actor=args.actor,
        reason=args.reason,
        scope=args.scope,
        silence_expires_at=args.silence_expires_at,
        expected_revision=args.expected_revision,
        correlation_id=args.correlation_id or new_id("corr"),
        human_decision_prompt=args.human_decision_prompt,
    )
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    return 0 if receipt.outcome == "accepted" else 1


def cmd_work_item_recovery_retry(args) -> int:
    mesh_config, service = _recovery_service(args)
    request = _recovery_request(args, "retry_work_item_recovery")
    request = replace(request, project_id=mesh_config.project.project_id)
    receipt = service.execute(request)
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    return 0 if receipt.outcome in {"queued", "duplicate"} else 1


def cmd_work_item_recovery_recover(args) -> int:
    mesh_config, service = _recovery_service(args)
    request = _recovery_request(args, "record_work_item_recovery")
    request = replace(request, project_id=mesh_config.project.project_id)
    receipt = service.execute(request)
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    return 0 if receipt.outcome in {"queued", "accepted", "duplicate"} else 1


def cmd_work_item_recovery_supersede(args) -> int:
    mesh_config, service = _recovery_service(args)
    request = _recovery_request(args, "supersede_work_item_recovery")
    request = replace(request, project_id=mesh_config.project.project_id)
    receipt = service.execute(request)
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    return 0 if receipt.outcome == "superseded" else 1


def cmd_work_item_recovery_mark_needs_decision(args) -> int:
    mesh_config, service = _recovery_service(args)
    request = _recovery_request(args, "mark_work_item_recovery_needs_decision")
    request = replace(request, project_id=mesh_config.project.project_id)
    receipt = service.execute(request)
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
    return 0 if receipt.outcome == "accepted" else 1


def _now_for_cli() -> str:
    from agentic_mesh.models import utc_now_iso

    return utc_now_iso()


def parser() -> argparse.ArgumentParser:
    root = Path(os.getenv("AGENTIC_MESH_CONFIG_ROOT", Path.cwd()))
    workspace_root = Path(os.getenv("AGENTIC_MESH_WORKSPACE_ROOT", root))
    state_root = Path(os.getenv("AGENTIC_MESH_STATE_ROOT", workspace_root / "state"))
    project_file = os.getenv(
        "AGENTIC_MESH_PROJECT_FILE",
        "examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
    )
    parser = argparse.ArgumentParser(prog="agentic-mesh")
    parser.add_argument(
        "--root",
        type=Path,
        dest="config_root",
        default=root,
        help="Deprecated alias for --config-root.",
    )
    parser.add_argument("--config-root", type=Path, default=root)
    parser.add_argument("--project-file", default=project_file)
    parser.add_argument("--workspace-root", type=Path, default=workspace_root)
    parser.add_argument("--state-root", type=Path, default=state_root)
    subcommands = parser.add_subparsers(required=True)

    validate = subcommands.add_parser("validate-config")
    validate.set_defaults(func=cmd_validate)

    enqueue = subcommands.add_parser("enqueue")
    enqueue.add_argument("--role")
    enqueue.add_argument("--type", default="sdlc.intake")
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--summary", required=True)
    enqueue.add_argument("--work-item-id")
    enqueue.add_argument("--work-item-type", default="slice")
    enqueue.add_argument("--lifecycle-state")
    enqueue.add_argument("--source", default="local-cli")
    enqueue.set_defaults(func=cmd_enqueue)

    run_agent = subcommands.add_parser("run-agent")
    run_agent.add_argument("--instance", required=True)
    run_agent.add_argument(
        "--claim-lease-seconds",
        type=int,
        default=int(os.environ.get("AGENTIC_MESH_CLAIM_LEASE_SECONDS", "21600")),
        help=(
            "Requeue this role instance's claimed messages after this many "
            "seconds without completion. Set 0 to disable."
        ),
    )
    run_agent.set_defaults(func=cmd_run_agent)

    tick = subcommands.add_parser("control-plane-tick")
    tick.add_argument("--idle-grace-seconds", type=int, default=300)
    tick.set_defaults(func=cmd_control_plane_tick)

    status = subcommands.add_parser("status")
    status.set_defaults(func=cmd_status)

    capability_readiness = subcommands.add_parser("capability-readiness")
    capability_readiness.set_defaults(func=cmd_capability_readiness)

    approval_status = subcommands.add_parser("approval-status")
    approval_status.add_argument("--work-item-id", required=True)
    approval_status.add_argument("--lifecycle-state")
    approval_status.add_argument("--gate-id")
    approval_status.add_argument("--current-status")
    approval_status.add_argument("--status-url")
    approval_status.set_defaults(func=cmd_approval_status)

    approval_reconcile = subcommands.add_parser("approval-reconcile")
    approval_reconcile.add_argument("--work-item-id", required=True)
    approval_reconcile.add_argument("--lifecycle-state")
    approval_reconcile.add_argument("--gate-id")
    approval_reconcile.add_argument("--current-status")
    approval_reconcile.add_argument("--execute", action="store_true")
    approval_reconcile.add_argument("--actor")
    approval_reconcile.add_argument("--reason")
    approval_reconcile.set_defaults(func=cmd_approval_reconcile)

    approval_resend = subcommands.add_parser("approval-resend")
    approval_resend.add_argument("--work-item-id", required=True)
    approval_resend.add_argument("--lifecycle-state")
    approval_resend.add_argument("--gate-id")
    approval_resend.add_argument("--actor")
    approval_resend.add_argument("--reason")
    approval_resend.set_defaults(func=cmd_approval_resend)

    approval_supersede = subcommands.add_parser("approval-mark-superseded")
    approval_supersede.add_argument("--work-item-id", required=True)
    approval_supersede.add_argument("--lifecycle-state")
    approval_supersede.add_argument("--gate-id")
    approval_supersede.add_argument("--actor")
    approval_supersede.add_argument("--reason")
    approval_supersede.set_defaults(func=cmd_approval_mark_superseded)

    work_queue = subcommands.add_parser("work-queue")
    work_queue_commands = work_queue.add_subparsers(required=True)

    queue_capture = work_queue_commands.add_parser("capture")
    queue_capture.add_argument("--title", required=True)
    queue_capture.add_argument("--summary", required=True)
    queue_capture.add_argument("--owner-role", required=True)
    queue_capture.add_argument("--work-item-type", default="spike")
    queue_capture.add_argument("--connector-type", default="cli")
    queue_capture.add_argument("--connector-id", default="local-cli")
    queue_capture.add_argument("--source-scope", default="cli")
    queue_capture.add_argument("--source-message-id")
    queue_capture.add_argument("--actor")
    queue_capture.add_argument("--received-at")
    queue_capture.add_argument("--display-label")
    queue_capture.add_argument("--external-url")
    queue_capture.add_argument("--idempotency-key")
    queue_capture.add_argument("--raw-payload")
    queue_capture.add_argument("--retain-raw-payload", action="store_true")
    queue_capture.set_defaults(func=cmd_work_queue_capture)

    queue_list = work_queue_commands.add_parser("list")
    queue_list.add_argument("--status")
    queue_list.add_argument("--owner-role")
    queue_list.set_defaults(func=cmd_work_queue_list)

    queue_show = work_queue_commands.add_parser("show")
    queue_show.add_argument("--queue-item-id", required=True)
    queue_show.add_argument("--support", action="store_true")
    queue_show.add_argument("--actor")
    queue_show.add_argument("--reason")
    queue_show.add_argument("--correlation-id")
    queue_show.set_defaults(func=cmd_work_queue_show)

    queue_transition = work_queue_commands.add_parser("transition")
    queue_transition.add_argument("--queue-item-id", required=True)
    queue_transition.add_argument("--status", required=True)
    queue_transition.add_argument("--actor-role", required=True)
    queue_transition.add_argument("--reason")
    queue_transition.add_argument("--correlation-id")
    queue_transition.set_defaults(func=cmd_work_queue_transition)

    queue_readiness = work_queue_commands.add_parser("readiness")
    queue_readiness.add_argument("--queue-item-id", required=True)
    queue_readiness.add_argument("--actor-role", required=True)
    queue_readiness.add_argument("--evidence", required=True)
    queue_readiness.add_argument("--correlation-id")
    queue_readiness.set_defaults(func=cmd_work_queue_readiness)

    queue_promote = work_queue_commands.add_parser("promote")
    queue_promote.add_argument("--queue-item-id", required=True)
    queue_promote.add_argument("--actor-role", default="promotion-service")
    queue_promote.add_argument("--target-role")
    queue_promote.add_argument("--lifecycle-state")
    queue_promote.add_argument("--work-item-id")
    queue_promote.add_argument("--work-item-type")
    queue_promote.add_argument("--message-type", default="sdlc.intake")
    queue_promote.add_argument("--idempotency-key")
    queue_promote.add_argument("--reason")
    queue_promote.add_argument(
        "--allow",
        action="store_true",
        help="Explicitly authorize local CLI promotion for this request.",
    )
    queue_promote.set_defaults(func=cmd_work_queue_promote)

    queue_purge = work_queue_commands.add_parser("purge")
    queue_purge.add_argument("--queue-item-id")
    queue_purge.add_argument("--execute", action="store_true")
    queue_purge.add_argument("--actor")
    queue_purge.add_argument("--reason")
    queue_purge.add_argument("--correlation-id")
    queue_purge.set_defaults(func=cmd_work_queue_purge)

    work_item = subcommands.add_parser("work-item")
    work_item_commands = work_item.add_subparsers(required=True)
    recovery = work_item_commands.add_parser("recovery")
    recovery_commands = recovery.add_subparsers(required=True)

    recovery_status = recovery_commands.add_parser("status")
    recovery_status.add_argument("--work-item-id", required=True)
    recovery_status.add_argument("--work-item-type")
    recovery_status.add_argument("--queue-item-id")
    recovery_status.add_argument("--lifecycle-state")
    recovery_status.add_argument("--affected-role")
    recovery_status.add_argument("--actor")
    recovery_status.add_argument("--correlation-id")
    recovery_status.set_defaults(func=cmd_work_item_recovery_status)

    recovery_observability = recovery_commands.add_parser("observability")
    recovery_observability.add_argument("--work-item-id", required=True)
    recovery_observability.add_argument("--work-item-type")
    recovery_observability.add_argument("--queue-item-id")
    recovery_observability.add_argument("--lifecycle-state")
    recovery_observability.add_argument("--affected-role")
    recovery_observability.add_argument("--actor")
    recovery_observability.add_argument("--correlation-id")
    recovery_observability.set_defaults(func=cmd_work_item_recovery_observability)

    recovery_provider_probe = recovery_commands.add_parser("provider-probe")
    recovery_provider_probe.add_argument("--failure-class", required=True)
    recovery_provider_probe.add_argument(
        "--confidence",
        choices=["confirmed", "inferred", "unknown"],
        default="inferred",
    )
    recovery_provider_probe.add_argument("--retry-after")
    recovery_provider_probe.add_argument("--next-check-at")
    recovery_provider_probe.add_argument("--unsafe", action="store_true")
    recovery_provider_probe.set_defaults(func=cmd_work_item_recovery_provider_probe)

    recovery_poller_once = recovery_commands.add_parser("poller-once")
    recovery_poller_once.add_argument("--disabled", action="store_true")
    recovery_poller_once.set_defaults(func=cmd_work_item_recovery_poller_once)

    recovery_alert = recovery_commands.add_parser("alert")
    recovery_alert_commands = recovery_alert.add_subparsers(required=True)
    recovery_alert_ack = recovery_alert_commands.add_parser("acknowledge")
    recovery_alert_ack.add_argument("--work-item-id", required=True)
    recovery_alert_ack.add_argument("--actor", required=True)
    recovery_alert_ack.add_argument("--reason", required=True)
    recovery_alert_ack.add_argument("--scope", required=True)
    recovery_alert_ack.add_argument("--expected-revision", type=int, required=True)
    recovery_alert_ack.add_argument("--correlation-id")
    recovery_alert_ack.set_defaults(func=cmd_work_item_recovery_alert_acknowledge)

    recovery_alert_silence = recovery_alert_commands.add_parser("silence")
    recovery_alert_silence.add_argument("--work-item-id", required=True)
    recovery_alert_silence.add_argument("--actor", required=True)
    recovery_alert_silence.add_argument("--reason", required=True)
    recovery_alert_silence.add_argument("--scope", required=True)
    recovery_alert_silence.add_argument("--silence-expires-at", required=True)
    recovery_alert_silence.add_argument("--expected-revision", type=int, required=True)
    recovery_alert_silence.add_argument("--correlation-id")
    recovery_alert_silence.add_argument("--human-decision-prompt", action="store_true")
    recovery_alert_silence.set_defaults(func=cmd_work_item_recovery_alert_silence)

    recovery_retry = recovery_commands.add_parser("retry")
    recovery_retry.add_argument("--work-item-id", required=True)
    recovery_retry.add_argument("--work-item-type")
    recovery_retry.add_argument("--queue-item-id")
    recovery_retry.add_argument("--lifecycle-state")
    recovery_retry.add_argument("--affected-role")
    recovery_retry.add_argument("--actor", required=True)
    recovery_retry.add_argument("--reason", required=True)
    recovery_retry.add_argument("--idempotency-key", required=True)
    recovery_retry.add_argument("--expected-revision", type=int, required=True)
    recovery_retry.add_argument("--correlation-id")
    recovery_retry.add_argument("--source-type", default="cli")
    recovery_retry.set_defaults(func=cmd_work_item_recovery_retry)

    recovery_recover = recovery_commands.add_parser("recover")
    recovery_recover.add_argument("--work-item-id", required=True)
    recovery_recover.add_argument("--work-item-type")
    recovery_recover.add_argument("--queue-item-id")
    recovery_recover.add_argument("--lifecycle-state")
    recovery_recover.add_argument("--affected-role")
    recovery_recover.add_argument("--actor", required=True)
    recovery_recover.add_argument("--reason", required=True)
    recovery_recover.add_argument("--idempotency-key", required=True)
    recovery_recover.add_argument("--expected-revision", type=int, required=True)
    recovery_recover.add_argument("--repair-confirmed", action="store_true")
    recovery_recover.add_argument("--correlation-id")
    recovery_recover.add_argument("--source-type", default="cli")
    recovery_recover.set_defaults(func=cmd_work_item_recovery_recover)

    recovery_supersede = recovery_commands.add_parser("supersede")
    recovery_supersede.add_argument("--work-item-id", required=True)
    recovery_supersede.add_argument("--replacement-work-item-id", required=True)
    recovery_supersede.add_argument("--lifecycle-state")
    recovery_supersede.add_argument("--affected-role")
    recovery_supersede.add_argument("--actor", required=True)
    recovery_supersede.add_argument("--reason", required=True)
    recovery_supersede.add_argument("--idempotency-key", required=True)
    recovery_supersede.add_argument("--expected-revision", type=int, required=True)
    recovery_supersede.add_argument("--correlation-id")
    recovery_supersede.add_argument("--source-type", default="cli")
    recovery_supersede.set_defaults(func=cmd_work_item_recovery_supersede)

    recovery_decision = recovery_commands.add_parser("mark-needs-decision")
    recovery_decision.add_argument("--work-item-id", required=True)
    recovery_decision.add_argument("--decision-owner")
    recovery_decision.add_argument("--lifecycle-state")
    recovery_decision.add_argument("--affected-role")
    recovery_decision.add_argument("--actor", required=True)
    recovery_decision.add_argument("--reason", required=True)
    recovery_decision.add_argument("--idempotency-key", required=True)
    recovery_decision.add_argument("--expected-revision", type=int, required=True)
    recovery_decision.add_argument("--correlation-id")
    recovery_decision.add_argument("--source-type", default="cli")
    recovery_decision.set_defaults(func=cmd_work_item_recovery_mark_needs_decision)

    document_manifest = subcommands.add_parser("document-manifest")
    document_manifest.add_argument("--write", action="store_true")
    document_manifest.set_defaults(func=cmd_document_manifest)

    document_library = subcommands.add_parser("document-library")
    document_library_subcommands = document_library.add_subparsers(required=True)

    document_library_validate = document_library_subcommands.add_parser("validate")
    document_library_validate.add_argument("--work-item-id")
    document_library_validate.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
    )
    document_library_validate.add_argument("--write-report")
    document_library_validate.add_argument("--correlation-id")
    document_library_validate.set_defaults(func=cmd_document_library_validate)

    document_library_migration = document_library_subcommands.add_parser(
        "migration-dry-run"
    )
    document_library_migration.add_argument("--work-item-id", required=True)
    document_library_migration.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
    )
    document_library_migration.add_argument("--migration-slice-id")
    document_library_migration.add_argument("--write-report")
    document_library_migration.add_argument("--correlation-id")
    document_library_migration.set_defaults(
        func=cmd_document_library_migration_dry_run
    )

    flow_mermaid = subcommands.add_parser("flow-mermaid")
    flow_mermaid.add_argument("--output", type=Path)
    flow_mermaid.set_defaults(func=cmd_flow_mermaid)

    flow_export = subcommands.add_parser("flow-export")
    flow_export.add_argument("--format", required=True)
    flow_export.add_argument("--work-item-id", required=True)
    flow_export.add_argument("--queue-item-id")
    flow_export.add_argument("--work-item-type", default="slice")
    flow_export.add_argument("--lifecycle-state", default="implementation")
    flow_export.add_argument("--correlation-id")
    flow_export.add_argument("--output", required=True)
    flow_export.set_defaults(func=cmd_flow_export)

    work_item_index = subcommands.add_parser("work-item-index")
    work_item_index_subcommands = work_item_index.add_subparsers(required=True)

    work_item_index_validate = work_item_index_subcommands.add_parser("validate")
    work_item_index_validate.add_argument("--work-item-id")
    work_item_index_validate.add_argument("--correlation-id")
    work_item_index_validate.set_defaults(func=cmd_work_item_index_validate)

    work_item_index_backfill = work_item_index_subcommands.add_parser("backfill")
    work_item_index_backfill.add_argument("--work-item-id")
    work_item_index_backfill.add_argument("--correlation-id")
    backfill_mode = work_item_index_backfill.add_mutually_exclusive_group()
    backfill_mode.add_argument("--dry-run", action="store_true")
    backfill_mode.add_argument("--execute", action="store_true")
    work_item_index_backfill.set_defaults(func=cmd_work_item_index_backfill)

    auth_plan = subcommands.add_parser("auth-plan")
    auth_plan.add_argument("--instance")
    auth_plan.set_defaults(func=cmd_auth_plan)

    auth_store = subcommands.add_parser("auth-store-secret")
    auth_store.add_argument("--credential", required=True)
    auth_store.add_argument(
        "--value-env",
        help="Read the secret value from this environment variable instead of stdin.",
    )
    auth_store.add_argument("--overwrite", action="store_true")
    auth_store.set_defaults(func=cmd_auth_store_secret)

    codex_login = subcommands.add_parser("codex-auth-login")
    codex_login.add_argument("--credential", required=True)
    codex_login.add_argument(
        "--device-auth",
        action="store_true",
        help="Use Codex device authentication for terminals without browser launch.",
    )
    codex_login.set_defaults(func=cmd_codex_auth_login)

    codex_status = subcommands.add_parser("codex-auth-status")
    codex_status.add_argument("--credential", required=True)
    codex_status.set_defaults(func=cmd_codex_auth_status)

    human_response = subcommands.add_parser("record-human-response")
    human_response.add_argument("--work-item-id", required=True)
    human_response.add_argument("--work-item-type", default="slice")
    human_response.add_argument("--lifecycle-state", required=True)
    human_response.add_argument("--gate-id", required=True)
    human_response.add_argument("--approval-request-id")
    human_response.add_argument("--response-request-id", required=True)
    human_response.add_argument("--responder", required=True)
    human_response.add_argument("--value", required=True)
    human_response.add_argument("--role")
    human_response.add_argument("--source", default="cli")
    human_response.add_argument("--correlation-id")
    human_response.add_argument(
        "--server-url",
        help=(
            "Control-plane base URL. Defaults to AGENTIC_MESH_CONTROL_PLANE_URL, "
            "AGENTIC_MESH_URL_ROOT, AGENTIC_MESH_STATUS_BASE_URL, "
            "AGENTIC_MESH_AUTH_ADMIN_URL base, or "
            "the configured live linuxch endpoint."
        ),
    )
    human_response.add_argument("--server-timeout", type=int, default=30)
    human_response.add_argument(
        "--local-state",
        action="store_true",
        help="Bypass the live control plane and enqueue into the configured local state root.",
    )
    human_response.set_defaults(func=cmd_record_human_response)

    safe_output = subcommands.add_parser("safe-output")
    safe_output.add_argument("tool")
    safe_output.add_argument(
        "stdin_marker",
        help="Use `.` to read a JSON object payload from stdin.",
    )
    safe_output.set_defaults(func=cmd_safe_output)

    agent_loop = subcommands.add_parser("agent-loop")
    agent_loop.add_argument("--instance", required=True)
    agent_loop.add_argument("--poll-seconds", type=int, default=5)
    agent_loop.add_argument(
        "--no-reclaim-existing-claims-on-start",
        action="store_false",
        dest="reclaim_existing_claims_on_start",
        help=(
            "Disable startup recovery of this role instance's claims that "
            "predate the current agent-loop process."
        ),
    )
    agent_loop.add_argument(
        "--claim-lease-seconds",
        type=int,
        default=int(os.environ.get("AGENTIC_MESH_CLAIM_LEASE_SECONDS", "21600")),
        help=(
            "Requeue this role instance's claimed messages after this many "
            "seconds without completion. Set 0 to disable."
        ),
    )
    agent_loop.set_defaults(func=cmd_agent_loop)

    agent_reload = subcommands.add_parser("request-agent-reload")
    agent_reload.add_argument(
        "--instance",
        required=True,
        help="Role instance id to reload after its current iteration, or `all`.",
    )
    agent_reload.add_argument("--reason", default="operator requested reload")
    agent_reload.set_defaults(func=cmd_request_agent_reload)

    control_loop = subcommands.add_parser("control-plane-loop")
    control_loop.add_argument("--idle-grace-seconds", type=int, default=300)
    control_loop.add_argument("--poll-seconds", type=int, default=5)
    control_loop.add_argument("--auth-admin-host", default="127.0.0.1")
    control_loop.add_argument("--auth-admin-port", type=int, default=0)
    control_loop.set_defaults(func=cmd_control_plane_loop)

    auth_admin = subcommands.add_parser("auth-admin-server")
    auth_admin.add_argument("--host", default="127.0.0.1")
    auth_admin.add_argument("--port", type=int, default=8080)
    auth_admin.set_defaults(func=cmd_auth_admin_server)

    router_loop = subcommands.add_parser("router-loop")
    router_loop.add_argument("--poll-seconds", type=int, default=10)
    router_loop.set_defaults(func=cmd_router_loop)

    teams_once = subcommands.add_parser("teams-connector-once")
    teams_once.add_argument("--channel", default="approvals")
    teams_once.add_argument("--connector-id", default="local-teams-connector")
    teams_once.set_defaults(func=cmd_teams_connector_once)

    teams_loop = subcommands.add_parser("teams-connector-loop")
    teams_loop.add_argument("--channel", default="approvals")
    teams_loop.add_argument("--connector-id", default="local-teams-connector")
    teams_loop.add_argument("--poll-seconds", type=int, default=5)
    teams_loop.set_defaults(func=cmd_teams_connector_loop)

    teams_graph_once = subcommands.add_parser("teams-graph-connector-once")
    teams_graph_once.add_argument("--connector", default="teams")
    teams_graph_once.add_argument("--channel", default="all")
    teams_graph_once.add_argument("--connector-id", default="teams-graph-connector")
    teams_graph_once.add_argument("--token")
    teams_graph_once.add_argument("--token-file", type=Path)
    teams_graph_once.add_argument("--tenant-id")
    teams_graph_once.add_argument("--client-id")
    teams_graph_once.add_argument("--client-secret")
    teams_graph_once.add_argument("--client-secret-file", type=Path)
    teams_graph_once.set_defaults(func=cmd_teams_graph_connector_once)

    teams_graph_loop = subcommands.add_parser("teams-graph-connector-loop")
    teams_graph_loop.add_argument("--connector", default="teams")
    teams_graph_loop.add_argument("--channel", default="all")
    teams_graph_loop.add_argument("--connector-id", default="teams-graph-connector")
    teams_graph_loop.add_argument("--token")
    teams_graph_loop.add_argument("--token-file", type=Path)
    teams_graph_loop.add_argument("--tenant-id")
    teams_graph_loop.add_argument("--client-id")
    teams_graph_loop.add_argument("--client-secret")
    teams_graph_loop.add_argument("--client-secret-file", type=Path)
    teams_graph_loop.add_argument("--poll-seconds", type=int, default=5)
    teams_graph_loop.set_defaults(func=cmd_teams_graph_connector_loop)

    teams_graph_ingress_once = subcommands.add_parser("teams-graph-ingress-once")
    teams_graph_ingress_once.add_argument("--connector", default="teams")
    teams_graph_ingress_once.add_argument("--channel", default="all-agents")
    teams_graph_ingress_once.add_argument("--connector-id", default="teams-graph-ingress")
    teams_graph_ingress_once.add_argument("--token")
    teams_graph_ingress_once.add_argument("--token-file", type=Path)
    teams_graph_ingress_once.add_argument("--tenant-id")
    teams_graph_ingress_once.add_argument("--client-id")
    teams_graph_ingress_once.add_argument("--client-secret")
    teams_graph_ingress_once.add_argument("--client-secret-file", type=Path)
    teams_graph_ingress_once.add_argument("--max-messages", type=int, default=25)
    teams_graph_ingress_once.set_defaults(func=cmd_teams_graph_ingress_once)

    teams_graph_ingress_loop = subcommands.add_parser("teams-graph-ingress-loop")
    teams_graph_ingress_loop.add_argument("--connector", default="teams")
    teams_graph_ingress_loop.add_argument("--channel", default="all-agents")
    teams_graph_ingress_loop.add_argument("--connector-id", default="teams-graph-ingress")
    teams_graph_ingress_loop.add_argument("--token")
    teams_graph_ingress_loop.add_argument("--token-file", type=Path)
    teams_graph_ingress_loop.add_argument("--tenant-id")
    teams_graph_ingress_loop.add_argument("--client-id")
    teams_graph_ingress_loop.add_argument("--client-secret")
    teams_graph_ingress_loop.add_argument("--client-secret-file", type=Path)
    teams_graph_ingress_loop.add_argument("--max-messages", type=int, default=25)
    teams_graph_ingress_loop.add_argument("--poll-seconds", type=int, default=10)
    teams_graph_ingress_loop.set_defaults(func=cmd_teams_graph_ingress_loop)

    teams_bot_once = subcommands.add_parser("teams-bot-connector-once")
    teams_bot_once.add_argument("--connector", default="teams")
    teams_bot_once.add_argument("--channel", default="all")
    teams_bot_once.add_argument("--connector-id", default="teams-bot-connector")
    teams_bot_once.add_argument("--secret-root", type=Path, default=state_root / "secrets")
    teams_bot_once.add_argument("--service-url", default="https://smba.trafficmanager.net/teams")
    teams_bot_once.set_defaults(func=cmd_teams_bot_connector_once)

    teams_bot_loop = subcommands.add_parser("teams-bot-connector-loop")
    teams_bot_loop.add_argument("--connector", default="teams")
    teams_bot_loop.add_argument("--channel", default="all")
    teams_bot_loop.add_argument("--connector-id", default="teams-bot-connector")
    teams_bot_loop.add_argument("--secret-root", type=Path, default=state_root / "secrets")
    teams_bot_loop.add_argument("--service-url", default="https://smba.trafficmanager.net/teams")
    teams_bot_loop.add_argument("--poll-seconds", type=int, default=5)
    teams_bot_loop.set_defaults(func=cmd_teams_bot_connector_loop)

    teams_listener = subcommands.add_parser("teams-bot-listener")
    teams_listener.add_argument("--host", default="0.0.0.0")
    teams_listener.add_argument("--port", type=int, default=3978)
    teams_listener.add_argument("--connector", default="teams")
    teams_listener.add_argument("--connector-id", default="teams-bot-listener")
    teams_listener.add_argument("--secret-root", type=Path, default=state_root / "secrets")
    teams_listener.set_defaults(func=cmd_teams_bot_listener)
    return parser


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
