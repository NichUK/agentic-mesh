from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agentic_mesh.models import utc_now_iso
from agentic_mesh.problem_status import ProblemStatus
from agentic_mesh.problem_status import recovery_action_label
from agentic_mesh.problem_status import source_anchor_ref as problem_source_anchor_ref
from agentic_mesh.problem_status import source_anchor_summary


ACTIVATION_EVIDENCE_SCHEMA_VERSION = "activation-evidence-v0"
SMOKE_EVIDENCE_SCHEMA_VERSION = "smoke-evidence-v0"

IMPACT_CATEGORIES = {
    "runtime_code",
    "package_or_dependency",
    "container_image",
    "runtime_configuration",
    "service_restart_or_recreate",
    "schema_or_state_migration",
    "connector_surface",
    "route_or_ingress",
    "secret_or_identity_binding",
    "deployment_output",
    "telemetry_or_observability",
    "external_dependency_or_provider",
    "none",
}
ACTIVATION_PATHS = {
    "not_required",
    "reload_config",
    "restart_service",
    "recreate_container",
    "rebuild_image",
    "install_package_or_dependency",
    "run_migration",
    "refresh_connector",
    "update_route_or_ingress",
    "rotate_or_bind_secret",
    "update_deployment_output",
    "operator_action_required",
    "unknown_blocked",
}
SOURCE_STATUSES = {
    "not_started",
    "source_pending",
    "source_ready",
    "source_failed",
    "unknown",
}
ACTIVATION_STATUSES = {
    "not_required",
    "pending",
    "in_progress",
    "complete",
    "blocked",
    "failed",
    "residual_risk_accepted",
    "unknown",
}
SMOKE_STATUSES = {
    "not_required",
    "pending",
    "passed",
    "failed",
    "blocked",
    "residual_risk_accepted",
    "unknown",
}
SMOKE_RESULTS = {"passed", "failed", "blocked", "not_run"}
FAILURE_CLASSES = {
    "source_changed_running_service_not_updated",
    "activation_blocked",
    "activation_smoke_failed",
    "activation_target_unavailable",
}
NOTIFICATION_STATES = {
    "not_required",
    "pending",
    "sent",
    "failed",
    "blocked_unroutable",
}
WORK_ITEM_TYPES = {"slice", "subslice", "feature", "spike"}

JOURNAL_EVENT_TYPES = {
    "deployment_impact_assessed",
    "activation_path_recorded",
    "activation_action_recorded",
    "activation_smoke_recorded",
    "activation_blocked",
    "activation_failure_classified",
    "activation_residual_risk_recorded",
    "activation_notification_recorded",
}
ACTIVATION_EVENT_ALLOWLIST = {
    "project_id",
    "work_item_id",
    "work_item_type",
    "queue_item_id",
    "lifecycle_state",
    "role_id",
    "role_instance_id",
    "correlation_id",
    "source_anchor_ref",
    "schema_version",
    "impact_categories",
    "activation_paths",
    "target_labels",
    "source_status",
    "activation_status",
    "smoke_status",
    "failure_class",
    "action_owner",
    "retryable",
    "event_type",
    "status_url_present",
}

