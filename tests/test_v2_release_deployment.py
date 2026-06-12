from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.release import ComposeDeploymentTarget
from agentic_mesh_v2.release import ComposeCommandResult
from agentic_mesh_v2.release import ReleaseError
from agentic_mesh_v2.release import ReleaseEvidence
from agentic_mesh_v2.release import ReleaseEvidenceLink
from agentic_mesh_v2.release import ReleaseService
from agentic_mesh_v2.state_machine import TransitionRequest


def _db_with_release_work(tmp_path: Path) -> V2Database:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_queue_item(
        queue_item_id="queue-release-deployment",
        title="Release deployment validation",
        summary="Prove release deployment, rollback, and evidence capture.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-release-deployment", actor_role="product-manager", reason="Ready for implementation.")
    db.promote_queue_item(
        queue_item_id="queue-release-deployment",
        work_item_id="work-release-deployment",
        owner_role="product-manager",
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-deployment",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Product definition approved.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-deployment",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Engineering completed implementation.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-release-deployment",
            from_state="active",
            to_state="release_review",
            actor_role="qa-engineer",
            reason="QA passed.",
        )
    )
    return db


def _compose_file(tmp_path: Path) -> Path:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  v2-runtime:
    image: agentic-mesh:local
    command: python -m agentic_mesh_v2.cli --db /mesh/project/state/v2/agentic-mesh-v2.sqlite3 serve --host 0.0.0.0 --port 8080
    environment:
      AGENTIC_MESH_PROJECT_FILE: /mesh/project/agentic-mesh/project.yaml
    volumes:
      - ./project:/mesh/project
    ports:
      - "8100:8080"
