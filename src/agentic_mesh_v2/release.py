from __future__ import annotations

from dataclasses import dataclass

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


class ReleaseService:
    def __init__(self, db: V2Database) -> None:
        self.db = db

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
