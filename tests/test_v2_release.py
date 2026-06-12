from pathlib import Path

import pytest

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.release import ReleaseError
from agentic_mesh_v2.release import ReleaseEvidence
from agentic_mesh_v2.release import ReleaseService
from agentic_mesh_v2.state_machine import TransitionRequest


def _releasedb(tmp_path: Path) -> V2Database:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_queue_item(
        queue_item_id="queue-1",
        title="Tiny feature",
        summary="Ship a small feature.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-1", actor_role="product-manager", reason="Ready.")
    db.promote_queue_item(
        queue_item_id="queue-1",
        work_item_id="work-1",
        owner_role="product-manager",
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-1",
            from_state="shaping",
            to_state="ready",
            actor_role="product-manager",
            reason="Product approved.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-1",
            from_state="ready",
            to_state="active",
            actor_role="engineering",
            reason="Implementation started.",
        )
    )
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-1",
            from_state="active",
            to_state="release_review",
            actor_role="qa-engineer",
            reason="QA passed.",
        )
    )
    return db


def test_release_cannot_close_without_release_record(tmp_path: Path) -> None:
    db = _releasedb(tmp_path)
    service = ReleaseService(db)

    with pytest.raises(ReleaseError, match="without a release record"):
        service.close_released_work(
            work_item_id="work-1",
            from_state="release_review",
            actor_role="release-manager",
            reason="Approved.",
        )


def test_release_close_requires_deployment_or_no_deployment_disposition(tmp_path: Path) -> None:
    db = _releasedb(tmp_path)
    service = ReleaseService(db)
    evidence = ReleaseEvidence(
        work_item_id="work-1",
        release_id="rel-1",
        scope="Tiny feature.",
        commit_ref="abc123",
        approval_ref="approval-1",
        rollback_plan="Revert commit abc123.",
        residual_risks="None known.",
    )

    service.record_no_deployment(evidence, reason="Documentation-only release.")
    service.close_released_work(
        work_item_id="work-1",
        from_state="release_review",
        actor_role="release-manager",
        reason="Sponsor approved no-deployment closure.",
    )

    assert db.get_work_item("work-1").state == "closed"


def test_release_close_allows_already_released_work(tmp_path: Path) -> None:
    db = _releasedb(tmp_path)
    service = ReleaseService(db)
    evidence = ReleaseEvidence(
        work_item_id="work-1",
        release_id="rel-1",
        scope="Tiny feature.",
        commit_ref="abc123",
        approval_ref="approval-1",
        rollback_plan="Revert commit abc123.",
        residual_risks="None known.",
    )

    service.record_no_deployment(evidence, reason="Documentation-only release.")
    db.transition_work_item(
        TransitionRequest(
            work_item_id="work-1",
            from_state="release_review",
            to_state="released",
            actor_role="release-manager",
            reason="Release evidence accepted.",
        )
    )
    service.close_released_work(
        work_item_id="work-1",
        from_state="released",
        actor_role="release-manager",
        reason="Sponsor approved no-deployment closure.",
    )

    assert db.get_work_item("work-1").state == "closed"


def test_deployed_release_cannot_close_without_deployment_run_and_evidence_links(tmp_path: Path) -> None:
    db = _releasedb(tmp_path)
    service = ReleaseService(db)
    service.record_deployment(
        ReleaseEvidence(
            work_item_id="work-1",
            release_id="rel-shortcut",
            scope="Tiny feature.",
            commit_ref="abc123",
            approval_ref="approval-1",
            deployment_result="manual claim",
            smoke_result="passed",
            rollback_plan="Revert commit abc123.",
            residual_risks="None known.",
        )
    )

    with pytest.raises(ReleaseError, match="successful deployment run"):
        service.close_released_work(
            work_item_id="work-1",
            from_state="release_review",
            actor_role="release-manager",
            reason="Sponsor approved release.",
        )

    assert db.get_work_item("work-1").state == "release_review"
