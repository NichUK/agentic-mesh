from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol
from uuid import uuid4

import yaml

from agentic_mesh_v4.compose import render_compose
from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.db import QueuedMessage
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.lifecycle import ComposeLifecycle

STATE_ARTIFACT_PATHS = {
    "product_definition": "020-product-definition.md",
    "experience_design": "030-experience-design.md",
    "solution_design": "030-solution-design.md",
    "security_review": "050-security-review.md",
    "prompt_contract": "060-prompt-contract.md",
    "implementation": "100-implementation-log.md",
    "implementation_planned": "100-implementation-log.md",
    "implementation_complete": "100-implementation-log.md",
    "quality_review": "110-quality-evidence.md",
    "qa_review": "110-quality-evidence.md",
    "release_review": "140-release-record.md",
    "released": "140-release-record.md",
}


@dataclass(frozen=True)
class ProbeCommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ProbeRunner(Protocol):
    def run(self, *, service_name: str, command: list[str]) -> ProbeCommandResult:
        ...


@dataclass(frozen=True)
class ComposeProbeRunner:
    lifecycle: ComposeLifecycle

    def run(self, *, service_name: str, command: list[str]) -> ProbeCommandResult:
        result = self.lifecycle.exec_service(service_name, command)
        return ProbeCommandResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)


@dataclass(frozen=True)
class PreflightCheck:
    probe_type: str
    check_name: str
    status: str
    remediation: str = ""
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    diagnostic: dict[str, object] | None = None


@dataclass(frozen=True)
class PreflightOutcome:
    status: str
    message_state: str | None = None
    summary: str = ""
    checks: tuple[PreflightCheck, ...] = ()
    required_path: str | None = None
    canonical_path: str | None = None
    work_item_id: str | None = None
    lifecycle_state: str | None = None
    handoff_id: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"


class RoleArtifactPreflight:
    def __init__(
        self,
        *,
        project_config: V4ProjectConfig,
        compose_text: str | None = None,
        probe_runner: ProbeRunner | None = None,
        enabled: bool = True,
    ) -> None:
        self.project_config = project_config
        self.compose_text = compose_text
        self.probe_runner = probe_runner
        self.enabled = enabled

    def run(self, *, db: V4Database, message: QueuedMessage, role_instance_id: str) -> PreflightOutcome:
        if not self.enabled:
            return PreflightOutcome(status="passed")
        role = self.project_config.role(message.target_role)
        context = _resolve_context(db=db, message=message)
        checks: list[PreflightCheck] = []
        if context.get("work_item_id") is None and context.get("required_path") is None:
            return PreflightOutcome(status="passed")

        if self.project_config.document_root != "/documents":
            checks.append(
                PreflightCheck(
                    probe_type="static_config",
                    check_name="document_root_configured",
                    status="blocked",
                    remediation="Set document_library.root_path to /documents for V4 role artifact writes.",
                    diagnostic={"document_root": self.project_config.document_root},
                )
            )
            return _record_and_outcome(db, message, role, role_instance_id, context, checks, "blocked_preflight")
        checks.append(PreflightCheck("static_config", "document_root_configured", "passed", diagnostic={"document_root": "/documents"}))

        try:
            canonical_path = canonical_artifact_path(
                work_item_id=context["work_item_id"],
                required_path=context["required_path"],
            )
            context["canonical_path"] = canonical_path
            checks.append(PreflightCheck("static_config", "canonical_path", "passed", diagnostic={"canonical_path": canonical_path}))
        except ValueError as exc:
            checks.append(
                PreflightCheck(
                    probe_type="static_config",
                    check_name="canonical_path",
                    status="blocked",
                    remediation="Use a canonical path under /documents/work-items/{work_item_id}/ and remove absolute/traversal segments.",
                    diagnostic={"error": str(exc), "required_path": context.get("required_path")},
                )
            )
            return _record_and_outcome(db, message, role, role_instance_id, context, checks, "blocked_preflight")

        compose_checks = _compose_checks(
            project_config=self.project_config,
            role_id=role.role_id,
            service_name=role.service_name,
            compose_text=self.compose_text,
        )
        checks.extend(compose_checks)
        if any(item.status != "passed" for item in compose_checks):
            return _record_and_outcome(db, message, role, role_instance_id, context, checks, "blocked_preflight")

        sandbox_status = "passed" if role.sandbox_mode in {"workspace-write", "danger-full-access"} else "blocked"
        checks.append(
            PreflightCheck(
                "static_config",
                "sandbox_allows_write",
                sandbox_status,
                remediation="Configure role sandbox_mode to workspace-write or danger-full-access for canonical artifact writes.",
                diagnostic={"sandbox_mode": role.sandbox_mode, "role_config_hash": _role_config_hash(role)},
            )
        )
        if sandbox_status != "passed":
            return _record_and_outcome(db, message, role, role_instance_id, context, checks, "blocked_preflight")

        if self.probe_runner is None:
            return _record_and_outcome(db, message, role, role_instance_id, context, checks, None)

        live_checks = _live_checks(
            runner=self.probe_runner,
            role_id=role.role_id,
            service_name=role.service_name,
            message_id=message.message_id,
            canonical_path=str(context["canonical_path"]),
        )
        checks.extend(live_checks)
        if any(item.status != "passed" for item in live_checks):
            return _record_and_outcome(db, message, role, role_instance_id, context, checks, "failed_preflight")
        return _record_and_outcome(db, message, role, role_instance_id, context, checks, None)


