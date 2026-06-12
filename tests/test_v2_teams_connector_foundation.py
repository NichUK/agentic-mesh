from pathlib import Path

import pytest

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


def test_connector_config_requires_foundation_fields() -> None:
    with pytest.raises(ValueError, match="missing required keys"):
        ConnectorConfig.from_dict({"connector_id": "teams"})
    with pytest.raises(ValueError, match="unknown retention keys"):
        ConnectorConfig.from_dict(
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
                },
                "human_authorities": {"nicholas": ["sponsor", "operator"]},
                "retention": {"private_dm_days": 30, "mystery_days": 1},
                "team_wide_trigger": "@all-agents",
            }
        )


def test_local_teams_adapter_replays_foundation_events(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    dm = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-1",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "body": "Can you give me a status update?",
        }
    )
    assert dm.route_type == "role_direct_message"
    assert not dm.duplicate

    duplicate = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-1",
            "conversation_ref": "dm-nicholas-product",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "body": "Can you give me a status update?",
        }
    )
    assert duplicate.duplicate
    assert duplicate.receipt_id == dm.receipt_id

    channel_context = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-2",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "We should keep Teams as the human collaboration surface.",
        }
    )
    assert channel_context.route_type == "project_channel_context"

    mention = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-3",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Product Manager please shape this.",
            "mentioned_roles": ["product-manager"],
            "thread_ref": "thread-1",
        }
    )
    assert mention.route_type == "role_mention"

    unknown = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-4",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Missing please help.",
            "mentioned_roles": ["missing-role"],
        }
    )
    assert unknown.route_type == "unknown_role_mention"

    delivery_id = adapter.send_message(
        source_ref="safe-output-call-1",
        destination_ref="dm-nicholas-product",
        destination_type="dm",
        purpose="status.reply",
        body="Status is available.",
        role_id="product-manager",
        fail=True,
    )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["connectors"] == 1
    assert snapshot["counts"]["connector_participants"] == 4
    assert snapshot["counts"]["conversations"] == 2
    assert snapshot["counts"]["external_event_receipts"] == 4
    assert snapshot["counts"]["conversation_events"] == 4
    assert snapshot["counts"]["thread_bindings"] == 1
    assert snapshot["counts"]["delivery_records"] == 1
    assert snapshot["counts"]["connector_attention_items"] == 3
    assert snapshot["delivery_statuses"] == {"failed_transient": 1}
    assert snapshot["delivery_records"][0]["delivery_id"] == delivery_id
    assert {item["reason_class"] for item in snapshot["connector_attention_items"]} == {
        "delivery_failed_transient",
        "unrouteable_role_direct_message",
        "unknown_role_mention",
    }


def test_status_snapshot_exposes_empty_connector_foundation(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    snapshot = db.status_snapshot()
    assert snapshot["counts"]["connectors"] == 0
    assert snapshot["connectors"] == []
    assert snapshot["conversation_events"] == []
    assert snapshot["delivery_records"] == []
