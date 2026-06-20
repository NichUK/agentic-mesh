from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from agentic_mesh_v3.authority import ToolAuthorityPolicy
from agentic_mesh_v3.authority import role_from_instance
from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.broker_diagnostics import broker_inspection_payload
from agentic_mesh_v3.broker_diagnostics import role_consumer_name
from agentic_mesh_v3.connectors import OutboundMessage
from agentic_mesh_v3.connectors import StakeholderBridge
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.deployment import DeploymentTarget
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.documents import DocumentRef
from agentic_mesh_v3.documents import GovernanceRegisterItem
from agentic_mesh_v3.documents import WorkItemIndex
from agentic_mesh_v3.documents import validate_framework_artifact_path
from agentic_mesh_v3.documents import write_root_work_item_index
from agentic_mesh_v3.documents import write_governance_register
from agentic_mesh_v3.documents import write_work_item_index
from agentic_mesh_v3.lifecycle import ComposeLifecycleConfig
from agentic_mesh_v3.lifecycle import ComposeLifecycleExecutor
from agentic_mesh_v3.lifecycle import HibernationPolicy
from agentic_mesh_v3.lifecycle import LifecycleCommandResult
from agentic_mesh_v3.lifecycle import LifecycleCommandRunner
from agentic_mesh_v3.lifecycle import LifecycleDecision
from agentic_mesh_v3.lifecycle import plan_lifecycle_actions
from agentic_mesh_v3.lifecycle import reconcile_agent_statuses_with_compose
from agentic_mesh_v3.lifecycle import refresh_agent_statuses_from_broker
from agentic_mesh_v3.lifecycle import running_compose_services
from agentic_mesh_v3.observability import V3Telemetry
from agentic_mesh_v3.observability import get_telemetry
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.sweeps import ProjectSweepService
from agentic_mesh_v3.tool_contracts import TERMINAL_TOOLS
from agentic_mesh_v3.tool_contracts import validate_tool_required_fields


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool_name: str
    terminal: bool = False
    output: dict[str, Any] | None = None


