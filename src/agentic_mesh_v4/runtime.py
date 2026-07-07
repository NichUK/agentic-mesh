from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable
from typing import Protocol

from agentic_mesh_v4.auto_dispatch import WorkItemDispatchContext
from agentic_mesh_v4.auto_dispatch import resolve_auto_dispatch
from agentic_mesh_v4.auto_dispatch import without_self_dispatch_targets
from agentic_mesh_v4.auto_handoff import create_auto_dispatch_handoffs
from agentic_mesh_v4.artifact_preflight import RoleArtifactPreflight
from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import CodexProtocolError
from agentic_mesh_v4.completion_gate import evaluate_completion_contract
from agentic_mesh_v4.completion_gate import resolve_completion_contract
from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.db import utc_now
from agentic_mesh_v4.evidence_contracts import evaluate_evidence_contracts
from agentic_mesh_v4.evidence_contracts import resolve_evidence_contracts
from agentic_mesh_v4.handoff_lifecycle import suppress_terminal_handoff_message
from agentic_mesh_v4.teams_delivery import PROCESSING_REACTION_GLYPH
from agentic_mesh_v4.teams_delivery import PROCESSING_REACTION_NAME
from agentic_mesh_v4.teams_delivery import MISSING_DELEGATED_GRAPH_TOKEN_REASON
from agentic_mesh_v4.teams_delivery import TeamsReplySender
from agentic_mesh_v4.teams_delivery import processing_reaction_diagnostics
from agentic_mesh_v4.watchdog import collect_dispatch_invariant_findings


class ClientFactory(Protocol):
    def __call__(self, role_id: str) -> CodexAppServerClient:
        ...


@dataclass(frozen=True)
class DispatchResult:
    message_id: str
    state: str
    thread_id: str | None = None
    turn_id: str | None = None
    error: str | None = None


class AgentTurnStillRunning(RuntimeError):
    """Raised when a started Codex turn is still running after an event read timeout."""


@dataclass(frozen=True)
class TeamsRecipientRoleMismatch:
    target_role: str
    recipient_role: str
    recipient_id: str
    recipient_name: str


