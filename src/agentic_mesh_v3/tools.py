from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from agentic_mesh_v3.authority import ToolAuthorityPolicy
from agentic_mesh_v3.authority import role_from_instance
from agentic_mesh_v3.broker import BrokerAdapter
from agentic_mesh_v3.connectors import OutboundMessage
from agentic_mesh_v3.connectors import StakeholderBridge
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.deployment import DeploymentTarget
from agentic_mesh_v3.documents import DocumentLibraryAdapter
from agentic_mesh_v3.documents import DocumentRef
from agentic_mesh_v3.documents import WorkItemIndex
from agentic_mesh_v3.documents import write_root_work_item_index
from agentic_mesh_v3.documents import write_work_item_index
from agentic_mesh_v3.observability import V3Telemetry
from agentic_mesh_v3.observability import get_telemetry
from agentic_mesh_v3.reporting import AgentStatus


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool_name: str
    terminal: bool = False


TERMINAL_TOOLS = {"status.reply", "status.complete", "noop", "report.incomplete"}


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
    ) -> None:
        self.db = db
        self.document_library = document_library
        self.deployment_targets = deployment_targets or {}
        self.stakeholder_bridge = stakeholder_bridge
        self.broker = broker
        self.broker_stream = broker_stream
        self.authority_policy = authority_policy or ToolAuthorityPolicy.default()
        self.telemetry = telemetry or get_telemetry()

    def call(
        self,
        *,
        role_instance_id: str,
        tool_name: str,
        payload: dict[str, Any],
        terminal: bool | None = None,
    ) -> ToolResult:
        with self.telemetry.span(
            "v3.tool_call",
            role_instance_id=role_instance_id,
            tool_name=tool_name,
            terminal=bool(terminal),
        ):
            try:
                self.authority_policy.assert_allowed(role_instance_id=role_instance_id, tool_name=tool_name)
                call_id = f"call-{uuid4().hex}"
                is_terminal = bool(terminal) or tool_name in TERMINAL_TOOLS
                self.db.record_tool_call(
                    call_id=call_id,
                    role_instance_id=role_instance_id,
                    tool_name=tool_name,
                    payload=payload,
                    terminal=is_terminal,
                )
                self._apply_effect(
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
            return ToolResult(call_id=call_id, tool_name=tool_name, terminal=is_terminal)

    def _apply_effect(
        self, *, call_id: str, role_instance_id: str, tool_name: str, payload: dict[str, Any]
    ) -> None:
        if tool_name == "backlog.upsert":
            self.db.upsert_backlog_item(
                queue_item_id=_required(payload, "queue_item_id"),
                title=_required(payload, "title"),
                summary=_required(payload, "summary"),
                status=str(payload.get("status") or "queued"),
                owner_role=_required(payload, "owner_role"),
                linked_work_item_id=_optional(payload.get("linked_work_item_id")),
                source_ref=_optional(payload.get("source_ref")),
            )
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
            )
        elif tool_name == "work_item.update_state":
            self.db.update_work_item_state(
                work_item_id=_required(payload, "work_item_id"),
                state=_required(payload, "state"),
                owner_role=_optional(payload.get("owner_role")),
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
        elif tool_name == "document.write_work_item_index":
            self._write_work_item_index(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "document.write_root_work_item_index":
            self._write_root_work_item_index()
        elif tool_name == "approval.request":
            self._request_approval(role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "release.record":
            self.db.record_release(
                release_id=str(payload.get("release_id") or f"release-{uuid4().hex}"),
                work_item_id=_required(payload, "work_item_id"),
                status=str(payload.get("status") or "recorded"),
                scope=_required(payload, "scope"),
                deployment_result=_required(payload, "deployment_result"),
                rollback_plan=_required(payload, "rollback_plan"),
                residual_risks=str(payload.get("residual_risks") or "None recorded"),
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
            self.db.record_release(
                release_id=str(payload.get("release_id") or f"release-{uuid4().hex}"),
                work_item_id=work_item_id,
                status=result.status,
                scope=str(payload.get("scope") or f"Deployment target {target_id}"),
                deployment_result=result.output,
                rollback_plan=result.rollback_plan,
                residual_risks=str(payload.get("residual_risks") or "None recorded"),
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
            if detail.state != "released":
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
        elif tool_name == "memory.propose_update":
            self.db.record_role_memory(
                memory_id=str(payload.get("memory_id") or f"memory-{call_id}"),
                role_instance_id=role_instance_id,
                summary=_required(payload, "summary"),
                source_ref=_required(payload, "source_ref"),
            )
        elif tool_name == "messaging.send":
            self._send_message(payload)
        elif tool_name == "status.reply":
            self._send_optional_reply(payload)
        elif tool_name == "stakeholder.ask_question":
            self._ask_stakeholder(
                call_id=call_id,
                role_instance_id=role_instance_id,
                tool_name=tool_name,
                payload=payload,
            )
        elif tool_name in {
            "handoff.require",
            "consult.request",
            "informed.update",
            "governance.record_exception",
        }:
            self._record_governance_tool(
                call_id=call_id,
                role_instance_id=role_instance_id,
                tool_name=tool_name,
                payload=payload,
            )
        elif tool_name in TERMINAL_TOOLS or tool_name == "status.update":
            return
        else:
            raise ValueError(f"unknown V3 tool: {tool_name}")

    def _send_message(self, payload: dict[str, Any]) -> None:
        if self.stakeholder_bridge is None:
            raise ValueError("stakeholder bridge is not configured")
        self.stakeholder_bridge.send(
            OutboundMessage(
                connector=_required(payload, "connector"),
                target_ref=_required(payload, "target_ref"),
                text_markdown=_required(payload, "text_markdown"),
                thread_ref=_optional(payload.get("thread_ref")),
                importance=str(payload.get("importance") or "normal"),
            )
        )

    def _send_optional_reply(self, payload: dict[str, Any]) -> None:
        target_ref = _optional(payload.get("target_ref"))
        if target_ref is None:
            return
        if self.stakeholder_bridge is None:
            raise ValueError("stakeholder bridge is not configured")
        text_markdown = _optional(payload.get("text_markdown") or payload.get("message"))
        if text_markdown is None:
            raise ValueError("text_markdown or message is required")
        self.stakeholder_bridge.send(
            OutboundMessage(
                connector=_required(payload, "connector"),
                target_ref=target_ref,
                text_markdown=text_markdown,
                thread_ref=_optional(payload.get("thread_ref")),
                importance=str(payload.get("importance") or "normal"),
            )
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
        should_deliver = _optional(payload.get("target_ref") or payload.get("stakeholder_ref")) is not None
        if should_deliver and self.stakeholder_bridge is None:
            raise ValueError("stakeholder bridge is not configured")
        self._record_governance_tool(
            call_id=call_id,
            role_instance_id=role_instance_id,
            tool_name=tool_name,
            payload=payload,
        )
        if not should_deliver:
            return
        question = _required(payload, "question")
        text_markdown = _optional(payload.get("text_markdown")) or f"**Question**\n\n{question}"
        self.stakeholder_bridge.send(
            OutboundMessage(
                connector=_required(payload, "connector"),
                target_ref=str(target_ref),
                text_markdown=text_markdown,
                thread_ref=_optional(payload.get("thread_ref")),
                importance=str(payload.get("importance") or "high"),
            )
        )

    def _request_approval(self, *, role_instance_id: str, payload: dict[str, Any]) -> None:
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
        )
        if target_ref is None:
            return
        text_markdown = _optional(payload.get("text_markdown")) or (
            f"**Approval requested**\n\n{question}\n\n"
            f"Work item: `{work_item_id}`\n\n"
            f"Approval ID: `{approval_id}`"
        )
        self.stakeholder_bridge.send(
            OutboundMessage(
                connector=_required(payload, "connector"),
                target_ref=target_ref,
                text_markdown=text_markdown,
                thread_ref=_optional(payload.get("thread_ref")),
                importance=str(payload.get("importance") or "high"),
            )
        )

    def _link_artifact(self, *, call_id: str, role_instance_id: str, payload: dict[str, Any]) -> None:
        relative_path = _safe_relative_path(_required(payload, "relative_path"))
        filename = str(payload.get("filename") or PurePosixPath(relative_path).name)
        if not filename:
            raise ValueError("filename is required")
        self.db.add_artifact(
            artifact_id=str(payload.get("artifact_id") or f"artifact-{call_id}"),
            work_item_id=_required(payload, "work_item_id"),
            filename=filename,
            title=str(payload.get("title") or filename),
            relative_path=relative_path,
            document_type=str(payload.get("document_type") or "artifact"),
            status=str(payload.get("status") or "linked"),
            created_by_role=role_from_instance(role_instance_id),
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
        if tool_name == "handoff.require":
            _validate_handoff_requirements(payload)
        elif tool_name == "consult.request":
            _validate_consult_request(payload)
        elif tool_name == "informed.update":
            _validate_informed_update(payload)
        elif tool_name == "governance.record_exception":
            _validate_governance_exception(payload)
        self.db.record_governance_record(
            record_id=str(payload.get("record_id") or f"governance-{call_id}"),
            work_item_id=_required(payload, "work_item_id"),
            record_type=tool_name,
            role_instance_id=role_instance_id,
            target_ref=_target_ref(payload),
            summary=_summary(payload),
            status=str(payload.get("status") or _default_governance_status(tool_name)),
            payload=payload,
        )
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
        target_role = _optional(payload.get("target_role"))
        if target_role is None:
            return
        self.broker.publish(
            self.broker_stream,
            f"agent.{target_role}",
            {
                "message_type": tool_name,
                "source_call_id": call_id,
                "source_role_instance_id": role_instance_id,
                "work_item_id": _required(payload, "work_item_id"),
                "summary": _summary(payload),
                "payload": payload,
            },
        )


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value) == "":
        raise ValueError(f"{key} is required")
    return str(value)


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


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


def _target_ref(payload: dict[str, Any]) -> str | None:
    return _optional(payload.get("target_ref") or payload.get("target_role") or payload.get("stakeholder_ref"))


def _summary(payload: dict[str, Any]) -> str:
    for key in ("summary", "question", "reason", "required_next_action", "message"):
        value = payload.get(key)
        if value is not None and str(value):
            return str(value)
    raise ValueError("summary, question, reason, required_next_action, or message is required")


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


def _default_governance_status(tool_name: str) -> str:
    return {
        "consult.request": "requested",
        "handoff.require": "required",
        "informed.update": "sent",
        "stakeholder.ask_question": "requested",
        "governance.record_exception": "exception_recorded",
    }.get(tool_name, "recorded")