DISPLAY_LABELS = {
    "runtime_code": "Runtime code",
    "package_or_dependency": "Package or dependency",
    "container_image": "Container image",
    "runtime_configuration": "Runtime configuration",
    "service_restart_or_recreate": "Service restart or recreate",
    "schema_or_state_migration": "Schema or state migration",
    "connector_surface": "Connector surface",
    "route_or_ingress": "Route or ingress",
    "secret_or_identity_binding": "Secret or identity binding",
    "deployment_output": "Deployment output",
    "telemetry_or_observability": "Telemetry or observability",
    "external_dependency_or_provider": "External dependency or provider",
    "none": "No deployment impact",
    "not_required": "Not required",
    "reload_config": "Reload config",
    "restart_service": "Restart service",
    "recreate_container": "Recreate container",
    "rebuild_image": "Rebuild image",
    "install_package_or_dependency": "Install package or dependency",
    "run_migration": "Run migration",
    "refresh_connector": "Refresh connector",
    "update_route_or_ingress": "Update route or ingress",
    "rotate_or_bind_secret": "Rotate or bind secret",
    "update_deployment_output": "Update deployment output",
    "operator_action_required": "Operator action required",
    "unknown_blocked": "Unknown or blocked",
    "source_ready": "Source ready",
    "source_pending": "Source pending",
    "source_failed": "Source failed",
    "not_started": "Not started",
    "pending": "Pending",
    "in_progress": "In progress",
    "complete": "Complete",
    "blocked": "Blocked",
    "failed": "Failed",
    "passed": "Passed",
    "residual_risk_accepted": "Residual risk accepted",
    "unknown": "Unknown",
    "sent": "Sent",
    "blocked_unroutable": "Blocked unroutable",
    "source_changed_running_service_not_updated": "Stale runtime detected",
    "activation_blocked": "Activation blocked",
    "activation_smoke_failed": "Activation smoke failed",
    "activation_target_unavailable": "Activation target unavailable",
}

FORBIDDEN_FIELD_NAMES = {
    "tenant_id",
    "team_id",
    "channel_id",
    "activity_id",
    "conversation_id",
    "service_url",
    "bot_id",
    "app_id",
    "secret_ref",
    "mount_ref",
    "credential_ref",
    "oauth_path",
    "raw_payload",
    "raw_payload_ref",
    "response_body",
    "headers",
    "cookies",
    "stdout",
    "stderr",
    "command",
    "command_line",
    "image_digest",
    "container_id",
    "process_id",
    "pid",
    "hostname",
}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,160}$")
_SAFE_TARGET_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_ROUTE_LABEL_RE = re.compile(r"^/[A-Za-z0-9{}._~/-]{1,120}$")
_INTERNAL_IP_RE = re.compile(r"^(10\.|127\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|192\.168\.)")


@dataclass(frozen=True)
class SmokeEvidence:
    smoke_id: str
    target_label: str
    route_label: str
    observed_at: str
    result: str
    status_code: int | None = None
    expected_status_code: int | None = None
    schema_expectation: str | None = None
    content_expectation: str | None = None
    actual_summary: str | None = None
    failure_class: str | None = None
    evidence_ref: str | None = None
    schema_version: str = SMOKE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_logical_id(self.smoke_id, field_name="smoke_id")
        validate_target_label(self.target_label)
        validate_route_label(self.route_label)
        if self.result not in SMOKE_RESULTS:
            raise ValueError(f"unsupported smoke result `{self.result}`")
        if self.failure_class is not None and self.failure_class not in FAILURE_CLASSES:
            raise ValueError(f"unsupported activation failure class `{self.failure_class}`")
        if self.status_code is not None and not 100 <= int(self.status_code) <= 599:
            raise ValueError("status_code must be an HTTP status code")
        if self.expected_status_code is not None and not 100 <= int(self.expected_status_code) <= 599:
            raise ValueError("expected_status_code must be an HTTP status code")
        for field_name in ["schema_expectation", "content_expectation", "actual_summary", "evidence_ref"]:
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, safe_text(value, max_length=240))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload = {key: value for key, value in payload.items() if value is not None}
        assert_activation_payload_safe(payload)
        return payload

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SmokeEvidence":
        if data.get("schema_version") != SMOKE_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported smoke-evidence schema version")
        return SmokeEvidence(
            smoke_id=str(data["smoke_id"]),
            target_label=str(data["target_label"]),
            route_label=str(data["route_label"]),
            observed_at=str(data["observed_at"]),
            result=str(data["result"]),
            status_code=data.get("status_code"),
            expected_status_code=data.get("expected_status_code"),
            schema_expectation=data.get("schema_expectation"),
            content_expectation=data.get("content_expectation"),
            actual_summary=data.get("actual_summary"),
            failure_class=data.get("failure_class"),
            evidence_ref=data.get("evidence_ref"),
        )

    def to_summary(self) -> dict[str, Any]:
        return {
            "smoke_id": self.smoke_id,
            "target_label": self.target_label,
            "target_label_display": label_for(self.target_label),
            "route_label": self.route_label,
            "observed_at": self.observed_at,
            "result": self.result,
            "result_label": label_for(self.result),
            "status_code": self.status_code,
            "expected_status_code": self.expected_status_code,
            "schema_expectation": self.schema_expectation,
            "actual_summary": self.actual_summary,
            "failure_class": self.failure_class,
            "failure_class_label": label_for(self.failure_class) if self.failure_class else None,
        }


