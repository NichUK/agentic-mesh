from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_mesh.models import AgentRunResult
from agentic_mesh.models import DocumentUpdate
from agentic_mesh.models import FlowState
from agentic_mesh.models import Handoff
from agentic_mesh.models import Message
from agentic_mesh.models import RouteRequest
from agentic_mesh.models import RoleInstanceConfig
from agentic_mesh.models import utc_now_iso
from agentic_mesh.prompt_templates import render_prompt_template


SAFE_OUTPUT_SCHEMA_VERSION = "safe-output-v1"

SAFE_OUTPUT_TOOLS: tuple[str, ...] = (
    "work_item.update_summary",
    "document.propose_update",
    "document.add_review_comment",
    "document.link_artifact",
    "sponsor.ask_question",
    "sponsor.propose_decision",
    "handoff.propose",
    "route.consult",
    "route.raise_blocker",
    "queue.propose_item",
    "subslice.propose",
    "test_evidence.record",
    "release.propose_candidate",
    "risk.register",
    "decision.record",
    "memory.propose_update",
    "status.report_progress",
    "status.report_completion",
    "noop",
    "report_incomplete",
)

TERMINAL_SAFE_OUTPUT_TOOLS: frozenset[str] = frozenset(
    {
        "status.report_completion",
        "noop",
        "report_incomplete",
        "route.raise_blocker",
        "sponsor.ask_question",
        "sponsor.propose_decision",
        "handoff.propose",
    }
)


@dataclass(frozen=True)
class SafeOutputRecord:
    tool: str
    payload: dict[str, Any]
    recorded_at: str
    context: dict[str, Any]
    validation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SAFE_OUTPUT_SCHEMA_VERSION,
            "tool": self.tool,
            "payload": self.payload,
            "recorded_at": self.recorded_at,
            "context": self.context,
            "validation": self.validation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SafeOutputRecord":
        return cls(
            tool=str(data["tool"]),
            payload=dict(data.get("payload") or {}),
            recorded_at=str(data.get("recorded_at") or ""),
            context=dict(data.get("context") or {}),
            validation=dict(data.get("validation") or {}),
        )


def safe_output_context_from_env() -> dict[str, Any]:
    keys = [
        "AGENTIC_MESH_PROJECT_ID",
        "AGENTIC_MESH_ROLE_ID",
        "AGENTIC_MESH_ROLE_INSTANCE_ID",
        "AGENTIC_MESH_WORK_ITEM_ID",
        "AGENTIC_MESH_WORK_ITEM_TYPE",
        "AGENTIC_MESH_LIFECYCLE_STATE",
        "AGENTIC_MESH_MESSAGE_ID",
        "AGENTIC_MESH_CORRELATION_ID",
        "AGENTIC_MESH_SAFE_OUTPUT_FILE",
    ]
    return {key: os.environ.get(key) for key in keys if os.environ.get(key)}


def safe_output_file_from_env() -> Path:
    value = os.environ.get("AGENTIC_MESH_SAFE_OUTPUT_FILE")
    if not value:
        raise ValueError("AGENTIC_MESH_SAFE_OUTPUT_FILE is not set")
    return Path(value)