""".strip(),
        encoding="utf-8",
    )
    return compose


def _links() -> tuple[ReleaseEvidenceLink, ...]:
    return (
        ReleaseEvidenceLink("work-items/work-release-deployment/020-product-definition.md", "product", "product-manager"),
        ReleaseEvidenceLink("work-items/work-release-deployment/040-architecture.md", "architecture", "solution-architect"),
        ReleaseEvidenceLink("work-items/work-release-deployment/050-security.md", "security", "security-architect"),
        ReleaseEvidenceLink("work-items/work-release-deployment/060-prompt-contract.md", "prompt", "prompt-engineer"),
        ReleaseEvidenceLink("work-items/work-release-deployment/100-implementation-log.md", "engineering", "engineering"),
        ReleaseEvidenceLink("work-items/work-release-deployment/110-quality-evidence.md", "qa", "qa-engineer"),
        ReleaseEvidenceLink("work-items/work-release-deployment/140-release-record.md", "release", "release-manager"),
    )


def _target(compose: Path) -> ComposeDeploymentTarget:
    return ComposeDeploymentTarget(
        target_id="target-compose-dogfood",
        project_id="agentic-mesh-dev",
        connector_id="teams-agentic-mesh-dev",
        compose_files=(compose,),
        service_name="v2-runtime",
        external_base_url="http://linuxch:8100",
        disablement_path="Disable target-compose-dogfood and redeploy the previous image.",
    )


def test_compose_target_release_executes_and_records_deployment_smoke_and_evidence(tmp_path: Path) -> None:
    db = _db_with_release_work(tmp_path)
    commands: list[list[str]] = []

    def runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ComposeCommandResult:
        commands.append(command)
        assert timeout_seconds == 300
        return ComposeCommandResult(exit_code=0, stdout="started")

    release = ReleaseService(db, compose_runner=runner)
    release.register_compose_target(_target(_compose_file(tmp_path)))

    run_id = release.deploy_compose_release(
        ReleaseEvidence(
            work_item_id="work-release-deployment",
            release_id="release-compose-dogfood",
            scope="V2 Teams connector local dogfood release.",
            commit_ref="20edaa0",
            approval_ref="human-response-release-approval",
            rollback_plan="Disable the connector target and redeploy the previous image.",
            residual_risks="Real Teams tenant smoke is still required before enterprise release.",
        ),
        target_id="target-compose-dogfood",
        smoke_checks={
            "healthz": "passed",
            "status_json": "passed",
            "inbound_dm": "passed",
            "outbound_reply": "passed",
            "approval_response": "passed",
            "permission_failure": "passed",
        },
        evidence_links=_links(),
    )
    release.close_released_work(
        work_item_id="work-release-deployment",
        from_state="release_review",
        actor_role="release-manager",
        reason="Sponsor approved release after smoke evidence.",
    )

    snapshot = db.status_snapshot()
    assert commands == [
        [
            "docker",
            "compose",
            "-f",
            str(tmp_path / "docker-compose.yml"),
            "up",
            "-d",
            "v2-runtime",
        ]
    ]
    assert db.get_work_item("work-release-deployment").state == "closed"
    assert snapshot["releases"][0]["status"] == "deployed"
    assert snapshot["deployment_runs"][0]["run_id"] == run_id
    assert snapshot["deployment_runs"][0]["status"] == "succeeded"
    assert "healthz" in snapshot["deployment_runs"][0]["smoke_result"]
    assert len(snapshot["release_evidence_links"]) == 7
    assert {link["artifact_type"] for link in snapshot["release_evidence_links"]} == {
        "architecture",
        "engineering",
        "product",
        "prompt",
        "qa",
        "release",
        "security",
    }


def test_release_requires_complete_evidence_and_passing_smoke(tmp_path: Path) -> None:
    db = _db_with_release_work(tmp_path)
    release = ReleaseService(db)
    release.register_compose_target(_target(_compose_file(tmp_path)))

    with pytest.raises(ReleaseError, match="missing required types"):
        release.record_compose_deployment(
            ReleaseEvidence(
                work_item_id="work-release-deployment",
                release_id="release-incomplete-evidence",
                scope="Incomplete release.",
                rollback_plan="Disable target.",
                residual_risks="Missing evidence.",
            ),
            target_id="target-compose-dogfood",
            smoke_checks={"healthz": "passed"},
            evidence_links=(
                ReleaseEvidenceLink("work-items/work-release-deployment/020-product-definition.md", "product", "product-manager"),
            ),
        )
    with pytest.raises(ReleaseError, match="smoke checks failed"):
        release.record_compose_deployment(
            ReleaseEvidence(
                work_item_id="work-release-deployment",
                release_id="release-failing-smoke",
                scope="Failing release.",
                rollback_plan="Disable target.",
                residual_risks="Smoke failed.",
            ),
            target_id="target-compose-dogfood",
            smoke_checks={"healthz": "failed"},
            evidence_links=_links(),
        )


def test_release_evidence_links_must_be_accepted(tmp_path: Path) -> None:
    db = _db_with_release_work(tmp_path)
    release = ReleaseService(db)
    release.register_compose_target(_target(_compose_file(tmp_path)))
    links = (
        ReleaseEvidenceLink(
            "work-items/work-release-deployment/020-product-definition.md",
            "product",
            "product-manager",
            status="rejected",
        ),
        *_links()[1:],
    )

    with pytest.raises(ReleaseError, match="must be accepted"):
        release.record_compose_deployment(
            ReleaseEvidence(
                work_item_id="work-release-deployment",
                release_id="release-rejected-evidence",
                scope="Rejected evidence release.",
                rollback_plan="Do not deploy until evidence is accepted.",
                residual_risks="Evidence has not been accepted.",
            ),
            target_id="target-compose-dogfood",
            smoke_checks={"healthz": "passed"},
            evidence_links=links,
        )


def test_failed_compose_command_records_failed_deployment_without_release(tmp_path: Path) -> None:
    db = _db_with_release_work(tmp_path)

    def runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ComposeCommandResult:
        return ComposeCommandResult(exit_code=17, stdout="starting", stderr="compose failed")

    release = ReleaseService(db, compose_runner=runner)
    release.register_compose_target(_target(_compose_file(tmp_path)))

    with pytest.raises(ReleaseError, match="compose deployment command failed"):
        release.deploy_compose_release(
            ReleaseEvidence(
                work_item_id="work-release-deployment",
                release_id="release-command-failed",
                scope="Failed deployment.",
                rollback_plan="Inspect failed deployment and keep previous service running.",
                residual_risks="Deployment command failed.",
            ),
            target_id="target-compose-dogfood",
            smoke_checks={"healthz": "passed"},
            evidence_links=_links(),
        )

    snapshot = db.status_snapshot()
    assert snapshot["deployment_runs"][0]["status"] == "failed"
    assert snapshot["deployment_runs"][0]["smoke_result"] == "not_run: deployment command failed"
    assert snapshot["deployment_runs"][0]["evidence"]["exit_code"] == 17
    assert snapshot["releases"] == []
    assert db.get_work_item("work-release-deployment").state == "release_review"


def test_rollback_disablement_preserves_connector_runtime_state(tmp_path: Path) -> None:
    db = _db_with_release_work(tmp_path)
    config = ConnectorConfig.from_dict(
        {
            "connector_id": "teams-agentic-mesh-dev",
            "project_id": "agentic-mesh-dev",
            "connector_type": "teams",
            "display_name": "Agentic Mesh Dev Teams",
            "project_team_ref": "team-dev",
            "default_project_channel_ref": "channel-project",
            "external_base_url": "http://linuxch:8100",
            "role_identities": {
                "product-manager": {
                    "external_ref": "bot-product-manager",
                    "display_name": "AM-Product Manager",
                    "alias": "product-manager",
                    "mention_handle": "@AM-Product Manager",
                    "identity_model": "separate_bot",
                    "enabled": True,
                }
            },
            "human_authorities": {"nicholas": ["sponsor"]},
            "retention": {
                "private_dm_days": 30,
                "project_channel_days": 90,
                "compacted_summary_days": 365,
                "delivery_record_days": 90,
                "idempotency_receipt_days": 30,
            },
            "team_wide_trigger": "@all-agents",
        }
    )
    adapter = LocalTeamsTestAdapter(db, config)
    adapter.install()
    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-before-disable",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "Project context before rollback disablement.",
        }
    )
    release = ReleaseService(db)
    release.register_compose_target(_target(_compose_file(tmp_path)))
    before = db.status_snapshot()

    release.disable_deployment_target(
        target_id="target-compose-dogfood",
        reason="Rollback: stop connector ingress while preserving audit state.",
    )

    after = db.status_snapshot()
    assert after["deployment_targets"][0]["status"] == "disabled"
    assert after["deployment_targets"][0]["disable_reason"].startswith("Rollback")
    assert after["counts"]["connectors"] == before["counts"]["connectors"]
    assert after["counts"]["conversations"] == before["counts"]["conversations"]
    assert after["counts"]["conversation_events"] == before["counts"]["conversation_events"]
    assert after["counts"]["external_event_receipts"] == before["counts"]["external_event_receipts"]
    assert any(event["event_type"] == "deployment_target.disabled" for event in after["recent_events"])


def test_disabled_target_cannot_be_released_again(tmp_path: Path) -> None:
    db = _db_with_release_work(tmp_path)
    release = ReleaseService(db)
    release.register_compose_target(_target(_compose_file(tmp_path)))
    release.disable_deployment_target(target_id="target-compose-dogfood", reason="Rollback requested.")

    with pytest.raises(ReleaseError, match="not active"):
        release.record_compose_deployment(
            ReleaseEvidence(
                work_item_id="work-release-deployment",
                release_id="release-disabled-target",
                scope="Disabled target release.",
                rollback_plan="Already disabled.",
                residual_risks="Target disabled.",
            ),
            target_id="target-compose-dogfood",
            smoke_checks={"healthz": "passed"},
            evidence_links=_links(),
        )