@dataclass(frozen=True)
class ActivationEvidence:
    project_id: str
    work_item_id: str
    lifecycle_state: str
    impact_categories: tuple[str, ...]
    activation_paths: tuple[str, ...]
    source_status: str
    activation_status: str
    smoke_status: str
    updated_at: str
    work_item_type: str | None = None
    queue_item_id: str | None = None
    source_message_id: str | None = None
    source_anchor_ref: str | None = None
    correlation_id: str | None = None
    none_rationale: str | None = None
    live_smoke_required: bool = False
    target_labels: tuple[str, ...] = ()
    failure_class: str | None = None
    action_owner: str | None = None
    next_action: str | None = None
    retryable: bool | None = None
    rollback_summary: str | None = None
    residual_risk: str | None = None
    evidence_refs: tuple[str, ...] = ()
    notification_state: str = "not_required"
    status_url: str | None = None
    smoke_evidence: tuple[SmokeEvidence, ...] = ()
    schema_version: str = ACTIVATION_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_logical_id(self.project_id, field_name="project_id")
        validate_logical_id(self.work_item_id, field_name="work_item_id")
        if self.work_item_type is not None and self.work_item_type not in WORK_ITEM_TYPES:
            raise ValueError(f"unsupported work_item_type `{self.work_item_type}`")
        _validate_enum_values(self.impact_categories, IMPACT_CATEGORIES, "impact_categories")
        _validate_enum_values(self.activation_paths, ACTIVATION_PATHS, "activation_paths")
        if "none" in self.impact_categories:
            if self.impact_categories != ("none",):
                raise ValueError("impact category `none` must be the only impact category")
            if not self.none_rationale:
                raise ValueError("none_rationale is required when impact category is `none`")
        if self.source_status not in SOURCE_STATUSES:
            raise ValueError(f"unsupported source_status `{self.source_status}`")
        if self.activation_status not in ACTIVATION_STATUSES:
            raise ValueError(f"unsupported activation_status `{self.activation_status}`")
        if self.smoke_status not in SMOKE_STATUSES:
            raise ValueError(f"unsupported smoke_status `{self.smoke_status}`")
        if self.failure_class is not None and self.failure_class not in FAILURE_CLASSES:
            raise ValueError(f"unsupported activation failure class `{self.failure_class}`")
        if self.notification_state not in NOTIFICATION_STATES:
            raise ValueError(f"unsupported notification_state `{self.notification_state}`")
        for label in self.target_labels:
            validate_target_label(label)
        if self.status_url is not None:
            object.__setattr__(self, "status_url", safe_status_url(self.status_url))
        for field_name in [
            "source_anchor_ref",
            "correlation_id",
            "none_rationale",
            "action_owner",
            "next_action",
            "rollback_summary",
            "residual_risk",
        ]:
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, safe_text(value))
        if self.evidence_refs:
            object.__setattr__(
                self,
                "evidence_refs",
                tuple(safe_evidence_ref(value) for value in self.evidence_refs),
            )
        if _activation_needs_action(self) and not self.next_action:
            raise ValueError("next_action is required for activation attention states")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "work_item_id": self.work_item_id,
            "work_item_type": self.work_item_type,
            "queue_item_id": self.queue_item_id,
            "source_message_id": self.source_message_id,
            "source_anchor_ref": self.source_anchor_ref,
            "correlation_id": self.correlation_id,
            "lifecycle_state": self.lifecycle_state,
            "impact_categories": list(self.impact_categories),
            "impact_category_labels": [label_for(value) for value in self.impact_categories],
            "none_rationale": self.none_rationale,
            "live_smoke_required": self.live_smoke_required,
            "target_labels": list(self.target_labels),
            "target_label_displays": [label_for(value) for value in self.target_labels],
            "activation_paths": list(self.activation_paths),
            "activation_path_labels": [label_for(value) for value in self.activation_paths],
            "source_status": self.source_status,
            "source_status_label": label_for(self.source_status),
            "activation_status": self.activation_status,
            "activation_status_label": label_for(self.activation_status),
            "smoke_status": self.smoke_status,
            "smoke_status_label": label_for(self.smoke_status),
            "failure_class": self.failure_class,
            "failure_class_label": label_for(self.failure_class) if self.failure_class else None,
            "action_owner": self.action_owner,
            "next_action": self.next_action,
            "retryable": self.retryable,
            "rollback_summary": self.rollback_summary,
            "residual_risk": self.residual_risk,
            "evidence_refs": list(self.evidence_refs),
            "notification_state": self.notification_state,
            "notification_state_label": label_for(self.notification_state),
            "status_url": self.status_url,
            "updated_at": self.updated_at,
            "smoke_evidence": [record.to_dict() for record in self.smoke_evidence],
            "read_only": True,
            "redaction_applied": True,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        assert_activation_payload_safe(payload)
        return payload

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "schema_version": self.schema_version,
            "work_item_id": self.work_item_id,
            "deployment_impact_labels": [label_for(value) for value in self.impact_categories],
            "impact_categories": list(self.impact_categories),
            "activation_path_labels": [label_for(value) for value in self.activation_paths],
            "activation_paths": list(self.activation_paths),
            "target_labels": list(self.target_labels),
            "source_status": self.source_status,
            "source_status_label": label_for(self.source_status),
            "activation_status": self.activation_status,
            "activation_status_label": label_for(self.activation_status),
            "smoke_status": self.smoke_status,
            "smoke_status_label": label_for(self.smoke_status),
            "failure_class": self.failure_class,
            "failure_class_label": label_for(self.failure_class) if self.failure_class else None,
            "action_owner": self.action_owner,
            "next_action": self.next_action,
            "retryable": self.retryable,
            "notification_state": self.notification_state,
            "live_smoke_required": self.live_smoke_required,
            "activation_updated_at": self.updated_at,
            "attention_needed": activation_attention_needed(self),
            "latest_smoke": self.smoke_evidence[-1].to_summary() if self.smoke_evidence else None,
            "read_only": True,
            "redaction_applied": True,
        }
        return {key: value for key, value in summary.items() if value is not None}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ActivationEvidence":
        if data.get("schema_version") != ACTIVATION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported activation-evidence schema version")
        return ActivationEvidence(
            project_id=str(data["project_id"]),
            work_item_id=str(data["work_item_id"]),
            work_item_type=data.get("work_item_type"),
            queue_item_id=data.get("queue_item_id"),
            source_message_id=data.get("source_message_id"),
            source_anchor_ref=data.get("source_anchor_ref"),
            correlation_id=data.get("correlation_id"),
            lifecycle_state=str(data["lifecycle_state"]),
            impact_categories=tuple(data.get("impact_categories") or ()),
            none_rationale=data.get("none_rationale"),
            live_smoke_required=bool(data.get("live_smoke_required", False)),
            target_labels=tuple(data.get("target_labels") or ()),
            activation_paths=tuple(data.get("activation_paths") or ()),
            source_status=str(data["source_status"]),
            activation_status=str(data["activation_status"]),
            smoke_status=str(data["smoke_status"]),
            failure_class=data.get("failure_class"),
            action_owner=data.get("action_owner"),
            next_action=data.get("next_action"),
            retryable=data.get("retryable"),
            rollback_summary=data.get("rollback_summary"),
            residual_risk=data.get("residual_risk"),
            evidence_refs=tuple(data.get("evidence_refs") or ()),
            notification_state=str(data.get("notification_state") or "not_required"),
            status_url=data.get("status_url"),
            updated_at=str(data["updated_at"]),
            smoke_evidence=tuple(
                SmokeEvidence.from_dict(record)
                for record in data.get("smoke_evidence", [])
            ),
        )

    def journal_fields(
        self,
        *,
        event_type: str,
        role_id: str | None = None,
        role_instance_id: str | None = None,
    ) -> dict[str, Any]:
        if event_type not in JOURNAL_EVENT_TYPES:
            raise ValueError(f"unsupported activation event type `{event_type}`")
        payload = {
            "event_type": event_type,
            "project_id": self.project_id,
            "work_item_id": self.work_item_id,
            "work_item_type": self.work_item_type,
            "queue_item_id": self.queue_item_id,
            "lifecycle_state": self.lifecycle_state,
            "role_id": role_id,
            "role_instance_id": role_instance_id,
            "correlation_id": self.correlation_id,
            "source_anchor_ref": self.source_anchor_ref,
            "schema_version": self.schema_version,
            "impact_categories": list(self.impact_categories),
            "activation_paths": list(self.activation_paths),
            "target_labels": list(self.target_labels),
            "source_status": self.source_status,
            "activation_status": self.activation_status,
            "smoke_status": self.smoke_status,
            "failure_class": self.failure_class,
            "action_owner": self.action_owner,
            "retryable": self.retryable,
            "status_url_present": bool(self.status_url),
        }
        return activation_event_allowlist(payload)

    def telemetry_attributes(self, *, event_type: str) -> dict[str, Any]:
        return {
            f"agentic_mesh.activation.{key}": value
            for key, value in self.journal_fields(event_type=event_type).items()
        }


