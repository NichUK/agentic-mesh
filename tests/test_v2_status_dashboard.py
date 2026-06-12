from pathlib import Path

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.context import ContextRetentionService
from agentic_mesh_v2.context import ContextSummaryRequest
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.server import V2StatusHandler


def _config() -> ConnectorConfig:
    return ConnectorConfig.from_dict(
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
                },
                "engineering": {
                    "external_ref": "bot-engineering",
                    "display_name": "AM-Engineering",
                    "alias": "engineering",
                    "mention_handle": "@AM-Engineering",
                    "identity_model": "shared_gateway",
                    "enabled": True,
                },
            },
            "channel_bindings": [
                {
                    "channel_ref": "channel-feature-dashboard",
                    "scope_type": "feature",
                    "display_name": "Feature Dashboard",
                    "visibility": "project",
                    "work_scope": "work-dashboard",
                },
                {
                    "channel_ref": "channel-private-incident",
                    "scope_type": "incident",
                    "display_name": "Private Incident",
                    "visibility": "private",
                    "work_scope": "incident-1",
                    "private": True,
                }
            ],
            "human_authorities": {
                "nicholas": ["sponsor", "release_approver"],
            },
            "retention": {
                "private_dm_days": 30,
                "project_channel_days": 90,
                "focus_channel_days": 60,
                "compacted_summary_days": 365,
                "delivery_record_days": 90,
                "idempotency_receipt_days": 30,
            },
            "permission_validation": {
                "permissions": [
                    {
                        "permission": "ChannelMessage.Send",
                        "phase": "runtime",
                        "consent_type": "bot",
                        "status": "granted",
                        "required": True,
                    }
                ]
            },
            "team_wide_trigger": "@all-agents",
        }
    )


def _rich_snapshot(tmp_path: Path) -> dict[str, object]:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    config = _config()
    adapter = LocalTeamsTestAdapter(db, config)
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)
    project = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-project-context",
            "conversation_ref": "channel-feature-dashboard",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@all-agents dashboard visibility needs specialist review.",
            "thread_ref": "thread-dashboard",
        }
    )
    duplicate = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-project-context",
            "conversation_ref": "channel-feature-dashboard",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@all-agents dashboard visibility needs specialist review.",
            "thread_ref": "thread-dashboard",
        }
    )
    assert duplicate.duplicate is True
    event = db.status_snapshot()["conversation_events"][0]
    db.create_run(
        run_id="run-relevance-dashboard",
        role_id="engineering",
        role_instance_id="engineering-1",
        work_item_id=None,
    )
    service.record(
        run_id="run-relevance-dashboard",
        call=SafeOutputCall(
            role_id="engineering",
            tool_name="relevance.record",
            payload={
                "conversation_event_id": event["conversation_event_id"],
                "score": 0.88,
                "threshold": 0.6,
                "decision": "material",
                "reason": "Engineering owns the dashboard implementation.",
            },
        ),
    )
    private = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-private-status",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "product-manager",
            "target_ref": "bot-product-manager",
            "body": "PRIVATE_DASHBOARD_SENTINEL please reply privately.",
        }
    )
    private_event = [
        item
        for item in db.list_conversation_events()
        if item["conversation_id"] == private.conversation_id
    ][0]
    db.create_run(
        run_id="run-private-dashboard",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )
    service.record(
        run_id="run-private-dashboard",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="human_response.request",
            payload={
                "title": "PRIVATE_DASHBOARD_SENTINEL direction",
                "question": "Should PRIVATE_DASHBOARD_SENTINEL stay private?",
                "response_contract_id": "dashboard-question-v1",
                "required_authority": "sponsor",
                "conversation_id": private.conversation_id,
                "destination_ref": "dm-nicholas-product",
                "destination_type": "dm",
            },
            terminal=True,
        ),
    )
    db.create_run(
        run_id="run-proposal-dashboard",
        role_id="product-manager",
        role_instance_id="product-manager-1",
        work_item_id=None,
    )
    service.record(
        run_id="run-proposal-dashboard",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="queue.propose_item",
            payload={
                "title": "Improve dashboard observability",
                "summary": "Expose connector state and permission health in the v2 dashboard.",
                "source_ref": project.receipt_id,
                "source_conversation_id": project.conversation_id,
                "source_receipt_id": project.receipt_id,
                "source_conversation_event_id": event["conversation_event_id"],
                "rationale": "Sponsors need to see connector progress and blockers quickly.",
                "urgency": "normal",
                "suggested_owner": "engineering",
                "work_type": "slice",
                "classification": "feature",
                "initiated_by": "product-manager",
            },
            terminal=True,
        ),
    )
    ContextRetentionService(db, config).compact_events(
        ContextSummaryRequest(
            summary_id="summary-private-dashboard",
            source_event_ids=(private_event["conversation_event_id"],),
            summary="PRIVATE_DASHBOARD_SENTINEL compacted private context.",
            classification="note",
            visibility_scope="private",
            created_by_role="product-manager",
        )
    )
    try:
        return db.status_snapshot()
    finally:
        db.close()


