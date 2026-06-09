from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_mesh.models import (
    CapabilityConfig,
    CapabilityProfileConfig,
    MeshConfig,
    RoleInstanceConfig,
    utc_now_iso,
)


CAPABILITY_READINESS_SCHEMA = "capability-readiness-v0"
SUMMARY_SCHEMA = "role-capability-readiness-summary-v0"
PROFILE_SCHEMA = "role-capability-profile-v0"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CORE_STARTUP_CATEGORIES = {"worker_capability", "document_library"}
ACTION_NEEDED_STATUSES = {
    "missing_required",
    "validation_failed",
    "unavailable_with_fallback",
}
SENSITIVE_KEYS = {
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
    "path",
}


@dataclass(frozen=True)
class MergedCapability:
    config: CapabilityConfig
    configured_sources: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityValidationResult:
    schema_version: str
    project_id: str
    role_id: str
    role_instance_id: str
    capability_id: str
    display_name: str
    category: str
    requirement: str
    configured_source: str
    configured_sources: tuple[str, ...]
    status: str
    user_label: str
    presentation_group: str
    severity: str
    impact: str
    affects: dict[str, Any]
    validation_summary: dict[str, Any]
    validated_at: str
    fresh_until: str | None
    evidence_freshness: str
    fallback: dict[str, Any] | None
    waiver: dict[str, Any] | None
    next_action: str
    action_owner: str
    redaction_applied: bool = True

    def to_safe_dict(self) -> dict[str, Any]:
        return redact(asdict(self))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if str(key) in SENSITIVE_KEYS:
                continue
            safe[str(key)] = redact(item)
        return safe
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        if "-----BEGIN" in value or "token" in value.lower() or "secret" in value.lower():
            return "[redacted]"
    return value


def _safe_id(value: str, label: str) -> str:
    if not SAFE_ID.fullmatch(value) or ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be a safe logical id")
    return value


def build_merged_profile(
    mesh_config: MeshConfig,
    instance: RoleInstanceConfig,
) -> list[MergedCapability]:
    merged: dict[tuple[str, str], MergedCapability] = {}

    def apply(profile: CapabilityProfileConfig, source: str) -> None:
        if profile.schema_version != PROFILE_SCHEMA:
            raise ValueError(f"{source} has unsupported capability profile schema")
        for capability in profile.capabilities:
            key = (capability.capability_id, capability.category)
            current = merged.get(key)
            capability_with_source = replace(
                capability,
                configured_source=capability.configured_source or source,
            )
            if current is not None:
                if (
                    current.config.requirement == "required"
                    and capability.requirement not in {"required", "restricted"}
                    and capability.fallback is None
                    and capability.waiver is None
                ):
                    raise ValueError(
                        "Required capability cannot be downgraded without fallback or waiver: "
                        f"{capability.capability_id}"
                    )
                sources = current.configured_sources + (source,)
            else:
                sources = (source,)
            merged[key] = MergedCapability(
                config=capability_with_source,
                configured_sources=sources,
            )

    apply(mesh_config.organization.capability_defaults, "organization_defaults")
    apply(mesh_config.project.capability_defaults, "project_defaults")
    apply(instance.template.capabilities, f"role_template:{instance.template.role_id}")
    default_tool_profile = CapabilityProfileConfig(
        capabilities=[
            CapabilityConfig(
                capability_id=tool_id,
                category="compatibility_default_tool",
                requirement="required",
                display_name=tool_id.replace(".", " ").title(),
                description="Compatibility mapping from role template default_tools.",
                severity="medium",
                impact="Configured legacy default tool availability has not been validated.",
                next_action="Add a first-class capability validation before relying on this tool.",
                action_owner="engineering",
                configured_source="compatibility_default_tools",
                source_path=f"config.roles.{instance.template.role_id}.default_tools",
            )
            for tool_id in instance.template.default_tools
        ]
    )
    apply(default_tool_profile, "compatibility_default_tools")
    apply(instance.override.capabilities, f"project_role:{instance.role_id}")
    return [
        merged[key]
        for key in sorted(merged, key=lambda item: (item[0], item[1]))
    ]