@dataclass(frozen=True)
class ActivationReadError:
    work_item_id: str
    error_class: str
    schema_version: str = ACTIVATION_EVIDENCE_SCHEMA_VERSION

    def to_summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "work_item_id": safe_text(self.work_item_id, max_length=96),
            "activation_status": "unknown",
            "activation_status_label": "Unknown",
            "smoke_status": "unknown",
            "smoke_status_label": "Unknown",
            "failure_class": "activation_blocked",
            "failure_class_label": label_for("activation_blocked"),
            "attention_needed": True,
            "extraction_error": self.error_class,
            "read_only": True,
            "redaction_applied": True,
        }


class ActivationEvidenceStore:
    def write_current(self, evidence: ActivationEvidence, *, event_type: str = "activation_path_recorded") -> Path:
        raise NotImplementedError

    def read_current(self, work_item_id: str) -> ActivationEvidence | None:
        raise NotImplementedError


class FileActivationEvidenceStore(ActivationEvidenceStore):
    def __init__(self, state_root: Path, project_id: str, *, create_dirs: bool = True) -> None:
        project = validate_logical_id(project_id, field_name="project_id")
        self.project_id = project
        self.root = state_root / "projects" / project / "work_items"
        if create_dirs:
            self.root.mkdir(parents=True, exist_ok=True)
            _reject_symlink(self.root, "activation store root")

    def write_current(
        self,
        evidence: ActivationEvidence,
        *,
        event_type: str = "activation_path_recorded",
    ) -> Path:
        if evidence.project_id != self.project_id:
            raise ValueError("activation evidence project_id does not match store")
        path = self.current_path(evidence.work_item_id, create_dirs=True)
        _atomic_write_json(path, evidence.to_dict(), mode=0o600)
        history = self.history_path(evidence.work_item_id, create_dirs=True)
        event = {
            **evidence.journal_fields(event_type=event_type),
            "recorded_at": utc_now_iso(),
        }
        _append_jsonl(history, event, mode=0o600)
        return path

    def read_current(self, work_item_id: str) -> ActivationEvidence | None:
        result = self.read_current_with_error(work_item_id)
        if isinstance(result, ActivationReadError):
            return None
        return result

    def read_current_with_error(
        self,
        work_item_id: str,
    ) -> ActivationEvidence | ActivationReadError | None:
        try:
            path = self.current_path(work_item_id, create_dirs=False)
            if not path.exists():
                return None
            _reject_symlink(path, "activation current file")
            with path.open("r", encoding="utf-8") as handle:
                return ActivationEvidence.from_dict(json.load(handle))
        except Exception as exc:
            return ActivationReadError(work_item_id=str(work_item_id), error_class=exc.__class__.__name__)

    def list_current(self) -> tuple[list[ActivationEvidence], list[ActivationReadError]]:
        records: list[ActivationEvidence] = []
        errors: list[ActivationReadError] = []
        if not self.root.exists():
            return records, errors
        for path in sorted(self.root.glob("*/activation-evidence.json")):
            work_item_id = path.parent.name
            result = self.read_current_with_error(work_item_id)
            if isinstance(result, ActivationEvidence):
                records.append(result)
            elif isinstance(result, ActivationReadError):
                errors.append(result)
        return records, errors

    def current_path(self, work_item_id: str, *, create_dirs: bool = True) -> Path:
        work_dir = self._work_item_dir(work_item_id, create_dirs=create_dirs)
        path = (work_dir / "activation-evidence.json").resolve()
        _ensure_contained(self.root, path)
        if path.exists():
            _reject_symlink(path, "activation current file")
        return path

    def history_path(self, work_item_id: str, *, create_dirs: bool = True) -> Path:
        work_dir = self._work_item_dir(work_item_id, create_dirs=create_dirs)
        path = (work_dir / "activation-evidence-history.jsonl").resolve()
        _ensure_contained(self.root, path)
        if path.exists():
            _reject_symlink(path, "activation history file")
        return path

    def _work_item_dir(self, work_item_id: str, *, create_dirs: bool) -> Path:
        safe_id = validate_logical_id(work_item_id, field_name="work_item_id")
        if self.root.exists():
            _reject_symlink(self.root, "activation store root")
        elif create_dirs:
            self.root.mkdir(parents=True, exist_ok=True)
        path = (self.root / safe_id).resolve()
        _ensure_contained(self.root, path)
        if path.exists():
            _reject_symlink(path, "activation work item directory")
        elif create_dirs:
            path.mkdir(parents=True, exist_ok=True)
        return path


