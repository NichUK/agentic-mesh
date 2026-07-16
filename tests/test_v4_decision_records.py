from __future__ import annotations

import json

import pytest

from agentic_mesh_v4.decision_records import DecisionRecordError
from agentic_mesh_v4.decision_records import DecisionRequest
from agentic_mesh_v4.decision_records import cancel_decision
from agentic_mesh_v4.decision_records import record_card_delivery_attempt
from agentic_mesh_v4.decision_records import reconcile_resolved_decision_notifications
from agentic_mesh_v4.decision_records import render_decision_card
from agentic_mesh_v4.decision_records import request_decision
from agentic_mesh_v4.decision_records import resolve_decision_and_update_card
from agentic_mesh_v4.decision_records import _validate_request
from v4_postgres import make_v4_db


class _CardSender:
    def update_decision_card(self, **_: object) -> str:
        return "updated-activity"


def _request(*, question: str = "Approve this design so implementation planning can continue?", link_effects=()):
    return DecisionRequest(
        work_item_id="work-decision-test",
        requester_role="project-manager",
        owner_role="project-manager",
        authority_label="Sponsor",
        authorized_responders={"sponsor": "sponsor-aad-id"},
        decision_type="approval",
        title="Approve the safe-output transport design",
        question=question,
        options=("approved", "changes_requested", "deferred"),
        recommended_option="approved",
        link_effects=tuple(link_effects),
    )


def test_resolved_decision_queues_requester_and_reuses_teams_route() -> None:
    db = make_v4_db()
    try:
        result = request_decision(db=db, request=_request(), deliver=False)
        activity = {
            "conversation": {"id": "conversation-1"},
            "recipient": {"id": "project-manager-bot", "name": "AM-Project Manager"},
            "serviceUrl": "https://smba.trafficmanager.net/emea/",
        }
        delivery_id = record_card_delivery_attempt(
            db=db,
            decision_id=result["decision_id"],
            channel="teams",
            conversation_ref=json.dumps(activity),
            activity_id="activity-1",
            state="delivered",
        )

        resolved = resolve_decision_and_update_card(
            db=db,
            decision_id=result["decision_id"],
            responder_ref="sponsor-aad-id",
            selected_option="approved",
            sender=_CardSender(),
            delivery_id=delivery_id,
        )
        repeated = resolve_decision_and_update_card(
            db=db,
            decision_id=result["decision_id"],
            responder_ref="sponsor-aad-id",
            selected_option="approved",
            sender=_CardSender(),
            delivery_id=delivery_id,
        )

        messages = db.connection.execute("SELECT * FROM message_queue ORDER BY created_at").fetchall()
        assert resolved["requester_notification"]["state"] == "queued"
        assert repeated["requester_notification"]["idempotent"] is True
        assert len(messages) == 1
        assert messages[0]["target_role"] == "project-manager"
        assert messages[0]["source"] == "teams"
        assert messages[0]["conversation_ref"] == json.dumps(activity)
        assert "Continue from this decision now" in messages[0]["text"]
    finally:
        db.close()


def test_cross_role_decision_does_not_reuse_owner_bot_teams_route() -> None:
    db = make_v4_db()
    try:
        request = DecisionRequest(
            work_item_id="work-cross-role-decision",
            requester_role="solution-architect",
            owner_role="project-manager",
            authority_label="Sponsor",
            authorized_responders={"sponsor": "sponsor-aad-id"},
            decision_type="approval",
            title="Approve the architecture disposition",
            question="Approve this architecture disposition?",
            options=("approved", "changes_requested"),
            recommended_option="approved",
        )
        result = request_decision(db=db, request=request, deliver=False)
        activity = {
            "conversation": {"id": "project-manager-conversation"},
            "recipient": {"id": "project-manager-bot", "name": "AM-Project Manager"},
            "serviceUrl": "https://smba.trafficmanager.net/emea/",
        }
        delivery_id = record_card_delivery_attempt(
            db=db,
            decision_id=result["decision_id"],
            channel="teams",
            conversation_ref=json.dumps(activity),
            activity_id="activity-cross-role",
            state="delivered",
        )

        resolve_decision_and_update_card(
            db=db,
            decision_id=result["decision_id"],
            responder_ref="sponsor-aad-id",
            selected_option="approved",
            sender=_CardSender(),
            delivery_id=delivery_id,
        )

        message = db.connection.execute(
            "SELECT * FROM message_queue WHERE target_role=?",
            ("solution-architect",),
        ).fetchone()
        payload = json.loads(message["payload_json"])
        assert message["source"] == "decision_callback"
        assert message["conversation_ref"] is None
        assert "conversation" not in payload
        assert payload["owner_role"] == "project-manager"
        assert "hand the result back to project-manager" in message["text"]
    finally:
        db.close()


