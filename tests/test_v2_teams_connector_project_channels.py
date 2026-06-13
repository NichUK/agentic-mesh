from pathlib import Path

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database


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
                    "identity_model": "separate_bot",
                    "enabled": True,
                },
                "qa-engineer": {
                    "external_ref": "bot-qa",
                    "display_name": "AM-QA Engineer",
                    "alias": "qa-engineer",
                    "mention_handle": "@AM-QA Engineer",
                    "identity_model": "separate_bot",
                    "enabled": True,
                },
            },
            "human_authorities": {
                "nicholas": ["sponsor", "operator"],
            },
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


def test_project_channel_context_does_not_wake_roles_by_default(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-channel-1",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "This architecture decision should be preserved.",
        }
    )
    assert replayed.route_type == "project_channel_context"

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["conversation_events"] == 1
    assert snapshot["counts"]["role_assignments"] == 0
    assert snapshot["conversation_events"][0]["visibility_scope"] == "project"
    assert snapshot["conversation_events"][0]["body_preview"] == "This architecture decision should be preserved."


def test_configured_role_mentions_create_focused_assignments(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-channel-2",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Product Manager @AM-QA please look at this.",
            "mentioned_roles": ["product-manager", "qa-engineer"],
            "thread_ref": "thread-channel-2",
        }
    )
    assert replayed.route_type == "role_mention"

    duplicate = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-channel-2",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Product Manager @AM-QA please look at this.",
            "mentioned_roles": ["product-manager", "qa-engineer"],
            "thread_ref": "thread-channel-2",
        }
    )
    assert duplicate.duplicate

    snapshot = db.status_snapshot()
    assignments = snapshot["role_assignments"]
    assert snapshot["counts"]["role_assignments"] == 2
    assert assignments[0]["payload"]["destination_ref"] == "channel-project"
    assert assignments[0]["payload"]["destination_type"] == "channel"
    assert assignments[0]["payload"]["context"] == [
        "Teams channel message from nicholas: @AM-Product Manager @AM-QA please look at this."
    ]
    assert {assignment["role_id"] for assignment in assignments} == {"product-manager", "qa-engineer"}
    assert {assignment["assignment_type"] for assignment in assignments} == {"channel_role_mention"}
    assert {assignment["visibility_scope"] for assignment in assignments} == {"project"}
    assert snapshot["counts"]["thread_bindings"] == 1


def test_unknown_role_mention_does_not_create_assignment(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-channel-3",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Missing please look at this.",
            "mentioned_roles": ["missing-role"],
        }
    )
    assert replayed.route_type == "unknown_role_mention"

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["role_assignments"] == 0
    assert snapshot["counts"]["connector_attention_items"] == 1
    assert snapshot["connector_attention_items"][0]["reason_class"] == "unknown_role_mention"