def activation_attention_needed(evidence: ActivationEvidence | dict[str, Any] | None) -> bool:
    if evidence is None:
        return False
    if isinstance(evidence, ActivationEvidence):
        activation_status = evidence.activation_status
        smoke_status = evidence.smoke_status
        notification_state = evidence.notification_state
        failure_class = evidence.failure_class
        runtime_visible = evidence.live_smoke_required or evidence.impact_categories != ("none",)
    else:
        activation_status = str(evidence.get("activation_status") or "")
        smoke_status = str(evidence.get("smoke_status") or "")
        notification_state = str(evidence.get("notification_state") or "")
        failure_class = evidence.get("failure_class")
        runtime_visible = bool(evidence.get("live_smoke_required") or evidence.get("impact_categories"))
    return (
        failure_class == "source_changed_running_service_not_updated"
        or (runtime_visible and activation_status in {"blocked", "failed", "unknown"})
        or smoke_status in {"failed", "blocked"}
        or notification_state in {"failed", "blocked_unroutable"}
    )


def activation_problem_status(
    *,
    evidence: ActivationEvidence,
    affected_role: str,
    role_instance_id: str | None = None,
    source_anchor: dict[str, Any] | None = None,
    source_message_id: str | None = None,
    correlation_id: str | None = None,
    reason: str | None = None,
    next_action: str | None = None,
) -> ProblemStatus:
    failure_class = evidence.failure_class or _failure_class_for(evidence)
    if failure_class not in FAILURE_CLASSES:
        raise ValueError("activation problem status requires an activation failure class")
    return ProblemStatus(
        work_item_id=evidence.work_item_id,
        work_item_type=evidence.work_item_type,
        queue_item_id=evidence.queue_item_id,
        source_message_id=source_message_id or evidence.source_message_id,
        source_anchor_ref=evidence.source_anchor_ref or problem_source_anchor_ref(source_anchor),
        source_anchor_summary=source_anchor_summary(source_anchor),
        correlation_id=correlation_id or evidence.correlation_id or evidence.work_item_id,
        status="needs_runtime_recovery",
        problem_kind="runtime_failed",
        failure_class=failure_class,
        lifecycle_state=evidence.lifecycle_state,
        affected_role=affected_role,
        role_instance_id=role_instance_id,
        reason=reason or _problem_reason(evidence, failure_class),
        next_action=next_action or evidence.next_action or recovery_action_label("operator_review"),
        action_owner=evidence.action_owner or "runtime/operator",
        retryable=bool(evidence.retryable),
        recovery_action="operator_review",
        status_url=evidence.status_url,
    )