def test_resolved_card_is_human_facing_and_hides_internal_responder() -> None:
    card = render_decision_card(
        {
            "decision_id": "decision-1",
            "title": "Approve the implementation plan",
            "question": "Internal detail that is no longer needed after resolution.",
            "status": "resolved",
            "selected_option": "approved",
            "responder_ref": "2485de4b-4331-48f6-805d-68106b09be1b",
            "options_json": '["approved"]',
        }
    )
    rendered = json.dumps(card)
    assert "Decision recorded: **Approved**" in rendered
    assert "requesting agent has been notified" in rendered
    assert "2485de4b-4331-48f6-805d-68106b09be1b" not in rendered
    assert "Decision type" not in rendered
    assert "Requester" not in rendered


def test_decision_request_rejects_opaque_or_empty_declared_effects() -> None:
    db = make_v4_db()
    try:
        with pytest.raises(DecisionRecordError, match="supported target_type"):
            request_decision(db=db, request=_request(link_effects=({},)), deliver=False)
        with pytest.raises(DecisionRecordError, match="no more than 600"):
            request_decision(db=db, request=_request(question="x" * 601), deliver=False)
    finally:
        db.close()


def test_decision_question_rejects_internal_finding_code_instead_of_summary() -> None:
    request = _request(
        question="Authorize direct remediation of QA-SF-ST1-001 so Stages 2-4 may continue?"
    )

    with pytest.raises(DecisionRecordError, match="summarize the underlying issue"):
        _validate_request(request)


def test_decision_question_accepts_short_plain_english_problem_summary() -> None:
    request = _request(
        question=(
            "The migration cannot yet prove that every project uses the same agent fleet, so cutover is paused. "
            "Authorize Engineering to correct that validation gap and let QA accept the result?"
        )
    )

    _validate_request(request)


def test_decision_question_accepts_hyphenated_plain_english_words() -> None:
    """Ensure common English hyphenated words are not mistaken for internal IDs."""
    request = _request(
        question=(
            "A work-around exists but requires sponsor approval. "
            "The decision-making process is blocked pending your response. "
            "Approve the handoff-process change so Engineering can continue?"
        )
    )

    _validate_request(request)


def test_cancelled_card_does_not_claim_agent_was_notified() -> None:
    card = render_decision_card(
        {
            "decision_id": "decision-2",
            "title": "Approve the implementation plan",
            "status": "cancelled",
            "selected_option": "cancelled",
            "options_json": '["approved"]',
        }
    )
    rendered = json.dumps(card)
    assert "Decision recorded: **Cancelled**" in rendered
    assert "requesting agent has been notified" not in rendered
    assert "cancelled" in rendered.lower()


def test_reconcile_skips_already_notified_and_reaches_later_rows() -> None:
    """Watchdog must skip already-queued notifications so later resolved rows are not starved."""
    db = make_v4_db()
    try:
        # Create 3 resolved decisions
        ids = []
        for _ in range(3):
            r = request_decision(db=db, request=_request(), deliver=False)
            ids.append(r["decision_id"])
            resolve_decision_and_update_card(
                db=db,
                decision_id=r["decision_id"],
                responder_ref="sponsor-aad-id",
                selected_option="approved",
                sender=_CardSender(),
            )

        # All 3 should now have notifications; reconcile with limit=1 should find nothing new
        result = reconcile_resolved_decision_notifications(db=db, limit=1)
        assert result["queued"] == 0
        assert result["checked"] == 0

        # Manually delete the notification for the third decision to simulate a missed delivery
        third_id = ids[2].removeprefix("decision-")
        db.connection.execute(
            "DELETE FROM message_queue WHERE message_id=?",
            (f"msg-decision-result-{third_id}",),
        )

        # Reconcile with limit=1 — should find and queue the missing notification
        result = reconcile_resolved_decision_notifications(db=db, limit=1)
        assert result["queued"] == 1
        assert result["checked"] == 1
    finally:
        db.close()
