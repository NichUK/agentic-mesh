from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh.connectors import render_notification_event_html
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.models import NotificationPolicyConfig
from agentic_mesh.models import NotificationSurfaceConfig
from agentic_mesh.notifications import DocumentLinkBuilder
from agentic_mesh.notifications import FileNotificationAttemptStore
from agentic_mesh.notifications import FileSourceRouteStore
from agentic_mesh.notifications import MESSAGE_TYPE_NOTIFICATION_EVENT
from agentic_mesh.notifications import NotificationEvent
from agentic_mesh.notifications import NotificationPolicyEvaluator
from agentic_mesh.notifications import NotificationRouteResolver
from agentic_mesh.notifications import SourceRouteRecord
from agentic_mesh.notifications import StatusLinkBuilder
from agentic_mesh.notifications import activation_notification_event
from agentic_mesh.notifications import route_resolution_for_surface
from agentic_mesh.notifications import validate_logical_id


def test_notification_event_serializes_allowlisted_safe_fields_only() -> None:
    event = NotificationEvent.create(
        event_kind="problem.blocked",
        event_group="problem_status",
        visibility="notify",
        project_id="agentic-mesh-dev",
        action_needed=True,
        action_owner="engineering",
        next_action="<script>steal()</script>",
        work_item_id="work-hm",
        work_item_type="spike",
        lifecycle_state="implementation",
        queue_item_id="queue-hm",
        source_message_id="msg-hm",
        source_anchor_ref="source:hm",
        source_anchor_summary="Tenant https://graph.microsoft.com/raw",
        title="Blocked <b>title</b>",
        summary="Contains secret_ref and raw_payload marker",
        status_links=(StatusLinkBuilder(base_url="https://mesh.local").work_item("work-hm"),),
        document_links=(
            DocumentLinkBuilder(base_url="https://mesh.local").artifact(
                "work-items/work-hm/100-implementation-log.md",
                label="Implementation log",
            ),
        ),
        correlation_id="corr-hm",
    )

    payload = event.to_dict()

    assert payload["schema_version"] == "notification-event-v0"
    assert payload["source_anchor_ref"] == "source:hm"
    assert payload["source_anchor_summary"] == "[redacted]"
    assert payload["summary"] == "[redacted]"
    serialized = json.dumps(payload)
    assert "tenant_id" not in serialized
    assert "service_url" not in serialized
    assert "raw_payload" not in serialized
    assert "secret_ref" not in serialized
    assert "/work-items/work-hm" in serialized


def test_source_route_store_uses_contained_paths_and_hides_raw_by_default(
    tmp_path: Path,
) -> None:
    store = FileSourceRouteStore(tmp_path / "state", "agentic-mesh-dev")
    record = SourceRouteRecord(
        source_anchor_ref="source:abc123",
        connector_type="teams",
        connector_id="teams",
        route_label="Request thread",
        route_kind="teams_thread",
        raw_route={
            "teams_service_url": "https://smba.trafficmanager.net/emea/",
            "teams_conversation_id": "raw-conversation",
        },
        capabilities=("thread_reply", "message_update"),
        correlation_id="corr-route",
    )

    store.write(record)
    loaded = store.read("source:abc123")

    assert loaded is not None
    assert loaded.raw_route["teams_conversation_id"] == "raw-conversation"
    safe_payload = loaded.to_dict()
    assert "raw_route" not in safe_payload
    assert "teams_conversation_id" not in json.dumps(safe_payload)
    assert list((tmp_path / "state").glob("projects/agentic-mesh-dev/source-routes/*.json"))


@pytest.mark.parametrize(
    "unsafe_id",
    ["", "../x", "/tmp/x", "http://x", "route/one", "route\\one"],
)
def test_notification_store_rejects_unsafe_logical_ids(unsafe_id: str) -> None:
    with pytest.raises(ValueError):
        validate_logical_id(unsafe_id)


def test_attempt_store_deduplicates_and_dead_letters(tmp_path: Path) -> None:
    store = FileNotificationAttemptStore(tmp_path / "state", "agentic-mesh-dev")
    surface = NotificationSurfaceConfig(
        surface_id="status_fallback",
        connector="teams",
        route="all-agents",
        label="project status fallback",
    )
    event = NotificationEvent.create(
        event_kind="notification.failed",
        event_group="notification",
        visibility="notify",
        project_id="agentic-mesh-dev",
        work_item_id="work-hm",
        correlation_id="corr-hm",
        dedupe_key="dedupe-work-hm-notification",
    )
    resolution = route_resolution_for_surface(event=event, surface=surface)

    first = store.create_or_dedupe(event, resolution)
    second = store.create_or_dedupe(event, resolution)
    dead = store.dead_letter(first, "TimeoutError")

    assert first.status == "queued"
    assert second.status == "deduplicated"
    assert second.attempt_id == first.attempt_id
    assert dead.status == "dead_lettered"
    assert dead.redacted_error_class == "TimeoutError"
    assert list((tmp_path / "state").glob("projects/agentic-mesh-dev/notification_attempts/dead_letters/*.json"))