def activation_event_allowlist(payload: dict[str, Any]) -> dict[str, Any]:
    safe = {
        key: payload[key]
        for key in ACTIVATION_EVENT_ALLOWLIST
        if key in payload and payload[key] is not None
    }
    assert set(safe) <= ACTIVATION_EVENT_ALLOWLIST
    assert_activation_payload_safe(safe)
    return safe


def label_for(value: str | None) -> str:
    if not value:
        return "Unknown"
    return DISPLAY_LABELS.get(value, str(value).replace("_", " ").title())


def validate_logical_id(value: str, *, field_name: str = "id") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field_name} must not be empty")
    if "/" in candidate or "\\" in candidate or ".." in candidate:
        raise ValueError(f"{field_name} must be a contained logical id")
    if "://" in candidate:
        raise ValueError(f"{field_name} must not be a URI")
    if re.match(r"^[A-Za-z]:", candidate):
        raise ValueError(f"{field_name} must not be a Windows drive path")
    if not _SAFE_ID_RE.match(candidate):
        raise ValueError(f"{field_name} contains unsupported characters")
    return candidate


def validate_target_label(value: str) -> str:
    candidate = str(value).strip()
    lowered = candidate.lower()
    if not _SAFE_TARGET_LABEL_RE.match(candidate):
        raise ValueError("target label must be a safe logical label")
    if (
        "://" in lowered
        or "/" in candidate
        or "\\" in candidate
        or "." in candidate
        or "secret" in lowered
        or "token" in lowered
        or "tenant" in lowered
        or "channel" in lowered
        or "credential" in lowered
        or _INTERNAL_IP_RE.match(candidate)
    ):
        raise ValueError("target label contains unsafe environment detail")
    return candidate


