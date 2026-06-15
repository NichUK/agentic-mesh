import sys

from agentic_mesh_v3.deployment import CommandDeploymentTarget
from agentic_mesh_v3.deployment import NoDeploymentDisposition


def test_command_deployment_target_records_success() -> None:
    result = CommandDeploymentTarget(
        target_id="local-smoke",
        command=(sys.executable, "-c", "print('deployed')"),
        rollback_plan="Run previous command.",
    ).deploy()

    assert result.status == "deployed"
    assert "deployed" in result.output
    assert result.rollback_plan == "Run previous command."


def test_no_deployment_disposition_is_explicit() -> None:
    result = NoDeploymentDisposition(
        target_id="planning-only",
        reason="Planning slice only.",
    ).deploy()

    assert result.status == "no_deployment"
    assert result.output == "Planning slice only."
