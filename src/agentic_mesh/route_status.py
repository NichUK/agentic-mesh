from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_mesh.models import utc_now_iso
from agentic_mesh.problem_status import redact_optional_text
from agentic_mesh.problem_status import redact_text
from agentic_mesh.problem_status import safe_artifact_paths
from agentic_mesh.problem_status import source_anchor_ref
from agentic_mesh.problem_status import source_anchor_summary


CURRENT_ROUTE_SCHEMA_VERSION = "current-route-v0"
ROUTE_KINDS = {
    "configured_handoff",
    "configured_consult",
    "configured_correction",
    "reasoned_out_of_flow",
}
ROUTE_STATUSES = {
    "handoff_requested",
    "consult_requested",
    "correction_requested",
    "out_of_flow_requested",
}


@dataclass(frozen=True)
class CurrentRoute:
    work_item_id: str
    work_item_type: str | None
    queue_item_id: str | None
    source_message_id: str | None
    source_anchor_ref: str | None
    source_anchor_summary: str | None
    correlation_id: str
    route_id: str
    route_kind: str
    route_status: str
    source_role: str
    target_role: str
    source_lifecycle_state: str
    target_lifecycle_state: str
    message_type: str
    configured_route_id: str | None = None
    out_of_flow: bool = False
    out_of_flow_reason: str | None = None
    review_status: str | None = None
    correction_status: str | None = None
    defect_id: str | None = None
    gate_id: str | None = None
    review_artifact_path: str | None = None
    required_change: str | None = None
    evidence_required: str | None = None
    status_url: str | None = None
    delivered_message_id: str | None = None
    requested_at: str = ""
    schema_version: str = CURRENT_ROUTE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.route_kind not in ROUTE_KINDS:
            raise ValueError(f"unsupported route kind `{self.route_kind}`")
        if self.route_status not in ROUTE_STATUSES:
            raise ValueError(f"unsupported route status `{self.route_status}`")
        if not self.requested_at:
            object.__setattr__(self, "requested_at", utc_now_iso())
        object.__setattr__(self, "source_anchor_summary", redact_optional_text(self.source_anchor_summary))
        for field_name in [
            "out_of_flow_reason",
            "review_status",
            "correction_status",
            "defect_id",
            "gate_id",
            "required_change",
            "evidence_required",
        ]:
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, redact_text(str(value)))
        if self.review_artifact_path is not None:
            paths = safe_artifact_paths([self.review_artifact_path])
            object.__setattr__(
                self,
                "review_artifact_path",
                paths[0] if paths else None,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }

    def journal_fields(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "work_item_id": self.work_item_id,
            "work_item_type": self.work_item_type,
            "queue_item_id": self.queue_item_id,
            "source_message_id": self.source_message_id,
            "source_anchor_ref": self.source_anchor_ref,
            "correlation_id": self.correlation_id,
            "route_id": self.route_id,
            "route_kind": self.route_kind,
            "route_status": self.route_status,
            "configured_route_id": self.configured_route_id,
            "source_role": self.source_role,
            "target_role": self.target_role,
            "source_lifecycle_state": self.source_lifecycle_state,
            "target_lifecycle_state": self.target_lifecycle_state,
            "message_type": self.message_type,
            "out_of_flow": self.out_of_flow,
            "delivered_message_id": self.delivered_message_id,
        }


class CurrentRouteStore:
    def __init__(self, state_root: Path, project_id: str) -> None:
        self.root = state_root / "projects" / project_id / "work_items"

    def current_path(self, work_item_id: str) -> Path:
        return self.root / work_item_id / "current-route.json"

    def history_path(self, work_item_id: str) -> Path:
        return self.root / work_item_id / "current-route-history.jsonl"

    def write_current(self, route: CurrentRoute) -> Path:
        path = self.current_path(route.work_item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(route.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
        with self.history_path(route.work_item_id).open("a", encoding="utf-8") as handle:
            json.dump(route.to_dict(), handle, sort_keys=True)
            handle.write("\n")
        return path

    def read_current(self, work_item_id: str) -> dict[str, Any] | None:
        path = self.current_path(work_item_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)


def route_id_for(
    *,
    source_message_id: str | None,
    target_role: str,
    message_type: str,
    target_lifecycle_state: str,
    payload: dict[str, Any],
) -> str:
    stable_payload = {
        key: payload.get(key)
        for key in [
            "work_item_id",
            "work_item_type",
            "lifecycle_state",
            "review_status",
            "correction_status",
            "defect_id",
            "gate_id",
            "review_artifact_path",
            "required_change",
            "evidence_required",
            "out_of_flow_reason",
        ]
        if payload.get(key) is not None
    }
    raw = json.dumps(
        {
            "source_message_id": source_message_id,
            "target_role": target_role,
            "message_type": message_type,
            "target_lifecycle_state": target_lifecycle_state,
            "payload": stable_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "route-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def current_route_from_payload(
    *,
    work_item_id: str,
    work_item_type: str | None,
    queue_item_id: str | None,
    source_message_id: str | None,
    source_anchor: Any,
    correlation_id: str,
    route_id: str,
    route_kind: str,
    route_status: str,
    source_role: str,
    target_role: str,
    source_lifecycle_state: str,
    target_lifecycle_state: str,
    message_type: str,
    payload: dict[str, Any],
    configured_route_id: str | None,
    out_of_flow: bool,
    status_url: str | None,
) -> CurrentRoute:
    return CurrentRoute(
        work_item_id=work_item_id,
        work_item_type=work_item_type,
        queue_item_id=queue_item_id,
        source_message_id=source_message_id,
        source_anchor_ref=source_anchor_ref(source_anchor),
        source_anchor_summary=source_anchor_summary(source_anchor),
        correlation_id=correlation_id,
        route_id=route_id,
        route_kind=route_kind,
        route_status=route_status,
        configured_route_id=configured_route_id,
        source_role=source_role,
        target_role=target_role,
        source_lifecycle_state=source_lifecycle_state,
        target_lifecycle_state=target_lifecycle_state,
        message_type=message_type,
        out_of_flow=out_of_flow,
        out_of_flow_reason=payload.get("out_of_flow_reason"),
        review_status=payload.get("review_status"),
        correction_status=payload.get("correction_status"),
        defect_id=payload.get("defect_id"),
        gate_id=payload.get("gate_id"),
        review_artifact_path=payload.get("review_artifact_path"),
        required_change=payload.get("required_change"),
        evidence_required=payload.get("evidence_required"),
        status_url=status_url,
    )


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