class CapabilityValidator:
    def __init__(
        self,
        *,
        mesh_config: MeshConfig,
        workspace_root: Path,
        now: str | None = None,
    ) -> None:
        self.mesh_config = mesh_config
        self.workspace_root = workspace_root
        self.now = now or utc_now_iso()

    def validate_instance(self, instance: RoleInstanceConfig) -> list[CapabilityValidationResult]:
        return [
            self.validate_capability(instance, capability)
            for capability in build_merged_profile(self.mesh_config, instance)
        ]

    def validate_capability(
        self,
        instance: RoleInstanceConfig,
        merged: MergedCapability,
    ) -> CapabilityValidationResult:
        config = merged.config
        raw_status = self._evaluate(instance, config)
        status = self._apply_policy(config, raw_status)
        freshness_seconds = (
            config.validation.freshness_seconds if config.validation is not None else None
        )
        fresh_until = None
        if freshness_seconds is not None:
            try:
                parsed = datetime.fromisoformat(self.now)
                fresh_until = (
                    parsed.timestamp() + freshness_seconds
                )
                fresh_until = datetime.fromtimestamp(
                    fresh_until,
                    tz=parsed.tzinfo or timezone.utc,
                ).isoformat()
            except Exception:
                fresh_until = None
        return CapabilityValidationResult(
            schema_version=CAPABILITY_READINESS_SCHEMA,
            project_id=instance.project_id,
            role_id=instance.role_id,
            role_instance_id=instance.instance_id,
            capability_id=config.capability_id,
            display_name=config.display_name,
            category=config.category,
            requirement=config.requirement,
            configured_source=config.configured_source,
            configured_sources=merged.configured_sources,
            status=status,
            user_label=status.replace("_", " ").title(),
            presentation_group=_presentation_group(status),
            severity=config.severity,
            impact=config.impact or _default_impact(status),
            affects={
                "lifecycle_states": list(config.affects.lifecycle_states),
                "work_item_types": list(config.affects.work_item_types),
            },
            validation_summary=self._validation_summary(config, raw_status),
            validated_at=self.now,
            fresh_until=fresh_until,
            evidence_freshness="fresh",
            fallback=redact(asdict(config.fallback)) if config.fallback is not None else None,
            waiver=redact(asdict(config.waiver)) if config.waiver is not None else None,
            next_action=config.next_action or _default_next_action(status),
            action_owner=config.action_owner or _default_action_owner(status),
        )

    def _evaluate(self, instance: RoleInstanceConfig, config: CapabilityConfig) -> str:
        if config.requirement == "not_applicable":
            return "not_applicable"
        if config.waiver is not None and not _waiver_expired(config.waiver.expires_at):
            return "waived"
        validation = config.validation
        if validation is None:
            if config.category == "compatibility_default_tool":
                return "available_with_warning"
            return "not_configured"
        if validation.kind == "configured":
            return "available_with_warning"
        if validation.kind == "worker_auth":
            return "available" if instance.override.worker.auth is not None else "missing_required"
        if validation.kind == "document_library":
            library = self.mesh_config.project.document_library
            return "available" if library.backend and library.root else "missing_required"
        if validation.kind == "connector_config":
            target = validation.target or ""
            return "available" if target in self.mesh_config.project.connectors else _missing(config)
        if validation.kind == "logical_mount":
            return "available" if validation.target else _missing(config)
        if validation.kind == "python_import":
            target = validation.target or config.capability_id
            return "available" if importlib.util.find_spec(target) else _missing(config)
        if validation.kind == "command":
            executable = validation.command[0] if validation.command else ""
            if not executable or shutil.which(executable) is None:
                return _missing(config)
            try:
                completed = subprocess.run(
                    validation.command,
                    cwd=self.workspace_root,
                    env=_minimal_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=validation.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return "validation_failed"
            except OSError:
                return _missing(config)
            return "available" if completed.returncode == 0 else "validation_failed"
        return "validation_failed"

    def _apply_policy(self, config: CapabilityConfig, status: str) -> str:
        if status in {"missing_required", "validation_failed"} and config.fallback is not None:
            return "unavailable_with_fallback"
        if config.waiver is not None and _waiver_expired(config.waiver.expires_at):
            return "validation_failed"
        return status

    def _validation_summary(
        self,
        config: CapabilityConfig,
        raw_status: str,
    ) -> dict[str, Any]:
        kind = config.validation.kind if config.validation is not None else "not_validated"
        summary = {
            "kind": kind,
            "result_class": raw_status,
            "raw_output_stored": False,
        }
        if config.category == "compatibility_default_tool":
            summary["note"] = "Compatibility mapping; configured availability is not proof of installation."
        return summary


def _missing(config: CapabilityConfig) -> str:
    return "missing_required" if config.requirement == "required" else "missing_optional"


def _minimal_env() -> dict[str, str]:
    safe = {}
    for key in ["PATH", "SYSTEMROOT", "WINDIR"]:
        if os.environ.get(key):
            safe[key] = os.environ[key]
    return safe


def _waiver_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at)
        now = datetime.now(expiry.tzinfo or timezone.utc)
        return expiry < now
    except ValueError:
        return True


