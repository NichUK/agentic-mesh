from dataclasses import replace
from pathlib import Path

from agentic_mesh.journal import EventJournal
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import Message
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.storage import FileMessageStore


def test_two_instances_do_not_claim_same_message(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    store = FileMessageStore(tmp_path, "agentic-mesh-dev", journal)
    store.enqueue(
        Message.create(
            role_id="engineering",
            message_type="sdlc.implementation",
            payload={
                "title": "Build slice",
                "work_item_id": "slice-build",
                "work_item_type": "slice",
                "lifecycle_state": "implementation",
            },
            source="test",
        )
    )

    first = store.claim_next("engineering", "agentic-mesh-dev.engineering.1")
    second = store.claim_next("engineering", "agentic-mesh-dev.engineering.2")

    assert first is not None
    assert first.claimed_by == "agentic-mesh-dev.engineering.1"
    assert second is None
    assert [event["event_type"] for event in journal.read_all()] == [
        "message_accepted",
        "work_claimed",
    ]


def test_duplicate_active_message_is_suppressed(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    store = FileMessageStore(tmp_path, "agentic-mesh-dev", journal)
    first = Message.create(
        role_id="engineering",
        message_type="sdlc.consult.implementation",
        payload={
            "title": "Build slice",
            "work_item_id": "slice-build",
            "work_item_type": "slice",
            "lifecycle_state": "implementation",
        },
        source="test",
    )
    duplicate = Message.create(
        role_id="engineering",
        message_type="sdlc.consult.implementation",
        payload={
            "title": "Build slice",
            "work_item_id": "slice-build",
            "work_item_type": "slice",
            "lifecycle_state": "implementation",
        },
        source="test",
    )

    accepted = store.enqueue(first)
    suppressed = store.enqueue(duplicate)

    assert suppressed.message_id == accepted.message_id
    assert store.pending_count("engineering") == 1
    events = journal.read_all()
    assert [event["event_type"] for event in events] == [
        "message_accepted",
        "message_duplicate_suppressed",
    ]
    assert events[-1]["message_id"] == duplicate.message_id
    assert events[-1]["duplicate_of_message_id"] == first.message_id


def test_distinct_active_messages_for_same_work_item_are_not_suppressed(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    store = FileMessageStore(tmp_path, "agentic-mesh-dev", journal)
    first = Message.create(
        role_id="engineering",
        message_type="sdlc.consult.implementation",
        payload={
            "title": "Fix defect A",
            "work_item_id": "slice-build",
            "work_item_type": "slice",
            "lifecycle_state": "implementation",
            "defect_id": "DEF-A",
            "required_change": "Fix A.",
        },
        source="test",
    )
    second = Message.create(
        role_id="engineering",
        message_type="sdlc.consult.implementation",
        payload={
            "title": "Fix defect B",
            "work_item_id": "slice-build",
            "work_item_type": "slice",
            "lifecycle_state": "implementation",
            "defect_id": "DEF-B",
            "required_change": "Fix B.",
        },
        source="test",
    )

    store.enqueue(first)
    accepted_second = store.enqueue(second)

    assert accepted_second.message_id == second.message_id
    assert store.pending_count("engineering") == 2
    assert [event["event_type"] for event in journal.read_all()] == [
        "message_accepted",
        "message_accepted",
    ]


def test_same_work_item_lifecycle_claims_are_serialized_across_instances(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    store = FileMessageStore(tmp_path, "agentic-mesh-dev", journal)
    first = Message.create(
        role_id="engineering",
        message_type="sdlc.consult.implementation",
        payload={
            "title": "Fix defect A",
            "work_item_id": "slice-build",
            "work_item_type": "slice",
            "lifecycle_state": "implementation",
            "defect_id": "DEF-A",
            "required_change": "Fix A.",
        },
        source="test",
    )
    second = Message.create(
        role_id="engineering",
        message_type="sdlc.consult.implementation",
        payload={
            "title": "Fix defect B",
            "work_item_id": "slice-build",
            "work_item_type": "slice",
            "lifecycle_state": "implementation",
            "defect_id": "DEF-B",
            "required_change": "Fix B.",
        },
        source="test",
    )
    store.enqueue(first)
    store.enqueue(second)

    claimed = store.claim_next("engineering", "agentic-mesh-dev.engineering.1")
    blocked_by_conflict = store.claim_next(
        "engineering",
        "agentic-mesh-dev.engineering.2",
    )

    assert claimed is not None
    assert blocked_by_conflict is None
    assert store.pending_count("engineering") == 1
    assert [event["event_type"] for event in journal.read_all()] == [
        "message_accepted",
        "message_accepted",
        "work_claimed",
        "work_claim_deferred",
    ]


def test_duplicate_claimed_message_is_suppressed(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    store = FileMessageStore(tmp_path, "agentic-mesh-dev", journal)
    first = Message.create(
        role_id="qa-engineer",
        message_type="sdlc.quality_planning",
        payload={
            "title": "Plan quality",
            "work_item_id": "slice-quality",
            "work_item_type": "slice",
            "lifecycle_state": "quality_planning",
        },
        source="test",
    )
    duplicate = Message.create(
        role_id="qa-engineer",
        message_type="sdlc.quality_planning",
        payload={
            "title": "Plan quality",
            "work_item_id": "slice-quality",
            "work_item_type": "slice",
            "lifecycle_state": "quality_planning",
        },
        source="test",
    )

    store.enqueue(first)
    claimed = store.claim_next("qa-engineer", "agentic-mesh-dev.qa-engineer.1")
    assert claimed is not None
    suppressed = store.enqueue(duplicate)

    assert suppressed.message_id == claimed.message_id
    assert suppressed.claimed_by == "agentic-mesh-dev.qa-engineer.1"
    assert store.pending_count("qa-engineer") == 0
    assert [event["event_type"] for event in journal.read_all()] == [
        "message_accepted",
        "work_claimed",
        "message_duplicate_suppressed",
    ]


def test_stale_claim_is_reclaimed_for_same_instance(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    store = FileMessageStore(tmp_path, "agentic-mesh-dev", journal)
    store.enqueue(
        Message.create(
            role_id="product-manager",
            message_type="sdlc.product_definition",
            payload={
                "title": "Define slice",
                "work_item_id": "slice-product",
                "work_item_type": "slice",
                "lifecycle_state": "product_definition",
            },
            source="test",
        )
    )

    claimed = store.claim_next("product-manager", "agentic-mesh-dev.product-manager.1")
    assert claimed is not None
    stale_claim = replace(claimed, claimed_at="2000-01-01T00:00:00+00:00")
    claimed_path = store._find_claimed_path(claimed)
    assert claimed_path is not None
    store._write_message(claimed_path, stale_claim)

    reclaimed = store.reclaim_stale_claims(
        "product-manager",
        "agentic-mesh-dev.product-manager.1",
        max_claim_age_seconds=1,
    )

    assert [message.message_id for message in reclaimed] == [claimed.message_id]
    assert reclaimed[0].claimed_by is None
    assert store.pending_count("product-manager") == 1
    next_claim = store.claim_next(
        "product-manager",
        "agentic-mesh-dev.product-manager.1",
    )
    assert next_claim is not None
    assert next_claim.message_id == claimed.message_id
    assert [event["event_type"] for event in journal.read_all()] == [
        "message_accepted",
        "work_claimed",
        "work_claim_reclaimed",
        "work_claimed",
    ]


def test_claim_before_process_start_is_reclaimed_for_same_instance(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    store = FileMessageStore(tmp_path, "agentic-mesh-dev", journal)
    store.enqueue(
        Message.create(
            role_id="enterprise-architect",
            message_type="sdlc.enterprise_alignment",
            payload={
                "title": "Align enterprise",
                "work_item_id": "slice-enterprise",
                "work_item_type": "slice",
                "lifecycle_state": "enterprise_alignment",
            },
            source="test",
        )
    )

    claimed = store.claim_next(
        "enterprise-architect",
        "agentic-mesh-dev.enterprise-architect.1",
    )
    assert claimed is not None
    old_claim = replace(claimed, claimed_at="2026-06-05T09:21:01+00:00")
    claimed_path = store._find_claimed_path(claimed)
    assert claimed_path is not None
    store._write_message(claimed_path, old_claim)

    reclaimed = store.reclaim_claims_before(
        "enterprise-architect",
        "agentic-mesh-dev.enterprise-architect.1",
        claimed_before="2026-06-05T09:30:00+00:00",
    )

    assert [message.message_id for message in reclaimed] == [claimed.message_id]
    assert store.pending_count("enterprise-architect") == 1
    events = journal.read_all()
    assert events[-1]["event_type"] == "work_claim_reclaimed"
    assert events[-1]["reason"] == "process_start"


def test_claim_after_process_start_is_not_reclaimed(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    store = FileMessageStore(tmp_path, "agentic-mesh-dev", journal)
    store.enqueue(
        Message.create(
            role_id="enterprise-architect",
            message_type="sdlc.enterprise_alignment",
            payload={
                "title": "Align enterprise",
                "work_item_id": "slice-enterprise",
                "work_item_type": "slice",
                "lifecycle_state": "enterprise_alignment",
            },
            source="test",
        )
    )

    claimed = store.claim_next(
        "enterprise-architect",
        "agentic-mesh-dev.enterprise-architect.1",
    )
    assert claimed is not None
    new_claim = replace(claimed, claimed_at="2026-06-05T09:31:00+00:00")
    claimed_path = store._find_claimed_path(claimed)
    assert claimed_path is not None
    store._write_message(claimed_path, new_claim)

    reclaimed = store.reclaim_claims_before(
        "enterprise-architect",
        "agentic-mesh-dev.enterprise-architect.1",
        claimed_before="2026-06-05T09:30:00+00:00",
    )

    assert reclaimed == []
    assert store.pending_count("enterprise-architect") == 0
    assert [event["event_type"] for event in journal.read_all()] == [
        "message_accepted",
        "work_claimed",
    ]


def test_connector_outbox_claims_channel_messages(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, "agentic-mesh-dev")
    outbox = FileConnectorOutbox(tmp_path, "agentic-mesh-dev", journal)
    outbox.enqueue(
        ConnectorMessage.create(
            channel="approvals",
            message_type="human_response.requested",
            payload={
                "work_item_id": "slice-release",
                "work_item_type": "slice",
                "lifecycle_state": "release_review",
                "gate_id": "release_decision_response",
            },
            source="agentic-mesh-dev.release-manager.1",
        )
    )

    claimed = outbox.claim_next("approvals", "local-teams-connector")
    second = outbox.claim_next("approvals", "another-connector")

    assert claimed is not None
    assert claimed.claimed_by == "local-teams-connector"
    assert claimed.payload["gate_id"] == "release_decision_response"
    assert second is None
    outbox.complete(claimed, "sent")
    assert [event["event_type"] for event in journal.read_all()] == [
        "connector_message_queued",
        "connector_message_claimed",
        "connector_message_completed",
    ]