def validate_route_label(value: str) -> str:
    candidate = str(value).strip()
    lowered = candidate.lower()
    if not _SAFE_ROUTE_LABEL_RE.match(candidate) or candidate.startswith("//"):
        raise ValueError("route label must be a safe route label")
    if "://" in lowered or "\\" in candidate or ".." in candidate:
        raise ValueError("route label contains unsafe route detail")
    return candidate


def safe_text(value: Any, *, max_length: int = 500) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    forbidden_keys = "|".join(
        re.escape(key)
        for key in sorted(FORBIDDEN_FIELD_NAMES | {"raw_payload"}, key=len, reverse=True)
    )
    text = re.sub(r"[A-Za-z]:\\[^\s`]+", "[redacted-path]", text)
    text = re.sub(r"/(?:[^\s`]+/){2,}[^\s`]+", "[redacted-path]", text)
    text = re.sub(
        rf"(?i)`?(?:{forbidden_keys})`?\s*[`:= ]+\S+",
        "[redacted]",
        text,
    )
    text = re.sub(r"(?i)\b(secret|token|password|bearer)\s*[:=]\s*\S+", "[redacted]", text)
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text


def safe_evidence_ref(value: Any) -> str:
    text = safe_text(value, max_length=160).replace("\\", "/")
    requested = Path(text)
    if (
        not text
        or text.startswith("/")
        or requested.is_absolute()
        or ".." in requested.parts
        or text.startswith("state/")
        or re.match(r"^[A-Za-z]:/", text)
        or "://" in text
    ):
        raise ValueError("evidence reference must be a logical artifact or event reference")
    return text


