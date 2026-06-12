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
                    "alias": "pm",
                    "mention_handle": "@AM-Product Manager",
                    "identity_model": "separate_bot",
                    "enabled": True,
                },
                "engineering": {
                    "external_ref": "bot-gateway",
                    "display_name": "AM-Engineering",
                    "alias": "engineering",
                    "mention_handle": "@AM-Engineering",
                    "identity_model": "shared_gateway",
                    "enabled": True,
                },
                "release-manager": {
                    "external_ref": "bot-release-manager",
                    "display_name": "AM-Release Manager",
                    "alias": "release",
                    "mention_handle": "@AM-Release Manager",
                    "identity_model": "hybrid",
                    "enabled": False,
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


def test_role_identity_config_supports_distinct_visible_metadata(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    snapshot = db.status_snapshot()
    role_participants = {
        participant["role_id"]: participant
        for participant in snapshot["connector_participants"]
        if participant["participant_type"] == "role"
    }
    assert role_participants["product-manager"]["display_name"] == "AM-Product Manager"
    assert role_participants["product-manager"]["external_ref"] == "bot-product-manager"
    assert role_participants["product-manager"]["metadata"]["alias"] == "pm"
    assert role_participants["product-manager"]["metadata"]["mention_handle"] == "@AM-Product Manager"
    assert role_participants["engineering"]["metadata"]["identity_model"] == "shared_gateway"
    assert role_participants["release-manager"]["metadata"]["enabled"] is False


def test_mentions_resolve_from_configured_aliases_not_display_name_authority(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    alias = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-alias-mention",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "pm please shape this.",
            "mentioned_role_refs": ["pm"],
        }
    )
    display_name_only = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-display-name-text",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "AM-Engineering is mentioned as plain text, not a configured Teams mention.",
        }
    )

    snapshot = db.status_snapshot()
    assert alias.route_type == "role_mention"
    assert alias.mentioned_roles == ("product-manager",)
    assert display_name_only.route_type == "project_channel_context"
    assignments = snapshot["role_assignments"]
    assert len(assignments) == 1
    assert assignments[0]["role_id"] == "product-manager"


def test_disabled_role_identity_blocks_routing_and_delivery_identity_is_configured(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    disabled = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-disabled-role",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@AM-Release Manager please release this.",
            "mentioned_role_refs": ["@AM-Release Manager"],
        }
    )
    delivery_id = adapter.send_message(
        source_ref="safe-output-call-identity",
        destination_ref="dm-nicholas-product",
        destination_type="dm",
        purpose="status.reply",
        body="Engineering reply.",
        role_id="engineering",
    )

    snapshot = db.status_snapshot()
    assert disabled.route_type == "disabled_role_identity"
    assert snapshot["counts"]["role_assignments"] == 0
    assert snapshot["connector_attention_items"][0]["reason_class"] == "disabled_role_identity"
    delivery = snapshot["delivery_records"][0]
    assert delivery["delivery_id"] == delivery_id
    assert delivery["role_id"] == "engineering"
    assert delivery["payload"]["role_identity"]["display_name"] == "AM-Engineering"
    assert delivery["payload"]["role_identity"]["external_ref"] == "bot-gateway"
    assert delivery["payload"]["role_identity"]["identity_model"] == "shared_gateway"


def test_disabled_and_display_name_dm_targets_do_not_fallback_to_enabled_role(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    disabled_target = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-disabled-dm-target",
            "conversation_ref": "dm-nicholas-release",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_role_id": "release-manager",
            "target_ref": "bot-release-manager",
            "body": "Can you release this?",
        }
    )
    display_name_target = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-display-name-dm-target",
            "conversation_ref": "dm-nicholas-unknown",
            "sender_ref": "nicholas",
            "source_type": "dm",
            "target_ref": "AM-Product Manager",
            "body": "This text target is a display name, not a configured external identity.",
        }
    )

    snapshot = db.status_snapshot()
    assert disabled_target.route_type == "role_direct_message"
    assert display_name_target.route_type == "role_direct_message"
    assert snapshot["counts"]["role_assignments"] == 0
    assert snapshot["counts"]["connector_attention_items"] == 2
    assert {
        item["reason_class"] for item in snapshot["connector_attention_items"]
    } == {"unrouteable_role_direct_message"}


def test_disabled_role_identity_cannot_send_outbound_delivery(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    with pytest.raises(ValueError, match="disabled or not configured"):
        adapter.send_message(
            source_ref="safe-output-call-disabled",
            destination_ref="dm-nicholas-release",
            destination_type="dm",
            purpose="status.reply",
            body="Release Manager reply.",
            role_id="release-manager",
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["delivery_records"] == 0
    assert snapshot["counts"]["delivery_attempts"] == 0
    assert snapshot["connector_attention_items"][0]["reason_class"] == "disabled_role_identity_delivery"


def test_role_identity_config_rejects_bad_models_and_duplicate_external_refs() -> None:
    raw = _config().__dict__
    with pytest.raises(ValueError, match="unknown role identity model"):
        ConnectorConfig.from_dict(
            {
                **raw,
                "role_identities": {
                    "product-manager": {
                        "external_ref": "bot-product-manager",
                        "display_name": "AM-Product Manager",
                        "alias": "pm",
                        "mention_handle": "@AM-Product Manager",
                        "identity_model": "made_up",
                        "enabled": True,
                    }
                },
            }
        )
    with pytest.raises(ValueError, match="external_ref values must be unique"):
        ConnectorConfig.from_dict(
            {
                **raw,
                "role_identities": {
                    "product-manager": {
                        "external_ref": "same-bot",
                        "display_name": "AM-Product Manager",
                        "alias": "pm",
                        "mention_handle": "@AM-Product Manager",
                        "identity_model": "separate_bot",
                        "enabled": True,
                    },
                    "engineering": {
                        "external_ref": "same-bot",
                        "display_name": "AM-Engineering",
                        "alias": "engineering",
                        "mention_handle": "@AM-Engineering",
                        "identity_model": "shared_gateway",
                        "enabled": True,
                    },
                },
            }
        )
