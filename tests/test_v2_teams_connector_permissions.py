from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.permissions import PermissionValidationFailure
from agentic_mesh_v2.safe_outputs import SafeOutputCall


def _config(**overrides: object) -> ConnectorConfig:
    raw = {
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
            "release-manager": {
                "external_ref": "bot-release-manager",
                "display_name": "AM-Release Manager",
                "alias": "release-manager",
                "mention_handle": "@AM-Release Manager",
                "identity_model": "separate_bot",
                "enabled": True,
            },
        },
        "authority_groups": {
            "sponsors": ["sponsor"],
            "release-board": ["release_approver"],
        },
        "people": [
            {
                "person_id": "person-nicholas",
                "display_name": "Nicholas Overend",
                "external_refs": ["aad-nicholas"],
                "groups": ["sponsors", "release-board"],
            },
            {
                "person_id": "person-observer",
                "display_name": "Observer Human",
                "external_refs": ["aad-observer"],
                "groups": [],
            },
        ],
        "retention": {
            "private_dm_days": 30,
            "project_channel_days": 90,
            "compacted_summary_days": 365,
            "delivery_record_days": 90,
            "idempotency_receipt_days": 30,
        },
        "permission_validation": {
            "permissions": [
                {
                    "permission": "TeamsAppInstallation.ReadForTeam",
                    "phase": "setup",
                    "consent_type": "resource_specific",
                    "status": "granted",
                    "required": True,
                },
                {
                    "permission": "ChannelMessage.Send",
                    "phase": "runtime",
                    "consent_type": "bot",
                    "status": "granted",
                    "required": True,
                },
                {
                    "permission": "ChannelMessage.Read.All",
                    "phase": "runtime",
                    "consent_type": "application",
                    "status": "granted",
                    "required": False,
                    "approval_ref": "security-approval-teams-broad-read",
                    "rationale": "Tenant smoke only; not required by local adapter.",
                },
            ]
        },
        "team_wide_trigger": "@all-agents",
    }
    raw.update(overrides)
    return ConnectorConfig.from_dict(raw)


def _db(tmp_path: Path) -> V2Database:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    return db


def _db_with_work_item(tmp_path: Path, config: ConnectorConfig | None = None) -> tuple[V2Database, LocalTeamsTestAdapter, str]:
    db = _db(tmp_path)
    db.create_queue_item(
        queue_item_id="queue-permission-card",
        title="Permission card slice",
        summary="Exercise permission and authority hardening.",
        owner_role="product-manager",
    )
    db.mark_queue_ready("queue-permission-card", actor_role="product-manager", reason="Ready for release test.")
    db.promote_queue_item(
        queue_item_id="queue-permission-card",
        work_item_id="work-permission-card",
        owner_role="product-manager",
    )
    adapter = LocalTeamsTestAdapter(db, config or _config())
    adapter.install()
    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-release-thread",
            "conversation_ref": "channel-project",
            "sender_ref": "aad-nicholas",
            "source_type": "channel",
            "body": "@AM-Release Manager please request release approval.",
            "mentioned_role_refs": ["@AM-Release Manager"],
            "thread_ref": "thread-release",
        }
    )
    return db, adapter, replayed.conversation_id


def _request_release_approval(
    db: V2Database,
    adapter: LocalTeamsTestAdapter,
    *,
    conversation_id: str,
) -> str:
    service = ConnectorSafeOutputService(db, adapter=adapter)
    db.create_run(
        run_id="run-release-permissions",
        role_id="release-manager",
        role_instance_id="release-manager-1",
        work_item_id="work-permission-card",
    )
    service.record(
        run_id="run-release-permissions",
        call=SafeOutputCall(
            role_id="release-manager",
            tool_name="release.request_approval",
            payload={
                "work_item_id": "work-permission-card",
                "title": "Approve permission hardening test release",
                "question": "Approve release of work-permission-card?",
                "conversation_id": conversation_id,
                "destination_ref": "channel-project",
                "destination_type": "channel",
                "thread_ref": "thread-release",
                "gate_id": "release_decision_response",
                "response_contract_id": "release-decision-v1",
                "required_authority": "release_approver",
            },
            terminal=True,
        ),
    )
    return db.status_snapshot()["human_response_requests"][0]["request_id"]


def test_startup_records_setup_runtime_and_broad_permission_evidence(tmp_path: Path) -> None:
    db = _db(tmp_path)
    adapter = LocalTeamsTestAdapter(db, _config())

    adapter.install()

    snapshot = db.status_snapshot()
    assert snapshot["connectors"][0]["status"] == "configured"
    checks = snapshot["connector_permission_checks"]
    assert any(check["capability"] == "app_installation" and check["status"] == "pass" for check in checks)
    assert any(check["capability"] == "send_capability" and check["phase"] == "runtime" for check in checks)
    broad = next(check for check in checks if check["permission_name"] == "ChannelMessage.Read.All")
    assert broad["broad_graph"] is True
    assert broad["approval_ref"] == "security-approval-teams-broad-read"
    assert broad["status"] == "pass"
    assert snapshot["counts"]["connector_attention_items"] == 0