def append_safe_output_record(
    *,
    output_file: Path,
    tool: str,
    payload: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> SafeOutputRecord:
    validation = validate_safe_output_payload(tool, payload)
    record = SafeOutputRecord(
        tool=tool,
        payload=payload,
        recorded_at=utc_now_iso(),
        context=context or {},
        validation=validation,
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return record


def load_safe_output_records(output_file: Path) -> list[SafeOutputRecord]:
    if not output_file.exists():
        return []
    records: list[SafeOutputRecord] = []
    for line in output_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("safe-output record must be a JSON object")
        record = SafeOutputRecord.from_dict(data)
        validate_safe_output_payload(record.tool, record.payload)
        records.append(record)
    return records


def validate_safe_output_payload(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    if tool not in SAFE_OUTPUT_TOOLS:
        raise ValueError(f"unsupported safe-output tool `{tool}`")
    if not isinstance(payload, dict):
        raise ValueError("safe-output payload must be an object")

    required_by_tool: dict[str, tuple[str, ...]] = {
        "document.propose_update": ("path", "content"),
        "document.add_review_comment": ("path", "comment"),
        "document.link_artifact": ("path",),
        "sponsor.ask_question": ("question",),
        "sponsor.propose_decision": ("decision",),
        "handoff.propose": ("target_role", "message_type"),
        "route.consult": ("target_role", "message_type"),
        "route.raise_blocker": ("reason",),
        "queue.propose_item": ("title", "summary"),
        "subslice.propose": ("title", "summary"),
        "test_evidence.record": ("summary",),
        "release.propose_candidate": ("summary",),
        "risk.register": ("risk",),
        "decision.record": ("decision",),
        "memory.propose_update": ("summary", "provenance"),
        "status.report_progress": ("message",),
        "status.report_completion": ("message",),
        "noop": ("message",),
        "report_incomplete": ("reason",),
    }
    missing = [
        name
        for name in required_by_tool.get(tool, ())
        if not isinstance(payload.get(name), str) or not payload.get(name, "").strip()
    ]
    if missing:
        raise ValueError(
            f"safe-output `{tool}` missing required fields: {', '.join(missing)}"
        )

    for path_field in ["path", "artifact_path", "review_artifact_path"]:
        if path_field in payload:
            _validate_relative_path(str(payload[path_field]))
    return {"status": "valid", "validated_at": utc_now_iso()}


def result_from_safe_output_records(
    *,
    records: list[SafeOutputRecord],
    message: Message,
    flow_state: FlowState,
) -> AgentRunResult:
    if not records:
        raise ValueError("agent did not call any safe-output tool")

    terminal = [record for record in records if record.tool in TERMINAL_SAFE_OUTPUT_TOOLS]
    if not terminal:
        raise ValueError("agent did not call a terminal safe-output tool")
    terminal_record = terminal[-1]

    updates = _document_updates_from_records(records)
    routes = _routes_from_records(records)
    handoffs = _handoffs_from_records(records, message, flow_state)

    if terminal_record.tool == "route.raise_blocker":
        return AgentRunResult(
            status="blocked",
            message=str(terminal_record.payload.get("reason") or "Blocked."),
            document_updates=[],
            routes=[],
            handoffs=[],
        )
    if terminal_record.tool == "report_incomplete":
        return AgentRunResult(
            status="failed",
            message=str(terminal_record.payload.get("reason") or "Incomplete."),
            document_updates=[],
            routes=[],
            handoffs=[],
        )
    if terminal_record.tool in {"sponsor.ask_question", "sponsor.propose_decision"}:
        message_text = str(
            terminal_record.payload.get("question")
            or terminal_record.payload.get("decision")
            or "Sponsor input required."
        )
        return AgentRunResult(
            status="needs_clarification",
            message=message_text,
            document_updates=updates,
            routes=routes,
            handoffs=handoffs,
        )

    if terminal_record.tool == "noop":
        message_text = str(terminal_record.payload.get("message") or "No action needed.")
    else:
        message_text = str(
            terminal_record.payload.get("message")
            or terminal_record.payload.get("summary")
            or f"Completed via {terminal_record.tool}."
        )
    return AgentRunResult(
        status="completed",
        message=message_text,
        document_updates=updates,
        routes=routes,
        handoffs=handoffs,
    )


def write_safe_output_audit(
    *,
    document_library_root: Path,
    instance: RoleInstanceConfig,
    message: Message,
    records: list[SafeOutputRecord],
    result: AgentRunResult | None = None,
    validation_error: str | None = None,
) -> dict[str, str]:
    work_item_id = str(message.payload.get("work_item_id") or "").strip()
    root = document_library_root / "debug" / "safe-outputs"
    if work_item_id:
        root = document_library_root / "work-items" / _safe_path_part(work_item_id) / "debug" / "safe-outputs"
    root = root / _safe_path_part(instance.instance_id)
    root.mkdir(parents=True, exist_ok=True)
    stem = f"{_timestamp_path_part()}-{_safe_path_part(message.message_id)}"
    path = root / f"{stem}.safe-outputs.json"
    payload = {
        "schema_version": SAFE_OUTPUT_SCHEMA_VERSION,
        "audit_kind": "safe_output_run",
        "recorded_at": utc_now_iso(),
        "project_id": instance.project_id,
        "role_id": instance.role_id,
        "role_instance_id": instance.instance_id,
        "work_item_id": work_item_id or None,
        "message_id": message.message_id,
        "records": [record.to_dict() for record in records],
        "validation_error": validation_error,
        "runtime_interpretation": (
            {
                "status": result.status,
                "message": result.message,
                "document_updates": [update.path for update in result.document_updates],
                "routes": [
                    {
                        "target_role": route.target_role,
                        "message_type": route.message_type,
                        "origin": route.origin,
                    }
                    for route in result.routes
                ],
                "handoffs": [
                    {
                        "target_role": handoff.target_role,
                        "message_type": handoff.message_type,
                    }
                    for handoff in result.handoffs
                ],
            }
            if result is not None
            else None
        ),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {"path": str(path.relative_to(document_library_root)).replace("\\", "/")}


def safe_output_tools_prompt() -> str:
    return render_prompt_template(
        "safe-output-tools.md",
        {
            "safe_output_command": (
                "python -m agentic_mesh.cli safe-output <tool-name> . < /tmp/payload.json"
            ),
            "terminal_tools": "\n".join(
                f"- {tool}" for tool in sorted(TERMINAL_SAFE_OUTPUT_TOOLS)
            ),
            "available_tools": "\n".join(f"- {tool}" for tool in SAFE_OUTPUT_TOOLS),
        },
    )


def _document_updates_from_records(
    records: list[SafeOutputRecord],
) -> list[DocumentUpdate]:
    updates: list[DocumentUpdate] = []
    for record in records:
        if record.tool != "document.propose_update":
            continue
        payload = record.payload
        updates.append(
            DocumentUpdate(
                path=str(payload["path"]),
                content=str(payload["content"]),
                purpose=_optional_string(payload.get("purpose")),
                review_status=_optional_string(payload.get("review_status")),
                index_summary=_optional_string(payload.get("index_summary")),
                maintain_work_item_index=(
                    bool(payload["maintain_work_item_index"])
                    if "maintain_work_item_index" in payload
                    else None
                ),
            )
        )
    return updates


def _routes_from_records(records: list[SafeOutputRecord]) -> list[RouteRequest]:
    routes: list[RouteRequest] = []
    for record in records:
        if record.tool != "route.consult":
            continue
        payload = record.payload
        routes.append(
            RouteRequest(
                target_role=str(payload["target_role"]),
                message_type=str(payload["message_type"]),
                payload=_route_payload(payload),
                origin="consult",
            )
        )
    return routes


def _handoffs_from_records(
    records: list[SafeOutputRecord],
    message: Message,
    flow_state: FlowState,
) -> list[Handoff]:
    handoffs: list[Handoff] = []
    for record in records:
        if record.tool != "handoff.propose":
            continue
        payload = record.payload
        handoffs.append(
            Handoff(
                target_role=str(payload["target_role"]),
                message_type=str(payload["message_type"]),
                payload=_route_payload(payload, message=message, flow_state=flow_state),
            )
        )
    return handoffs


def _route_payload(
    payload: dict[str, Any],
    *,
    message: Message | None = None,
    flow_state: FlowState | None = None,
) -> dict[str, Any]:
    routed = dict(payload.get("payload") or {})
    for key in [
        "title",
        "summary",
        "work_item_id",
        "work_item_type",
        "lifecycle_state",
        "previous_lifecycle_state",
        "source_message_id",
        "out_of_flow_reason",
        "review_status",
        "correction_status",
        "defect_id",
        "gate_id",
        "review_artifact_path",
        "required_change",
        "evidence_required",
    ]:
        if key in payload and key not in routed:
            routed[key] = payload[key]
    if message is not None:
        routed.setdefault("title", message.payload.get("title"))
        routed.setdefault("summary", message.payload.get("summary") or message.payload.get("text"))
        routed.setdefault("work_item_id", message.payload.get("work_item_id"))
        routed.setdefault("work_item_type", message.payload.get("work_item_type"))
        routed.setdefault("source_message_id", message.message_id)
    if flow_state is not None:
        routed.setdefault("previous_lifecycle_state", flow_state.state_id)
    routed.setdefault("out_of_flow", bool(routed.get("out_of_flow_reason")))
    return routed


def _validate_relative_path(value: str) -> None:
    path = value.strip()
    if not path:
        raise ValueError("path fields must not be empty")
    requested = Path(path)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("path fields must be safe relative paths")


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _safe_path_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def _timestamp_path_part() -> str:
    return utc_now_iso().replace(":", "").replace(".", "-").replace("+", "Z")
