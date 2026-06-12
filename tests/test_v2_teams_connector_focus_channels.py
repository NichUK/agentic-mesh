from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database


def _raw_config() -> dict[str, object]:
    return {
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
                "qa-engineer": {
                    "external_ref": "bot-qa",
                    "display_name": "AM-QA Engineer",
                    "alias": "qa-engineer",
                    "mention_handle": "@AM-QA Engineer",
                    "identity_model": "separate_bot",
                    "enabled": True,
                },
            },
            "channel_bindings": [
                {
                    "channel_ref": "channel-feature-dashboard",
                    "scope_type": "feature",
                    "display_name": "Feature - Status Dashboard",
                    "visibility": "project",
                    "work_scope": "work-dashboard",
                    "private": False,
                },
                {
                    "channel_ref": "channel-private-incident",
                    "scope_type": "incident",
                    "display_name": "Incident - Private Runtime Recovery",
                    "visibility": "private",
                    "work_scope": "incident-runtime-recovery",
                    "private": True,
                },
            ],
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


def _config() -> ConnectorConfig:
    return ConnectorConfig.from_dict(_raw_config())


def test_focus_channel_context_is_captured_with_scope_metadata(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-feature-context",
            "conversation_ref": "channel-feature-dashboard",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "Compact rows matter as much as the timestamp heading.",
        }
    )

    snapshot = db.status_snapshot()
    assert replayed.route_type == "project_channel_context"
    event = snapshot["conversation_events"][0]
    assert event["payload"]["channel_scope"]["scope_type"] == "feature"
    assert event["payload"]["channel_scope"]["work_scope"] == "work-dashboard"
    assert event["payload"]["channel_scope"]["display_name"] == "Feature - Status Dashboard"
    assert snapshot["connectors"][0]["health"]["channel_bindings"][0]["channel_ref"] == "channel-feature-dashboard"


def test_focus_channel_role_mention_and_thread_follow_default_routing_rules(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-feature-mention",
            "conversation_ref": "channel-feature-dashboard",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-QA Engineer please narrow checks to dashboard scan density.",
            "mentioned_role_refs": ["@AM-QA Engineer"],
            "thread_ref": "thread-feature-dashboard-qa",
        }
    )

    snapshot = db.status_snapshot()
    assert replayed.route_type == "role_mention"
    assert snapshot["counts"]["role_assignments"] == 1
    assignment = snapshot["role_assignments"][0]
    assert assignment["role_id"] == "qa-engineer"
    assert assignment["payload"]["channel_scope"]["scope_type"] == "feature"
    assert assignment["payload"]["channel_scope"]["work_scope"] == "work-dashboard"
    assert snapshot["thread_bindings"][0]["external_thread_ref"] == "thread-feature-dashboard-qa"


def test_private_focus_channel_requires_explicit_binding(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    bound = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-private-bound",
            "conversation_ref": "channel-private-incident",
            "sender_ref": "nicholas",
            "source_type": "private_channel",
            "body": "@AM-Product Manager please shape recovery comms.",
            "mentioned_role_refs": ["@AM-Product Manager"],
        }
    )
    unbound = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-private-unbound",
            "conversation_ref": "channel-private-unbound",
            "sender_ref": "nicholas",
            "source_type": "private_channel",
            "body": "@AM-QA Engineer please inspect this private area.",
            "mentioned_role_refs": ["@AM-QA Engineer"],
        }
    )

    snapshot = db.status_snapshot()
    assert bound.route_type == "role_mention"
    assert unbound.route_type == "unbound_private_channel"
    assert snapshot["counts"]["role_assignments"] == 1
    assert snapshot["role_assignments"][0]["payload"]["channel_scope"]["visibility"] == "private"
    assert snapshot["role_assignments"][0]["payload"]["channel_scope"]["private"] is True
    assert snapshot["counts"]["connector_attention_items"] == 1
    assert snapshot["connector_attention_items"][0]["reason_class"] == "unbound_private_channel"


def test_channel_binding_config_rejects_duplicate_default_and_bad_scope() -> None:
    raw = _raw_config()
    with pytest.raises(ValueError, match="default project channel"):
        ConnectorConfig.from_dict(
            {
                **raw,
                "channel_bindings": [
                    {
                        "channel_ref": "channel-project",
                        "scope_type": "feature",
                        "display_name": "Duplicate",
                        "visibility": "project",
                    }
                ],
            }
        )
    with pytest.raises(ValueError, match="unknown channel binding scope_type"):
        ConnectorConfig.from_dict(
            {
                **raw,
                "channel_bindings": [
                    {
                        "channel_ref": "channel-weird",
                        "scope_type": "weird",
                        "display_name": "Weird",
                        "visibility": "project",
                    }
                ],
            }
        )