def _presentation_group(status: str) -> str:
    if status in {"missing_required", "validation_failed", "unavailable_with_fallback"}:
        return "attention_needed"
    if status in {"missing_optional", "available_with_warning", "waived"}:
        return "warning"
    if status == "available":
        return "available"
    if status in {"not_applicable"}:
        return "not_applicable"
    return "unknown"


def _default_impact(status: str) -> str:
    if status in ACTION_NEEDED_STATUSES:
        return "Capability-dependent work may be blocked."
    if status in {"missing_optional", "available_with_warning", "waived"}:
        return "Role can start; operators should review the capability note."
    return ""


def _default_next_action(status: str) -> str:
    if status in ACTION_NEEDED_STATUSES:
        return "Review capability provisioning before assigning dependent work."
    if status in {"missing_optional", "available_with_warning"}:
        return "Add explicit validation if this capability is required for the next slice."
    return ""


def _default_action_owner(status: str) -> str:
    return "platform-engineer" if status in ACTION_NEEDED_STATUSES else "engineering"


def summarize_readiness(
    *,
    project_id: str,
    role_id: str,
    role_instance_id: str,
    results: list[CapabilityValidationResult],
    generated_at: str | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    generated = generated_at or utc_now_iso()
    if not results:
        return unknown_summary(
            project_id=project_id,
            role_id=role_id,
            role_instance_id=role_instance_id,
            generated_at=generated,
            diagnostic_class="missing_evidence",
        )
    statuses = Counter(result.status for result in results)
    groups = Counter(result.presentation_group for result in results)
    startup_blocked = any(
        result.category in CORE_STARTUP_CATEGORIES
        and result.requirement == "required"
        and result.status in {"missing_required", "validation_failed"}
        for result in results
    )
    capability_blocked = any(
        result.status in {"missing_required", "validation_failed"}
        for result in results
    )
    warnings = any(
        result.status
        in {"missing_optional", "available_with_warning", "unavailable_with_fallback", "waived"}
        for result in results
    )
    if startup_blocked:
        overall = "startup_blocked"
    elif capability_blocked:
        overall = "not_ready_for_capability_dependent_work"
    elif warnings:
        overall = "ready_with_warnings"
    else:
        overall = "ready"
    attention = next(
        (
            result
            for result in results
            if result.status in ACTION_NEEDED_STATUSES
        ),
        results[0],
    )
    return {
        "schema_version": SUMMARY_SCHEMA,
        "project_id": project_id,
        "role_id": role_id,
        "role_instance_id": role_instance_id,
        "overall_readiness": overall,
        "overall_label": overall.replace("_", " ").title(),
        "core_readiness": "blocked" if startup_blocked else "ready",
        "specialist_readiness": (
            "blocked" if capability_blocked and not startup_blocked else "ready"
        ),
        "counts_by_status": dict(statuses),
        "counts_by_presentation_group": dict(groups),
        "last_validated_at": max(result.validated_at for result in results),
        "generated_at": generated,
        "evidence_freshness": (
            "stale"
            if any(result.evidence_freshness == "stale" for result in results)
            else "fresh"
        ),
        "blocking_impact": attention.impact,
        "next_action": attention.next_action,
        "action_owner": attention.action_owner,
        "snapshot_id": snapshot_id,
        "status_url": f"/agents/current.json#role_instance={role_instance_id}",
        "detail_ref": f"capability-readiness:{role_instance_id}:current",
        "redaction_applied": True,
    }


def unknown_summary(
    *,
    project_id: str,
    role_id: str,
    role_instance_id: str,
    generated_at: str | None = None,
    diagnostic_class: str = "unknown",
) -> dict[str, Any]:
    generated = generated_at or utc_now_iso()
    return {
        "schema_version": SUMMARY_SCHEMA,
        "project_id": project_id,
        "role_id": role_id,
        "role_instance_id": role_instance_id,
        "overall_readiness": "unknown",
        "overall_label": "Unknown",
        "core_readiness": "unknown",
        "specialist_readiness": "unknown",
        "counts_by_status": {},
        "counts_by_presentation_group": {"unknown": 1},
        "last_validated_at": None,
        "generated_at": generated,
        "evidence_freshness": "unreadable",
        "blocking_impact": "Readiness evidence could not be read.",
        "next_action": "Run capability validation or repair readiness evidence.",
        "action_owner": "platform-engineer",
        "snapshot_id": None,
        "status_url": f"/agents/current.json#role_instance={role_instance_id}",
        "detail_ref": f"capability-readiness:{role_instance_id}:current",
        "diagnostic": {"error_class": diagnostic_class},
        "redaction_applied": True,
    }


class CapabilityReadinessStore:
    def __init__(
        self,
        state_root: Path,
        project_id: str,
        *,
        create_dirs: bool = True,
    ) -> None:
        self.state_root = state_root
        self.project_id = _safe_id(project_id, "project_id")
        self.root = state_root / "projects" / self.project_id / "capability-readiness"
        if create_dirs:
            self.root.mkdir(parents=True, exist_ok=True)

    def _instance_root(self, role_instance_id: str) -> Path:
        safe = _safe_id(role_instance_id, "role_instance_id")
        path = self.root / safe
        resolved_root = self.root.resolve()
        resolved_path = path.resolve(strict=False)
        if resolved_root != resolved_path and resolved_root not in resolved_path.parents:
            raise ValueError("readiness path escaped state root")
        if path.exists() and path.is_symlink():
            raise ValueError("readiness path must not be a symlink")
        return path

    def write_current(
        self,
        role_instance_id: str,
        *,
        summary: dict[str, Any],
        results: list[CapabilityValidationResult],
    ) -> dict[str, Any]:
        root = self._instance_root(role_instance_id)
        root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "capability-readiness-current-v0",
            "summary": redact(summary),
            "results": [result.to_safe_dict() for result in results],
        }
        self._atomic_write(root / "current.json", payload)
        return payload

    def read_current(self, role_instance_id: str) -> dict[str, Any] | None:
        path = self._instance_root(role_instance_id) / "current.json"
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                return redact(json.load(handle))
        except Exception:
            return {
                "schema_version": "capability-readiness-current-v0",
                "summary": unknown_summary(
                    project_id=self.project_id,
                    role_id=role_instance_id.split(".")[-2] if "." in role_instance_id else "unknown",
                    role_instance_id=role_instance_id,
                    diagnostic_class="unreadable_evidence",
                ),
                "results": [],
            }

    def snapshot_current(
        self,
        role_instance_id: str,
        *,
        snapshot_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        current = self.read_current(role_instance_id)
        if current is None:
            raise FileNotFoundError("No current readiness evidence")
        safe_snapshot_id = _safe_id(
            snapshot_id or f"snap-{uuid4().hex[:12]}",
            "snapshot_id",
        )
        root = self._instance_root(role_instance_id)
        snapshots = root / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        payload = redact(
            {
                "schema_version": "capability-readiness-snapshot-v0",
                "snapshot_id": safe_snapshot_id,
                "generated_at": utc_now_iso(),
                "current": current,
            }
        )
        self._atomic_write(snapshots / f"{safe_snapshot_id}.json", payload)
        return safe_snapshot_id, payload

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)


