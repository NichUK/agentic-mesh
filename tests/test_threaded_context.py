import json
from pathlib import Path

import pytest

from agentic_mesh.journal import EventJournal
from agentic_mesh.threaded_context import FileThreadedContextStore
from agentic_mesh.threaded_context import ThreadRouteRecord
from agentic_mesh.threaded_context import sanitize_text


def test_threaded_context_store_captures_known_parent_once(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    store = FileThreadedContextStore(tmp_path / "state", "agentic-mesh-dev", journal)
    route = ThreadRouteRecord.create(
        connector_type="teams",
        connector_id="teams-shared",
        source_scope="engineering",
        root_message_ref="activity-root",
        parent_work_item_id="work-parent-1",
        parent_work_item_type="slice",
        lifecycle_state="implementation",
        owner_role="engineering",
        source_anchor_ref="source:parent",
    )
    store.upsert_route(route)

    resolved = store.resolve_route(
        connector_type="teams",
        connector_id="teams-shared",
        source_scope="engineering",
        root_message_ref="activity-root",
    )
    first = store.capture(
        route=resolved.route,
        source_message_ref="reply-1",
        actor_label="Nich",
        text="Please include this context.",
        mentioned_roles=["product-manager"],
        source_anchor_ref="source:reply",
        correlation_id="corr-context",
    )
    duplicate = store.capture(
        route=resolved.route,
        source_message_ref="reply-1",
        actor_label="Nich",
        text="Please include this context.",
        mentioned_roles=["product-manager"],
        source_anchor_ref="source:reply",
        correlation_id="corr-context",
    )

    assert first.status == "bound"
    assert first.context.parent_work_item_id == "work-parent-1"
    assert first.context.mentioned_roles == ("product-manager",)
    assert duplicate.status == "duplicate"
    assert len(store.list_contexts("work-parent-1")) == 1
    rendered = json.dumps(first.context.to_safe_dict())
    assert "activity-root" not in rendered
    assert "reply-1" not in rendered
    assert "service_url" not in rendered

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "threaded_context.route_indexed" in event_types
    assert "threaded_context.captured" in event_types
    assert "threaded_context.duplicate_detected" in event_types


def test_threaded_context_deduplicates_cross_ingress_delivery_ids(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    store = FileThreadedContextStore(tmp_path / "state", "agentic-mesh-dev", journal)
    route = ThreadRouteRecord.create(
        connector_type="teams",
        connector_id="teams-shared",
        source_scope="engineering",
        root_message_ref="activity-root",
        parent_work_item_id="work-parent-1",
        parent_work_item_type="slice",
        lifecycle_state="implementation",
        owner_role="engineering",
        source_anchor_ref="source:parent",
    )
    store.upsert_route(route)

    first = store.capture(
        route=route,
        source_message_ref="bot-framework-reply-id",
        actor_label="Nich",
        text="Please include this context.",
        mentioned_roles=[],
        source_anchor_ref="source:bot-reply",
        correlation_id="corr-bot",
    )
    duplicate = store.capture(
        route=route,
        source_message_ref="graph-reply-id-for-same-human-reply",
        actor_label="Nich",
        text="Please include this context.",
        mentioned_roles=[],
        source_anchor_ref="source:graph-reply",
        correlation_id="corr-graph",
    )

    assert first.status == "bound"
    assert duplicate.status == "duplicate"
    assert len(store.list_contexts("work-parent-1")) == 1
    assert duplicate.context is not None
    assert duplicate.context.context_id == first.context.context_id
    event_types = [event["event_type"] for event in journal.read_all()]
    assert event_types.count("threaded_context.captured") == 1
    assert event_types.count("threaded_context.duplicate_detected") == 1


def test_threaded_context_conflicting_parent_route_candidates_fail_closed(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    store = FileThreadedContextStore(tmp_path / "state", "agentic-mesh-dev", journal)
    active_route = ThreadRouteRecord.create(
        connector_type="teams",
        connector_id="teams-bot-listener",
        source_scope="engineering",
        root_message_ref="activity-root",
        parent_work_item_id="work-parent-active",
        parent_work_item_type="slice",
        lifecycle_state="implementation",
        owner_role="engineering",
        source_anchor_ref="source:active-parent",
    )
    shared_route = ThreadRouteRecord.create(
        connector_type="teams",
        connector_id="teams-shared",
        source_scope="engineering",
        root_message_ref="activity-root",
        parent_work_item_id="work-parent-shared",
        parent_work_item_type="slice",
        lifecycle_state="implementation",
        owner_role="engineering",
        source_anchor_ref="source:shared-parent",
    )
    store.upsert_route(active_route)
    store.upsert_route(shared_route)

    resolved = store.resolve_route(
        connector_type="teams",
        connector_id="teams-bot-listener",
        source_scope="engineering",
        root_message_ref="activity-root",
        candidate_connector_ids=["teams-bot-listener", "teams-shared"],
    )

    assert resolved.status == "conflict"
    assert resolved.reason == "conflicting_parent_candidates"
    assert resolved.route is None
    assert resolved.conflict_count == 2
    assert store.list_contexts("work-parent-active") == []
    assert store.list_contexts("work-parent-shared") == []
    conflict_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "threaded_context.route_conflict"
    ]
    assert len(conflict_events) == 1
    assert conflict_events[0]["candidate_count"] == 2
    assert conflict_events[0]["conflict_count"] == 2
    rendered = json.dumps(conflict_events[0])
    assert "work-parent-active" not in rendered
    assert "work-parent-shared" not in rendered


def test_terminal_parent_threaded_context_requires_triage(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    store = FileThreadedContextStore(tmp_path / "state", "agentic-mesh-dev", journal)
    route = ThreadRouteRecord.create(
        connector_type="teams",
        connector_id="teams-shared",
        source_scope="engineering",
        root_message_ref="activity-terminal-parent",
        parent_work_item_id="work-parent-terminal",
        parent_work_item_type="slice",
        lifecycle_state="completed",
        owner_role="engineering",
        source_anchor_ref="source:terminal-parent",
    )
    store.upsert_route(route)

    result = store.capture(
        route=route,
        source_message_ref="reply-terminal",
        actor_label="Nich",
        text="Please consider this note after closure.",
        mentioned_roles=[],
        source_anchor_ref="source:terminal-reply",
        correlation_id="corr-terminal-context",
    )

    assert result.status == "bound"
    assert result.context is not None
    assert result.context.action_state == "needs_triage"
    assert result.context.context_kind == "parent_context"
    assert store.summary_counts()["needs_triage"] == 1
    captured = [
        event
        for event in journal.read_all()
        if event["event_type"] == "threaded_context.captured"
    ][0]
    assert captured["action_state"] == "needs_triage"
    assert captured["lifecycle_state"] == "completed"


def test_threaded_context_rejects_unsafe_ids_and_redacts_text(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    store = FileThreadedContextStore(tmp_path / "state", "agentic-mesh-dev", journal)

    with pytest.raises(ValueError):
        ThreadRouteRecord.create(
            connector_type="teams",
            connector_id="teams",
            source_scope="engineering",
            root_message_ref="activity-root",
            parent_work_item_id="../work-parent",
            parent_work_item_type="slice",
            lifecycle_state="implementation",
            owner_role="engineering",
        )

    assert "[redacted]" in sanitize_text("see https://graph.microsoft.com/raw")
    with pytest.raises(PermissionError):
        store.support_access(
            actor="operator",
            reason=None,
            authority="incident",
            target_id="work-parent-1",
            correlation_id="corr-support",
        )


def test_support_access_purge_rebuild_and_recovery_are_audited(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "state", "agentic-mesh-dev")
    store = FileThreadedContextStore(tmp_path / "state", "agentic-mesh-dev", journal)

    access = store.support_access(
        actor="operator",
        reason="incident review",
        authority="support-ticket",
        target_id="work-parent-1",
        correlation_id="corr-support",
    )
    purge = store.purge_expired_raw(
        actor="operator",
        reason="retention",
        older_than="2026-06-01T00:00:00+00:00",
        correlation_id="corr-purge",
        dry_run=True,
    )
    rebuild = store.rebuild_indexes(dry_run=True, correlation_id="corr-rebuild")
    recovery = store.recover_known_relationship(
        parent_work_item_id="work-9fcf1d4fcf994193ab8acb022c7739ea",
        stray_work_item_id="work-398923b9b9dc43d4a5d7ac2f1a6797a4",
        verified=True,
        actor="engineering",
        reason="verified parent thread relationship",
        correlation_id="corr-recovery",
    )

    assert access["schema_version"] == "threaded-context-support-access-v0"
    assert purge["dry_run"] is True
    assert rebuild["conflict_count"] == 0
    assert recovery["status"] == "linked"
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "threaded_context.support_access" in event_types
    assert "threaded_context.purge" in event_types
    assert "threaded_context.index_rebuild" in event_types
    assert "threaded_context.recovery.linked" in event_types