def canonical_artifact_path(*, work_item_id: str | None, required_path: str | None) -> str:
    if not work_item_id:
        raise ValueError("work_item_id is required for canonical artifact path resolution")
    if not required_path:
        raise ValueError("required artifact path is required")
    raw = required_path.removeprefix("/documents/").strip()
    path = Path(raw)
    if path.is_absolute():
        raise ValueError("artifact path must be relative to /documents or already under /documents")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact path must not contain traversal segments")
    expected = ("work-items", work_item_id)
    if len(path.parts) < 3 or path.parts[:2] != expected:
        raise ValueError(f"artifact path must be under work-items/{work_item_id}/")
    return "/documents/" + "/".join(path.parts)


def required_artifact_path(payload: dict[str, Any], *, work_item_id: str | None) -> str | None:
    value = payload.get("required_artifact_path") or payload.get("artifact_path") or payload.get("path")
    if isinstance(value, str) and value.strip():
        return _normalize_slot_alias(value.strip())
    state = payload.get("state") or payload.get("lifecycle_state")
    if isinstance(state, str) and state in STATE_ARTIFACT_PATHS and work_item_id:
        return f"work-items/{work_item_id}/{STATE_ARTIFACT_PATHS[state]}"
    return None


def _resolve_context(*, db: V4Database, message: QueuedMessage) -> dict[str, str | None]:
    payload = message.payload if isinstance(message.payload, dict) else {}
    work_item_id = _string(payload.get("work_item_id"))
    if work_item_id is None:
        work_item_id = _work_item_for_handoff(db=db, handoff_id=_string(payload.get("handoff_id")))
    lifecycle_state = _string(payload.get("state") or payload.get("lifecycle_state"))
    if lifecycle_state is None and work_item_id:
        row = db.connection.execute("SELECT state FROM work_items WHERE work_item_id=?", (work_item_id,)).fetchone()
        lifecycle_state = str(row["state"]) if row is not None else None
    required_path = required_artifact_path(payload, work_item_id=work_item_id)
    if required_path is None and lifecycle_state in STATE_ARTIFACT_PATHS and work_item_id:
        required_path = f"work-items/{work_item_id}/{STATE_ARTIFACT_PATHS[lifecycle_state]}"
    return {
        "work_item_id": work_item_id,
        "handoff_id": _string(payload.get("handoff_id")),
        "lifecycle_state": lifecycle_state,
        "required_path": required_path,
        "canonical_path": None,
    }