def _render(snapshot: dict[str, object], *, project_file: Path | None = None) -> str:
    handler = object.__new__(V2StatusHandler)
    handler._snapshot = lambda: snapshot  # type: ignore[method-assign]
    handler.db_path = Path(snapshot["database"])
    handler.project_file = project_file
    return handler._render_status()


def test_status_json_contract_includes_connector_dashboard_sections(tmp_path: Path) -> None:
    snapshot = _rich_snapshot(tmp_path)

    assert snapshot["counts"]["connector_permission_checks"] > 0
    assert snapshot["counts"]["connector_participants"] >= 2
    assert snapshot["counts"]["conversation_events"] >= 2
    assert snapshot["counts"]["relevance_checks"] == 1
    assert snapshot["counts"]["work_proposals"] == 1
    assert snapshot["counts"]["human_response_requests"] == 1
    assert snapshot["counts"]["context_summaries"] == 1
    assert "connector_permission_checks" in snapshot
    assert "context_summaries" in snapshot
    assert "work_proposals" in snapshot
    metrics = snapshot["connector_metrics"]
    assert metrics["inbound_events"] >= 2
    assert metrics["duplicates_suppressed"] == 1
    assert metrics["active_conversations"] >= 2
    assert metrics["delivery_failures"] == 0
    assert metrics["relevance_decisions"] == {"material": 1}
    assert metrics["compaction_count"] == 1


def test_status_html_smoke_shows_connector_sections_and_redacts_private_text(tmp_path: Path) -> None:
    html = _render(_rich_snapshot(tmp_path))

    for heading in (
        "Role Identities",
        "Channel Bindings",
        "Conversation Events",
        "Permission Checks",
        "Relevance Checks",
        "Work Proposals",
        "Human Responses",
        "Context Summaries",
        "Connector Metrics",
        "Duplicates suppressed",
    ):
        assert heading in html
    assert "AM-Product Manager" in html
    assert "Feature Dashboard" in html
    assert "ChannelMessage.Send" in html
    assert "Improve dashboard observability" in html
    assert "PRIVATE_DASHBOARD_SENTINEL" not in html
    assert "[redacted private conversation]" in html


def test_status_html_shows_project_supervisor_commands_when_project_file_configured(tmp_path: Path) -> None:
    snapshot = _rich_snapshot(tmp_path)
    project_file = tmp_path / "Project Root" / "agentic-mesh" / "project.yaml"

    rendered = _render(snapshot, project_file=project_file)

    assert "Supervisor Commands" in rendered
    assert "Project file:" in rendered
    assert str(project_file) in rendered
    assert f'--db &quot;{snapshot["database"]}&quot;' in rendered
    assert f'--project-file &quot;{project_file}&quot;' in rendered
    assert "run-project-supervisor-tick" in rendered
    assert "run-project-supervisor-loop" in rendered
    assert "--cycles 10 --poll-seconds 5" in rendered
    assert "--execute" in rendered
    assert "dashboard remains read-only" in rendered


def test_status_html_without_project_file_does_not_fake_supervisor_commands(tmp_path: Path) -> None:
    rendered = _render(_rich_snapshot(tmp_path))

    assert "Supervisor Commands" in rendered
    assert "Start the status server with --project-file" in rendered
    assert "run-project-supervisor-loop" not in rendered
    assert "Project file:" not in rendered


def test_bound_private_channel_is_redacted_in_status_json_and_html(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-private-channel-dashboard",
            "conversation_ref": "channel-private-incident",
            "sender_ref": "nicholas",
            "source_type": "private_channel",
            "body": "PRIVATE_CHANNEL_DASHBOARD_SENTINEL incident details",
        }
    )

    try:
        snapshot = db.status_snapshot()
    finally:
        db.close()
    html = _render(snapshot)
    event = snapshot["conversation_events"][0]
    assert event["visibility_scope"] == "private"
    assert event["payload"]["channel_scope"]["visibility"] == "private"
    assert event["body_preview"] == "[redacted private conversation]"
    assert "PRIVATE_CHANNEL_DASHBOARD_SENTINEL" not in str(snapshot)
    assert "PRIVATE_CHANNEL_DASHBOARD_SENTINEL" not in html
    assert "[redacted private conversation]" in html
