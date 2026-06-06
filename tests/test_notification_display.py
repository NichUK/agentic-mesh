from __future__ import annotations

import json

from agentic_mesh import telemetry
from agentic_mesh.connectors import BotFrameworkTeamsConnectorAdapter
from agentic_mesh.connectors import render_notification_event_html
from agentic_mesh.models import ConnectorMessage
from agentic_mesh.notification_display import build_notification_display_facts
from agentic_mesh.notifications import DocumentLinkBuilder
from agentic_mesh.notifications import MESSAGE_TYPE_NOTIFICATION_EVENT
from agentic_mesh.notifications import StatusLinkBuilder


def test_display_facts_cover_v0_categories_with_labels_and_action_defaults() -> None:
    cases = [
        ("queue.captured", "queue_captured", "Request captured", False),
        ("queue.promoted", "queue_promoted", "Work started", False),
        ("work.created", "lifecycle_work_created", "Work item created", False),
        ("problem.blocked", "blocker_or_role_failure", "Action required: Blocked by role", True),
        ("problem.failed", "blocker_or_role_failure", "Action required: Role failed", True),
        ("problem.needs_runtime_recovery", "runtime_recovery_queued", "Operator action required: Runtime recovery queued", True),
        ("human_response.requested", "approval_requested", "Approval requested", True),
        ("human_response.completed", "approval_decision_recorded", "Decision recorded", False),
        ("work.completed", "work_completed", "Completed", False),
        ("release.ready", "publish_or_release_ready", "Release ready", False),
        ("audit.update", "consolidated_audit_update", "Audit update", False),
        ("notification.failed", "notification_delivery_problem", "Action required: Notification failed", True),
        ("future.kind", "unknown_status_update", "Status update", False),
    ]

    for event_kind, category, label, action_needed in cases:
        facts = build_notification_display_facts(
            {
                "event_kind": event_kind,
                "title": "Readable message",
                "summary": "A concise summary.",
                "work_item_id": "work-readable",
                "queue_item_id": "queue-readable",
                "lifecycle_state": "implementation",
                "occurred_at": "2026-06-05T12:00:00+00:00",
            }
        )

        assert facts["schema_version"] == "notification-display-v0"
        assert facts["display_category"] == category
        assert facts["display_label"] == label
        assert facts["action_needed"] is action_needed
        fact_labels = [fact["label"] for fact in facts["facts"]]
        assert fact_labels[:3] == ["Work item", "Queue item", "State"]


def test_display_facts_link_precedence_artifact_limit_and_forbidden_metadata() -> None:
    payload = {
        "event_kind": "work.completed",
        "title": "Completed",
        "summary": "Raw service_url https://graph.microsoft.com/v1.0 should not render",
        "work_item_id": "work-readable",
        "document_links": [
            DocumentLinkBuilder(base_url="https://mesh.example").artifact(
                "work-items/work-readable/100-implementation-log.md",
                label="Implementation log",
            ).to_dict(),
            DocumentLinkBuilder(base_url="https://mesh.example").artifact(
                "work-items/work-readable/110-quality-evidence.md",
                label="QA evidence",
            ).to_dict(),
            DocumentLinkBuilder(base_url="https://mesh.example").artifact(
                "work-items/work-readable/120-release.md",
                label="Release record",
            ).to_dict(),
            {"label": "Unsafe", "href": "https://graph.microsoft.com/raw", "available": True},
        ],
        "status_links": [
            {"label": "Raw", "href": "https://graph.microsoft.com/teams/raw", "available": True}
        ],
    }

    facts = build_notification_display_facts(payload, base_url="https://mesh.example")

    assert facts["summary"] == "[redacted]"
    assert facts["status_links"] == [
        {
            "schema_version": "notification-link-v0",
            "label": "Open work item status",
            "href": "https://mesh.example/work-items/work-readable",
            "available": True,
        }
    ]
    assert len(facts["artifact_links"]) == 2
    assert facts["artifact_overflow"]["hidden_count"] == 1
    assert "graph.microsoft.com" not in json.dumps(facts)