def test_source_first_route_resolution_falls_back_to_configured_surface(tmp_path: Path) -> None:
    policy = NotificationPolicyConfig(
        surfaces={
            "status_fallback": NotificationSurfaceConfig(
                surface_id="status_fallback",
                connector="teams",
                route="all-agents",
                label="project status fallback",
            )
        }
    )
    source_routes = FileSourceRouteStore(tmp_path / "state", "agentic-mesh-dev")
    source_routes.write(
        SourceRouteRecord(
            source_anchor_ref="source:route-ready",
            connector_type="teams",
            connector_id="teams",
            route_label="request thread",
            route_kind="source_thread",
            raw_route={"route": "intake"},
            capabilities=("notification_event",),
            correlation_id="corr-route",
        )
    )
    event = NotificationEvent.create(
        event_kind="queue.captured",
        event_group="work_queue",
        visibility="notify",
        project_id="agentic-mesh-dev",
        source_anchor_ref="source:route-ready",
        queue_item_id="queue-route",
        correlation_id="corr-route",
    )

    resolved = NotificationRouteResolver(
        policy=policy,
        source_route_store=source_routes,
    ).resolve(event)

    assert resolved.result == "resolved"
    assert resolved.selected_surface_key == "source_thread"
    assert resolved.dispatch_route == "intake"
    assert resolved.route_label == "request thread"

    missing_source_event = NotificationEvent.create(
        event_kind="queue.captured",
        event_group="work_queue",
        visibility="notify",
        project_id="agentic-mesh-dev",
        source_anchor_ref="source:missing",
        queue_item_id="queue-fallback",
        correlation_id="corr-route",
    )
    fallback = NotificationRouteResolver(
        policy=policy,
        source_route_store=source_routes,
    ).resolve(missing_source_event)

    assert fallback.result == "fallback_selected"
    assert fallback.selected_surface_key == "status_fallback"
    assert fallback.dispatch_route == "all-agents"
    assert fallback.fallback_reason == "source_route_missing"


def test_policy_defaults_keep_routine_routes_dashboard_only() -> None:
    policy = NotificationPolicyConfig(
        surfaces={
            "status_fallback": NotificationSurfaceConfig(
                surface_id="status_fallback",
                connector="teams",
                route="all-agents",
                label="project status fallback",
            )
        }
    )
    evaluator = NotificationPolicyEvaluator(policy)

    assert (
        evaluator.visibility_for(
            event_kind="lifecycle.handoff_requested",
            route_kind="configured_handoff",
        )
        == "dashboard_only"
    )
    assert evaluator.visibility_for(event_kind="problem.blocked") == "notify"


def test_notification_renderer_escapes_hostile_content_and_has_no_mutation_actions() -> None:
    message = ConnectorMessage.create(
        channel="all-agents",
        message_type=MESSAGE_TYPE_NOTIFICATION_EVENT,
        source="agentic-mesh-dev.engineering.1",
        payload={
            "event_kind": "problem.blocked",
            "action_needed": True,
            "title": "<img src=x onerror=alert(1)>",
            "summary": "{\"type\":\"Action.Submit\"}",
            "action_owner": "engineering",
            "next_action": "<script>retry()</script>",
            "occurred_at": "2026-06-05T12:00:00+00:00",
            "status_links": [
                {
                    "label": "Status",
                    "href": "/work-items/work-hm",
                    "available": True,
                }
            ],
        },
    )

    html = render_notification_event_html(message)

    assert "Blocked by role" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Action.Submit" in html
    assert "retry" in html
    assert "human_response.submit" not in html


def test_activation_notification_event_is_safe_and_uses_status_base_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTIC_MESH_AUTH_ADMIN_URL", "http://127.0.0.1:8100/auth/status")
    monkeypatch.delenv("AGENTIC_MESH_STATUS_BASE_URL", raising=False)
    summary = {
        "work_item_id": "work-activation",
        "lifecycle_state": "implementation",
        "target_labels": ["dogfood_compose"],
        "source_status": "source_ready",
        "activation_status": "blocked",
        "smoke_status": "failed",
        "failure_class": "source_changed_running_service_not_updated",
        "failure_class_label": "Stale runtime detected",
        "action_owner": "runtime/operator",
        "next_action": "Rebuild image and smoke /agents/current.json.",
        "notification_state": "failed",
        "latest_smoke": {"actual_summary": "404 returned"},
    }

    event = activation_notification_event(
        project_id="agentic-mesh-dev",
        activation_summary=summary,
        correlation_id="corr-activation",
    )
    payload = event.to_dict()

    assert payload["event_kind"] == "activation.stale_runtime"
    assert payload["action_needed"] is True
    assert payload["status_links"][0]["href"] == "/work-items/work-activation"
    serialized = json.dumps(payload)
    assert "127.0.0.1" not in serialized
    assert "service_url" not in serialized
    assert "tenant_id" not in serialized
    assert "secret_ref" not in serialized

    approved = activation_notification_event(
        project_id="agentic-mesh-dev",
        activation_summary=summary,
        status_link_builder=StatusLinkBuilder(base_url="https://status.example.com"),
    ).to_dict()
    assert approved["status_links"][0]["href"] == "https://status.example.com/work-items/work-activation"
