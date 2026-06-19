from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Protocol
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentic_mesh_v3.project_config import V3ProjectConfig


@dataclass(frozen=True)
class DeploymentResult:
    target_id: str
    status: str
    output: str
    rollback_plan: str


class DeploymentTarget(Protocol):
    def deploy(self) -> DeploymentResult:
        """Execute a release-manager-approved deployment action."""


@dataclass(frozen=True)
class CommandDeploymentTarget:
    target_id: str
    command: tuple[str, ...]
    cwd: Path | None = None
    rollback_plan: str = "Re-run the previous known-good deployment target."
    timeout_seconds: int = 300
    env: dict[str, str] = field(default_factory=dict)

    def deploy(self) -> DeploymentResult:
        if not self.command:
            raise ValueError("deployment command is required")
        env = os.environ.copy()
        env.update(self.env)
        try:
            completed = subprocess.run(
                list(self.command),
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            output = _timeout_output(exc, timeout_seconds=self.timeout_seconds)
            return DeploymentResult(
                target_id=self.target_id,
                status="failed",
                output=output,
                rollback_plan=self.rollback_plan,
            )
        output = (completed.stdout + completed.stderr).strip()
        return DeploymentResult(
            target_id=self.target_id,
            status="deployed" if completed.returncode == 0 else "failed",
            output=output,
            rollback_plan=self.rollback_plan,
        )


@dataclass(frozen=True)
class NoDeploymentDisposition:
    target_id: str
    reason: str
    rollback_plan: str = "No deployment was performed; no runtime rollback is required."

    def deploy(self) -> DeploymentResult:
        return DeploymentResult(
            target_id=self.target_id,
            status="no_deployment",
            output=self.reason,
            rollback_plan=self.rollback_plan,
        )


def deployment_targets_from_project_config(config: "V3ProjectConfig") -> dict[str, DeploymentTarget]:
    targets: dict[str, DeploymentTarget] = {}
    for target in config.release_deployment_targets:
        target_type = target.target_type.casefold().replace("_", "-")
        if target_type == "command":
            targets[target.target_id] = CommandDeploymentTarget(
                target_id=target.target_id,
                command=target.command,
                cwd=target.working_directory,
                rollback_plan=target.rollback_plan,
                timeout_seconds=target.timeout_seconds,
                env={"AGENTIC_MESH_STOP_ROLE_SERVICES_ON_RELEASE": "0"},
            )
            continue
        if target_type in {"no-deployment", "no-deployment-disposition"}:
            targets[target.target_id] = NoDeploymentDisposition(
                target_id=target.target_id,
                reason=target.reason or "No deployment is required for this target.",
                rollback_plan=target.rollback_plan,
            )
            continue
        raise ValueError(f"unsupported deployment target type: {target.target_type}")
    return targets


def _timeout_output(exc: subprocess.TimeoutExpired, *, timeout_seconds: int) -> str:
    output_parts = [f"Deployment command timed out after {timeout_seconds} seconds."]
    for label, value in (("stdout", exc.stdout), ("stderr", exc.stderr)):
        if value is None:
            continue
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        if text.strip():
            output_parts.append(f"{label}: {text.strip()}")
    return "\n".join(output_parts)
