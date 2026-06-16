from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.documents import DocumentLibraryAdapter


@dataclass(frozen=True)
class DogfoodAuditCheck:
    check_id: str
    passed: bool
    summary: str


@dataclass(frozen=True)
class DogfoodAuditResult:
    work_item_id: str
    passed: bool
    checks: tuple[DogfoodAuditCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "passed": self.passed,
            "checks": [
                {"check_id": check.check_id, "passed": check.passed, "summary": check.summary}
                for check in self.checks
            ],
        }


def audit_v3_dogfood_completion(
    *,
    db: V3Database,
    document_library: DocumentLibraryAdapter,
    work_item_id: str = "work-v3-local-e2e",
    require_onedrive_artifacts: bool = False,
) -> DogfoodAuditResult:
    """Audit whether a V3 dogfood slice proves the requested end-to-end path."""

    detail = db.work_item_detail(work_item_id)
    checks: list[DogfoodAuditCheck] = []

    def add(check_id: str, passed: bool, summary: str) -> None:
        checks.append(DogfoodAuditCheck(check_id=check_id, passed=passed, summary=summary))

    add("work_item.exists", detail is not None, f"Work item `{work_item_id}` exists.")
    if detail is None:
        return DogfoodAuditResult(work_item_id=work_item_id, passed=False, checks=tuple(checks))

    add(
        "work_item.closed",
        detail.state == "closed",
        f"Work item state is `{detail.state}`; expected `closed`.",
    )
    add(
        "queue.closed",
        _linked_backlog_is_closed(db, work_item_id=work_item_id),
        "Linked queue/backlog item is closed.",
    )
    add(
        "approval.approved",
        any(approval.status == "approved" for approval in detail.approvals),
        "At least one sponsor approval is recorded as approved.",
    )
    add(
        "approval.delivered",
        any(delivery.purpose == "approval.request" and delivery.status == "sent" for delivery in detail.deliveries),
        "Sponsor approval request was sent through the stakeholder bridge.",
    )
    add(
        "sponsor.closed_notification",
        any(delivery.purpose == "messaging.send" and delivery.status == "sent" for delivery in detail.deliveries),
        "Sponsor-facing release/closure notification was sent.",
    )

    record_types = {record.record_type for record in detail.governance_records}
    for record_type in ("handoff.require", "consult.request", "informed.update", "decision.record"):
        add(
            f"governance.{record_type}",
            record_type in record_types,
            f"Governance records include `{record_type}`.",
        )

    artifact_paths = {artifact.relative_path for artifact in detail.artifacts}
    work_index_path = f"work-items/{work_item_id}/index.md"
    add(
        "documents.work_item_index_recorded",
        work_index_path in artifact_paths,
        f"Work-item index artifact `{work_index_path}` is recorded.",
    )
    add(
        "documents.work_item_index_exists",
        document_library.exists(work_index_path),
        f"Work-item index `{work_index_path}` exists in the document library.",
    )
    add(
        "documents.root_work_item_index_exists",
        document_library.exists("work-items/index.md"),
        "Root work-item index `work-items/index.md` exists in the document library.",
    )
    if require_onedrive_artifacts:
        add(
            "documents.onedrive_urls",
            any(
                artifact.url is not None
                and artifact.url.startswith(("http://", "https://"))
                and artifact.relative_path.startswith("work-items/")
                for artifact in detail.artifacts
            ),
            "At least one work-item artifact has a recorded HTTP(S) URL.",
        )

    deployed_releases = [release for release in detail.releases if release.status == "deployed"]
    add("release.deployed", bool(deployed_releases), "At least one release is recorded as deployed.")
    add(
        "release.closed",
        any(release.closure_state == "closed" for release in detail.releases),
        "Release closure state is recorded as closed.",
    )
    add(
        "release.rollback_plan",
        any(release.rollback_plan and release.rollback_plan != "not-recorded" for release in detail.releases),
        "Release record includes rollback plan evidence.",
    )
    add(
        "release.smoke_evidence",
        any(release.smoke_evidence and release.smoke_evidence != "not-recorded" for release in detail.releases),
        "Release record includes smoke evidence.",
    )
    add(
        "release.deployment_result",
        any(release.deployment_result.strip() for release in detail.releases),
        "Release record includes deployment output.",
    )

    passed = all(check.passed for check in checks)
    return DogfoodAuditResult(work_item_id=work_item_id, passed=passed, checks=tuple(checks))


def _linked_backlog_is_closed(db: V3Database, *, work_item_id: str) -> bool:
    row = db.connection.execute(
        """
        SELECT status
        FROM backlog_items
        WHERE linked_work_item_id=?
        ORDER BY updated_at DESC, queue_item_id ASC
        LIMIT 1
        """,
        (work_item_id,),
    ).fetchone()
    return row is not None and row["status"] == "closed"
