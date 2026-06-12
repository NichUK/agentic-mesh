from pathlib import Path

import pytest

from agentic_mesh_v2.connectors import ConnectorConfig
from agentic_mesh_v2.connectors import ConnectorSafeOutputService
from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall


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
                "release-manager": {
                    "external_ref": "bot-release-manager",
                    "display_name": "AM-Release Manager",
                    "alias": "release-manager",
                    "mention_handle": "@AM-Release Manager",
                    "identity_model": "separate_bot",
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


def test_team_wide_prompt_creates_relevance_assignments_for_enabled_roles(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-team-wide",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@all-agents please check whether this affects your role.",
            "thread_ref": "thread-team-wide",
        }
    )

    snapshot = db.status_snapshot()
    assert replayed.route_type == "team_wide_prompt"
    assert snapshot["counts"]["role_assignments"] == 3
    assignments = snapshot["role_assignments"]
    assert {assignment["role_id"] for assignment in assignments} == {
        "product-manager",
        "engineering",
        "qa-engineer",
    }
    assert {assignment["assignment_type"] for assignment in assignments} == {"team_wide_relevance_check"}
    assert all(assignment["payload"]["threshold"] == 0.6 for assignment in assignments)
    assert all(assignment["payload"]["conversation_event_id"] for assignment in assignments)


def test_relevance_records_noop_material_and_exception_without_channel_noise(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-team-wide-decisions",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@all-agents please assess status-dashboard scan density.",
        }
    )
    event_id = db.status_snapshot()["conversation_events"][0]["conversation_event_id"]
    for role in ("product-manager", "engineering", "qa-engineer"):
        db.create_run(run_id=f"run-{role}", role_id=role, role_instance_id=f"{role}-1", work_item_id=None)

    service.record(
        run_id="run-product-manager",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="relevance.record",
            payload={
                "conversation_event_id": event_id,
                "score": 0.35,
                "threshold": 0.6,
                "decision": "not_relevant",
                "reason": "No product decision needed.",
                "noop": True,
            },
        ),
    )
    service.record(
        run_id="run-engineering",
        call=SafeOutputCall(
            role_id="engineering",
            tool_name="relevance.record",
            payload={
                "conversation_event_id": event_id,
                "score": 0.9,
                "threshold": 0.6,
                "decision": "material",
                "reason": "Engineering owns dashboard implementation impact.",
                "noop": False,
            },
        ),
    )
    service.record(
        run_id="run-engineering",
        call=SafeOutputCall(
            role_id="engineering",
            tool_name="status.reply",
            payload={
                "message": "Engineering has material input on the dashboard density change.",
                "conversation_id": replayed.conversation_id,
                "destination_ref": "channel-project",
                "destination_type": "channel",
            },
        ),
    )
    delivery_ref = db.status_snapshot()["delivery_records"][0]["delivery_id"]
    service.record(
        run_id="run-qa-engineer",
        call=SafeOutputCall(
            role_id="qa-engineer",
            tool_name="relevance.record",
            payload={
                "conversation_event_id": event_id,
                "score": 0.4,
                "threshold": 0.6,
                "decision": "exception",
                "reason": "Below threshold, but QA needs to call out regression risk.",
                "noop": False,
                "exception_reason": "Regression coverage risk.",
                "delivery_ref": delivery_ref,
            },
        ),
    )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["relevance_checks"] == 3
    assert snapshot["counts"]["delivery_records"] == 1
    checks = {check["role_id"]: check for check in snapshot["relevance_checks"]}
    assert checks["product-manager"]["noop"] is True
    assert checks["engineering"]["decision"] == "material"
    assert checks["qa-engineer"]["decision"] == "exception"
    assert checks["qa-engineer"]["exception_reason"] == "Regression coverage risk."
    assert checks["qa-engineer"]["delivery_ref"] == delivery_ref


def test_role_to_role_followup_uses_runtime_consult_not_teams_transport(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)

    adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-team-wide-consult",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@all-agents check whether this needs architecture input.",
        }
    )
    event_id = db.status_snapshot()["conversation_events"][0]["conversation_event_id"]
    db.create_run(run_id="run-engineering", role_id="engineering", role_instance_id="engineering-1", work_item_id=None)
    relevance_call = service.record(
        run_id="run-engineering",
        call=SafeOutputCall(
            role_id="engineering",
            tool_name="relevance.record",
            payload={
                "conversation_event_id": event_id,
                "score": 0.8,
                "threshold": 0.6,
                "decision": "material",
                "reason": "Engineering needs architecture consultation.",
                "noop": False,
            },
        ),
    )
    consult_call = service.record(
        run_id="run-engineering",
        call=SafeOutputCall(
            role_id="engineering",
            tool_name="consult.request",
            payload={"target_role": "product-manager", "reason": "Need product constraint before replying."},
        ),
    )

    snapshot = db.status_snapshot()
    assert relevance_call != consult_call
    assert snapshot["counts"]["safe_output_calls"] == 2
    assert snapshot["counts"]["delivery_records"] == 0
    assert {call["tool_name"] for call in snapshot["safe_output_calls"]} == {
        "relevance.record",
        "consult.request",
    }


def test_noop_relevance_decision_cannot_post_teams_reply_in_same_run(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, _config())
    adapter.install()
    service = ConnectorSafeOutputService(db, adapter=adapter)

    replayed = adapter.replay_event(
        {
            "event_type": "message.created",
            "message_id": "msg-team-wide-noop-guard",
            "conversation_ref": "channel-project",
            "sender_ref": "nicholas",
            "source_type": "channel",
            "body": "@all-agents please assess whether this needs QA input.",
        }
    )
    event_id = db.status_snapshot()["conversation_events"][0]["conversation_event_id"]
    db.create_run(run_id="run-qa-noop", role_id="qa-engineer", role_instance_id="qa-engineer-1", work_item_id=None)
    service.record(
        run_id="run-qa-noop",
        call=SafeOutputCall(
            role_id="qa-engineer",
            tool_name="relevance.record",
            payload={
                "conversation_event_id": event_id,
                "score": 0.1,
                "threshold": 0.6,
                "decision": "not_relevant",
                "reason": "No QA input needed.",
                "noop": True,
            },
        ),
    )

    with pytest.raises(ValueError, match="no-op relevance decision"):
        service.record(
            run_id="run-qa-noop",
            call=SafeOutputCall(
                role_id="qa-engineer",
                tool_name="status.reply",
                payload={
                    "message": "QA has something to say after all.",
                    "conversation_id": replayed.conversation_id,
                    "destination_ref": "channel-project",
                    "destination_type": "channel",
                },
            ),
        )

    snapshot = db.status_snapshot()
    assert snapshot["counts"]["relevance_checks"] == 1
    assert snapshot["counts"]["safe_output_calls"] == 1
    assert snapshot["counts"]["delivery_records"] == 0