def test_document_link_builder_rejects_unsafe_artifact_paths() -> None:
    builder = DocumentLinkBuilder(base_url="https://mesh.example")

    for path in [
        "/tmp/evidence.md",
        "../evidence.md",
        "state/projects/secret.json",
        "C:\\tmp\\evidence.md",
        "\\\\server\\share\\evidence.md",
        "https://graph.microsoft.com/raw",
    ]:
        assert builder.artifact(path).available is False


def test_notification_html_uses_scan_first_facts_and_omits_routine_action_block() -> None:
    message = ConnectorMessage.create(
        channel="all-agents",
        message_type=MESSAGE_TYPE_NOTIFICATION_EVENT,
        source="agentic-mesh-dev.engineering.1",
        payload={
            "event_kind": "queue.captured",
            "title": "Human-readable Teams message formatting",
            "summary": "Your request was captured for triage.",
            "queue_item_id": "queue-abc",
            "source_anchor_summary": "Codex queued from sponsor feedback",
            "owner_role": "product-manager",
            "occurred_at": "2026-06-05T12:00:00+00:00",
            "status_links": [StatusLinkBuilder().queue(label="Open work queue").to_dict()],
        },
    )

    rendered = render_notification_event_html(message)

    assert "Request captured: Human-readable Teams message formatting" in rendered
    assert "<strong>Queue item:</strong> <code>queue-abc</code>" in rendered
    assert "<strong>Owner:</strong> product-manager" in rendered
    assert "Action owner" not in rendered
    assert "Agentic Mesh message" not in rendered
    assert "said:" not in rendered


def test_bot_framework_notification_text_reuses_safe_renderer() -> None:
    message = ConnectorMessage.create(
        channel="all-agents",
        message_type=MESSAGE_TYPE_NOTIFICATION_EVENT,
        source="agentic-mesh-dev.engineering.1",
        payload={
            "event_kind": "problem.needs_runtime_recovery",
            "title": "<script>timeout</script>",
            "summary": "Worker timeout prevented a trusted result.",
            "work_item_id": "work-readable",
            "runtime_component": "worker",
            "action_owner": "runtime/operator",
            "next_action": "Reconcile partial artifacts before retrying.",
            "retryable": True,
        },
    )

    rendered = BotFrameworkTeamsConnectorAdapter._render_text(message)

    assert "Operator action required: Runtime recovery queued" in rendered
    assert "&lt;script&gt;timeout&lt;/script&gt;" in rendered
    assert "Action owner" in rendered
    assert "human_response.submit" not in rendered


def test_notification_rendering_emits_safe_display_telemetry() -> None:
    sink = telemetry.TelemetryTestSink()
    telemetry.set_test_sink(sink)
    try:
        message = ConnectorMessage.create(
            channel="all-agents",
            message_type=MESSAGE_TYPE_NOTIFICATION_EVENT,
            source="agentic-mesh-dev.engineering.1",
            payload={
                "project_id": "agentic-mesh-dev",
                "event_kind": "work.completed",
                "title": "Done",
                "summary": "Completed safely.",
                "work_item_id": "work-readable",
                "queue_item_id": "queue-readable",
                "lifecycle_state": "implementation",
                "owner_role": "engineering",
                "service_url": "https://smba.trafficmanager.net/raw",
            },
            correlation_id="corr-readable",
        )

        render_notification_event_html(message)
    finally:
        telemetry.set_test_sink(None)

    span_names = [span.name for span in sink.spans]
    assert "notification.display_facts_built" in span_names
    assert "notification.message_rendered" in span_names
    rendered_span = [
        span for span in sink.spans if span.name == "notification.message_rendered"
    ][0]
    assert rendered_span.attributes["display_category"] == "work_completed"
    serialized = json.dumps([span.attributes for span in sink.spans])
    assert "service_url" not in serialized
    assert "smba.trafficmanager" not in serialized
