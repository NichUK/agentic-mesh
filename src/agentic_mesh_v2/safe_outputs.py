from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.documents import DocumentFramework
from agentic_mesh_v2.documents import TOGAF_SDLC_V1
from agentic_mesh_v2.documents import read_existing
from agentic_mesh_v2.documents import render_path
from agentic_mesh_v2.documents import validate_document_content
from agentic_mesh_v2.release import ReleaseEvidence
from agentic_mesh_v2.release import ReleaseEvidenceLink
from agentic_mesh_v2.release import ReleaseService
from agentic_mesh_v2.state_machine import TransitionRequest


TERMINAL_TOOLS: frozenset[str] = frozenset(
    {
        "status.reply",
        "status.complete",
        "sponsor.ask_question",
        "human_response.request",
        "handoff.request",
        "consult.request",
        "queue.propose_item",
        "release.request_approval",
        "release.close",
        "quality.approve",
        "quality.request_changes",
        "noop",
        "report.blocked",
        "report.incomplete",
    }
)

COMMON_TOOLS: frozenset[str] = frozenset(
    {
        "status.reply",
        "status.progress",
        "status.complete",
        "sponsor.ask_question",
        "human_response.request",
        "handoff.request",
        "consult.request",
        "queue.propose_item",
        "document.propose_update",
        "document.add_review_comment",
        "memory.propose_update",
        "risk.register",
        "decision.record",
        "relevance.record",
        "noop",
        "report.blocked",
        "report.incomplete",
    }
)

ROLE_TOOLS: dict[str, frozenset[str]] = {
    "product-manager": COMMON_TOOLS
    | {
        "product.mark_sponsor_ready",
        "work_item.mark_ready",
    },
    "engineering": COMMON_TOOLS
    | {
        "test_evidence.record",
        "implementation.record_change",
    },
    "qa-engineer": COMMON_TOOLS
    | {
        "test_evidence.record",
        "quality.approve",
        "quality.request_changes",
    },
    "release-manager": COMMON_TOOLS
    | {
        "release.request_approval",
        "release.record_decision",
        "release.deploy",
        "release.record_no_deployment",
        "release.rollback_plan",
        "release.close",
        "work_item.close",
        "work_item.supersede",
        "work_item.reopen",
        "work_item.override_blocker",
    },
}

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "status.reply": ("message",),
    "status.progress": ("message",),
    "status.complete": ("message",),
    "sponsor.ask_question": ("question", "reason"),
    "human_response.request": (
        "title",
        "question",
        "response_contract_id",
        "required_authority",
        "destination_ref",
    ),
    "handoff.request": ("target_role", "reason"),
    "consult.request": ("target_role", "reason"),
    "queue.propose_item": (
        "title",
        "summary",
        "source_ref",
        "rationale",
        "urgency",
        "suggested_owner",
        "work_type",
        "classification",
        "initiated_by",
    ),
    "document.propose_update": ("path", "document_type", "content"),
    "document.add_review_comment": ("path", "comment"),
    "memory.propose_update": ("summary", "provenance_ref"),
    "risk.register": ("risk", "impact"),
    "decision.record": ("decision", "rationale"),
    "relevance.record": ("conversation_event_id", "score", "threshold", "decision", "reason"),
    "noop": ("reason",),
    "report.blocked": ("reason", "owner", "next_action"),
    "report.incomplete": ("reason",),
    "product.mark_sponsor_ready": ("work_item_id", "summary"),
    "work_item.mark_ready": ("work_item_id", "reason"),
    "test_evidence.record": ("work_item_id", "summary"),
    "implementation.record_change": ("work_item_id", "summary"),
    "quality.approve": ("work_item_id", "summary"),
    "quality.request_changes": ("work_item_id", "reason"),
    "release.request_approval": ("work_item_id", "question"),
    "release.record_decision": ("work_item_id", "decision"),
    "release.deploy": (
        "work_item_id",
        "target_id",
        "reason",
        "scope",
        "rollback_plan",
        "residual_risks",
        "approval_ref",
        "commit_ref",
    ),
    "release.record_no_deployment": (
        "work_item_id",
        "reason",
        "scope",
        "rollback_plan",
        "residual_risks",
        "approval_ref",
        "commit_ref",
    ),
    "release.rollback_plan": ("work_item_id", "plan"),
    "release.close": ("work_item_id", "reason"),
    "work_item.close": ("work_item_id", "reason"),
    "work_item.supersede": ("work_item_id", "reason", "replacement_ref"),
    "work_item.reopen": ("work_item_id", "target_role", "reason"),
    "work_item.override_blocker": ("work_item_id", "reason"),
}