def _work_item_for_handoff(*, db: V4Database, handoff_id: str | None) -> str | None:
    if not handoff_id:
        return None
    row = db.connection.execute("SELECT work_item_id FROM handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
    return str(row["work_item_id"]) if row is not None and row["work_item_id"] else None


def _compose_checks(
    *,
    project_config: V4ProjectConfig,
    role_id: str,
    service_name: str,
    compose_text: str | None,
) -> list[PreflightCheck]:
    text = compose_text or render_compose(project_config)
    service = _compose_service(text, service_name)
    if service is None:
        return [
            PreflightCheck(
                "static_config",
                "compose_service_declared",
                "blocked",
                remediation=f"Declare compose service {service_name} for role {role_id}.",
                diagnostic={"service_name": service_name},
            )
        ]
    volumes = [str(item) for item in service.get("volumes") or []]
    checks = []
    for mount, remediation in {
        "/documents": "Ensure the role service mounts ${AGENTIC_MESH_DOCUMENTS_HOST_PATH} at /documents with write access.",
        "/mesh/project": "Ensure the role service mounts ${AGENTIC_MESH_PROJECT_HOST_PATH} at /mesh/project for safe-output DB access.",
    }.items():
        status = "passed" if any(_volume_target(volume) == mount for volume in volumes) else "blocked"
        checks.append(
            PreflightCheck(
                "static_config",
                f"compose_mount_declared:{mount}",
                status,
                remediation=remediation,
                diagnostic={"service_name": service_name, "volumes": volumes},
            )
        )
    return checks


def _live_checks(
    *,
    runner: ProbeRunner,
    role_id: str,
    service_name: str,
    message_id: str,
    canonical_path: str,
) -> list[PreflightCheck]:
    parent = str(Path(canonical_path).parent)
    scratch = f"/documents/.preflight/{role_id}/{message_id}-{uuid4().hex}.txt"
    commands = [
        (
            "parent_accessible",
            ["sh", "-lc", f"mkdir -p {json.dumps(parent)} && test -d {json.dumps(parent)}"],
            "Ensure the canonical artifact parent exists or can be created under /documents.",
        ),
        (
            "document_root.write",
            [
                "sh",
                "-lc",
                (
                    f"mkdir -p {json.dumps(str(Path(scratch).parent))} && "
                    f"printf preflight > {json.dumps(scratch)} && "
                    f"test \"$(cat {json.dumps(scratch)})\" = preflight && "
                    f"rm -f {json.dumps(scratch)}"
                ),
            ],
            "Ensure the role service has write access to /documents and can create/read/delete .preflight scratch files.",
        ),
        (
            "safe_output_db_access",
            [
                "python",
                "-c",
                (
                    "from agentic_mesh_v4.db import V4Database; "
                    "db=V4Database(); "
                    "db.connection.execute('SELECT 1').fetchone(); "
                    "db.close()"
                ),
            ],
            "Ensure the role container has the Postgres database environment and can open a V4 database connection.",
        ),
    ]
    checks = []
    for check_name, command, remediation in commands:
        try:
            result = runner.run(service_name=service_name, command=command)
        except Exception as exc:  # noqa: BLE001 - probe failures must become diagnostics.
            checks.append(
                PreflightCheck(
                    "live_container",
                    check_name,
                    "failed",
                    remediation=remediation,
                    exit_code=None,
                    stderr=str(exc),
                    diagnostic={"probe_mechanism": "container_exec", "command": command},
                )
            )
            continue
        status = "passed" if result.exit_code == 0 else "failed"
        checks.append(
            PreflightCheck(
                "live_container",
                check_name,
                status,
                remediation=remediation,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                diagnostic={"probe_mechanism": "container_exec", "command": command},
            )
        )
    return checks


def _record_and_outcome(
    db: V4Database,
    message: QueuedMessage,
    role: object,
    role_instance_id: str,
    context: dict[str, str | None],
    checks: list[PreflightCheck],
    message_state: str | None,
) -> PreflightOutcome:
    service_name = str(getattr(role, "service_name"))
    for check in checks:
        db.record_preflight_result(
            message_id=message.message_id,
            handoff_id=context.get("handoff_id"),
            work_item_id=context.get("work_item_id"),
            role_id=str(getattr(role, "role_id")),
            role_instance_id=role_instance_id,
            service_name=service_name,
            lifecycle_state=context.get("lifecycle_state"),
            required_path=context.get("required_path"),
            canonical_path=context.get("canonical_path"),
            probe_type=check.probe_type,
            check_name=check.check_name,
            status=check.status,
            exit_code=check.exit_code,
            stdout_excerpt=check.stdout,
            stderr_excerpt=check.stderr,
            diagnostic=check.diagnostic,
            remediation=check.remediation,
        )
    failed = next((item for item in checks if item.status != "passed"), None)
    if failed is None:
        return PreflightOutcome(
            status="passed",
            checks=tuple(checks),
            required_path=context.get("required_path"),
            canonical_path=context.get("canonical_path"),
            work_item_id=context.get("work_item_id"),
            lifecycle_state=context.get("lifecycle_state"),
            handoff_id=context.get("handoff_id"),
        )
    state = message_state or "blocked_preflight"
    return PreflightOutcome(
        status=failed.status,
        message_state=state,
        summary=f"Role artifact preflight {failed.status}: {failed.check_name}. {failed.remediation}",
        checks=tuple(checks),
        required_path=context.get("required_path"),
        canonical_path=context.get("canonical_path"),
        work_item_id=context.get("work_item_id"),
        lifecycle_state=context.get("lifecycle_state"),
        handoff_id=context.get("handoff_id"),
    )


def _compose_service(compose_text: str, service_name: str) -> dict[str, Any] | None:
    parsed = yaml.safe_load(compose_text) or {}
    if not isinstance(parsed, dict):
        return None
    services = parsed.get("services")
    if not isinstance(services, dict):
        return None
    service = services.get(service_name)
    return service if isinstance(service, dict) else None


def _volume_target(volume: str) -> str:
    parts = volume.rsplit(":", 2)
    if len(parts) < 2:
        return volume
    if parts[-1] in {"ro", "rw", "z", "Z", "cached", "delegated", "consistent"} and len(parts) >= 3:
        return parts[-2]
    return parts[-1]


def _role_config_hash(role: object) -> str:
    data = {
        "role_id": getattr(role, "role_id", ""),
        "sandbox_mode": getattr(role, "sandbox_mode", ""),
        "approval_policy": getattr(role, "approval_policy", ""),
        "model": getattr(role, "model", ""),
        "service_name": getattr(role, "service_name", ""),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _normalize_slot_alias(path: str) -> str:
    aliases = {
        "/20-product-definition.md": "/020-product-definition.md",
        "/30-experience-design.md": "/030-experience-design.md",
        "/30-solution-design.md": "/030-solution-design.md",
        "/50-security-review.md": "/050-security-review.md",
        "/60-prompt-contract.md": "/060-prompt-contract.md",
    }
    for old, new in aliases.items():
        if path.endswith(old):
            return path[: -len(old)] + new
    return path


def _string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