def validate_and_store_all(
    *,
    mesh_config: MeshConfig,
    workspace_root: Path,
    state_root: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    validator = CapabilityValidator(
        mesh_config=mesh_config,
        workspace_root=workspace_root,
        now=generated_at,
    )
    store = CapabilityReadinessStore(state_root, mesh_config.project.project_id)
    roles = []
    for instance in sorted(mesh_config.instances.values(), key=lambda item: item.instance_id):
        results = validator.validate_instance(instance)
        summary = summarize_readiness(
            project_id=instance.project_id,
            role_id=instance.role_id,
            role_instance_id=instance.instance_id,
            results=results,
            generated_at=validator.now,
        )
        store.write_current(instance.instance_id, summary=summary, results=results)
        roles.append(
            {
                "role_instance_id": instance.instance_id,
                "summary": summary,
                "results": [result.to_safe_dict() for result in results],
            }
        )
    return {
        "schema_version": "capability-readiness-report-v0",
        "project_id": mesh_config.project.project_id,
        "generated_at": validator.now,
        "roles": roles,
        "redaction_applied": True,
    }


def capability_prompt_context(
    *,
    mesh_config: MeshConfig,
    instance: RoleInstanceConfig,
    state_root: Path | None = None,
) -> dict[str, Any]:
    configured = build_merged_profile(mesh_config, instance)
    current = None
    if state_root is not None:
        current = CapabilityReadinessStore(
            state_root,
            mesh_config.project.project_id,
            create_dirs=False,
        ).read_current(instance.instance_id)
    if current is None:
        return {
            "schema_version": "capability-prompt-context-v0",
            "availability_statement": "Capability availability has not been validated for this role instance.",
            "configured_required": [
                {
                    "capability_id": item.config.capability_id,
                    "category": item.config.category,
                    "display_name": item.config.display_name,
                    "availability": "not_validated",
                }
                for item in configured
                if item.config.requirement == "required"
            ],
            "available": [],
            "missing_required": [],
            "fallbacks": [],
            "waivers": [],
            "redaction_applied": True,
        }
    results = current.get("results", [])
    return redact(
        {
            "schema_version": "capability-prompt-context-v0",
            "availability_statement": (
                "Capability availability is validated only for entries marked available; "
                "configured entries without evidence must not be treated as installed."
            ),
            "configured_required": [
                {
                    "capability_id": item.config.capability_id,
                    "category": item.config.category,
                    "display_name": item.config.display_name,
                    "availability": "see_readiness_evidence",
                }
                for item in configured
                if item.config.requirement == "required"
            ],
            "available": [
                _prompt_item(item)
                for item in results
                if item.get("status") == "available"
            ],
            "missing_required": [
                _prompt_item(item)
                for item in results
                if item.get("status") in {"missing_required", "validation_failed"}
            ],
            "fallbacks": [
                _prompt_item(item)
                for item in results
                if item.get("status") == "unavailable_with_fallback"
            ],
            "waivers": [
                _prompt_item(item)
                for item in results
                if item.get("status") == "waived"
            ],
            "evidence_freshness": current.get("summary", {}).get("evidence_freshness"),
            "redaction_applied": True,
        }
    )


def _prompt_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "capability_id": item.get("capability_id"),
        "display_name": item.get("display_name"),
        "status": item.get("status"),
        "impact": item.get("impact"),
        "next_action": item.get("next_action"),
    }

