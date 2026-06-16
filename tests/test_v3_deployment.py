import sys
from pathlib import Path

from agentic_mesh_v3.deployment import CommandDeploymentTarget
from agentic_mesh_v3.deployment import NoDeploymentDisposition
from agentic_mesh_v3.deployment import deployment_targets_from_project_config
from agentic_mesh_v3.project_config import V3BrokerConfig
from agentic_mesh_v3.project_config import V3DocumentLibraryConfig
from agentic_mesh_v3.project_config import V3ProjectConfig
from agentic_mesh_v3.project_config import V3ReleaseDeploymentTargetConfig


def test_command_deployment_target_records_success() -> None:
    result = CommandDeploymentTarget(
        target_id="local-smoke",
        command=(sys.executable, "-c", "print('deployed')"),
        rollback_plan="Run previous command.",
    ).deploy()

    assert result.status == "deployed"
    assert "deployed" in result.output
    assert result.rollback_plan == "Run previous command."


def test_command_deployment_target_records_timeout_as_failure() -> None:
    result = CommandDeploymentTarget(
        target_id="slow-target",
        command=(sys.executable, "-c", "import time; print('starting'); time.sleep(5)"),
        rollback_plan="Keep previous deployment active.",
        timeout_seconds=1,
    ).deploy()

    assert result.status == "failed"
    assert "timed out after 1 seconds" in result.output
    assert result.rollback_plan == "Keep previous deployment active."


def test_no_deployment_disposition_is_explicit() -> None:
    result = NoDeploymentDisposition(
        target_id="planning-only",
        reason="Planning slice only.",
    ).deploy()

    assert result.status == "no_deployment"
    assert result.output == "Planning slice only."


def test_deployment_targets_from_project_config_builds_targets(tmp_path: Path) -> None:
    config = V3ProjectConfig(
        project_id="agentic-mesh-dev",
        broker=V3BrokerConfig(adapter="in-memory"),
        document_library=V3DocumentLibraryConfig(adapter="filesystem", root=tmp_path / "documents"),
        roles=(),
        release_deployment_targets=(
            V3ReleaseDeploymentTargetConfig(
                target_id="local-smoke",
                target_type="command",
                command=(sys.executable, "-c", "print('configured')"),
                rollback_plan="Rollback configured.",
            ),
            V3ReleaseDeploymentTargetConfig(
                target_id="planning-only",
                target_type="no_deployment",
                reason="Planning-only.",
            ),
        ),
    )

    targets = deployment_targets_from_project_config(config)

    assert targets["local-smoke"].deploy().status == "deployed"
    assert targets["planning-only"].deploy().status == "no_deployment"


def test_deployment_targets_from_project_config_rejects_unknown_target_type(tmp_path: Path) -> None:
    config = V3ProjectConfig(
        project_id="agentic-mesh-dev",
        broker=V3BrokerConfig(adapter="in-memory"),
        document_library=V3DocumentLibraryConfig(adapter="filesystem", root=tmp_path / "documents"),
        roles=(),
        release_deployment_targets=(
            V3ReleaseDeploymentTargetConfig(
                target_id="custom",
                target_type="unsupported",
            ),
        ),
    )

    try:
        deployment_targets_from_project_config(config)
    except ValueError as exc:
        assert "unsupported deployment target type" in str(exc)
    else:
        raise AssertionError("unknown deployment target types should be rejected")
