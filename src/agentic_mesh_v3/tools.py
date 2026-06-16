from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from agentic_mesh_v3.authority import ToolAuthorityPolicy
from agentic_mesh_v3.authority import role_from_instance
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
        authority_policy: ToolAuthorityPolicy | None = None,
        telemetry: V3Telemetry | None = None,
    ) -> None:
        self.db = db
        self.document_library = document_library
        self.deployment_targets = deployment_targets or {}
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
            self.db.upsert_work_item(
                work_item_id=_required(payload, "work_item_id"),
                queue_item_id=_optional(payload.get("queue_item_id")),
                title=_required(payload, "title"),
                description=_required(payload, "description"),
                state=str(payload.get("state") or "queued"),
                owner_role=_required(payload, "owner_role"),
                current_phase=_optional(payload.get("current_phase")),
                next_action=str(payload.get("next_action") or ""),
                governance=_dict(payload.get("governance")),
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
                    governance_waits=tuple(str(item) for item in payload.get("governance_waits") or ()),
                )
            )
        elif tool_name == "document.write_work_item_index":
            self._write_work_item_index(call_id=call_id, role_instance_id=role_instance_id, payload=payload)
        elif tool_name == "document.write_root_work_item_index":
            self._write_root_work_item_index()
        elif tool_name == "approval.request":
            self.db.request_approval(
                approval_id=str(payload.get("approval_id") or f"approval-{uuid4().hex}"),
                work_item_id=_required(payload, "work_item_id"),
                requested_by_role=role_from_instance(role_instance_id),
                question=_required(payload, "question"),
            )
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
            result = target.deploy()
            self.db.record_release(
                release_id=str(payload.get("release_id") or f"release-{uuid4().hex}"),
                work_item_id=_required(payload, "work_item_id"),
                status=result.status,
                scope=str(payload.get("scope") or f"Deployment target {target_id}"),
                deployment_result=result.output,
                rollback_plan=result.rollback_plan,
                residual_risks=str(payload.get("residual_risks") or "None recorded"),
            )
        elif tool_name == "memory.propose_update":
            self.db.record_role_memory(
                memory_id=str(payload.get("memory_id") or f"memory-{call_id}"),
                role_instance_id=role_instance_id,
                summary=_required(payload, "summary"),
                source_ref=_required(payload, "source_ref"),
            )
        elif tool_name in TERMINAL_TOOLS or tool_name in {
            "messaging.send",
            "handoff.require",
            "consult.request",
            "informed.update",
            "governance.record_exception",
            "stakeholder.ask_question",
            "status.update",
        }:
            return
        else:
            raise ValueError(f"unknown V3 tool: {tool_name}")

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
            decisions=tuple(str(item) for item in payload.get("decisions") or ()),
            risks=tuple(str(item) for item in payload.get("risks") or ()),
            next_action=str(payload.get("next_action") or ""),
        )
        ref = write_work_item_index(self.document_library, index)
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


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