NUMERIC_FIELDS: dict[str, tuple[str, ...]] = {
    "relevance.record": ("score", "threshold"),
}


class SafeOutputError(ValueError):
    pass


@dataclass(frozen=True)
class ToolPolicy:
    role_tools: dict[str, frozenset[str]] = field(default_factory=lambda: ROLE_TOOLS)

    def tools_for_role(self, role_id: str) -> frozenset[str]:
        return self.role_tools.get(role_id, COMMON_TOOLS)

    def authorize(self, *, role_id: str, tool_name: str) -> None:
        if tool_name not in self.tools_for_role(role_id):
            raise SafeOutputError(f"`{role_id}` is not authorized to use `{tool_name}`")


@dataclass(frozen=True)
class SafeOutputCall:
    tool_name: str
    payload: dict[str, Any]
    role_id: str
    terminal: bool = False


class SafeOutputService:
    def __init__(
        self,
        db: V2Database,
        policy: ToolPolicy | None = None,
        release_service: ReleaseService | None = None,
        process_effects: bool = True,
        document_library_root: Path | None = None,
        document_framework: DocumentFramework | None = None,
        project_id: str | None = None,
        role_memory_path_resolver: Callable[[str], Path | None] | None = None,
    ) -> None:
        self.db = db
        self.policy = policy or ToolPolicy()
        self.release_service = release_service or ReleaseService(db)
        self.process_effects = process_effects
        self.document_library_root = Path(document_library_root).resolve(strict=False) if document_library_root else None
        self.document_framework = document_framework or TOGAF_SDLC_V1
        self.project_id = _optional_text(project_id)
        self.role_memory_path_resolver = role_memory_path_resolver

    def record(self, *, run_id: str, call: SafeOutputCall) -> str:
        self.policy.authorize(role_id=call.role_id, tool_name=call.tool_name)
        validate_payload(call.tool_name, call.payload)
        if self.process_effects and call.tool_name == "document.add_review_comment":
            self._validate_document_review_comment_target(call)
        if self.process_effects and call.tool_name in {"implementation.record_change", "test_evidence.record"}:
            self._validate_work_item_evidence_target(call)
        if self.process_effects and call.tool_name in {"quality.approve", "quality.request_changes"}:
            self._validate_quality_decision_target(call)
        if self.process_effects and call.tool_name == "release.record_decision":
            self._validate_release_decision_target(call)
        if self.process_effects and call.tool_name in {"release.deploy", "release.record_no_deployment"}:
            self._validate_release_activation_approval(call)
        terminal = call.terminal or call.tool_name in TERMINAL_TOOLS
        call_id = f"call-{uuid4().hex}"
        self.db.record_safe_output(
            call_id=call_id,
            run_id=run_id,
            role_id=call.role_id,
            tool_name=call.tool_name,
            payload=call.payload,
            terminal=terminal,
        )
        if call.tool_name in {"handoff.request", "consult.request"}:
            self._record_role_assignment_from_route(
                call_id=call_id,
                run_id=run_id,
                call=call,
            )
        if self.process_effects:
            self.process_recorded_call(call_id=call_id, run_id=run_id, call=call)
        return call_id

    def process_recorded_call(self, *, call_id: str, run_id: str, call: SafeOutputCall) -> None:
        if call.tool_name == "document.propose_update":
            self._publish_document_update(call_id=call_id, run_id=run_id, call=call)
        if call.tool_name == "document.add_review_comment":
            self._append_document_review_comment(call_id=call_id, call=call)
        if call.tool_name == "memory.propose_update":
            self._publish_role_memory_update(call_id=call_id, call=call)
        if call.tool_name == "implementation.record_change":
            self._record_work_item_evidence(call_id=call_id, call=call, evidence_type="implementation_change")
        if call.tool_name == "test_evidence.record":
            self._record_work_item_evidence(call_id=call_id, call=call, evidence_type="test_evidence")
        if call.tool_name == "quality.approve":
            self._approve_quality(call_id=call_id, run_id=run_id, call=call)
        if call.tool_name == "quality.request_changes":
            self._request_quality_changes(call)
        if call.tool_name == "human_response.request":
            self._record_human_response_request(call_id=call_id, call=call, request_type="human_response")
        if call.tool_name == "release.request_approval":
            self._record_human_response_request(call_id=call_id, call=call, request_type="release_approval")
        if call.tool_name == "release.record_decision":
            self._record_release_decision(call_id=call_id, call=call)
        if call.tool_name == "release.record_no_deployment":
            self._record_no_deployment(call)
        if call.tool_name == "release.deploy":
            self._deploy_release(call)
        if call.tool_name == "release.close":
            self._close_released_work(call)
        return None

    def _publish_document_update(self, *, call_id: str, run_id: str, call: SafeOutputCall) -> None:
        if self.document_library_root is None:
            return
        if _artifact_exists(self.db, artifact_id=f"artifact-{call_id}"):
            return
        source_run = self.db.get_agent_run(run_id)
        work_item_id = _optional_text(call.payload.get("work_item_id"))
        if work_item_id is None and source_run is not None:
            work_item_id = _optional_text(source_run.get("work_item_id"))
        if work_item_id is None:
            raise SafeOutputError("document.propose_update requires work_item_id or a work-item-backed run")
        document_type = _required_text(call.payload, "document_type")
        relative_path = _required_text(call.payload, "path")
        expected_path = render_path(
            framework=self.document_framework,
            document_type=document_type,
            work_item_id=work_item_id,
        )
        if relative_path != expected_path:
            raise SafeOutputError(
                f"document.propose_update path must match framework path `{expected_path}`"
            )
        target = _contained_document_path(self.document_library_root, relative_path)
        content = _required_text(call.payload, "content")
        validate_document_content(
            framework=self.document_framework,
            document_type=document_type,
            content=content,
            existing_content=read_existing(self.document_library_root, relative_path),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        self.db.add_artifact(
            artifact_id=f"artifact-{call_id}",
            work_item_id=work_item_id,
            path=relative_path,
            document_type=document_type,
            status=_optional_text(call.payload.get("status")) or "proposed",
            created_by_role=call.role_id,
        )

    def _append_document_review_comment(self, *, call_id: str, call: SafeOutputCall) -> None:
        if self.document_library_root is None:
            return
        self._validate_document_review_comment_target(call)
        target = _contained_document_path(self.document_library_root, _required_text(call.payload, "path"))
        content = target.read_text(encoding="utf-8")
        if _review_comment_exists(content, call_id=call_id):
            return
        comment = _single_line_text(_required_text(call.payload, "comment"))
        target.write_text(
            _content_with_review_comment(
                content=content,
                call_id=call_id,
                role_id=call.role_id,
                comment=comment,
            ),
            encoding="utf-8",
        )

    def _validate_document_review_comment_target(self, call: SafeOutputCall) -> None:
        if self.document_library_root is None:
            return
        relative_path = _required_text(call.payload, "path")
        target = _contained_document_path(self.document_library_root, relative_path)
        if not target.exists():
            raise SafeOutputError(f"review comment target document `{relative_path}` was not found")
        content = target.read_text(encoding="utf-8")
        _review_log_bounds(content)

    def _publish_role_memory_update(self, *, call_id: str, call: SafeOutputCall) -> None:
        if self.role_memory_path_resolver is None:
            return
        memory_path = self.role_memory_path_resolver(call.role_id)
        if memory_path is None:
            return
        summary = _single_line_text(_required_text(call.payload, "summary"))
        provenance_ref = _single_line_text(_required_text(call.payload, "provenance_ref"))
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        existing = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
        marker = _memory_marker(summary=summary, provenance_ref=provenance_ref)
        if not _memory_marker_exists(existing, marker):
            memory_path.write_text(
                _memory_content_with_entry(
                    existing_content=existing,
                    call_id=call_id,
                    summary=summary,
                    provenance_ref=provenance_ref,
                ),
                encoding="utf-8",
            )
        if self.project_id is not None:
            self.db.add_role_memory(
                memory_id=f"memory-{call_id}",
                role_id=call.role_id,
                project_id=self.project_id,
                summary=summary,
                provenance_ref=provenance_ref,
            )

    def _validate_work_item_evidence_target(self, call: SafeOutputCall) -> None:
        work_item_id = _required_text(call.payload, "work_item_id")
        self.db.get_work_item(work_item_id)

    def _record_work_item_evidence(
        self,
        *,
        call_id: str,
        call: SafeOutputCall,
        evidence_type: str,
    ) -> None:
        self._validate_work_item_evidence_target(call)
        self.db.add_work_item_evidence(
            evidence_id=f"evidence-{call_id}",
            work_item_id=_required_text(call.payload, "work_item_id"),
            evidence_type=evidence_type,
            summary=_single_line_text(_required_text(call.payload, "summary")),
            role_id=call.role_id,
            safe_output_ref=call_id,
        )

    def _validate_quality_decision_target(self, call: SafeOutputCall) -> None:
        work_item = self.db.get_work_item(_required_text(call.payload, "work_item_id"))
        if work_item.state != "active":
            raise SafeOutputError(
                f"`{call.tool_name}` requires work item state `active`, found `{work_item.state}`"
            )
        if call.tool_name == "quality.approve" and not _has_test_evidence(self.db, work_item_id=work_item.work_item_id):
            raise SafeOutputError("quality.approve requires existing test_evidence for the work item")

    def _approve_quality(self, *, call_id: str, run_id: str, call: SafeOutputCall) -> None:
        work_item_id = _required_text(call.payload, "work_item_id")
        work_item = self.db.get_work_item(work_item_id)
        if work_item.state == "release_review":
            self._create_release_review_assignment(
                call_id=call_id,
                run_id=run_id,
                call=call,
                work_item=work_item,
            )
            return
        self._validate_quality_decision_target(call)
        self.db.transition_work_item(
            TransitionRequest(
                work_item_id=work_item_id,
                from_state="active",
                to_state="release_review",
                actor_role=call.role_id,
                reason=_required_text(call.payload, "summary"),
                owner="release-manager",
            )
        )
        self._create_release_review_assignment(
            call_id=call_id,
            run_id=run_id,
            call=call,
            work_item=work_item,
        )

    def _request_quality_changes(self, call: SafeOutputCall) -> None:
        work_item_id = _required_text(call.payload, "work_item_id")
        work_item = self.db.get_work_item(work_item_id)
        if work_item.state == "waiting_agent":
            return
        self._validate_quality_decision_target(call)
        reason = _required_text(call.payload, "reason")
        self.db.transition_work_item(
            TransitionRequest(
                work_item_id=work_item_id,
                from_state="active",
                to_state="waiting_agent",
                actor_role=call.role_id,
                reason=reason,
                owner="engineering",
                reason_class="quality_changes_requested",
                next_action=reason,
                retryable=True,
            )
        )

    def _validate_release_decision_target(self, call: SafeOutputCall) -> None:
        work_item_id = _required_text(call.payload, "work_item_id")
        work_item = self.db.get_work_item(work_item_id)
        if work_item.state != "release_review":
            raise SafeOutputError(
                f"`release.record_decision` requires work item state `release_review`, found `{work_item.state}`"
            )
        decision = _normalize_release_decision(_required_text(call.payload, "decision"))
        approval_ref = _release_approval_ref(call.payload)
        if approval_ref is None:
            return
        request = self.db.get_human_response_request(approval_ref)
        if request is None:
            raise SafeOutputError(f"release.record_decision approval_ref `{approval_ref}` was not found")
        if request.get("request_type") != "release_approval":
            raise SafeOutputError("release.record_decision approval_ref must reference a release_approval request")
        if request.get("work_item_id") != work_item_id:
            raise SafeOutputError("release.record_decision approval_ref belongs to a different work item")
        if request.get("status") != "responded":
            raise SafeOutputError("release.record_decision approval_ref has not been answered")
        response_value = _normalize_release_decision(str(request.get("response_value") or ""))
        if response_value != decision:
            raise SafeOutputError(
                f"release.record_decision decision `{decision}` does not match approval_ref response `{response_value}`"
            )

    def _record_release_decision(self, *, call_id: str, call: SafeOutputCall) -> None:
        if _has_work_item_evidence_ref(self.db, safe_output_ref=call_id):
            return
        self._validate_release_decision_target(call)
        self.db.add_work_item_evidence(
            evidence_id=f"evidence-{call_id}",
            work_item_id=_required_text(call.payload, "work_item_id"),
            evidence_type="release_decision",
            summary=_release_decision_summary(call.payload),
            role_id=call.role_id,
            safe_output_ref=call_id,
        )

    def _validate_release_activation_approval(self, call: SafeOutputCall) -> None:
        work_item_id = _required_text(call.payload, "work_item_id")
        self.db.get_work_item(work_item_id)
        approval_ref = _required_text(call.payload, "approval_ref")
        if not _has_approved_release_decision(
            self.db,
            work_item_id=work_item_id,
            approval_ref=approval_ref,
        ):
            raise SafeOutputError(
                f"`{call.tool_name}` requires approval_ref to reference an approved release decision for this work item"
            )

    def _record_human_response_request(
        self,
        *,
        call_id: str,
        call: SafeOutputCall,
        request_type: str,
    ) -> None:
        request_id = _human_response_request_id(call_id)
        if self.db.get_human_response_request(request_id) is not None:
            return
        work_item_id = _optional_text(call.payload.get("work_item_id"))
        title = _optional_text(call.payload.get("title")) or (
            "Release approval requested"
            if request_type == "release_approval"
            else "Human response requested"
        )
        question = _required_text(call.payload, "question")
        response_contract_id = _optional_text(call.payload.get("response_contract_id")) or (
            "release-decision-v1" if request_type == "release_approval" else "human-response-v1"
        )
        required_authority = _optional_text(call.payload.get("required_authority")) or "sponsor"
        destination_ref = _optional_text(call.payload.get("destination_ref")) or "sponsor"
        destination_type = _optional_text(call.payload.get("destination_type")) or "runtime"
        thread_ref = _optional_text(call.payload.get("thread_ref")) or f"thread-{call_id}"
        card = {
            "type": "AdaptiveCard",
            "version": "1.5",
            "title": title,
            "question": question,
            "request_id": request_id,
            "request_type": request_type,
            "response_contract_id": response_contract_id,
            "required_authority": required_authority,
            "work_item_id": work_item_id,
            "gate_id": _optional_text(call.payload.get("gate_id")),
            "actions": [
                {"type": "Action.Submit", "title": "Approve", "data": {"value": "approve"}},
                {"type": "Action.Submit", "title": "Reject", "data": {"value": "reject"}},
                {"type": "Action.Submit", "title": "Request changes", "data": {"value": "request_changes"}},
            ],
        }
        connector_id = _optional_text(call.payload.get("connector_id")) or "runtime"
        if connector_id == "runtime" and not _connector_exists(self.db, connector_id=connector_id):
            self.db.upsert_connector(
                connector_id=connector_id,
                project_id=self.project_id or "runtime",
                connector_type="runtime",
                display_name="Runtime",
                status="active",
                health={},
            )
        self.db.create_human_response_request(
            request_id=request_id,
            connector_id=connector_id,
            source_ref=call_id,
            request_type=request_type,
            title=title,
            question=question,
            required_authority=required_authority,
            response_contract_id=response_contract_id,
            created_by_role=call.role_id,
            destination_ref=destination_ref,
            destination_type=destination_type,
            work_item_id=work_item_id,
            gate_id=_optional_text(call.payload.get("gate_id")),
            target_ref=_optional_text(call.payload.get("target_ref")),
            thread_ref=thread_ref,
            payload={"card": card},
        )

    def _record_no_deployment(self, call: SafeOutputCall) -> None:
        self._validate_release_activation_approval(call)
        self.release_service.record_no_deployment(
            _release_evidence_from_payload(call.payload),
            reason=_required_text(call.payload, "reason"),
        )

    def _deploy_release(self, call: SafeOutputCall) -> None:
        self._validate_release_activation_approval(call)
        evidence = _release_evidence_from_payload(call.payload)
        if _has_successful_deployment(self.db, release_id=evidence.release_id, work_item_id=evidence.work_item_id):
            return
        self.release_service.deploy_compose_release(
            evidence,
            target_id=_required_text(call.payload, "target_id"),
            smoke_checks=_smoke_checks(call.payload.get("smoke_checks")),
            evidence_links=_release_evidence_links(call.payload.get("evidence_links")),
            timeout_seconds=_optional_int(call.payload.get("timeout_seconds"), default=300),
        )

    def _close_released_work(self, call: SafeOutputCall) -> None:
        work_item_id = _required_text(call.payload, "work_item_id")
        work_item = self.db.get_work_item(work_item_id)
        if work_item.state == "closed":
            return
        self.release_service.close_released_work(
            work_item_id=work_item_id,
            from_state=_optional_text(call.payload.get("from_state")) or work_item.state,
            actor_role=call.role_id,
            reason=_required_text(call.payload, "reason"),
        )

    def _record_role_assignment_from_route(
        self,
        *,
        call_id: str,
        run_id: str,
        call: SafeOutputCall,
    ) -> None:
        source_run = self.db.get_agent_run(run_id)
        target_role = _required_text(call.payload, "target_role")
        reason = _required_text(call.payload, "reason")
        work_item_id = _optional_text(call.payload.get("work_item_id"))
        if work_item_id is None and source_run is not None:
            work_item_id = _optional_text(source_run.get("work_item_id"))
        visibility_scope = _optional_text(call.payload.get("context_visibility")) or "project"
        assignment_type = "role_handoff" if call.tool_name == "handoff.request" else "role_consult"
        title = _optional_text(call.payload.get("title")) or (
            f"Handoff to {target_role}"
            if call.tool_name == "handoff.request"
            else f"Consult {target_role}"
        )
        payload = {
            "safe_output_ref": call_id,
            "source_run_id": run_id,
            "source_role": call.role_id,
            "target_role": target_role,
            "reason": reason,
            "route_tool": call.tool_name,
            "work_item_id": work_item_id,
            "current_flow_state": call.payload.get("current_flow_state"),
            "source_documents": _string_list(call.payload.get("source_documents")),
            "target_outputs": _string_list(call.payload.get("target_outputs")),
            "allowed_tools": sorted(self.policy.tools_for_role(target_role)),
            "context_visibility": visibility_scope,
        }
        self.db.create_role_assignment(
            assignment_id=f"assignment-{call_id}",
            role_id=target_role,
            work_item_id=work_item_id,
            source_ref=call_id,
            title=title,
            summary=reason,
            assignment_type=assignment_type,
            visibility_scope=visibility_scope,
            payload=payload,
        )

    def _create_release_review_assignment(
        self,
        *,
        call_id: str,
        run_id: str,
        call: SafeOutputCall,
        work_item: Any,
    ) -> None:
        assignment_id = f"assignment-{call_id}"
        if self.db.get_role_assignment(assignment_id) is not None:
            return
        summary = _single_line_text(_required_text(call.payload, "summary"))
        self.db.create_role_assignment(
            assignment_id=assignment_id,
            role_id="release-manager",
            work_item_id=work_item.work_item_id,
            source_ref=call_id,
            title=f"Release review: {work_item.title}",
            summary=summary,
            assignment_type="release_review",
            visibility_scope="project",
            payload={
                "safe_output_ref": call_id,
                "source_run_id": run_id,
                "source_role": call.role_id,
                "target_role": "release-manager",
                "reason": summary,
                "route_tool": call.tool_name,
                "work_item_id": work_item.work_item_id,
                "current_flow_state": "release_review",
                "allowed_tools": sorted(self.policy.tools_for_role("release-manager")),
            },
        )


def validate_payload(tool_name: str, payload: dict[str, Any]) -> None:
    if tool_name not in REQUIRED_FIELDS:
        raise SafeOutputError(f"unsupported safe-output tool `{tool_name}`")
    numeric_fields = set(NUMERIC_FIELDS.get(tool_name, ()))
    missing = []
    for field in REQUIRED_FIELDS[tool_name]:
        value = payload.get(field)
        if field in numeric_fields:
            try:
                float(value)
            except (TypeError, ValueError):
                missing.append(field)
        elif not isinstance(value, str) or not value.strip():
            missing.append(field)
    if missing:
        raise SafeOutputError(
            f"`{tool_name}` missing required fields: {', '.join(missing)}"
        )
    _reject_fake_claims(tool_name, payload)


def _reject_fake_claims(tool_name: str, payload: dict[str, Any]) -> None:
    if tool_name in {"status.reply", "status.complete"}:
        text = str(payload.get("message") or "").casefold()
        risky = [
            "created work-",
            "created queue-",
            "promoted work-",
            "deployed",
            "released",
            "closed work-",
            "approval received",
            "approved by sponsor",
            "sponsor approved",
        ]
        if any(marker in text for marker in risky):
            raise SafeOutputError(
                "status messages must not claim durable mutations; use the matching tool"
            )
        delivery_claims = [
            "delivered the teams reply successfully",
            "delivered successfully",
            "delivery succeeded",
            "human delivery succeeded",
            "teams delivery succeeded",
            "message was delivered",
            "sent successfully",
            "successfully sent",
            "sent the teams reply",
            "delivered to the human",
        ]
        if any(marker in text for marker in delivery_claims):
            raise SafeOutputError(
                "status messages must not claim connector delivery success; runtime delivery records own that fact"
            )
    for key in payload:
        if key in {"created_work_item_id", "created_queue_item_id"}:
            raise SafeOutputError("safe-output payloads must not invent created ids")
    if tool_name == "queue.propose_item":
        private_source_keys = {"body", "message", "raw_text", "raw_message"}
        if private_source_keys.intersection(payload):
            raise SafeOutputError(
                "queue proposals must store source references and rationale, not raw conversation text"
            )
    if tool_name == "release.deploy":
        if {"command", "cwd"}.intersection(payload):
            raise SafeOutputError(
                "release.deploy must use the configured deployment target command; command and cwd overrides are not allowed"
            )


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SafeOutputError(f"`{key}` must be a non-empty string")
    return value.strip()


def _release_evidence_from_payload(payload: dict[str, Any]) -> ReleaseEvidence:
    work_item_id = _required_text(payload, "work_item_id")
    return ReleaseEvidence(
        work_item_id=work_item_id,
        release_id=_optional_text(payload.get("release_id")) or f"release-{work_item_id}",
        scope=_required_text(payload, "scope"),
        commit_ref=_optional_text(payload.get("commit_ref")),
        approval_ref=_optional_text(payload.get("approval_ref")),
        rollback_plan=_required_text(payload, "rollback_plan"),
        residual_risks=_required_text(payload, "residual_risks"),
        deployment_result=_optional_text(payload.get("deployment_result")),
        smoke_result=_optional_text(payload.get("smoke_result")),
    )


def _normalize_release_decision(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "approve": "approve",
        "approved": "approve",
        "accept": "approve",
        "accepted": "approve",
        "reject": "reject",
        "rejected": "reject",
        "decline": "reject",
        "declined": "reject",
        "request_changes": "request_changes",
        "changes_requested": "request_changes",
        "change_requested": "request_changes",
        "needs_changes": "request_changes",
    }
    decision = aliases.get(normalized)
    if decision is None:
        raise SafeOutputError(
            "release.record_decision decision must be approve, reject, or request_changes"
        )
    return decision


def _release_approval_ref(payload: dict[str, Any]) -> str | None:
    approval_ref = _optional_text(payload.get("approval_ref"))
    response_request_id = _optional_text(payload.get("response_request_id"))
    if approval_ref is not None and response_request_id is not None and approval_ref != response_request_id:
        raise SafeOutputError("release.record_decision approval_ref and response_request_id must match when both are provided")
    return approval_ref or response_request_id


def _release_decision_summary(payload: dict[str, Any]) -> str:
    parts = [f"Release decision: {_normalize_release_decision(_required_text(payload, 'decision'))}"]
    approval_ref = _release_approval_ref(payload)
    if approval_ref is not None:
        parts.append(f"approval_ref: {approval_ref}")
    reason = _optional_text(payload.get("reason"))
    if reason is not None:
        parts.append(f"reason: {_single_line_text(reason)}")
    return "; ".join(parts)


def _smoke_checks(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise SafeOutputError("release.deploy requires non-empty smoke_checks")
    checks: dict[str, str] = {}
    for key, result in value.items():
        if not isinstance(key, str) or not key.strip():
            raise SafeOutputError("release.deploy smoke_checks keys must be non-empty strings")
        if not isinstance(result, str) or not result.strip():
            raise SafeOutputError("release.deploy smoke_checks results must be non-empty strings")
        checks[key.strip()] = result.strip()
    return checks


def _release_evidence_links(value: object) -> tuple[ReleaseEvidenceLink, ...]:
    if not isinstance(value, list) or not value:
        raise SafeOutputError("release.deploy requires evidence_links")
    links: list[ReleaseEvidenceLink] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SafeOutputError(f"release.deploy evidence_links item {index} must be an object")
        artifact_ref = _optional_text(item.get("artifact_ref")) or _optional_text(item.get("path"))
        artifact_type = _optional_text(item.get("artifact_type")) or _optional_text(item.get("type"))
        role_id = _optional_text(item.get("role_id"))
        if artifact_ref is None or artifact_type is None or role_id is None:
            raise SafeOutputError(
                f"release.deploy evidence_links item {index} requires artifact_ref, artifact_type, and role_id"
            )
        links.append(
            ReleaseEvidenceLink(
                artifact_ref=artifact_ref,
                artifact_type=artifact_type,
                role_id=role_id,
                status=_optional_text(item.get("status")) or "accepted",
            )
        )
    return tuple(links)


def _optional_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SafeOutputError("timeout_seconds must be a positive integer when provided")
    return value


def _has_successful_deployment(db: V2Database, *, release_id: str, work_item_id: str) -> bool:
    return any(
        run.get("release_id") == release_id
        and run.get("work_item_id") == work_item_id
        and run.get("status") == "succeeded"
        for run in db.list_deployment_runs()
    )


def _has_work_item_evidence_ref(db: V2Database, *, safe_output_ref: str) -> bool:
    return any(
        evidence.get("safe_output_ref") == safe_output_ref
        for evidence in db.list_work_item_evidence()
    )


def _has_approved_release_decision(
    db: V2Database,
    *,
    work_item_id: str,
    approval_ref: str,
) -> bool:
    for evidence in db.list_work_item_evidence():
        if evidence.get("work_item_id") != work_item_id:
            continue
        if evidence.get("evidence_type") != "release_decision":
            continue
        if not str(evidence.get("summary") or "").startswith("Release decision: approve"):
            continue
        if evidence.get("safe_output_ref") == approval_ref:
            return True
        if _release_decision_summary_field(str(evidence.get("summary") or ""), "approval_ref") == approval_ref:
            return True
    return False


def _release_decision_summary_field(summary: str, field_name: str) -> str | None:
    prefix = f"{field_name}: "
    for part in summary.split(";"):
        text = part.strip()
        if text.startswith(prefix):
            value = text[len(prefix) :].strip()
            return value or None
    return None


def _artifact_exists(db: V2Database, *, artifact_id: str) -> bool:
    return any(artifact.get("artifact_id") == artifact_id for artifact in db.list_artifacts())


def _has_test_evidence(db: V2Database, *, work_item_id: str) -> bool:
    return any(
        evidence.get("work_item_id") == work_item_id
        and evidence.get("evidence_type") == "test_evidence"
        for evidence in db.list_work_item_evidence()
    )


def _connector_exists(db: V2Database, *, connector_id: str) -> bool:
    return any(connector.get("connector_id") == connector_id for connector in db.list_connectors())


def _human_response_request_id(call_id: str) -> str:
    return f"human-response-{hashlib.sha256(call_id.encode('utf-8')).hexdigest()[:16]}"


def _contained_document_path(root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise SafeOutputError("document paths must be relative to the document library root")
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SafeOutputError("document path must resolve inside the document library root") from exc
    return resolved


def _memory_content_with_entry(
    *,
    existing_content: str,
    call_id: str,
    summary: str,
    provenance_ref: str,
) -> str:
    content = existing_content.strip()
    if not content:
        content = "# Role Memory"
    if "## Safe-Output Memory Updates" not in content:
        content = f"{content}\n\n## Safe-Output Memory Updates"
    entry = f"- {call_id} | {_memory_marker(summary=summary, provenance_ref=provenance_ref)}"
    return f"{content.rstrip()}\n{entry}\n"


def _content_with_review_comment(
    *,
    content: str,
    call_id: str,
    role_id: str,
    comment: str,
) -> str:
    lines = content.rstrip().splitlines()
    _review_start, insert_index = _review_log_bounds(content)
    entry = f"- {call_id} | {role_id} | review-comment | {comment}"
    if insert_index == len(lines):
        return "\n".join([*lines, entry]) + "\n"
    updated = [*lines[:insert_index], entry, "", *lines[insert_index:]]
    return "\n".join(updated).rstrip() + "\n"


def _review_comment_exists(content: str, *, call_id: str) -> bool:
    lines = content.splitlines()
    review_start, review_end = _review_log_bounds(content)
    expected_prefix = f"- {call_id} | "
    for line in lines[review_start + 1 : review_end]:
        text = line.strip()
        if text.startswith(expected_prefix) and " | review-comment | " in text:
            return True
    return False


def _review_log_bounds(content: str) -> tuple[int, int]:
    lines = content.rstrip().splitlines()
    review_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "## Review Log"),
        None,
    )
    if review_index is None:
        raise SafeOutputError("review comment target document must contain a `## Review Log` section")
    section_end = len(lines)
    for index in range(review_index + 1, len(lines)):
        if lines[index].startswith("## "):
            section_end = index
            break
    return review_index, section_end


def _memory_marker(*, summary: str, provenance_ref: str) -> str:
    return f"{provenance_ref} | {summary}"


def _memory_marker_exists(existing_content: str, marker: str) -> bool:
    marker_parts = marker.split(" | ", maxsplit=1)
    if len(marker_parts) != 2:
        return False
    for line in existing_content.splitlines():
        text = line.strip()
        if not text.startswith("- "):
            continue
        parts = text[2:].split(" | ", maxsplit=2)
        if len(parts) != 3:
            continue
        if f"{parts[1]} | {parts[2]}" == marker:
            return True
    return False


def _single_line_text(value: str) -> str:
    return " ".join(value.split())


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