def test_missing_or_revoked_permissions_fail_closed_for_receive_and_send(tmp_path: Path) -> None:
    db = _db(tmp_path)
    adapter = LocalTeamsTestAdapter(
        db,
        _config(
            permission_validation={
                "capabilities": {
                    "member_metadata_access": "revoked",
                    "send_capability": "missing",
                }
            }
        ),
    )

    adapter.install()

    with pytest.raises(PermissionValidationFailure, match="member_metadata_access=revoked"):
        adapter.replay_event(
            {
                "event_type": "message.created",
                "message_id": "msg-blocked-receive",
                "conversation_ref": "channel-project",
                "sender_ref": "aad-nicholas",
                "source_type": "channel",
                "body": "@AM-Product Manager can you see this?",
                "mentioned_role_refs": ["@AM-Product Manager"],
            }
        )
    with pytest.raises(PermissionValidationFailure, match="send_capability=missing"):
        adapter.send_message(
            source_ref="call-blocked-send",
            destination_ref="channel-project",
            destination_type="channel",
            purpose="status.reply",
            body="This should not send.",
            role_id="product-manager",
        )

    snapshot = db.status_snapshot()
    assert snapshot["connectors"][0]["status"] == "permission_failed"
    assert {item["reason_class"] for item in snapshot["connector_attention_items"]} == {
        "connector_permission_failed"
    }
    assert snapshot["counts"]["delivery_records"] == 0
    assert any(
        check["capability"] == "member_metadata_access" and check["actual_status"] == "revoked"
        for check in snapshot["connector_permission_checks"]
    )


def test_unbound_channel_ingress_fails_closed_before_project_context_capture(tmp_path: Path) -> None:
    db = _db(tmp_path)
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    with pytest.raises(PermissionValidationFailure, match="channel_binding:channel-outside-project=missing"):
        adapter.replay_event(
            {
                "event_type": "message.created",
                "message_id": "msg-outside-boundary",
                "conversation_ref": "channel-outside-project",
                "sender_ref": "aad-nicholas",
                "source_type": "channel",
                "body": "OUTSIDE_BOUNDARY_SENTINEL should not become project context.",
            }
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["conversation_events"] == 0
    assert snapshot["counts"]["role_assignments"] == 0
    assert snapshot["connector_attention_items"][0]["reason_class"] == "connector_permission_failed"
    check = next(
        item
        for item in snapshot["connector_permission_checks"]
        if item["capability"] == "channel_binding:channel-outside-project"
    )
    assert check["status"] == "fail"
    assert check["actual_status"] == "missing"
    assert "OUTSIDE_BOUNDARY_SENTINEL" not in str(snapshot)


def test_broad_graph_permission_requires_explicit_approval(tmp_path: Path) -> None:
    db = _db(tmp_path)
    adapter = LocalTeamsTestAdapter(
        db,
        _config(
            permission_validation={
                "permissions": [
                    {
                        "permission": "ChannelMessage.Read.All",
                        "phase": "runtime",
                        "consent_type": "application",
                        "status": "granted",
                        "required": False,
                    }
                ]
            }
        ),
    )

    adapter.install()

    snapshot = db.status_snapshot()
    check = next(
        item
        for item in snapshot["connector_permission_checks"]
        if item["permission_name"] == "ChannelMessage.Read.All"
    )
    assert snapshot["connectors"][0]["status"] == "permission_failed"
    assert check["permission_name"] == "ChannelMessage.Read.All"
    assert check["broad_graph"] is True
    assert check["approval_ref"] is None
    assert check["status"] == "fail"
    assert snapshot["connector_attention_items"][0]["next_action"].startswith(
        "Document explicit security approval"
    )


def test_response_authority_uses_external_people_records_not_display_names(tmp_path: Path) -> None:
    db, adapter, conversation_id = _db_with_work_item(tmp_path)
    request_id = _request_release_approval(db, adapter, conversation_id=conversation_id)

    adapter.submit_card_response(
        request_id=request_id,
        responder_ref="Nicholas Overend",
        response_value="approve",
        comment="Display names must not authorize privileged actions.",
        submission_id="submission-display-name",
    )
    adapter.submit_card_response(
        request_id=request_id,
        responder_ref="aad-nicholas",
        response_value="approve",
        comment="External identity is authorized.",
        submission_id="submission-authorized-ref",
    )

    snapshot = db.status_snapshot()
    submissions = {submission["submission_id"]: submission for submission in snapshot["human_response_submissions"]}
    assert submissions["submission-display-name"]["status"] == "rejected_unauthorized"
    assert submissions["submission-display-name"]["authority"] == []
    assert submissions["submission-authorized-ref"]["status"] == "accepted"
    assert "release_approver" in submissions["submission-authorized-ref"]["authority"]
    assert snapshot["human_response_requests"][0]["status"] == "responded"


def test_connector_config_rejects_inline_credential_material() -> None:
    with pytest.raises(ValueError, match="inline credential material"):
        _config(client_secret="super-secret-value")
    with pytest.raises(ValueError, match="inline credential material"):
        _config(auth={"access_token": "token-value"})
