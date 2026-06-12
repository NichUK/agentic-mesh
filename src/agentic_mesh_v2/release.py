from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.state_machine import TransitionRequest


class ReleaseError(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseEvidence:
    work_item_id: str
    release_id: str
    scope: str
    rollback_plan: str
    residual_risks: str
    commit_ref: str | None = None
    approval_ref: str | None = None
    deployment_result: str | None = None
    smoke_result: str | None = None


@dataclass(frozen=True)
class ComposeDeploymentTarget:
    target_id: str
    project_id: str
    compose_files: tuple[Path, ...]
    service_name: str
    connector_id: str | None = None
    external_base_url: str | None = None
    disablement_path: str = "Set deployment target status to disabled and redeploy without connector ingress."


@dataclass(frozen=True)
class ReleaseEvidenceLink:
    artifact_ref: str
    artifact_type: str
    role_id: str
    status: str = "accepted"


@dataclass(frozen=True)
class ComposeCommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ComposeCommandRunner(Protocol):
    def __call__(self, command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ComposeCommandResult:
        ...


class ReleaseService:
    def __init__(self, db: V2Database, *, compose_runner: ComposeCommandRunner | None = None) -> None:
        self.db = db
        self._compose_runner = compose_runner or _run_compose_command

    def register_compose_target(self, target: ComposeDeploymentTarget) -> None:
        if not target.compose_files:
            raise ReleaseError("compose deployment target requires at least one Compose file")
        compose_summaries = [_compose_summary(path) for path in target.compose_files]
        if not any(target.service_name in summary["services"] for summary in compose_summaries):
            raise ReleaseError(f"compose target must define service `{target.service_name}`")
        service = _compose_service(target.service_name, compose_summaries)
        command = _string_or_list(service.get("command"))
        environment = service.get("environment") if isinstance(service.get("environment"), dict) else {}
        volumes = service.get("volumes") if isinstance(service.get("volumes"), list) else []
        if "agentic_mesh_v2.cli" not in command and "agentic-mesh" not in command:
            raise ReleaseError("compose target service must run the Agentic Mesh v2 runtime")
        if not (
            "AGENTIC_MESH_PROJECT_FILE" in environment
            or any("/mesh/project" in str(volume) for volume in volumes)
        ):
            raise ReleaseError("compose target must mount or configure the project boundary")
        self.db.upsert_deployment_target(
            target_id=target.target_id,
            connector_id=target.connector_id,
            project_id=target.project_id,
            target_type="compose",
            service_name=target.service_name,
            compose_files=[str(path) for path in target.compose_files],
            external_base_url=target.external_base_url,
            status="active",
            disable_reason=None,
            metadata={
                "disablement_path": target.disablement_path,
                "compose": compose_summaries,
            },
        )

    def record_compose_deployment(
        self,
        evidence: ReleaseEvidence,
        *,
        target_id: str,
        smoke_checks: dict[str, str],
        evidence_links: tuple[ReleaseEvidenceLink, ...],
        command: list[str] | None = None,
    ) -> str:
        target = self.db.get_deployment_target(target_id)
        if target is None:
            raise ReleaseError(f"unknown deployment target `{target_id}`")
        if target.get("status") != "active":
            raise ReleaseError(f"deployment target `{target_id}` is not active")
        _require_release_evidence_links(evidence_links)
        if not smoke_checks:
            raise ReleaseError("release deployment requires smoke checks")
        smoke_result = _smoke_summary(smoke_checks)
        complete_evidence = ReleaseEvidence(
            work_item_id=evidence.work_item_id,
            release_id=evidence.release_id,
            scope=evidence.scope,
            rollback_plan=evidence.rollback_plan,
            residual_risks=evidence.residual_risks,
            commit_ref=evidence.commit_ref,
            approval_ref=evidence.approval_ref,
            deployment_result=evidence.deployment_result
            or f"compose target `{target_id}` validated for service `{target['service_name']}`",
            smoke_result=smoke_result,
        )
        self.record_deployment(complete_evidence)
        run_id = f"deployment-{_short_hash(f'{target_id}:{evidence.release_id}:{evidence.work_item_id}')}"
        self.db.record_deployment_run(
            run_id=run_id,
            target_id=target_id,
            work_item_id=evidence.work_item_id,
            release_id=evidence.release_id,
            status="succeeded",
            command=command
            or [
                "docker",
                "compose",
                *[
                    part
                    for compose_file in target.get("compose_files", [])
                    for part in ("-f", str(compose_file))
                ],
                "up",
                "-d",
                str(target["service_name"]),
            ],
            smoke_result=smoke_result,
            rollback_plan=evidence.rollback_plan,
            evidence={
                "target_id": target_id,
                "external_base_url": target.get("external_base_url"),
                "smoke_checks": smoke_checks,
            },
        )
        for link in evidence_links:
            self.db.record_release_evidence_link(
                link_id=f"release-link-{_short_hash(f'{evidence.release_id}:{link.artifact_ref}:{link.role_id}')}",
                release_id=evidence.release_id,
                work_item_id=evidence.work_item_id,
                artifact_ref=link.artifact_ref,
                artifact_type=link.artifact_type,
                role_id=link.role_id,
                status=link.status,
            )
        return run_id

    def deploy_compose_release(
        self,
        evidence: ReleaseEvidence,
        *,
        target_id: str,
        smoke_checks: dict[str, str],
        evidence_links: tuple[ReleaseEvidenceLink, ...],
        command: list[str] | None = None,
        cwd: Path | None = None,
        timeout_seconds: int = 300,
    ) -> str:
        target = self.db.get_deployment_target(target_id)
        if target is None:
            raise ReleaseError(f"unknown deployment target `{target_id}`")
        if target.get("status") != "active":
            raise ReleaseError(f"deployment target `{target_id}` is not active")
        deploy_command = command or _compose_up_command(target)
        result = self._compose_runner(deploy_command, cwd=cwd, timeout_seconds=timeout_seconds)
        if result.exit_code != 0:
            run_id = f"deployment-{_short_hash(f'{target_id}:{evidence.release_id}:{evidence.work_item_id}:failed')}"
            self.db.record_deployment_run(
                run_id=run_id,
                target_id=target_id,
                work_item_id=evidence.work_item_id,
                release_id=evidence.release_id,
                status="failed",
                command=deploy_command,
                smoke_result="not_run: deployment command failed",
                rollback_plan=evidence.rollback_plan or "Deployment failed before activation; preserve runtime state and inspect command output.",
                evidence={
                    "target_id": target_id,
                    "external_base_url": target.get("external_base_url"),
                    "exit_code": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
            )
            raise ReleaseError(f"compose deployment command failed with exit code {result.exit_code}")
        return self.record_compose_deployment(
            evidence,
            target_id=target_id,
            smoke_checks=smoke_checks,
            evidence_links=evidence_links,
            command=deploy_command,
        )

    def disable_deployment_target(self, *, target_id: str, reason: str) -> None:
        if not reason.strip():
            raise ReleaseError("disablement requires a reason")
        target = self.db.get_deployment_target(target_id)
        if target is None:
            raise ReleaseError(f"unknown deployment target `{target_id}`")
        self.db.disable_deployment_target(target_id=target_id, reason=reason)

    def record_no_deployment(self, evidence: ReleaseEvidence, *, reason: str) -> None:
        if not reason.strip():
            raise ReleaseError("no-deployment disposition requires a reason")
        self.db.upsert_release(
            release_id=evidence.release_id,
            work_item_id=evidence.work_item_id,
            status="no_deployment_disposition",
            scope=evidence.scope,
            commit_ref=evidence.commit_ref,
            approval_ref=evidence.approval_ref,
            deployment_result=f"not_required: {reason}",
            smoke_result="not_required",
            rollback_plan=evidence.rollback_plan,
            residual_risks=evidence.residual_risks,
        )

    def record_deployment(self, evidence: ReleaseEvidence) -> None:
        if not evidence.deployment_result:
            raise ReleaseError("release deployment requires deployment_result")
        if not evidence.smoke_result:
            raise ReleaseError("release deployment requires smoke_result")
        if not evidence.rollback_plan.strip():
            raise ReleaseError("release deployment requires rollback_plan")
        self.db.upsert_release(
            release_id=evidence.release_id,
            work_item_id=evidence.work_item_id,
            status="deployed",
            scope=evidence.scope,
            commit_ref=evidence.commit_ref,
            approval_ref=evidence.approval_ref,
            deployment_result=evidence.deployment_result,
            smoke_result=evidence.smoke_result,
            rollback_plan=evidence.rollback_plan,
            residual_risks=evidence.residual_risks,
        )

    def close_released_work(
        self,
        *,
        work_item_id: str,
        from_state: str,
        actor_role: str,
        reason: str,
    ) -> None:
        release = self.db.connection.execute(
            "SELECT * FROM releases WHERE work_item_id = ? ORDER BY updated_at DESC LIMIT 1",
            (work_item_id,),
        ).fetchone()
        if release is None:
            raise ReleaseError("work item cannot close as released without a release record")
        if release["status"] not in {"deployed", "no_deployment_disposition"}:
            raise ReleaseError("release record is not deployable or explicitly no-deployment")
        if release["status"] == "deployed":
            _require_deployed_release_closure_evidence(
                db=self.db,
                release_id=str(release["release_id"]),
                work_item_id=work_item_id,
            )
        self.db.transition_work_item(
            TransitionRequest(
                work_item_id=work_item_id,
                from_state=from_state,
                to_state="released",
                actor_role=actor_role,
                reason=reason,
            )
        )
        self.db.transition_work_item(
            TransitionRequest(
                work_item_id=work_item_id,
                from_state="released",
                to_state="closed",
                actor_role=actor_role,
                reason="Released work closed with release evidence.",
            )
        )


def _compose_summary(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise ReleaseError(f"compose file does not exist: {path}")
    raw = _load_compose_yaml(path)
    services = raw.get("services") if isinstance(raw.get("services"), dict) else {}
    return {
        "path": str(path),
        "services": sorted(str(service) for service in services),
        "raw": raw,
    }


def _compose_service(service_name: str, summaries: list[dict[str, Any]]) -> dict[str, Any]:
    for summary in summaries:
        raw = summary.get("raw")
        services = raw.get("services") if isinstance(raw, dict) and isinstance(raw.get("services"), dict) else {}
        service = services.get(service_name)
        if isinstance(service, dict):
            return service
    raise ReleaseError(f"compose service `{service_name}` was not found")


class _ComposeLoader(yaml.SafeLoader):
    pass


def _unknown_yaml(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("", _unknown_yaml)


def _load_compose_yaml(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.load(handle, Loader=_ComposeLoader)
    if not isinstance(raw, dict):
        raise ReleaseError(f"compose file `{path}` must contain a mapping")
    return raw


def _string_or_list(value: object) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _smoke_summary(smoke_checks: dict[str, str]) -> str:
    failures = [f"{name}={result}" for name, result in smoke_checks.items() if str(result).casefold() != "passed"]
    if failures:
        raise ReleaseError(f"release smoke checks failed: {', '.join(failures)}")
    return "passed: " + ", ".join(sorted(smoke_checks))


def _require_release_evidence_links(evidence_links: tuple[ReleaseEvidenceLink, ...]) -> None:
    required = {
        "product",
        "architecture",
        "security",
        "prompt",
        "engineering",
        "qa",
        "release",
    }
    present = {link.artifact_type for link in evidence_links}
    missing = sorted(required - present)
    if missing:
        raise ReleaseError(f"release evidence links missing required types: {', '.join(missing)}")


def _require_deployed_release_closure_evidence(*, db: V2Database, release_id: str, work_item_id: str) -> None:
    runs = [
        run
        for run in db.list_deployment_runs()
        if run.get("release_id") == release_id
        and run.get("work_item_id") == work_item_id
        and run.get("status") == "succeeded"
    ]
    if not runs:
        raise ReleaseError("deployed release cannot close without a successful deployment run")
    release_links = tuple(
        ReleaseEvidenceLink(
            artifact_ref=str(link["artifact_ref"]),
            artifact_type=str(link["artifact_type"]),
            role_id=str(link["role_id"]),
            status=str(link["status"]),
        )
        for link in db.list_release_evidence_links()
        if link.get("release_id") == release_id and link.get("work_item_id") == work_item_id
    )
    _require_release_evidence_links(release_links)


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _compose_up_command(target: dict[str, Any]) -> list[str]:
    return [
        "docker",
        "compose",
        *[
            part
            for compose_file in target.get("compose_files", [])
            for part in ("-f", str(compose_file))
        ],
        "up",
        "-d",
        str(target["service_name"]),
    ]


def _run_compose_command(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ComposeCommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return ComposeCommandResult(
            exit_code=124,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\nTimed out after {timeout_seconds} seconds.",
        )
    except OSError as exc:
        return ComposeCommandResult(exit_code=127, stderr=str(exc))
    return ComposeCommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