def safe_status_url(value: str) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("/"):
        if "?" in text or "#" in text or "/auth" in text:
            raise ValueError("status URL must be a read-only status path")
        return text
    parsed = urlparse(text)
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        raise ValueError("external status URL must be approved HTTPS without query or fragment")
    if parsed.path.startswith("/auth"):
        raise ValueError("status URL must not target auth/admin routes")
    return text.rstrip("/")


def assert_activation_payload_safe(payload: Any) -> None:
    _assert_safe_node(payload, path="")


def _assert_safe_node(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        forbidden = set(value) & FORBIDDEN_FIELD_NAMES
        if forbidden:
            raise ValueError(f"activation payload contains forbidden fields: {sorted(forbidden)}")
        for key, child in value.items():
            _assert_safe_node(child, path=f"{path}.{key}" if path else str(key))
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _assert_safe_node(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.lower()
        for marker in [
            "tenant_id=",
            "channel_id=",
            "conversation_id=",
            "activity_id=",
            "service_url=",
            "secret_ref=",
            "mount_ref=",
            "credential_ref=",
            "raw_payload",
            "stdout=",
            "stderr=",
            "command=",
            "image_digest=",
            "container_id=",
        ]:
            if marker in lowered:
                raise ValueError(f"activation payload contains unsafe marker at {path}")


def _validate_enum_values(values: tuple[str, ...], allowed: set[str], field_name: str) -> None:
    if not values:
        raise ValueError(f"{field_name} must not be empty")
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"unsupported {field_name}: {unknown}")


def _activation_needs_action(evidence: ActivationEvidence) -> bool:
    return (
        evidence.failure_class is not None
        or evidence.activation_status in {"blocked", "failed"}
        or evidence.smoke_status in {"failed", "blocked"}
    )


def _failure_class_for(evidence: ActivationEvidence) -> str:
    if evidence.activation_status == "blocked":
        return "activation_blocked"
    if evidence.smoke_status == "failed":
        return "activation_smoke_failed"
    if evidence.smoke_status == "blocked":
        return "activation_target_unavailable"
    return "activation_blocked"


def _problem_reason(evidence: ActivationEvidence, failure_class: str) -> str:
    target = ", ".join(evidence.target_labels) or "target_unknown"
    if failure_class == "source_changed_running_service_not_updated":
        return f"Source-ready behaviour is not active on target label {target}."
    if failure_class == "activation_smoke_failed":
        return f"Activation smoke failed for target label {target}."
    if failure_class == "activation_target_unavailable":
        return f"Activation target label {target} is unavailable for smoke evidence."
    return f"Activation is blocked for target label {target}."


def _ensure_contained(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    if path_resolved != root_resolved and root_resolved not in path_resolved.parents:
        raise ValueError("activation evidence path escapes configured root")


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} refuses symlink paths")


def _atomic_write_json(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path.parent, "activation evidence parent")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, mode)
        except OSError:
            pass
        os.replace(temp_name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _append_jsonl(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path.parent, "activation evidence parent")
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