class V3ToolService:
    """V3 safe-output style tools.

    Tools record durable intent/effects. Agents decide which tools to call;
    the runtime service validates and projects those calls for reporting.
    """

    def __init__(
        self,
        db: V3Database,
        document_library: DocumentLibraryAdapter | None = None,
        deployment_targets: dict[str, DeploymentTarget] | None = None,
        stakeholder_bridge: StakeholderBridge | None = None,
        broker: BrokerAdapter | None = None,
        broker_stream: str | None = None,
        authority_policy: ToolAuthorityPolicy | None = None,
        telemetry: V3Telemetry | None = None,
        configured_role_instance_ids: tuple[str, ...] = (),
        lifecycle_config: ComposeLifecycleConfig | None = None,
        lifecycle_runner: LifecycleCommandRunner | None = None,
    ) -> None:
        self.db = db
        self.document_library = document_library
        self.deployment_targets = deployment_targets or {}
        self.stakeholder_bridge = stakeholder_bridge
        self.broker = broker
        self.broker_stream = broker_stream
        self.authority_policy = authority_policy or ToolAuthorityPolicy.default()
        self.telemetry = telemetry or get_telemetry()
        self.configured_role_instance_ids = configured_role_instance_ids
        self.lifecycle_config = lifecycle_config
        self.lifecycle_runner = lifecycle_runner

    def call(
        self,
        *,
        role_instance_id: str,
        tool_name: str,
        payload: dict[str, Any],
        terminal: bool | None = None,
    ) -> ToolResult:
        tool_name = _canonical_tool_name(tool_name)
        payload = dict(payload)
        if tool_name == "noop" and not payload.get("reason"):
            payload["reason"] = "No durable action was applicable for this assignment."
        if tool_name == "decision.record" and not payload.get("summary"):
            payload["summary"] = _decision_summary(payload)
        if tool_name == "stakeholder.ask_question" and not payload.get("record_id"):
            payload["record_id"] = f"question-{uuid4().hex}"
        with self.telemetry.span(
            "v3.tool_call",
            role_instance_id=role_instance_id,
            tool_name=tool_name,
            terminal=bool(terminal),
        ):
            try:
                self.authority_policy.assert_allowed(role_instance_id=role_instance_id, tool_name=tool_name)
                validate_tool_required_fields(tool_name, payload)
                self._validate_runtime_dependencies(tool_name=tool_name, payload=payload)
                call_id = f"call-{uuid4().hex}"
                is_terminal = bool(terminal) or tool_name in TERMINAL_TOOLS
                self.db.record_tool_call(
                    call_id=call_id,
                    role_instance_id=role_instance_id,
                    tool_name=tool_name,
                    payload=payload,
                    terminal=is_terminal,
                )
                output = self._apply_effect(
                    call_id=call_id,
                    role_instance_id=role_instance_id,
                    tool_name=tool_name,
                    payload=payload,
                )
            except Exception as exc:
                self.telemetry.increment(
                    "agentic_mesh_v3_tool_calls",
                    attributes={"tool.name": tool_name, "role.instance": role_instance_id, "status": "error"},
                )
                self.telemetry.log_event(
                    "v3.tool_call.failed",
                    level=logging.ERROR,
                    role_instance_id=role_instance_id,
                    tool_name=tool_name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            self.telemetry.increment(
                "agentic_mesh_v3_tool_calls",
                attributes={"tool.name": tool_name, "role.instance": role_instance_id, "status": "ok"},
            )
            self.telemetry.log_event(
                "v3.tool_call.recorded",
                role_instance_id=role_instance_id,
                tool_name=tool_name,
                terminal=is_terminal,
            )
            return ToolResult(call_id=call_id, tool_name=tool_name, terminal=is_terminal, output=output)

    def _validate_runtime_dependencies(self, *, tool_name: str, payload: dict[str, Any]) -> None:
        if tool_name == "messaging.send" and self.stakeholder_bridge is None:
            raise ValueError("stakeholder bridge is not configured")
        if tool_name == "status.reply":
            target_ref = _optional(payload.get("target_ref")) or _optional(payload.get("reply_target_ref"))
            if target_ref is not None and self.stakeholder_bridge is None:
                raise ValueError("stakeholder bridge is not configured")
        if tool_name == "stakeholder.ask_question":
            should_deliver = _optional(payload.get("target_ref") or payload.get("stakeholder_ref")) is not None
            if should_deliver and self.stakeholder_bridge is None:
                raise ValueError("stakeholder bridge is not configured")
        if tool_name == "approval.request":
            target_ref = _optional(payload.get("target_ref"))
            if target_ref is not None and self.stakeholder_bridge is None:
                raise ValueError("stakeholder bridge is not configured")
        if tool_name == "release.deploy":
            target_id = _required(payload, "target_id")
            if target_id not in self.deployment_targets:
                raise ValueError(f"deployment target is not configured: {target_id}")
        if tool_name == "runtime.sweep.request":
            if self.broker is None or self.broker_stream is None:
                raise ValueError("broker is not configured")
        if tool_name == "runtime.broker.inspect":
            if self.broker is None or self.broker_stream is None:
                raise ValueError("broker is not configured")
        if tool_name == "runtime.lifecycle.request":
            if self.lifecycle_config is None:
                raise ValueError("lifecycle config is not configured")
        if tool_name == "agent.delegate":
            if self.broker is None or self.broker_stream is None:
                raise ValueError("broker is not configured")
        if tool_name == "runtime.status.inspect":
            return
        if tool_name in {"document.write_artifact", "document.write_work_item_index", "document.write_root_work_item_index"}:
            if self.document_library is None:
                raise ValueError("document library is not configured")

    def _apply_effect(
        self, *, call_id: str, role_instance_id: str, tool_name: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if tool_name == "backlog.upsert":
            self.db.upsert_backlog_item(
                queue_item_id=_required(payload, "queue_item_id"),
                title=_required(payload, "title"),
                summary=_required(payload, "summary"),
                status=str(payload.get("status") or "queued"),
                owner_role=_required(payload, "owner_role"),
                linked_work_item_id=_optional(payload.get("linked_work_item_id")),
                source_ref=_optional(payload.get("source_ref")),
                correlation_id=_optional(payload.get("correlation_id")),
                trace_id=_optional(payload.get("trace_id")),
                created_by_role_instance=role_instance_id,
                origin_message_id=_optional(payload.get("source_message_id") or payload.get("message_id")),
            )
        elif tool_name == "agent.delegate":
            return self._delegate_agent(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "work_item.upsert":
            owner_role = _required(payload, "owner_role")
            state = str(payload.get("state") or "queued")
            current_phase = _optional(payload.get("current_phase"))
            self.db.upsert_work_item(
                work_item_id=_required(payload, "work_item_id"),
                queue_item_id=_optional(payload.get("queue_item_id")),
                title=_required(payload, "title"),
                description=_required(payload, "description"),
                state=state,
                owner_role=owner_role,
                current_phase=current_phase,
                next_action=str(payload.get("next_action") or ""),
                governance=_work_item_governance(
                    payload.get("governance"),
                    owner_role=owner_role,
                    state=state,
                    current_phase=current_phase,
                ),
                source_ref=_optional(payload.get("source_ref")),
                correlation_id=_optional(payload.get("correlation_id")),
                trace_id=_optional(payload.get("trace_id")),
                created_by_role_instance=role_instance_id,
                origin_message_id=_optional(payload.get("source_message_id") or payload.get("message_id")),
            )
        elif tool_name == "work_item.update_state":
            self.db.update_work_item_state(
                work_item_id=_required(payload, "work_item_id"),
                state=_required(payload, "state"),
                owner_role=_optional(payload.get("owner_role")),
                current_phase=_optional(payload.get("current_phase")),
                next_action=str(payload.get("next_action") or ""),
                source_ref=_optional(payload.get("source_ref")),
                correlation_id=_optional(payload.get("correlation_id")),
                trace_id=_optional(payload.get("trace_id")),
                created_by_role_instance=role_instance_id,
                origin_message_id=_optional(payload.get("source_message_id") or payload.get("message_id")),
            )
        elif tool_name == "work_item.reopen":
            self.db.reopen_work_item(
                work_item_id=_required(payload, "work_item_id"),
                state=_required(payload, "state"),
                reason=_required(payload, "reason"),
                owner_role=str(payload.get("owner_role") or role_from_instance(role_instance_id)),
                current_phase=_optional(payload.get("current_phase")),
                next_action=str(payload.get("next_action") or ""),
            )
        elif tool_name == "agent.heartbeat":
            self.db.upsert_agent_status(
                AgentStatus(
                    role_instance_id=role_instance_id,
                    container_state=str(payload.get("container_state") or "running"),
                    heartbeat_at=_optional(payload.get("heartbeat_at")),
                    current_work=_optional(payload.get("current_work")),
                    inbox_depth=int(payload.get("inbox_depth") or 0),
                    dead_letter_depth=int(payload.get("dead_letter_depth") or 0),
                    governance_waits=tuple(str(item) for item in payload.get("governance_waits") or ()),
                )
            )
        elif tool_name == "artifact.link":
            self._link_artifact(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "document.write_artifact":
            self._write_artifact(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "document.write_work_item_index":
            self._write_work_item_index(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "document.write_root_work_item_index":
            self._write_root_work_item_index()
        elif tool_name == "approval.request":
            self._request_approval(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "conversation.compact_context":
            self._compact_conversation_context(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "release.record":
            self.db.record_release(
                release_id=str(payload.get("release_id") or f"release-{uuid4().hex}"),
                work_item_id=_required(payload, "work_item_id"),
                status=str(payload.get("status") or "recorded"),
                scope=_required(payload, "scope"),
                deployment_result=_required(payload, "deployment_result"),
                rollback_plan=_required(payload, "rollback_plan"),
                residual_risks=str(payload.get("residual_risks") or "None recorded"),
                version_ref=_required(payload, "version_ref"),
                approval_ref=_required(payload, "approval_ref"),
                smoke_evidence=_required(payload, "smoke_evidence"),
                closure_state=str(payload.get("closure_state") or "open"),
            )
        elif tool_name == "release.deploy":
            target_id = _required(payload, "target_id")
            target = self.deployment_targets.get(target_id)
            if target is None:
                raise ValueError(f"deployment target is not configured: {target_id}")
            work_item_id = _required(payload, "work_item_id")
            self.db.update_work_item_state(
                work_item_id=work_item_id,
                state="deploying",
                owner_role=role_from_instance(role_instance_id),
                current_phase="deployment",
                next_action=f"Deployment target `{target_id}` is running.",
            )
            result = target.deploy()
            release_id = str(payload.get("release_id") or f"release-{uuid4().hex}")
            smoke_evidence = str(payload.get("smoke_evidence") or result.output or "not-recorded")
            deployment_run_status = "succeeded" if result.status in {"deployed", "no_deployment"} else "failed"
            command = list(getattr(target, "command", ()) or ())
            self.db.record_deployment_target_reference(
                target_id=target_id,
                project_id=str(payload.get("project_id") or os.environ.get("AGENTIC_MESH_PROJECT_ID") or "default"),
                target_type=_deployment_target_type(target),
                service_name=target_id,
                metadata={"source": "release.deploy"},
            )
            self.db.record_release(
                release_id=release_id,
                work_item_id=work_item_id,
                status=result.status,
                scope=str(payload.get("scope") or f"Deployment target {target_id}"),
                deployment_result=result.output,
                rollback_plan=result.rollback_plan,
                residual_risks=str(payload.get("residual_risks") or "None recorded"),
                version_ref=_required(payload, "version_ref"),
                approval_ref=_required(payload, "approval_ref"),
                smoke_evidence=smoke_evidence,
                closure_state="release_disposition_recorded",
            )
            self.db.record_deployment_run(
                run_id=str(payload.get("deployment_run_id") or f"deployment-{uuid4().hex}"),
                target_id=target_id,
                work_item_id=work_item_id,
                release_id=release_id,
                status=deployment_run_status,
                command=command,
                smoke_result=smoke_evidence,
                rollback_plan=result.rollback_plan,
                evidence={
                    "deployment_status": result.status,
                    "deployment_output": result.output,
                    "approval_ref": str(payload.get("approval_ref") or ""),
                    "version_ref": str(payload.get("version_ref") or ""),
                },
            )
            if result.status == "failed":
                self.db.update_work_item_state(
                    work_item_id=work_item_id,
                    state="recovering",
                    owner_role=role_from_instance(role_instance_id),
                    current_phase="deployment",
                    next_action=f"Deployment target `{target_id}` failed. {result.output or 'No output recorded.'}",
                )
            else:
                self.db.update_work_item_state(
                    work_item_id=work_item_id,
                    state="released",
                    owner_role=role_from_instance(role_instance_id),
                    current_phase="deployment",
                    next_action=(
                        f"Release disposition `{result.status}` recorded for target `{target_id}`; "
                        "ready for closure."
                    ),
                )
        elif tool_name == "release.close":
            work_item_id = _required(payload, "work_item_id")
            if not self.db.has_release_disposition(work_item_id):
                raise ValueError(f"work item `{work_item_id}` has no deployment or no-deployment release disposition")
            detail = self.db.work_item_detail(work_item_id)
            if detail is None:
                raise ValueError(f"work item `{work_item_id}` was not found")
            is_project_closure_wait = detail.state == "waiting_agent" and detail.current_phase == "project-closure"
            if detail.state != "released" and not is_project_closure_wait:
                self.db.update_work_item_state(
                    work_item_id=work_item_id,
                    state="released",
                    owner_role=role_from_instance(role_instance_id),
                    current_phase="deployment",
                    next_action=str(payload.get("release_note") or "Release disposition recorded; ready for closure."),
                )
            self.db.update_work_item_state(
                work_item_id=work_item_id,
                state="closed",
                owner_role=str(payload.get("closure_owner_role") or "project-manager"),
                current_phase="project-closure",
                next_action=str(payload.get("closure_note") or "Release closed with deployment disposition recorded."),
            )
            self.db.update_release_closure_state(work_item_id=work_item_id, closure_state="closed")
        elif tool_name == "memory.propose_update":
            self.db.record_role_memory(
                memory_id=str(payload.get("memory_id") or f"memory-{call_id}"),
                role_instance_id=role_instance_id,
                summary=_required(payload, "summary"),
                source_ref=_required(payload, "source_ref"),
            )
        elif tool_name == "messaging.send":
            self._send_message(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "status.reply":
            self._send_optional_reply(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "stakeholder.ask_question":
            self._ask_stakeholder(
                call_id=call_id,
                role_instance_id=role_instance_id,
                tool_name=tool_name,
                payload=payload,
            )
        elif tool_name in {
            "blocker.raise",
            "handoff.require",
            "consult.request",
            "informed.update",
            "governance.record_exception",
            "decision.record",
            "risk.register",
            "relevance.record",
        }:
            self._record_governance_tool(
                call_id=call_id,
                role_instance_id=role_instance_id,
                tool_name=tool_name,
                payload=payload,
            )
        elif tool_name == "runtime.sweep.request":
            self._request_runtime_sweep(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "runtime.broker.inspect":
            return self._inspect_broker(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "runtime.lifecycle.request":
            return self._request_runtime_lifecycle(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "runtime.message_journal.inspect":
            return self._inspect_message_journal(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "runtime.status.inspect":
            return self._inspect_status(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "status.update":
            self._update_status(payload)
        elif tool_name in TERMINAL_TOOLS:
            return
        else:
            raise ValueError(f"unknown V3 tool: {tool_name}")
        return None

    def _delegate_agent(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.broker is None or self.broker_stream is None:
            raise ValueError("broker is not configured")
        target_role = _required(payload, "target_role")
        subject = f"agent.{target_role}"
        self.broker.ensure_stream(self.broker_stream, [subject])
        instance_id = str(payload.get("instance_id") or "1")
        consumer = role_consumer_name(target_role, instance_id)
        self.broker.ensure_consumer(
            self.broker_stream,
            consumer,
            filter_subject=subject,
        )
        message_payload: dict[str, object] = {
            "message_type": "agent.delegate",
            "source_call_id": call_id,
            "source_role_instance_id": role_instance_id,
            "source_role": role_from_instance(role_instance_id),
            "target_role": target_role,
            "task": _required(payload, "task"),
            "reason": _required(payload, "reason"),
            "expected_output": _required(payload, "expected_output"),
            "priority": str(payload.get("priority") or "normal"),
            "context": str(payload.get("context") or ""),
            "correlation_id": str(payload.get("correlation_id") or f"corr-{call_id}"),
        }
        for optional_key in (
            "work_item_id",
            "queue_item_id",
            "source_message_id",
            "conversation_ref",
            "reply_target_ref",
            "reply_thread_ref",
            "connector",
        ):
            value = _optional(payload.get(optional_key))
            if value is not None:
                message_payload[optional_key] = value
        message = self.broker.publish(self.broker_stream, subject, message_payload)
        event_payload = {
            "call_id": call_id,
            "target_role": target_role,
            "message_id": message.message_id,
            "broker_subject": subject,
            "task": message_payload["task"],
            "reason": message_payload["reason"],
            "expected_output": message_payload["expected_output"],
            "work_item_id": _optional(payload.get("work_item_id")),
        }
        with self.db.connection:
            self.db.record_event("agent.delegated", "agent", role_instance_id, event_payload)
        self.db.record_message_journal(
            message_id=message.message_id,
            correlation_id=str(message_payload["correlation_id"]),
            direction="broker",
            stage="published",
            status="published",
            source_ref=call_id,
            target_role=target_role,
            role_instance_id=role_instance_id,
            work_item_id=_optional(payload.get("work_item_id")),
            queue_item_id=_optional(payload.get("queue_item_id")),
            broker_subject=subject,
            broker_consumer=consumer,
            summary=f"Delegated task to {target_role}: {_truncate_for_summary(message_payload['task'])}",
            payload=message_payload,
        )
        return {"message_id": message.message_id, "target_role": target_role, "broker_subject": subject}

    def _inspect_broker(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.broker is None or self.broker_stream is None:
            raise ValueError("broker is not configured")
        limit = int(payload.get("limit") or 20)
        if limit < 1:
            raise ValueError("limit must be positive")
        role_ids = _role_ids_from_payload(payload)
        instance_id = str(payload.get("instance_id") or "1")
        consumer = _optional(payload.get("consumer"))
        role_id = _optional(payload.get("role_id"))
        if role_id:
            if consumer:
                raise ValueError("consumer and role_id cannot both be provided")
            consumer = role_consumer_name(role_id, instance_id)
            self.broker.ensure_consumer(self.broker_stream, consumer, filter_subject=f"agent.{role_id}")
            if not role_ids:
                role_ids = (role_id,)
        inspection = broker_inspection_payload(
            self.broker,
            stream=self.broker_stream,
            consumer=consumer,
            limit=limit,
            role_ids=role_ids,
            instance_id=instance_id,
        )
        event_payload = {
            "call_id": call_id,
            "reason": _required(payload, "reason"),
            "stream": self.broker_stream,
            "consumer": consumer,
            "limit": limit,
            "role_ids": list(role_ids),
            "inspection": inspection,
        }
        with self.db.connection:
            self.db.record_event("runtime.broker_inspected", "agent", role_instance_id, event_payload)
        self.db.record_message_journal(
            message_id=f"runtime-broker-inspect-{call_id}",
            correlation_id=str(payload.get("correlation_id") or f"corr-{call_id}"),
            direction="runtime",
            stage="tool_call_recorded",
            status="completed",
            target_role=role_from_instance(role_instance_id),
            role_instance_id=role_instance_id,
            summary=_broker_inspection_summary(inspection),
            payload=event_payload,
        )
        return {"inspection": inspection}

    def _inspect_status(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or _project_from_instance(role_instance_id))
        include_recent = int(payload.get("include_recent") or 5)
        if include_recent < 0:
            raise ValueError("include_recent must be zero or positive")
        snapshot = self.db.status_snapshot(
            project_id=project_id,
            configured_role_instance_ids=self.configured_role_instance_ids,
        )
        inspection = {
            "project_id": snapshot.project_id,
            "counts": {
                "backlog": len(snapshot.backlog),
                "current_work": len(snapshot.work_items),
                "recent_completions": len(snapshot.recent_completions),
                "agents": len(snapshot.agents),
                "attention_needed": sum(1 for item in snapshot.work_items if _is_attention_needed_state(item.state)),
                "blocked": sum(1 for item in snapshot.work_items if item.state == "blocked"),
                "governance_waits": sum(len(agent.governance_waits) for agent in snapshot.agents),
                "lifecycle_alerts": sum(1 for agent in snapshot.agents if _has_lifecycle_alert(agent)),
            },
            "backlog": [
                {
                    "queue_item_id": item.queue_item_id,
                    "title": item.title,
                    "status": item.status,
                    "owner_role": item.owner_role,
                    "linked_work_item_id": item.linked_work_item_id,
                }
                for item in snapshot.backlog
            ],
            "work_items": [
                {
                    "work_item_id": item.work_item_id,
                    "title": item.title,
                    "state": item.state,
                    "owner_role": item.owner_role,
                    "next_action": item.next_action,
                    "attention_reason": item.attention_reason,
                    "updated_at": item.updated_at,
                }
                for item in snapshot.work_items
            ],
            "agents": [
                {
                    "role_instance_id": agent.role_instance_id,
                    "container_state": agent.container_state,
                    "current_work": agent.current_work,
                    "inbox_depth": agent.inbox_depth,
                    "dead_letter_depth": agent.dead_letter_depth,
                    "governance_waits": list(agent.governance_waits),
                    "last_activity_at": agent.last_activity_at,
                    "last_run_status": agent.last_run_status,
                    "last_lifecycle_action": agent.last_lifecycle_action,
                    "last_lifecycle_exit_code": agent.last_lifecycle_exit_code,
                    "last_lifecycle_error": agent.last_lifecycle_error,
                    "session_status": agent.session_status,
                    "session_mode": agent.session_mode,
                }
                for agent in snapshot.agents
            ],
            "recent_completions": [
                {
                    "work_item_id": item.work_item_id,
                    "title": item.title,
                    "state": item.state,
                    "owner_role": item.owner_role,
                    "next_action": item.next_action,
                    "updated_at": item.updated_at,
                }
                for item in snapshot.recent_completions[:include_recent]
            ],
        }
        event_payload = {
            "call_id": call_id,
            "reason": _required(payload, "reason"),
            "project_id": project_id,
            "inspection": inspection,
        }
        with self.db.connection:
            self.db.record_event("runtime.status_inspected", "agent", role_instance_id, event_payload)
        self.db.record_message_journal(
            message_id=f"runtime-status-inspect-{call_id}",
            correlation_id=str(payload.get("correlation_id") or f"corr-{call_id}"),
            direction="runtime",
            stage="tool_call_recorded",
            status="completed",
            target_role=role_from_instance(role_instance_id),
            role_instance_id=role_instance_id,
            summary=_status_inspection_summary(inspection),
            payload=event_payload,
        )
        return {"inspection": inspection}

    def _inspect_message_journal(
        self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        limit = int(payload.get("limit") or 20)
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        filters = {
            "message_id": _optional(payload.get("message_id")),
            "correlation_id": _optional(payload.get("correlation_id")),
            "conversation_ref": _optional(payload.get("conversation_ref")),
            "work_item_id": _optional(payload.get("work_item_id")),
            "queue_item_id": _optional(payload.get("queue_item_id")),
            "target_role": _optional(payload.get("target_role") or payload.get("role_id")),
            "role_instance_id": _optional(payload.get("role_instance_id")),
            "stage": _optional(payload.get("stage")),
            "status": _optional(payload.get("status")),
        }
        query = _optional(payload.get("query") or payload.get("text"))
        where = []
        params: list[object] = []
        for column, value in filters.items():
            if value is None:
                continue
            where.append(f"{column} = ?")
            params.append(value)
        if query is not None:
            like_query = f"%{query}%"
            where.append(
                "("
                "message_id LIKE ? OR correlation_id LIKE ? OR conversation_ref LIKE ? "
                "OR thread_ref LIKE ? OR source_ref LIKE ? OR target_role LIKE ? "
                "OR work_item_id LIKE ? OR queue_item_id LIKE ? OR broker_subject LIKE ? "
                "OR broker_consumer LIKE ? OR status LIKE ? OR summary LIKE ? OR payload_hash LIKE ? "
                ")"
            )
            params.extend([like_query] * 13)
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        params.append(limit)
        rows = self.db.connection.execute(
            f"""
            SELECT message_id, correlation_id, direction, stage, connector,
                   conversation_ref, thread_ref, source_ref, target_role,
                   role_instance_id, work_item_id, queue_item_id, broker_subject,
                   broker_consumer, delivery_attempt, status, summary, created_at
            FROM message_journal
            {where_sql}
            ORDER BY created_at DESC, journal_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        entries = [dict(row) for row in rows]
        active_filters = {key: value for key, value in filters.items() if value is not None}
        if query is not None:
            active_filters["query"] = query
        inspection = {
            "filters": active_filters,
            "count": len(entries),
            "entries": entries,
        }
        event_payload = {
            "call_id": call_id,
            "reason": _required(payload, "reason"),
            "inspection": inspection,
        }
        with self.db.connection:
            self.db.record_event("runtime.message_journal_inspected", "agent", role_instance_id, event_payload)
        self.db.record_message_journal(
            message_id=f"runtime-message-journal-inspect-{call_id}",
            correlation_id=str(payload.get("correlation_id") or f"corr-{call_id}"),
            direction="runtime",
            stage="tool_call_recorded",
            status="completed",
            target_role=role_from_instance(role_instance_id),
            role_instance_id=role_instance_id,
            summary=_message_journal_inspection_summary(inspection),
            payload=event_payload,
        )
        return {"inspection": inspection}

    def _request_runtime_sweep(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> None:
        if self.broker is None or self.broker_stream is None:
            raise ValueError("broker is not configured")
        target_role = str(payload.get("target_role") or "project-manager")
        findings = ProjectSweepService(self.db).sweep()
        published_message_ids = ProjectSweepService(self.db).publish_findings(
            self.broker,
            stream=self.broker_stream,
            findings=findings,
            project_manager_role_id=target_role,
        )
        event_payload = {
            "call_id": call_id,
            "reason": _required(payload, "reason"),
            "target_role": target_role,
            "finding_count": len(findings),
            "published_message_count": len(published_message_ids),
            "published_message_ids": list(published_message_ids),
        }
        with self.db.connection:
            self.db.record_event("runtime.sweep_requested", "agent", role_instance_id, event_payload)
        self.db.record_message_journal(
            message_id=f"runtime-sweep-{call_id}",
            correlation_id=str(payload.get("correlation_id") or f"corr-{call_id}"),
            direction="runtime",
            stage="tool_call_recorded",
            status="completed",
            target_role=target_role,
            role_instance_id=role_instance_id,
            summary=(
                f"Runtime sweep requested: {len(findings)} findings, "
                f"{len(published_message_ids)} published."
            ),
            payload=event_payload,
        )

    def _request_runtime_lifecycle(
        self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self.lifecycle_config is None:
            raise ValueError("lifecycle config is not configured")
        project_id = str(payload.get("project_id") or _project_from_instance(role_instance_id))
        action = str(payload.get("action") or "reconcile")
        if action not in {"reconcile", "wake", "hibernate"}:
            raise ValueError("runtime.lifecycle.request action must be one of reconcile, wake, hibernate")
        execute = _bool_payload(payload.get("execute"), default=True)
        role_instance_filter = _optional(payload.get("role_instance_id"))
        role_ids = _role_ids_from_payload(payload)
        if action in {"wake", "hibernate"} and not role_instance_filter and not role_ids:
            raise ValueError(f"runtime.lifecycle.request action {action} requires role_id or role_instance_id")

        agents = self.db.status_snapshot(
            project_id=project_id,
            configured_role_instance_ids=self.configured_role_instance_ids,
        ).agents
        if self.broker is not None and self.broker_stream is not None:
            agents = refresh_agent_statuses_from_broker(
                agents,
                role_instance_ids=self.configured_role_instance_ids,
                broker=self.broker,
                stream=self.broker_stream,
            )
            for status in agents:
                self.db.upsert_agent_status(status)
        if execute:
            try:
                running_services = running_compose_services(
                    self.lifecycle_config,
                    runner=self.lifecycle_runner,
                )
            except Exception as exc:
                self.db.record_event(
                    "runtime.lifecycle_compose_reconcile_failed",
                    "agent",
                    role_instance_id,
                    {"call_id": call_id, "error": str(exc)},
                )
            else:
                agents = reconcile_agent_statuses_with_compose(
                    agents,
                    running_services=running_services,
                )
                for status in agents:
                    self.db.upsert_agent_status(status)

        scoped_agents = _filter_lifecycle_agents(
            agents,
            role_instance_id=role_instance_filter,
            role_ids=role_ids,
        )
        if action == "reconcile":
            decisions = plan_lifecycle_actions(
                scoped_agents,
                policy=HibernationPolicy(
                    idle_after_seconds=int(payload.get("idle_after_seconds") or 1800),
                    min_warm_instances_per_role=int(payload.get("min_warm_instances_per_role") or 0),
                ),
            )
        else:
            decisions = tuple(
                LifecycleDecision(action, status.role_instance_id, _required(payload, "reason"))
                for status in scoped_agents
            )
        for decision in decisions:
            if decision.action in {"start", "wake"}:
                self.db.record_message_journal(
                    message_id=f"lifecycle-{decision.role_instance_id}",
                    correlation_id=str(payload.get("correlation_id") or f"corr-{call_id}"),
                    direction="lifecycle",
                    stage="agent_wake_requested",
                    status="requested",
                    target_role=decision.role_instance_id.split(".")[-2],
                    role_instance_id=decision.role_instance_id,
                    summary=decision.reason,
                    payload={"call_id": call_id, "reason": _required(payload, "reason")},
                )
        results = ComposeLifecycleExecutor(
            self.lifecycle_config,
            runner=self.lifecycle_runner,
        ).apply(decisions, execute=execute)
        for result in results:
            self.db.record_agent_lifecycle_result(
                role_instance_id=result.decision.role_instance_id,
                action=result.decision.action,
                reason=result.decision.reason,
                service_name=result.service_name,
                command=result.command,
                working_directory=str(result.working_directory) if result.working_directory else None,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                executed=result.executed,
            )
            stage = _lifecycle_result_stage(result.decision.action, result.exit_code)
            self.db.record_message_journal(
                message_id=f"lifecycle-{result.decision.role_instance_id}",
                correlation_id=str(payload.get("correlation_id") or f"corr-{call_id}"),
                direction="lifecycle",
                stage=stage,
                status="completed" if result.exit_code in {None, 0} else "failed",
                target_role=result.decision.role_instance_id.split(".")[-2],
                role_instance_id=result.decision.role_instance_id,
                summary=_lifecycle_result_summary(result),
                payload={
                    "call_id": call_id,
                    "service_name": result.service_name,
                    "command": list(result.command),
                    "exit_code": result.exit_code,
                    "executed": result.executed,
                },
            )
        event_payload = {
            "call_id": call_id,
            "reason": _required(payload, "reason"),
            "action": action,
            "execute": execute,
            "project_id": project_id,
            "role_instance_id": role_instance_filter,
            "role_ids": list(role_ids),
            "decision_count": len(decisions),
            "result_count": len(results),
            "decisions": [decision.__dict__ for decision in decisions],
            "results": [_lifecycle_result_payload(result) for result in results],
        }
        with self.db.connection:
            self.db.record_event("runtime.lifecycle_requested", "agent", role_instance_id, event_payload)
        self.db.record_message_journal(
            message_id=f"runtime-lifecycle-{call_id}",
            correlation_id=str(payload.get("correlation_id") or f"corr-{call_id}"),
            direction="runtime",
            stage="tool_call_recorded",
            status="completed",
            target_role=role_from_instance(role_instance_id),
            role_instance_id=role_instance_id,
            summary=f"Runtime lifecycle requested: {len(decisions)} decisions, {len(results)} results.",
            payload=event_payload,
        )
        return {
            "action": action,
            "execute": execute,
            "decision_count": len(decisions),
            "result_count": len(results),
            "decisions": event_payload["decisions"],
            "results": event_payload["results"],
        }

    def _send_message(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> None:
        if self.stakeholder_bridge is None:
            raise ValueError("stakeholder bridge is not configured")
        self._record_reply_requested(call_id=call_id, role_instance_id=role_instance_id, purpose="messaging.send", payload=payload)
        receipt = self.stakeholder_bridge.send(
            OutboundMessage(
                connector=_required(payload, "connector"),
                target_ref=_required(payload, "target_ref"),
                text_markdown=_required(payload, "text_markdown"),
                thread_ref=_optional(payload.get("thread_ref")),
                importance=str(payload.get("importance") or "normal"),
                sender_role=role_from_instance(role_instance_id),
            )
        )
        self._record_delivery_receipt(
            receipt=receipt,
            call_id=call_id,
            role_instance_id=role_instance_id,
            purpose="messaging.send",
            payload=payload,
        )

    def _send_optional_reply(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> None:
        target_ref = _optional(payload.get("target_ref")) or _optional(payload.get("reply_target_ref"))
        if target_ref is None:
            route = self._reply_route_from_source_message(payload)
            if route is not None:
                target_ref = _optional(route.get("reply_target_ref"))
                payload.setdefault("connector", route.get("connector"))
                payload.setdefault("conversation_ref", route.get("conversation_ref"))
                payload.setdefault("reply_thread_ref", route.get("reply_thread_ref") or route.get("thread_ref"))
        if target_ref is None:
            return
        if self.stakeholder_bridge is None:
            raise ValueError("stakeholder bridge is not configured")
        text_markdown = _required(payload, "text_markdown")
        thread_ref = _optional(payload.get("thread_ref")) or _optional(payload.get("reply_thread_ref"))
        connector = _required(payload, "connector")
        try:
            self._record_reply_requested(call_id=call_id, role_instance_id=role_instance_id, purpose="status.reply", payload=payload)
            receipt = self.stakeholder_bridge.send(
                OutboundMessage(
                    connector=connector,
                    target_ref=target_ref,
                    text_markdown=text_markdown,
                    thread_ref=thread_ref,
                    importance=str(payload.get("importance") or "normal"),
                    sender_role=role_from_instance(role_instance_id),
                )
            )
        except Exception as exc:
            self.db.record_outbound_delivery(
                delivery_id=f"delivery-failed-{call_id}",
                call_id=call_id,
                role_instance_id=role_instance_id,
                purpose="status.reply",
                connector=connector,
                target_ref=f"{target_ref} ({type(exc).__name__}: {exc})",
                thread_ref=thread_ref,
                work_item_id=_optional(payload.get("work_item_id")),
                status="failed",
            )
            self.db.record_message_journal(
                message_id=_optional(payload.get("source_message_id") or payload.get("message_id")) or call_id,
                correlation_id=_optional(payload.get("correlation_id")),
                direction="outbound",
                stage="failed",
                status="failed",
                connector=connector,
                conversation_ref=_optional(payload.get("conversation_ref")),
                thread_ref=thread_ref,
                source_ref=call_id,
                role_instance_id=role_instance_id,
                work_item_id=_optional(payload.get("work_item_id")),
                summary=f"status.reply delivery failed: {type(exc).__name__}: {exc}",
            )
            raise
        else:
            self._record_delivery_receipt(
                receipt=receipt,
                call_id=call_id,
                role_instance_id=role_instance_id,
                purpose="status.reply",
                payload=payload,
            )

    def _reply_route_from_source_message(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        source_message_id = _optional(payload.get("source_message_id") or payload.get("message_id"))
        if source_message_id is None:
            return None
        route = self.db.conversation_reply_route(source_message_id)
        if route is None:
            return None
        if _optional(route.get("reply_target_ref")) is None:
            return None
        return route

    def _update_status(self, payload: dict[str, Any]) -> None:
        work_item_id = _optional(payload.get("work_item_id"))
        if work_item_id is None:
            return
        self.db.update_work_item_progress(
            work_item_id=work_item_id,
            owner_role=_optional(payload.get("owner_role")),
            current_phase=_optional(payload.get("current_phase")),
            next_action=_summary(payload),
        )

    def _ask_stakeholder(
        self,
        *,
        call_id: str,
        role_instance_id: str,
        tool_name: str,
        payload: dict[str, Any],
    ) -> None:
        _validate_stakeholder_question(payload)
        target_ref = _target_ref(payload)
        work_item_id = _required(payload, "work_item_id")
        question = _required(payload, "question")
        should_deliver = _optional(payload.get("target_ref") or payload.get("stakeholder_ref")) is not None
        if should_deliver and self.stakeholder_bridge is None:
            raise ValueError("stakeholder bridge is not configured")
        self._record_governance_tool(
            call_id=call_id,
            role_instance_id=role_instance_id,
            tool_name=tool_name,
            payload=payload,
        )
        waiting_owner = str(payload.get("waiting_owner_role") or payload.get("owner_role") or "sponsor")
        current_phase = _optional(payload.get("current_phase"))
        next_action = str(payload.get("next_action") or f"Awaiting stakeholder answer: {question}")
        self.db.update_work_item_state(
            work_item_id=work_item_id,
            state="waiting_human",
            owner_role=waiting_owner,
            current_phase=current_phase,
            next_action=next_action,
        )
        if not should_deliver:
            return
        question_id = str(payload.get("record_id"))
        question_id_line = f"Question ID: `{question_id}`"
        text_markdown = _optional(payload.get("text_markdown"))
        if text_markdown is None:
            text_markdown = f"**Question**\n\n{question}\n\n{question_id_line}"
        elif question_id not in text_markdown:
            text_markdown = f"{text_markdown.rstrip()}\n\n{question_id_line}"
        self._record_reply_requested(
            call_id=call_id,
            role_instance_id=role_instance_id,
            purpose="stakeholder.ask_question",
            payload=payload,
        )
        receipt = self.stakeholder_bridge.send(
            OutboundMessage(
                connector=_required(payload, "connector"),
                target_ref=str(target_ref),
                text_markdown=text_markdown,
                thread_ref=_optional(payload.get("thread_ref")),
                importance=str(payload.get("importance") or "high"),
                sender_role=role_from_instance(role_instance_id),
            )
        )
        self._record_delivery_receipt(
            receipt=receipt,
            call_id=call_id,
            role_instance_id=role_instance_id,
            purpose="stakeholder.ask_question",
            payload=payload,
        )

    def _request_approval(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> None:
        target_ref = _optional(payload.get("target_ref"))
        if target_ref is not None and self.stakeholder_bridge is None:
            raise ValueError("stakeholder bridge is not configured")
        approval_id = str(payload.get("approval_id") or f"approval-{uuid4().hex}")
        work_item_id = _required(payload, "work_item_id")
        question = _required(payload, "question")
        self.db.request_approval(
            approval_id=approval_id,
            work_item_id=work_item_id,
            requested_by_role=role_from_instance(role_instance_id),
            question=question,
            waiting_owner_role=str(payload.get("waiting_owner_role") or "sponsor"),
            current_phase=_optional(payload.get("current_phase")),
            next_action=str(
                payload.get("next_action")
                or f"Approval `{approval_id}` requested by {role_from_instance(role_instance_id)}; awaiting sponsor response."
            ),
        )
        if target_ref is None:
            return
        text_markdown = _optional(payload.get("text_markdown")) or (
            f"**Approval requested**\n\n{question}\n\n"
            f"Work item: `{work_item_id}`\n\n"
            f"Approval ID: `{approval_id}`"
        )
        self._record_reply_requested(
            call_id=call_id,
            role_instance_id=role_instance_id,
            purpose="approval.request",
            payload=payload,
        )
        receipt = self.stakeholder_bridge.send(
            OutboundMessage(
                connector=_required(payload, "connector"),
                target_ref=target_ref,
                text_markdown=text_markdown,
                thread_ref=_optional(payload.get("thread_ref")),
                importance=str(payload.get("importance") or "high"),
                sender_role=role_from_instance(role_instance_id),
            )
        )
        self._record_delivery_receipt(
            receipt=receipt,
            call_id=call_id,
            role_instance_id=role_instance_id,
            purpose="approval.request",
            payload=payload,
        )

    def _record_delivery_receipt(
        self,
        *,
        receipt: object,
        call_id: str,
        role_instance_id: str,
        purpose: str,
        payload: dict[str, Any],
    ) -> None:
        self.db.record_outbound_delivery(
            delivery_id=str(getattr(receipt, "delivery_id")),
            call_id=call_id,
            role_instance_id=role_instance_id,
            purpose=purpose,
            connector=str(getattr(receipt, "connector")),
            target_ref=str(getattr(receipt, "target_ref")),
            thread_ref=_optional(getattr(receipt, "thread_ref")),
            work_item_id=_optional(payload.get("work_item_id")),
            status="sent",
        )
        self.db.record_message_journal(
            message_id=_optional(payload.get("source_message_id") or payload.get("message_id")) or str(getattr(receipt, "delivery_id")),
            correlation_id=_optional(payload.get("correlation_id")),
            direction="outbound",
            stage="reply_delivered",
            status="sent",
            connector=str(getattr(receipt, "connector")),
            conversation_ref=_optional(payload.get("conversation_ref")),
            thread_ref=_optional(getattr(receipt, "thread_ref")),
            source_ref=call_id,
            role_instance_id=role_instance_id,
            work_item_id=_optional(payload.get("work_item_id")),
            summary=f"{purpose} delivered to {getattr(receipt, 'target_ref')}",
        )

    def _record_reply_requested(
        self,
        *,
        call_id: str,
        role_instance_id: str,
        purpose: str,
        payload: dict[str, Any],
    ) -> None:
        self.db.record_message_journal(
            message_id=_optional(payload.get("source_message_id") or payload.get("message_id")) or call_id,
            correlation_id=_optional(payload.get("correlation_id")),
            direction="outbound",
            stage="reply_requested",
            status="requested",
            connector=_optional(payload.get("connector")),
            conversation_ref=_optional(payload.get("conversation_ref")),
            thread_ref=_optional(payload.get("thread_ref") or payload.get("reply_thread_ref")),
            source_ref=call_id,
            role_instance_id=role_instance_id,
            work_item_id=_optional(payload.get("work_item_id")),
            summary=f"{purpose} requested outbound delivery.",
        )

    def _compact_conversation_context(
        self,
        *,
        call_id: str,
        role_instance_id: str,
        payload: dict[str, Any],
    ) -> None:
        self.db.compact_conversation_context(
            summary_id=str(payload.get("summary_id") or f"conversation-summary-{call_id}"),
            conversation_ref=_required(payload, "conversation_ref"),
            visibility=_required(payload, "visibility"),
            summary=_required(payload, "summary"),
            source_message_ids=tuple(str(item) for item in payload.get("source_message_ids") or ()),
            durable_refs=tuple(str(item) for item in payload.get("durable_refs") or ()),
            created_by_role=role_from_instance(role_instance_id),
        )

    def _link_artifact(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> None:
        relative_path = _safe_relative_path(_required(payload, "relative_path"))
        filename = str(payload.get("filename") or PurePosixPath(relative_path).name)
        if not filename:
            raise ValueError("filename is required")
        document_type = str(payload.get("document_type") or "artifact")
        if self.document_library is not None:
            validate_framework_artifact_path(
                framework_id=getattr(self.document_library, "framework_id", "togaf-sdlc-v1"),
                document_type=document_type,
                work_item_id=_required(payload, "work_item_id"),
                relative_path=relative_path,
            )
        self.db.add_artifact(
            artifact_id=str(payload.get("artifact_id") or f"artifact-{call_id}"),
            work_item_id=_required(payload, "work_item_id"),
            filename=filename,
            title=str(payload.get("title") or filename),
            relative_path=relative_path,
            document_type=document_type,
            status=str(payload.get("status") or "linked"),
            created_by_role=role_from_instance(role_instance_id),
            url=_optional(payload.get("url")),
        )

    def _write_artifact(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> None:
        if self.document_library is None:
            raise ValueError("document library is not configured")
        relative_path = _safe_relative_path(_required(payload, "relative_path"))
        document_type = str(payload.get("document_type") or "artifact")
        work_item_id = _required(payload, "work_item_id")
        validate_framework_artifact_path(
            framework_id=getattr(self.document_library, "framework_id", "togaf-sdlc-v1"),
            document_type=document_type,
            work_item_id=work_item_id,
            relative_path=relative_path,
        )
        ref = self.document_library.write_text(relative_path, _required(payload, "content_markdown"))
        filename = str(payload.get("filename") or PurePosixPath(relative_path).name)
        self.db.add_artifact(
            artifact_id=str(payload.get("artifact_id") or f"artifact-{call_id}"),
            work_item_id=work_item_id,
            filename=filename,
            title=str(payload.get("title") or ref.title or filename),
            relative_path=ref.relative_path,
            document_type=document_type,
            status=str(payload.get("status") or "published"),
            created_by_role=role_from_instance(role_instance_id),
            url=ref.url,
        )

    def _write_work_item_index(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> None:
        if self.document_library is None:
            raise ValueError("document library is not configured")
        artifacts = tuple(
            DocumentRef(
                relative_path=str(artifact["relative_path"]),
                title=str(artifact["title"]),
                url=_optional(artifact.get("url")),
            )
            for artifact in payload.get("artifacts") or ()
        )
        work_item_id = _required(payload, "work_item_id")
        index = WorkItemIndex(
            work_item_id=work_item_id,
            title=_required(payload, "title"),
            status=_required(payload, "status"),
            owner_role=_required(payload, "owner_role"),
            raci_summary=_required(payload, "raci_summary"),
            governance_state=_required(payload, "governance_state"),
            artifacts=artifacts,
            consultations=tuple(str(item) for item in payload.get("consultations") or ()),
            approvals=tuple(str(item) for item in payload.get("approvals") or ()),
            evidence=tuple(str(item) for item in payload.get("evidence") or ()),
            decisions=tuple(str(item) for item in payload.get("decisions") or ()),
            risks=tuple(str(item) for item in payload.get("risks") or ()),
            next_action=str(payload.get("next_action") or ""),
        )
        ref = write_work_item_index(self.document_library, index)
        self._write_root_work_item_index()
        self.db.add_artifact(
            artifact_id=f"artifact-{call_id}",
            work_item_id=work_item_id,
            filename="index.md",
            title="Work item index",
            relative_path=ref.relative_path,
            document_type="work_item_index",
            status="published",
            created_by_role=role_from_instance(role_instance_id),
            url=ref.url,
        )

    def _write_root_work_item_index(self) -> None:
        if self.document_library is None:
            raise ValueError("document library is not configured")
        snapshot = self.db.status_snapshot(project_id="project")
        indexes = [
            WorkItemIndex(
                work_item_id=item.work_item_id,
                title=item.title,
                status=item.state,
                owner_role=item.owner_role,
                raci_summary="See work-item index",
                governance_state=item.next_action or "No governance wait recorded",
            )
            for item in (*snapshot.work_items, *snapshot.recent_completions)
        ]
        write_root_work_item_index(self.document_library, indexes)

    def _record_governance_tool(
        self,
        *,
        call_id: str,
        role_instance_id: str,
        tool_name: str,
        payload: dict[str, Any],
    ) -> None:
        if tool_name == "blocker.raise":
            _validate_blocker_raise(payload)
            owner_role = _required(payload, "owner_role")
            self.db.update_work_item_state(
                work_item_id=_required(payload, "work_item_id"),
                state="blocked",
                owner_role=owner_role,
                current_phase=_optional(payload.get("current_phase")),
                next_action=_required(payload, "next_action"),
            )
        elif tool_name == "handoff.require":
            _validate_handoff_requirements(payload)
            self.db.update_work_item_state(
                work_item_id=_required(payload, "work_item_id"),
                state="waiting_agent",
                owner_role=_required(payload, "target_role"),
                current_phase=_required(payload, "phase"),
                next_action=_required(payload, "required_next_action"),
                governance=_handoff_governance(payload),
            )
        elif tool_name == "consult.request":
            _validate_consult_request(payload)
        elif tool_name == "informed.update":
            _validate_informed_update(payload)
        elif tool_name == "governance.record_exception":
            _validate_governance_exception(payload)
        elif tool_name == "decision.record":
            _validate_decision_record(payload)
        elif tool_name == "risk.register":
            _validate_risk_register(payload)
        elif tool_name == "relevance.record":
            _validate_relevance_record(payload)
        self.db.record_governance_record(
            record_id=str(payload.get("record_id") or f"governance-{call_id}"),
            work_item_id=_governance_record_scope(payload),
            record_type=tool_name,
            role_instance_id=role_instance_id,
            target_ref=_target_ref(payload),
            summary=_summary(payload),
            status=str(payload.get("status") or _default_governance_status(tool_name)),
            payload=payload,
        )
        self._refresh_governance_register(tool_name)
        self._publish_role_governance_message(
            call_id=call_id,
            role_instance_id=role_instance_id,
            tool_name=tool_name,
            payload=payload,
        )

    def _publish_role_governance_message(
        self,
        *,
        call_id: str,
        role_instance_id: str,
        tool_name: str,
        payload: dict[str, Any],
    ) -> None:
        if self.broker is None or self.broker_stream is None:
            return
        target_role = _governance_message_target_role(tool_name, payload)
        if target_role is None:
            return
        if not _is_role_message_target(target_role, self.configured_role_instance_ids):
            return
        if target_role == role_from_instance(role_instance_id):
            self.db.record_event(
                "agent.governance_self_publish_skipped",
                "agent",
                role_instance_id,
                {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "target_role": target_role,
                    "work_item_id": _optional(payload.get("work_item_id")),
                    "summary": _summary(payload),
                },
            )
            return
        broker_subject = f"agent.{target_role}"
        instance_id = str(payload.get("instance_id") or "1")
        broker_consumer = role_consumer_name(target_role, instance_id)
        self.broker.ensure_stream(self.broker_stream, [broker_subject])
        self.broker.ensure_consumer(
            self.broker_stream,
            broker_consumer,
            filter_subject=broker_subject,
        )
        work_item_id = _required(payload, "work_item_id")
        message_payload = {
            "message_type": tool_name,
            "source_call_id": call_id,
            "source_role_instance_id": role_instance_id,
            "source_role": role_from_instance(role_instance_id),
            "target_role": target_role,
            "work_item_id": work_item_id,
            "summary": _summary(payload),
            "payload": payload,
        }
        message = self.broker.publish(
            self.broker_stream,
            broker_subject,
            message_payload,
        )
        self.db.record_message_journal(
            message_id=message.message_id,
            correlation_id=_optional(payload.get("correlation_id")),
            direction="broker",
            stage="published",
            status="published",
            source_ref=call_id,
            target_role=target_role,
            role_instance_id=role_instance_id,
            work_item_id=work_item_id,
            queue_item_id=_optional(payload.get("queue_item_id")),
            broker_subject=broker_subject,
            broker_consumer=broker_consumer,
            summary=f"Published {tool_name} to {target_role}: {_summary(payload)}",
            payload=message_payload,
        )

    def _refresh_governance_register(self, tool_name: str) -> None:
        if self.document_library is None:
            return
        if tool_name == "decision.record":
            relative_path = "decisions/index.md"
            title = "Decision Register"
        elif tool_name == "risk.register":
            relative_path = "risks/index.md"
            title = "Risk Register"
        else:
            return
        records = self.db.list_governance_records(record_type=tool_name)
        write_governance_register(
            self.document_library,
            relative_path=relative_path,
            title=title,
            items=(
                GovernanceRegisterItem(
                    record_id=str(record["record_id"]),
                    work_item_id=str(record["work_item_id"]),
                    summary=str(record["summary"]),
                    status=str(record["status"]),
                    role_instance_id=str(record["role_instance_id"]),
                    target_ref=_optional(record.get("target_ref")),
                )
                for record in records
            ),
        )


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value) == "":
        raise ValueError(f"{key} is required")
    return str(value)


def _canonical_tool_name(tool_name: str) -> str:
    return {
        "status.report_progress": "status.update",
        "status.report_completion": "status.complete",
    }.get(tool_name, tool_name)


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _role_ids_from_payload(payload: dict[str, Any]) -> tuple[str, ...]:
    value = payload.get("role_ids")
    if value is None:
        value = payload.get("roles")
    if value is None:
        role_id = _optional(payload.get("role_id"))
        return (role_id,) if role_id else ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(part).strip() for part in value if str(part).strip())
    raise ValueError("role_ids must be a list or comma-separated string")


def _filter_lifecycle_agents(
    agents: tuple[AgentStatus, ...],
    *,
    role_instance_id: str | None,
    role_ids: tuple[str, ...],
) -> tuple[AgentStatus, ...]:
    if role_instance_id is not None:
        matched = tuple(status for status in agents if status.role_instance_id == role_instance_id)
        if not matched:
            raise ValueError(f"configured role instance not found: {role_instance_id}")
        return matched
    if role_ids:
        requested = set(role_ids)
        matched = tuple(status for status in agents if role_from_instance(status.role_instance_id) in requested)
        missing = sorted(requested - {role_from_instance(status.role_instance_id) for status in matched})
        if missing:
            raise ValueError(f"configured role ids not found: {', '.join(missing)}")
        return matched
    return agents


def _bool_payload(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def _lifecycle_result_stage(action: str, exit_code: int | None) -> str:
    if action in {"start", "wake"}:
        return "agent_started" if exit_code in {None, 0} else "failed"
    if action == "hibernate":
        return "agent_hibernated" if exit_code in {None, 0} else "failed"
    return "tool_call_recorded"


def _lifecycle_result_summary(result: LifecycleCommandResult) -> str:
    if result.exit_code not in {None, 0}:
        return (
            f"Lifecycle {result.decision.action} failed for {result.service_name}: "
            f"{_truncate_for_summary(result.stderr or result.stdout or 'no detail')}"
        )
    disposition = "planned" if not result.executed else "completed"
    return f"Lifecycle {result.decision.action} {disposition} for {result.service_name}."


def _lifecycle_result_payload(result: LifecycleCommandResult) -> dict[str, object]:
    return {
        "action": result.decision.action,
        "role_instance_id": result.decision.role_instance_id,
        "reason": result.decision.reason,
        "service_name": result.service_name,
        "command": list(result.command),
        "working_directory": str(result.working_directory) if result.working_directory else None,
        "exit_code": result.exit_code,
        "executed": result.executed,
    }


def _project_from_instance(role_instance_id: str) -> str:
    parts = role_instance_id.split(".")
    return ".".join(parts[:-2]) if len(parts) >= 3 else "project"


def _broker_inspection_summary(inspection: dict[str, object]) -> str:
    role_consumers = inspection.get("role_consumers")
    if isinstance(role_consumers, list) and role_consumers:
        parts = []
        for item in role_consumers[:8]:
            if not isinstance(item, dict):
                continue
            role_id = str(item.get("role_id") or "unknown")
            pending_count = item.get("pending_count")
            if pending_count is None:
                pending = "unknown"
            else:
                pending = str(pending_count)
            parts.append(f"{role_id}={pending}")
        if parts:
            return "Broker inspected: " + ", ".join(parts)
    pending = inspection.get("pending")
    pending_count = len(pending) if isinstance(pending, list) else 0
    dead_letters = inspection.get("dead_letters")
    dead_letter_count = len(dead_letters) if isinstance(dead_letters, list) else 0
    return f"Broker inspected: {pending_count} pending sampled, {dead_letter_count} dead letters sampled."


def _status_inspection_summary(inspection: dict[str, object]) -> str:
    counts = inspection.get("counts")
    if not isinstance(counts, dict):
        return "Runtime status inspected."
    return (
        "Runtime status inspected: "
        f"{counts.get('current_work', 0)} current work, "
        f"{counts.get('backlog', 0)} backlog, "
        f"{counts.get('attention_needed', 0)} attention needed, "
        f"{counts.get('blocked', 0)} blocked, "
        f"{counts.get('lifecycle_alerts', 0)} lifecycle alerts."
    )


def _message_journal_inspection_summary(inspection: dict[str, object]) -> str:
    count = inspection.get("count", 0)
    filters = inspection.get("filters")
    if isinstance(filters, dict) and filters:
        filter_text = ", ".join(f"{key}={value}" for key, value in sorted(filters.items()))
        return f"Message journal inspected: {count} entries matched {filter_text}."
    return f"Message journal inspected: {count} recent entries sampled."


def _truncate_for_summary(value: object, limit: int = 140) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _is_attention_needed_state(state: str) -> bool:
    return state in {
        "waiting_human",
        "waiting_agent",
        "waiting_external",
        "blocked",
        "recovering",
        "failed_terminal",
    }


def _has_lifecycle_alert(agent: AgentStatus) -> bool:
    return (
        agent.container_state == "lifecycle_failed"
        or agent.last_lifecycle_error is not None
        or (agent.last_lifecycle_exit_code not in (None, 0))
    )


def _decision_summary(payload: dict[str, Any]) -> str:
    for key in ("decision", "rationale", "description"):
        value = _optional(payload.get(key))
        if value is not None:
            return value
    work_item_id = _optional(payload.get("work_item_id"))
    if work_item_id is not None:
        return f"Decision recorded for {work_item_id}."
    return "Decision recorded."


def _safe_relative_path(value: str) -> str:
    text = value.replace("\\", "/").lstrip("/")
    parts = PurePosixPath(text).parts
    if not text or "\x00" in text or any(part == ".." for part in parts):
        raise ValueError("relative_path must stay inside the document library")
    return text


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _work_item_governance(
    value: Any,
    *,
    owner_role: str,
    state: str,
    current_phase: str | None,
) -> dict[str, Any]:
    governance = dict(_dict(value))
    governance.setdefault("phase", current_phase or state)
    governance.setdefault("accountable_role", owner_role)
    governance.setdefault("responsible_roles", [owner_role])
    return governance


def _handoff_governance(payload: dict[str, Any]) -> dict[str, Any]:
    target_role = _required(payload, "target_role")
    return {
        "phase": _required(payload, "phase"),
        "accountable_role": _required(payload, "accountable_role"),
        "responsible_roles": [target_role],
        "consulted_roles": [str(role) for role in payload.get("consulted_roles") or ()],
        "informed_roles": [str(role) for role in payload.get("informed_roles") or ()],
        "required_evidence": [str(item) for item in payload.get("evidence_requirements") or ()],
    }


def _target_ref(payload: dict[str, Any]) -> str | None:
    return _optional(
        payload.get("target_ref")
        or payload.get("target_role")
        or payload.get("stakeholder_ref")
        or payload.get("source_message_id")
    )


def _summary(payload: dict[str, Any]) -> str:
    for key in ("summary", "question", "reason", "required_next_action", "message", "rationale"):
        value = payload.get(key)
        if value is not None and str(value):
            return str(value)
    raise ValueError("summary, question, reason, required_next_action, message, or rationale is required")


def _validate_handoff_requirements(payload: dict[str, Any]) -> None:
    for key in (
        "work_item_id",
        "target_role",
        "phase",
        "accountable_role",
        "required_next_action",
        "acceptance_criteria",
        "evidence_requirements",
        "artifact_links",
        "open_decisions",
        "open_risks",
        "consulted_roles",
        "informed_roles",
        "stakeholder_follow_up",
    ):
        if key not in payload:
            raise ValueError(f"handoff.require requires {key}")
    for key in (
        "acceptance_criteria",
        "evidence_requirements",
        "artifact_links",
        "open_decisions",
        "open_risks",
        "consulted_roles",
        "informed_roles",
        "stakeholder_follow_up",
    ):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"handoff.require {key} must be a list")
    _required(payload, "target_role")
    _required(payload, "phase")
    _required(payload, "accountable_role")
    _required(payload, "required_next_action")


def _validate_blocker_raise(payload: dict[str, Any]) -> None:
    _required(payload, "work_item_id")
    _required(payload, "summary")
    _required(payload, "next_action")
    _required(payload, "owner_role")


def _governance_message_target_role(tool_name: str, payload: dict[str, Any]) -> str | None:
    if tool_name == "blocker.raise":
        return _optional(payload.get("owner_role"))
    return _optional(payload.get("target_role"))


def _is_role_message_target(target_role: str, configured_role_instance_ids: tuple[str, ...]) -> bool:
    if target_role in {"sponsor", "stakeholder", "human", "operator"}:
        return False
    if not configured_role_instance_ids:
        return True
    return target_role in {role_from_instance(role_instance_id) for role_instance_id in configured_role_instance_ids}


def _validate_consult_request(payload: dict[str, Any]) -> None:
    _required(payload, "work_item_id")
    _required(payload, "target_role")
    _required(payload, "question")


def _validate_informed_update(payload: dict[str, Any]) -> None:
    _required(payload, "work_item_id")
    _required(payload, "target_role")
    _required(payload, "message")


def _validate_stakeholder_question(payload: dict[str, Any]) -> None:
    _required(payload, "work_item_id")
    _required(payload, "question")


def _validate_governance_exception(payload: dict[str, Any]) -> None:
    _required(payload, "work_item_id")
    _required(payload, "reason")


def _validate_decision_record(payload: dict[str, Any]) -> None:
    _required(payload, "work_item_id")
    _required(payload, "summary")


def _validate_risk_register(payload: dict[str, Any]) -> None:
    _required(payload, "work_item_id")
    _required(payload, "summary")


def _validate_relevance_record(payload: dict[str, Any]) -> None:
    _required(payload, "source_message_id")
    _required(payload, "relevance_score")
    _required(payload, "rationale")
    score = int(_required(payload, "relevance_score"))
    if score < 0 or score > 100:
        raise ValueError("relevance_score must be between 0 and 100")


def _governance_record_scope(payload: dict[str, Any]) -> str:
    work_item_id = _optional(payload.get("work_item_id"))
    if work_item_id is not None:
        return work_item_id
    source_message_id = _optional(payload.get("source_message_id"))
    if source_message_id is not None:
        return f"message:{source_message_id}"
    return _required(payload, "work_item_id")


def _deployment_target_type(target: DeploymentTarget) -> str:
    if hasattr(target, "command"):
        return "command"
    if target.__class__.__name__ == "NoDeploymentDisposition":
        return "no-deployment"
    return target.__class__.__name__


def _default_governance_status(tool_name: str) -> str:
    return {
        "blocker.raise": "blocked",
        "consult.request": "requested",
        "decision.record": "decision_recorded",
        "handoff.require": "required",
        "informed.update": "sent",
        "risk.register": "risk_open",
        "stakeholder.ask_question": "requested",
        "governance.record_exception": "exception_recorded",
        "relevance.record": "relevance_recorded",
    }.get(tool_name, "recorded")
