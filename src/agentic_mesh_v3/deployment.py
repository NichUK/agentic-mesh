from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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

    def deploy(self) -> DeploymentResult:
        if not self.command:
            raise ValueError("deployment command is required")
        completed = subprocess.run(
            list(self.command),
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
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