class V4Runtime:
    def __init__(
        self,
        *,
        db: V4Database,
        project_config: V4ProjectConfig,
        client_factory: ClientFactory | None = None,
        teams_reply_sender: TeamsReplySender | None = None,
        document_syncer: Callable[[], object] | None = None,
        agent_config_root: str | Path | None = None,
        artifact_preflight: RoleArtifactPreflight | None = None,
    ) -> None:
        self.db = db
        self.project_config = project_config
        self.client_factory = client_factory
        self.teams_reply_sender = teams_reply_sender
        self.document_syncer = document_syncer
        self.agent_config_root = Path(agent_config_root) if agent_config_root is not None else None
        self.artifact_preflight = artifact_preflight

    def register_roles(self) -> None:
        for role in self.project_config.roles:
            self.db.upsert_role_instance(
                role_instance_id=f"{self.project_config.project_id}.{role.role_id}.1",
                role_id=role.role_id,
                display_name=role.display_name,
                service_name=role.service_name,
                authority=role.authority,
                codex_endpoint=f"ws://{role.service_name}:{role.codex_port}",
            )

    def enqueue_conversation(
        self,
        *,
        target_role: str,
        text: str,
        source: str = "teams",
        conversation_ref: str | None = None,
        thread_ref: str | None = None,
        steering: bool = False,
        payload: dict[str, object] | None = None,
    ) -> str:
        self.project_config.role(target_role)
        return self.db.enqueue_message(
            target_role=target_role,
            text=text,
            source=source,
            conversation_ref=conversation_ref,
            thread_ref=thread_ref,
            steering=steering,
            payload=dict(payload or {}),
        )

    def enqueue_or_steer_conversation(
        self,
        *,
        target_role: str,
        text: str,
        source: str = "teams",
        conversation_ref: str | None = None,
        thread_ref: str | None = None,
        steering: bool = False,
        payload: dict[str, object] | None = None,
    ) -> str:
        role = self.project_config.role(target_role)
        active = self.db.active_message_for_role(
            target_role=target_role,
            conversation_ref=conversation_ref,
        )
        force_queue = _starts_with_queue_directive(text)
        should_steer = (steering or (active is not None and bool(conversation_ref))) and not force_queue
        message_id = self.db.enqueue_message(
            target_role=target_role,
            text=text,
            source=source,
            conversation_ref=conversation_ref,
            thread_ref=thread_ref,
            steering=should_steer,
            payload=dict(payload or {}),
        )
        if not should_steer or self.client_factory is None:
            return message_id
        role_instance_id = f"{self.project_config.project_id}.{role.role_id}.1"
        thread_id = self._active_thread_id(role_instance_id)
        if not thread_id:
            return message_id
        try:
            client = self.client_factory(role.role_id)
            if not client.initialized:
                client.initialize()
            self._acknowledge_message_processing_payload(
                source=source,
                payload=dict(payload or {}),
                message_id=message_id,
                correlation_id=f"corr-{message_id}",
                role_id=role.role_id,
                role_instance_id=role_instance_id,
            )
            client.resume_thread(thread_id)
            client.steer_turn(thread_id=thread_id, text=text)
        except Exception as exc:
            self.db.mark_message_state(
                message_id,
                state="queued",
                summary=f"Steering failed; queued for normal delivery: {exc}",
            )
            return message_id
        self.db.mark_message_state(
            message_id,
            state="steered",
            summary=f"Steered into active turn for {role_instance_id}",
        )
        return message_id

    def dispatch_once(self, *, role_id: str) -> DispatchResult | None:
        role = self.project_config.role(role_id)
        role_instance_id = f"{self.project_config.project_id}.{role.role_id}.1"
        active = self.db.active_message_for_role(target_role=role.role_id)
        if active is not None:
            return None
        message = self.db.claim_next_message(role_id=role.role_id, worker_id=role_instance_id)
        if message is None:
            return None
        stale_state = suppress_terminal_handoff_message(db=self.db, message=message)
        if stale_state is not None:
            return DispatchResult(message_id=message.message_id, state=stale_state)
        preflight = self._run_artifact_preflight(
            message=message,
            role=role,
            role_instance_id=role_instance_id,
        )
        if preflight is not None:
            return preflight
        if self.client_factory is None:
            self.db.mark_message_state(
                message.message_id,
                state="failed",
                summary="No Codex app-server client factory is configured.",
            )
            return DispatchResult(message_id=message.message_id, state="failed", error="missing client factory")
        try:
            client = self.client_factory(role.role_id)
            if not client.initialized:
                client.initialize()
            self._acknowledge_message_processing(
                message=message,
                role_id=role.role_id,
                role_instance_id=role_instance_id,
            )
            thread_id = self._thread_for_role(client=client, role_instance_id=role_instance_id, role=role)
            if message.steering:
                client.steer_turn(thread_id=thread_id, text=message.text)
                turn_id = None
                state = "steered"
            else:
                turn_id = client.start_turn(thread_id=thread_id, text=message.text, model=role.model)
                state = "active_turn"
                now = utc_now()
                with self.db.connection:
                    self.db.connection.execute(
                        """
                        UPDATE role_instances
                        SET active_turn_id=?, state='active', updated_at=?
                        WHERE role_instance_id=?
                        """,
                        (turn_id, now, role_instance_id),
                    )
                    if turn_id is not None:
                        self.db.connection.execute(
                            """
                            INSERT INTO codex_turns(
                              turn_id, thread_id, message_id, status, started_at, completed_at
                            ) VALUES(?,?,?,?,?,NULL)
                            ON CONFLICT(turn_id) DO UPDATE SET
                              thread_id=excluded.thread_id,
                              message_id=excluded.message_id,
                              status=excluded.status,
                              started_at=excluded.started_at,
                              completed_at=NULL
                            """,
                            (turn_id, thread_id, message.message_id, "active", now),
                        )
            self.db.mark_message_state(message.message_id, state=state, summary=f"Delivered to {role_instance_id}")
            reply_text = self._drain_available_events(
                client=client,
                role_instance_id=role_instance_id,
                approval_policy=getattr(role, "approval_policy"),
                thread_id=thread_id,
                turn_id=turn_id,
                message_id=message.message_id,
            )
            if message.source == "teams" and reply_text.strip():
                mismatch = _teams_recipient_role_mismatch(
                    target_role=role.role_id,
                    activity=message.payload,
                    roles=self.project_config.roles,
                )
                if mismatch is not None:
                    self.db.record_message_journal(
                        message_id=message.message_id,
                        correlation_id=message.correlation_id,
                        stage="teams_recipient_role_mismatch",
                        status="failed",
                        summary=(
                            "Teams reply skipped: teams_recipient_role_mismatch; "
                            f"target_role={mismatch.target_role}; "
                            f"recipient_role={mismatch.recipient_role}; "
                            f"recipient_id={mismatch.recipient_id}; "
                            f"recipient_name={mismatch.recipient_name}."
                        ),
                        role_instance_id=role_instance_id,
                    )
                    self.db.record_agent_event(
                        role_instance_id=role_instance_id,
                        event_type="teams_recipient_role_mismatch",
                        content="Teams reply skipped because queued target_role does not match payload recipient bot identity.",
                        payload={
                            "target_role": mismatch.target_role,
                            "recipient_role": mismatch.recipient_role,
                            "recipient_id": mismatch.recipient_id,
                            "recipient_name": mismatch.recipient_name,
                        },
                        thread_id=thread_id,
                        turn_id=turn_id,
                        message_id=message.message_id,
                    )
                    raise RuntimeError(
                        "teams_recipient_role_mismatch: "
                        f"target_role={mismatch.target_role}; recipient_role={mismatch.recipient_role}"
                    )
                sender = self.teams_reply_sender or TeamsReplySender.from_env()
                delivery_id = sender.send_reply(
                    role_id=role.role_id,
                    activity=message.payload,
                    text_markdown=reply_text.strip(),
                )
                self.db.record_message_journal(
                    message_id=message.message_id,
                    correlation_id=message.correlation_id,
                    stage="reply_delivered",
                    status="delivered",
                    summary=f"Delivered Teams reply {delivery_id}",
                    role_instance_id=role_instance_id,
                )
            completion_evaluation = evaluate_completion_contract(
                db=self.db,
                contract=resolve_completion_contract(message.payload),
                role_instance_id=role_instance_id,
                message_id=message.message_id,
                turn_id=turn_id,
            )
            if completion_evaluation.state != "completed":
                now = utc_now()
                diagnostic_id = self.db.record_turn_completion_diagnostic(
                    message_id=message.message_id,
                    turn_id=turn_id,
                    role_instance_id=role_instance_id,
                    work_item_id=completion_evaluation.work_item_id,
                    state=completion_evaluation.state,
                    missing_predicates=list(completion_evaluation.missing_predicates),
                    observed_outputs=completion_evaluation.observed_outputs or {},
                    next_action=completion_evaluation.next_action,
                )
                self.db.record_message_journal(
                    message_id=message.message_id,
                    correlation_id=message.correlation_id,
                    role_instance_id=role_instance_id,
                    stage="completion_gate",
                    status=completion_evaluation.state,
                    summary=completion_evaluation.next_action,
                    payload={
                        "diagnostic_id": diagnostic_id,
                        "missing_predicates": list(completion_evaluation.missing_predicates),
                        "observed_outputs": completion_evaluation.observed_outputs or {},
                    },
                )
                self.db.record_agent_event(
                    role_instance_id=role_instance_id,
                    event_type=f"completion_gate/{completion_evaluation.state}",
                    content=completion_evaluation.next_action,
                    payload={
                        "diagnostic_id": diagnostic_id,
                        "missing_predicates": list(completion_evaluation.missing_predicates),
                        "observed_outputs": completion_evaluation.observed_outputs or {},
                    },
                    thread_id=thread_id,
                    turn_id=turn_id,
                    message_id=message.message_id,
                )
                with self.db.connection:
                    self.db.connection.execute(
                        """
                        UPDATE role_instances
                        SET active_turn_id=NULL, state='ready', updated_at=?
                        WHERE role_instance_id=?
                        """,
                        (now, role_instance_id),
                    )
                    if turn_id is not None:
                        self.db.connection.execute(
                            """
                            UPDATE codex_turns
                            SET status=?, completed_at=?
                            WHERE turn_id=?
                            """,
                            (completion_evaluation.state, now, turn_id),
                        )
                self.db.mark_message_state(
                    message.message_id,
                    state=completion_evaluation.state,
                    summary=completion_evaluation.next_action,
                )
                return DispatchResult(
                    message_id=message.message_id,
                    state=completion_evaluation.state,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
            self._record_evidence_contract_warnings(
                message=message,
                role_id=role.role_id,
                role_instance_id=role_instance_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            self._record_dispatch_invariant_findings(
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                role_instance_id=role_instance_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            now = utc_now()
            with self.db.connection:
                self.db.connection.execute(
                    """
                    UPDATE role_instances
                    SET active_turn_id=NULL, state='ready', updated_at=?
                    WHERE role_instance_id=?
                    """,
                    (now, role_instance_id),
                )
                if turn_id is not None:
                    self.db.connection.execute(
                        """
                        UPDATE codex_turns
                        SET status='completed', completed_at=?
                        WHERE turn_id=?
                        """,
                        (now, turn_id),
                    )
            self.db.mark_message_state(message.message_id, state="completed", summary=f"Completed delivery to {role_instance_id}")
            self._sync_documents_after_turn(message_id=message.message_id, correlation_id=message.correlation_id, role_instance_id=role_instance_id)
            return DispatchResult(message_id=message.message_id, state="completed", thread_id=thread_id, turn_id=turn_id)
        except AgentTurnStillRunning as exc:
            self.db.mark_message_state(
                message.message_id,
                state="active_turn",
                summary=f"Agent turn is still running for {role_instance_id}: {exc}",
            )
            return DispatchResult(
                message_id=message.message_id,
                state="active_turn",
                thread_id=self._active_thread_id(role_instance_id),
                turn_id=self._active_turn_id(role_instance_id),
                error=str(exc),
            )
        except Exception as exc:
            with self.db.connection:
                self.db.connection.execute(
                    """
                    UPDATE role_instances
                    SET active_turn_id=NULL, state='ready', updated_at=?
                    WHERE role_instance_id=?
                    """,
                    (utc_now(), role_instance_id),
                )
            if _looks_like_agent_unavailable(exc):
                self.db.mark_message_state(
                    message.message_id,
                    state="queued",
                    summary=f"Agent app-server unavailable; queued for retry: {exc}",
                )
                return DispatchResult(message_id=message.message_id, state="queued", error=str(exc))
            self.db.mark_message_state(message.message_id, state="failed", summary=str(exc))
            return DispatchResult(message_id=message.message_id, state="failed", error=str(exc))

    def _record_evidence_contract_warnings(
        self,
        *,
        message: object,
        role_id: str,
        role_instance_id: str,
        thread_id: str,
        turn_id: str | None,
    ) -> None:
        payload = getattr(message, "payload", {})
        if not isinstance(payload, dict):
            return
        contracts = resolve_evidence_contracts(
            db=self.db,
            payload=payload,
            target_role=role_id,
        )
        if not contracts:
            return
        evaluations = evaluate_evidence_contracts(
            db=self.db,
            contracts=contracts,
            payload=payload,
            role_instance_id=role_instance_id,
            message_id=getattr(message, "message_id"),
            turn_id=turn_id,
        )
        for evaluation in evaluations:
            if not evaluation.missing or evaluation.contract.enforcement != "warn":
                continue
            next_action = next(
                (
                    str(item.get("remediation") or item.get("next_action") or "")
                    for item in evaluation.missing_predicates
                    if item.get("remediation") or item.get("next_action")
                ),
                "Record required evidence before completing this lifecycle state.",
            )
            diagnostic_id = self.db.record_turn_completion_diagnostic(
                message_id=getattr(message, "message_id"),
                turn_id=turn_id,
                role_instance_id=role_instance_id,
                work_item_id=evaluation.work_item_id,
                state="completed_with_missing_evidence_warning",
                missing_predicates=list(evaluation.missing_predicates),
                observed_outputs=evaluation.observed_outputs,
                next_action=next_action,
            )
            self.db.record_message_journal(
                message_id=getattr(message, "message_id"),
                correlation_id=getattr(message, "correlation_id"),
                role_instance_id=role_instance_id,
                stage="evidence_contract",
                status="completed_with_missing_evidence_warning",
                summary=next_action,
                payload={
                    "diagnostic_id": diagnostic_id,
                    "contract_id": evaluation.contract.contract_id,
                    "missing_predicates": list(evaluation.missing_predicates),
                },
            )
            self.db.record_agent_event(
                role_instance_id=role_instance_id,
                event_type="evidence_contract/completed_with_missing_evidence_warning",
                content=next_action,
                payload={
                    "diagnostic_id": diagnostic_id,
                    "contract_id": evaluation.contract.contract_id,
                    "missing_predicates": list(evaluation.missing_predicates),
                },
                thread_id=thread_id,
                turn_id=turn_id,
                message_id=getattr(message, "message_id"),
            )

    def _run_artifact_preflight(self, *, message: object, role: object, role_instance_id: str) -> DispatchResult | None:
        if self.artifact_preflight is None:
            return None
        outcome = self.artifact_preflight.run(db=self.db, message=message, role_instance_id=role_instance_id)
        if outcome.passed:
            self.db.record_message_journal(
                message_id=getattr(message, "message_id"),
                correlation_id=getattr(message, "correlation_id"),
                role_instance_id=role_instance_id,
                stage="artifact_preflight",
                status="passed",
                summary="Role artifact workspace preflight passed.",
                payload={
                    "required_path": outcome.required_path,
                    "canonical_path": outcome.canonical_path,
                    "checks": [check.check_name for check in outcome.checks],
                },
            )
            return None
        state = outcome.message_state or "blocked_preflight"
        self.db.mark_message_state(
            getattr(message, "message_id"),
            state=state,
            summary=outcome.summary,
        )
        self.db.record_agent_event(
            role_instance_id=role_instance_id,
            event_type=f"artifact_preflight/{state}",
            content=outcome.summary,
            payload={
                "required_path": outcome.required_path,
                "canonical_path": outcome.canonical_path,
                "work_item_id": outcome.work_item_id,
                "handoff_id": outcome.handoff_id,
            },
            message_id=getattr(message, "message_id"),
        )
        return DispatchResult(message_id=getattr(message, "message_id"), state=state, error=outcome.summary)

    def _record_dispatch_invariant_findings(
        self,
        *,
        message_id: str,
        correlation_id: str,
        role_instance_id: str,
        thread_id: str,
        turn_id: str | None,
    ) -> list[object]:
        findings = collect_dispatch_invariant_findings(db=self.db, exclude_message_ids={message_id})
        finding_keys_before_dispatch = {
            str(getattr(finding, "finding_key", ""))
            for finding in findings
            if getattr(finding, "finding_type", "") == "planned_not_dispatched"
        }
        findings = self._auto_dispatch_planned_findings(
            findings=findings,
            source_message_id=message_id,
            source_correlation_id=correlation_id,
            role_instance_id=role_instance_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        remaining_finding_keys = {
            str(getattr(finding, "finding_key", ""))
            for finding in findings
            if getattr(finding, "finding_type", "") == "planned_not_dispatched"
        }
        _resolve_repaired_dispatch_findings(
            db=self.db,
            repaired_finding_keys=finding_keys_before_dispatch - remaining_finding_keys,
        )
        if not findings:
            return findings
        sweep_run_id = self.db.start_watchdog_sweep(initiator_role="platform-engineer", mode="runtime_completion")
        for finding in findings:
            self.db.upsert_watchdog_finding(
                sweep_run_id=sweep_run_id,
                finding_key=finding.finding_key,
                finding_type=finding.finding_type,
                severity=finding.severity,
                work_item_id=finding.work_item_id,
                handoff_id=finding.handoff_id,
                message_id=finding.message_id,
                target_role=finding.target_role,
                owner_role=finding.owner_role,
                evidence={**(finding.evidence or {}), "source_message_id": message_id},
                next_action=finding.next_action,
            )
        summary = {
            "findings": len(findings),
            "counts_by_type": _counts_by_attribute(findings, "finding_type"),
            "counts_by_severity": _counts_by_attribute(findings, "severity"),
        }
        self.db.finish_watchdog_sweep(sweep_run_id=sweep_run_id, status="completed", summary=summary)
        return findings

    def _auto_dispatch_planned_findings(
        self,
        *,
        findings: list[object],
        source_message_id: str,
        source_correlation_id: str,
        role_instance_id: str,
        thread_id: str,
        turn_id: str | None,
    ) -> list[object]:
        remaining: list[object] = []
        source_row = self.db.connection.execute(
            "SELECT payload_json FROM message_queue WHERE message_id=?",
            (source_message_id,),
        ).fetchone()
        source_payload = {}
        if source_row is not None:
            source_payload = _json_mapping(source_row["payload_json"])
        from_role = _role_from_instance_id(role_instance_id)
        for finding in findings:
            if getattr(finding, "finding_type", "") != "planned_not_dispatched":
                remaining.append(finding)
                continue
            context = _dispatch_context_from_finding(finding)
            if context is None:
                remaining.append(finding)
                continue
            resolution = resolve_auto_dispatch(payload=source_payload, work_item=context)
            resolution = without_self_dispatch_targets(resolution=resolution, source_role=from_role)
            if not resolution.should_dispatch:
                remaining.append(finding)
                continue
            try:
                create_auto_dispatch_handoffs(
                    db=self.db,
                    project_id=self.project_config.project_id,
                    from_role=from_role,
                    work_item=context,
                    resolution=resolution,
                    source_message_id=source_message_id,
                    source_turn_id=turn_id,
                    source_thread_id=thread_id,
                    source_correlation_id=source_correlation_id,
                )
            except Exception as exc:  # noqa: BLE001 - unresolved findings keep existing diagnostic path.
                self.db.record_agent_event(
                    role_instance_id=role_instance_id,
                    event_type="auto_dispatch/handoff_failed",
                    content=str(exc),
                    payload={
                        "finding_key": getattr(finding, "finding_key", ""),
                        "work_item_id": getattr(finding, "work_item_id", None),
                        "target_role": getattr(finding, "target_role", None),
                    },
                    thread_id=thread_id,
                    turn_id=turn_id,
                    message_id=source_message_id,
                )
                remaining.append(finding)
        return remaining

    def _acknowledge_message_processing(
        self,
        *,
        message: object,
        role_id: str,
        role_instance_id: str,
    ) -> None:
        if getattr(message, "source") != "teams":
            return
        payload = getattr(message, "payload")
        if not isinstance(payload, dict):
            return
        self._acknowledge_message_processing_payload(
            source=getattr(message, "source"),
            payload=payload,
            message_id=getattr(message, "message_id"),
            correlation_id=getattr(message, "correlation_id"),
            role_id=role_id,
            role_instance_id=role_instance_id,
        )

    def _acknowledge_message_processing_payload(
        self,
        *,
        source: str,
        payload: dict[str, object],
        message_id: str,
        correlation_id: str,
        role_id: str,
        role_instance_id: str,
    ) -> None:
        if source != "teams":
            return
        sender = self.teams_reply_sender or TeamsReplySender.from_env()
        diagnostics = processing_reaction_diagnostics(payload)
        try:
            delivery_id = sender.add_processing_reaction(
                role_id=role_id,
                activity=payload,
            )
        except Exception as exc:  # noqa: BLE001 - acknowledgement must not block processing.
            self.db.record_message_journal(
                message_id=message_id,
                correlation_id=correlation_id,
                role_instance_id=role_instance_id,
                stage="processing_reaction",
                status="failed",
                summary=f"Processing reaction failed: {exc}",
            )
            self.db.record_agent_event(
                role_instance_id=role_instance_id,
                event_type="processing_reaction/failed",
                content=str(exc),
                payload={
                    "error": str(exc),
                    "reaction": PROCESSING_REACTION_NAME,
                    "route_type": diagnostics.route_type,
                    "message_id": diagnostics.message_id,
                },
                message_id=message_id,
            )
            return
        if delivery_id is None:
            unsupported_reason = diagnostics.unsupported_reason or MISSING_DELEGATED_GRAPH_TOKEN_REASON
            self.db.record_message_journal(
                message_id=message_id,
                correlation_id=correlation_id,
                role_instance_id=role_instance_id,
                stage="processing_reaction",
                status="unsupported",
                summary=(
                    "Processing reaction skipped: "
                    f"reason={unsupported_reason}; route_type={diagnostics.route_type}; "
                    f"message_id={diagnostics.message_id or 'unknown'}."
                ),
            )
            return
        self.db.record_message_journal(
            message_id=message_id,
            correlation_id=correlation_id,
            role_instance_id=role_instance_id,
            stage="processing_reaction",
            status="delivered",
            summary=(
                f"Delivered Teams processing reaction {PROCESSING_REACTION_NAME} {PROCESSING_REACTION_GLYPH}: "
                f"{delivery_id}; route_type={diagnostics.route_type}; "
                f"message_id={diagnostics.message_id or 'unknown'}"
            ),
        )

    def _sync_documents_after_turn(self, *, message_id: str, correlation_id: str, role_instance_id: str) -> None:
        if self.document_syncer is None:
            return
        try:
            result = self.document_syncer()
        except Exception as exc:  # noqa: BLE001 - sync must not fail the completed agent turn.
            self.db.record_message_journal(
                message_id=message_id,
                correlation_id=correlation_id,
                role_instance_id=role_instance_id,
                stage="document_sync",
                status="failed",
                summary=f"Document sync failed after agent turn: {exc}",
            )
            self.db.record_agent_event(
                role_instance_id=role_instance_id,
                event_type="document_sync/failed",
                content=str(exc),
                message_id=message_id,
            )
            return
        uploaded = getattr(result, "uploaded", None)
        folders_created = getattr(result, "folders_created", None)
        root_path = getattr(result, "root_path", "")
        summary = "Document sync completed"
        if uploaded is not None and folders_created is not None:
            summary = f"Document sync completed: uploaded {uploaded}, folders created {folders_created}, root {root_path}"
        self.db.record_message_journal(
            message_id=message_id,
            correlation_id=correlation_id,
            role_instance_id=role_instance_id,
            stage="document_sync",
            status="completed",
            summary=summary,
        )

    def _thread_for_role(self, *, client: CodexAppServerClient, role_instance_id: str, role: object) -> str:
        agent_config_hash = self._agent_config_hash(role_id=getattr(role, "role_id"))
        snapshot = self.db.snapshot()
        for item in snapshot["roles"]:
            if item["role_instance_id"] == role_instance_id and item.get("active_thread_id"):
                thread_id = str(item["active_thread_id"])
                if self._thread_matches_role(
                    thread_id=thread_id,
                    role_instance_id=role_instance_id,
                    role=role,
                    agent_config_hash=agent_config_hash,
                ):
                    client.resume_thread(thread_id)
                    return thread_id
                self._retire_thread(role_instance_id=role_instance_id, thread_id=thread_id)
        thread_id = client.start_thread(
            model=getattr(role, "model"),
            sandbox_mode=getattr(role, "sandbox_mode"),
            approval_policy=getattr(role, "approval_policy"),
        )
        now = utc_now()
        with self.db.connection:
            self.db.connection.execute(
                """
                UPDATE role_instances
                SET active_thread_id=?, state='ready', updated_at=?
                WHERE role_instance_id=?
                """,
                (thread_id, now, role_instance_id),
            )
            self.db.connection.execute(
                """
                INSERT INTO codex_threads(
                  thread_id, role_instance_id, agent_config_hash, sandbox_mode, approval_policy, status, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(thread_id) DO NOTHING
                """,
                (
                    thread_id,
                    role_instance_id,
                    agent_config_hash,
                    getattr(role, "sandbox_mode"),
                    getattr(role, "approval_policy"),
                    "active",
                    now,
                    now,
                ),
            )
        return thread_id

    def _active_thread_id(self, role_instance_id: str) -> str | None:
        row = self.db.connection.execute(
            """
            SELECT active_thread_id
            FROM role_instances
            WHERE role_instance_id=?
            """,
            (role_instance_id,),
        ).fetchone()
        if row is None or row["active_thread_id"] is None:
            return None
        return str(row["active_thread_id"])

    def _thread_matches_role(
        self,
        *,
        thread_id: str,
        role_instance_id: str,
        role: object,
        agent_config_hash: str,
    ) -> bool:
        row = self.db.connection.execute(
            """
            SELECT sandbox_mode, approval_policy, agent_config_hash, status
            FROM codex_threads
            WHERE thread_id=? AND role_instance_id=?
            """,
            (thread_id, role_instance_id),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["status"]) == "active"
            and row["sandbox_mode"] == getattr(role, "sandbox_mode")
            and row["approval_policy"] == getattr(role, "approval_policy")
            and (row["agent_config_hash"] or "") == agent_config_hash
        )

    def _agent_config_hash(self, *, role_id: str) -> str:
        digest = hashlib.sha256()
        if self.agent_config_root is not None:
            role_root = self.agent_config_root / role_id / "1"
            for filename in ("AGENTS.md", "container.json"):
                path = role_root / filename
                if path.exists():
                    digest.update(filename.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
            if digest.digest() != hashlib.sha256().digest():
                return digest.hexdigest()
        role = self.project_config.role(role_id)
        digest.update(role.role_id.encode("utf-8"))
        digest.update(str(role.sandbox_mode).encode("utf-8"))
        digest.update(str(role.approval_policy).encode("utf-8"))
        digest.update(str(role.model).encode("utf-8"))
        digest.update(str(role.reasoning_effort).encode("utf-8"))
        return digest.hexdigest()

    def _retire_thread(self, *, role_instance_id: str, thread_id: str) -> None:
        now = utc_now()
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE codex_threads SET status='retired', updated_at=? WHERE thread_id=?",
                (now, thread_id),
            )
            self.db.connection.execute(
                """
                UPDATE role_instances
                SET active_thread_id=NULL, active_turn_id=NULL, updated_at=?
                WHERE role_instance_id=? AND active_thread_id=?
                """,
                (now, role_instance_id, thread_id),
            )

    def _drain_available_events(
        self,
        *,
        client: CodexAppServerClient,
        role_instance_id: str,
        approval_policy: str,
        thread_id: str,
        turn_id: str | None,
        message_id: str,
    ) -> str:
        reply_parts: list[str] = []
        while True:
            try:
                event = client.receive_event()
            except Exception as exc:
                if _looks_like_receive_timeout(exc):
                    event_type = "turn/readTimeoutAfterOutput" if reply_parts else "turn/readTimeoutStillRunning"
                    resolved_turn_id = turn_id or self._active_turn_id(role_instance_id)
                    self.db.record_agent_event(
                        role_instance_id=role_instance_id,
                        event_type=event_type,
                        content=str(exc),
                        payload={"error": str(exc)},
                        thread_id=thread_id,
                        turn_id=resolved_turn_id,
                        message_id=message_id,
                    )
                    raise AgentTurnStillRunning(
                        f"no app-server event before read timeout; leaving turn {resolved_turn_id or '<unknown>'} active"
                    ) from exc
                raise
            if event is None:
                return "".join(reply_parts)
            method = str(event.get("method") or "unknown")
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            content = _event_content(method, params)
            if method == "item/agentMessage/delta":
                reply_parts.append(content)
            if _should_auto_accept_server_request(event=event, approval_policy=approval_policy):
                client.respond_to_server_request(request_id=event["id"], result={"decision": "accept"})
                self.db.record_agent_event(
                    role_instance_id=role_instance_id,
                    event_type=f"{method}/autoAccepted",
                    content="Auto-accepted server approval request for approval_policy=never.",
                    payload={"request_id": event["id"], "method": method},
                    thread_id=thread_id,
                    turn_id=turn_id,
                    message_id=message_id,
                )
            self.db.record_agent_event(
                role_instance_id=role_instance_id,
                event_type=method,
                content=content,
                payload=event,
                thread_id=thread_id,
                turn_id=turn_id,
                message_id=message_id,
            )
            if method == "turn/completed":
                return "".join(reply_parts)


def _event_content(method: str, params: dict[str, object]) -> str:
    for key in ("text", "delta", "message", "summary"):
        value = params.get(key)
        if isinstance(value, str):
            return value
    return method


def _should_auto_accept_server_request(*, event: dict[str, object], approval_policy: str) -> bool:
    if approval_policy != "never" or "id" not in event:
        return False
    method = str(event.get("method") or "")
    return method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
    }


def _looks_like_agent_unavailable(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "connection refused",
            "name or service not known",
            "temporary failure in name resolution",
            "timed out",
            "connection reset",
            "websocket",
        )
    )


def _looks_like_receive_timeout(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text


def _starts_with_queue_directive(text: str) -> bool:
    return re.match(r"^\s*queue\s*:", text, flags=re.IGNORECASE) is not None


def _counts_by_attribute(items: list[object], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(getattr(item, attribute))
        counts[value] = counts.get(value, 0) + 1
    return counts


def _dispatch_context_from_finding(finding: object) -> WorkItemDispatchContext | None:
    work_item_id = _string_value(getattr(finding, "work_item_id", None))
    owner_role = _string_value(getattr(finding, "owner_role", None))
    evidence = getattr(finding, "evidence", None)
    evidence = evidence if isinstance(evidence, dict) else {}
    state = _string_value(evidence.get("state"))
    if work_item_id is None or state is None:
        return None
    return WorkItemDispatchContext(
        work_item_id=work_item_id,
        state=state,
        owner_role=owner_role,
        next_action=_string_value(evidence.get("next_action")) or "",
    )


def _json_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _role_from_instance_id(role_instance_id: str) -> str:
    parts = role_instance_id.split(".")
    if len(parts) >= 3:
        return parts[-2]
    return role_instance_id


def _resolve_repaired_dispatch_findings(*, db: V4Database, repaired_finding_keys: set[str]) -> None:
    keys = {key for key in repaired_finding_keys if key}
    if not keys:
        return
    now = utc_now()
    with db.connection:
        for key in sorted(keys):
            db.connection.execute(
                """
                UPDATE watchdog_findings
                SET state='resolved', updated_at=?, resolved_at=?
                WHERE finding_key=? AND state='open'
                """,
                (now, now, key),
            )


def _teams_recipient_role_mismatch(
    *,
    target_role: str,
    activity: dict[str, object],
    roles: Iterable[object],
) -> TeamsRecipientRoleMismatch | None:
    recipient = activity.get("recipient")
    if not isinstance(recipient, dict):
        return None
    recipient_role = _teams_recipient_role(activity=activity, roles=roles)
    if recipient_role is None or recipient_role == target_role:
        return None
    return TeamsRecipientRoleMismatch(
        target_role=_sanitize_diagnostic_value(target_role),
        recipient_role=_sanitize_diagnostic_value(recipient_role),
        recipient_id=_sanitize_diagnostic_value(recipient.get("id")),
        recipient_name=_sanitize_diagnostic_value(recipient.get("name")),
    )


def _teams_recipient_role(*, activity: dict[str, object], roles: Iterable[object]) -> str | None:
    recipient = activity.get("recipient")
    if not isinstance(recipient, dict):
        return None
    recipient_id = _string_value(recipient.get("id"))
    recipient_name = _normalise_role_label(_string_value(recipient.get("name")) or "")
    for role in roles:
        role_id = str(getattr(role, "role_id", ""))
        display_name = str(getattr(role, "display_name", ""))
        if recipient_name and recipient_name in {
            _normalise_role_label(display_name),
            _normalise_role_label(f"AM-{display_name}"),
            _normalise_role_label(role_id),
            _normalise_role_label(role_id.replace("-", " ")),
        }:
            return role_id
        env_role = role_id.upper().replace("-", "_")
        app_id = _string_value(os.environ.get(f"TEAMS_BOT_{env_role}_APP_ID"))
        if app_id and recipient_id in {app_id, f"28:{app_id}"}:
            return role_id
    return None


def _normalise_role_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _string_value(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _sanitize_diagnostic_value(value: object, *, limit: int = 120) -> str:
    text = value if isinstance(value, str) else ""
    text = "".join(char if char.isprintable() else "?" for char in text.strip())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text or "unknown"
