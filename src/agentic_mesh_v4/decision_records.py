from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Protocol
from uuid import uuid4

from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.db import utc_now


DECISION_OPTIONS: dict[str, set[str]] = {
    "approval": {"approved", "changes_requested", "deferred"},
    "exception_override": {"override_authorized", "override_denied", "mitigation_required"},
    "release": {"release_approved", "release_held", "changes_requested"},
    "request_changes": {"minor_changes", "major_changes", "rejected", "approved"},
}
AUTHORITY_LABELS = {
    "Sponsor",
    "Operator",
    "Product Manager",
    "Release Approver",
    "Security Reviewer",
    "Platform Engineer",
    "QA Lead",
}
OPEN_DECISION_STATES = {"pending", "delivery_failed_pending", "resolution_failed"}
TERMINAL_DECISION_STATES = {"resolved", "cancelled"}
DECISION_NOTIFICATION_PREFIX = "msg-decision-result-"
MAX_DECISION_TITLE_LENGTH = 120
MAX_DECISION_QUESTION_LENGTH = 600
SUPPORTED_EFFECT_TARGET_TYPES = {"work_item", "handoff", "preflight", "artifact", "release"}


class DecisionCardSender(Protocol):
    def send_decision_card(
        self,
        *,
        role_id: str,
        activity: dict[str, object],
        card: dict[str, object],
    ) -> str:
        ...

    def update_decision_card(
        self,
        *,
        role_id: str,
        activity: dict[str, object],
        activity_id: str,
        card: dict[str, object],
    ) -> str:
        ...


class DecisionRecordError(ValueError):
    pass


@dataclass(frozen=True)
class DecisionRequest:
    work_item_id: str
    requester_role: str
    owner_role: str
    authority_label: str
    authorized_responders: dict[str, str]
    decision_type: str
    title: str
    question: str
    options: tuple[str, ...]
    recommended_option: str | None = None
    tradeoffs: dict[str, str] | None = None
    source_refs: tuple[str, ...] = ()
    affected_refs: tuple[dict[str, Any], ...] = ()
    link_effects: tuple[dict[str, Any], ...] = ()
    sla_due_at: str | None = None
    conversation_ref: str | None = None


def request_decision(
    *,
    db: V4Database,
    request: DecisionRequest,
    decision_id: str | None = None,
    deliver: bool = True,
) -> dict[str, Any]:
    _validate_request(request)
    decision_id = decision_id or f"decision-{uuid4().hex}"
    now = utc_now()
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO decision_records(
              decision_id, work_item_id, requester_role, owner_role, authority_label,
              authorized_responders_json, decision_type, title, question, options_json,
              recommended_option, tradeoffs_json, source_refs_json, affected_refs_json,
              link_effects_json, sla_due_at, sla_state, status, selected_option,
              rationale, responder_ref, resolved_at, cancelled_at, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                decision_id,
                request.work_item_id,
                request.requester_role,
                request.owner_role,
                request.authority_label,
                json.dumps(request.authorized_responders, sort_keys=True),
                request.decision_type,
                request.title,
                request.question,
                json.dumps(list(request.options), sort_keys=True),
                request.recommended_option,
                json.dumps(request.tradeoffs or {}, sort_keys=True),
                json.dumps(list(request.source_refs), sort_keys=True),
                json.dumps(list(request.affected_refs), sort_keys=True),
                json.dumps(list(request.link_effects), sort_keys=True),
                request.sla_due_at,
                _sla_state(request.sla_due_at),
                "pending",
                None,
                None,
                None,
                None,
                None,
                now,
                now,
            ),
        )
        for effect in request.link_effects:
            _insert_decision_link(db=db, decision_id=decision_id, effect=effect, applied_at=None)
        delivery_id = None
        if deliver:
            delivery_id = record_card_delivery_attempt(
                db=db,
                decision_id=decision_id,
                channel="teams",
                conversation_ref=request.conversation_ref,
                state="pending_delivery",
            )
    return {"decision_id": decision_id, "delivery_id": delivery_id}


def record_card_delivery_attempt(
    *,
    db: V4Database,
    decision_id: str,
    channel: str,
    conversation_ref: str | None = None,
    activity_id: str | None = None,
    state: str = "delivered",
    error: str | None = None,
    delivery_id: str | None = None,
) -> str:
    delivery_id = delivery_id or f"delivery-{uuid4().hex}"
    now = utc_now()
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO decision_card_deliveries(
              delivery_id, decision_id, channel, conversation_ref, activity_id,
              card_version, state, error, attempt_count, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                delivery_id,
                decision_id,
                channel,
                conversation_ref,
                activity_id,
                "decision-card-v1",
                state,
                error,
                1,
                now,
                now,
            ),
        )
        if state == "delivery_failed":
            db.connection.execute(
                "UPDATE decision_records SET status='delivery_failed_pending', updated_at=? WHERE decision_id=? AND status='pending'",
                (now, decision_id),
            )
    return delivery_id


def resolve_decision(
    *,
    db: V4Database,
    decision_id: str,
    responder_ref: str,
    selected_option: str,
    rationale: str = "",
    idempotency_key: str | None = None,
    delivery_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision = _decision(db, decision_id)
    idempotency_key = idempotency_key or f"{decision_id}:{responder_ref}:{selected_option}"
    raw_hash = _payload_hash(raw_payload or {"decision_id": decision_id, "responder_ref": responder_ref, "selected_option": selected_option})
    existing = db.connection.execute(
        "SELECT * FROM decision_callbacks WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        result = {
            "decision_id": decision_id,
            "callback_id": existing["callback_id"],
            "state": existing["state"],
            "idempotent": True,
        }
        if existing["state"] == "accepted":
            result["requester_notification"] = ensure_decision_resolution_notification(
                db=db,
                decision_id=decision_id,
            )
        return result
    callback_id = f"callback-{uuid4().hex}"
    now = utc_now()
    try:
        normalized = _normalize_option(decision=decision, submitted_option=selected_option)
    except DecisionRecordError as exc:
        return _record_failed_callback(
            db=db,
            callback_id=callback_id,
            decision_id=decision_id,
            delivery_id=delivery_id,
            responder_ref=responder_ref,
            submitted_option=selected_option,
            normalized_option="",
            raw_payload_hash=raw_hash,
            idempotency_key=idempotency_key,
            error=str(exc),
        )
    if decision["status"] not in OPEN_DECISION_STATES:
        return _record_failed_callback(
            db=db,
            callback_id=callback_id,
            decision_id=decision_id,
            delivery_id=delivery_id,
            responder_ref=responder_ref,
            submitted_option=selected_option,
            normalized_option=normalized,
            raw_payload_hash=raw_hash,
            idempotency_key=idempotency_key,
            error="decision is not open",
        )
    if not _authorized(decision=decision, responder_ref=responder_ref):
        return _record_failed_callback(
            db=db,
            callback_id=callback_id,
            decision_id=decision_id,
            delivery_id=delivery_id,
            responder_ref=responder_ref,
            submitted_option=selected_option,
            normalized_option=normalized,
            raw_payload_hash=raw_hash,
            idempotency_key=idempotency_key,
            error="responder is not authorized",
        )
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO decision_callbacks(
              callback_id, decision_id, delivery_id, responder_ref, submitted_option,
              normalized_option, raw_payload_hash, state, error, idempotency_key,
              created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                callback_id,
                decision_id,
                delivery_id,
                responder_ref,
                selected_option,
                normalized,
                raw_hash,
                "accepted",
                None,
                idempotency_key,
                now,
                now,
            ),
        )
        db.connection.execute(
            """
            UPDATE decision_records
            SET status='resolved',
                selected_option=?,
                rationale=?,
                responder_ref=?,
                resolved_at=?,
                sla_state='resolved',
                updated_at=?
            WHERE decision_id=?
            """,
            (normalized, rationale, responder_ref, now, now, decision_id),
        )
        _apply_declared_effects(db=db, decision_id=decision_id, selected_option=normalized, now=now)
    result = {"decision_id": decision_id, "callback_id": callback_id, "state": "accepted", "idempotent": False}
    result["requester_notification"] = ensure_decision_resolution_notification(
        db=db,
        decision_id=decision_id,
    )
    return result


def ensure_decision_resolution_notification(
    *,
    db: V4Database,
    decision_id: str,
    recovery: bool = False,
) -> dict[str, Any]:
    """Durably queue an accepted decision back to the role that requested it."""
    decision = _decision(db, decision_id)
    if decision["status"] != "resolved":
        return {"state": "not_required", "reason": "decision is not resolved"}

    message_id = f"{DECISION_NOTIFICATION_PREFIX}{decision_id.removeprefix('decision-')}"
    existing = db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    if existing is not None:
        return {"state": str(existing["state"]), "message_id": message_id, "idempotent": True}

    delivery = _latest_conversation_delivery(db=db, decision_id=decision_id)
    conversation_ref = str(delivery.get("conversation_ref") or "") if delivery else ""
    activity: dict[str, object] = {}
    if conversation_ref:
        try:
            activity = _conversation_activity(conversation_ref)
        except DecisionRecordError:
            activity = {}

    selected_option = _option_label(str(decision.get("selected_option") or ""))
    text = "\n".join(
        [
            "A sponsor decision has been received and recorded.",
            f"Work item: {decision['work_item_id']}",
            f"Decision: {selected_option}",
            f"Subject: {decision['title']}",
            "Continue from this decision now. Carry out or hand off the next required action, then reply to the sponsor in plain English with what changed and what happens next.",
            (
                "This is a recovered notification from an earlier callback. Reconcile it with current work before acting, and record a superseded or already-completed disposition when appropriate."
                if recovery
                else "Do not merely acknowledge the decision."
            ),
        ]
    )
    payload: dict[str, object] = dict(activity)
    payload.update(
        {
            "decision_notification": True,
            "decision_id": decision_id,
            "work_item_id": str(decision["work_item_id"]),
            "selected_option": str(decision.get("selected_option") or ""),
            "requester_role": str(decision["requester_role"]),
            "recovery": recovery,
        }
    )
    db.enqueue_message(
        target_role=str(decision["requester_role"]),
        text=text,
        source="teams" if activity else "decision_callback",
        payload=payload,
        message_id=message_id,
        correlation_id=f"corr-{decision_id}",
        conversation_ref=conversation_ref or None,
    )
    return {"state": "queued", "message_id": message_id, "idempotent": False}


def reconcile_resolved_decision_notifications(
    *,
    db: V4Database,
    limit: int = 100,
) -> dict[str, Any]:
    rows = db.connection.execute(
        """
        SELECT decision_id
        FROM decision_records
        WHERE status='resolved'
        AND NOT EXISTS (
            SELECT 1 FROM message_queue
            WHERE message_id = 'msg-decision-result-' || SUBSTR(decision_id, 10)
        )
        ORDER BY resolved_at, decision_id
        LIMIT ?
        """,
        (limit,),
    )
    results = [
        ensure_decision_resolution_notification(
            db=db,
            decision_id=str(row["decision_id"]),
            recovery=True,
        )
        for row in rows
    ]
    return {
        "checked": len(results),
        "queued": sum(1 for item in results if item.get("state") == "queued" and not item.get("idempotent")),
        "existing": sum(1 for item in results if item.get("idempotent")),
        "results": results,
    }


def resolve_decision_and_update_card(
    *,
    db: V4Database,
    decision_id: str,
    responder_ref: str,
    selected_option: str,
    sender: DecisionCardSender | None,
    rationale: str = "",
    idempotency_key: str | None = None,
    delivery_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    detail_url: str | None = None,
) -> dict[str, Any]:
    result = resolve_decision(
        db=db,
        decision_id=decision_id,
        responder_ref=responder_ref,
        selected_option=selected_option,
        rationale=rationale,
        idempotency_key=idempotency_key,
        delivery_id=delivery_id,
        raw_payload=raw_payload,
    )
    if result.get("state") == "accepted" and sender is not None:
        result["card_update"] = update_resolved_decision_card(
            db=db,
            decision_id=decision_id,
            sender=sender,
            delivery_id=delivery_id,
            detail_url=detail_url,
        )
    return result


def cancel_decision(*, db: V4Database, decision_id: str, responder_ref: str, rationale: str = "") -> dict[str, Any]:
    decision = _decision(db, decision_id)
    if not _authorized(decision=decision, responder_ref=responder_ref):
        raise DecisionRecordError("responder is not authorized")
    now = utc_now()
    with db.connection:
        db.connection.execute(
            """
            UPDATE decision_records
            SET status='cancelled', selected_option='cancelled', rationale=?, responder_ref=?,
                cancelled_at=?, updated_at=?
            WHERE decision_id=? AND status IN ('pending','delivery_failed_pending','resolution_failed')
            """,
            (rationale, responder_ref, now, now, decision_id),
        )
    return {"decision_id": decision_id, "state": "cancelled"}


def update_resolved_decision_card(
    *,
    db: V4Database,
    decision_id: str,
    sender: DecisionCardSender,
    delivery_id: str | None = None,
    detail_url: str | None = None,
) -> dict[str, Any]:
    decision = _decision(db, decision_id)
    delivery = _latest_update_target(db=db, decision_id=decision_id, delivery_id=delivery_id)
    if delivery is None:
        return {"decision_id": decision_id, "state": "update_skipped", "error": "no delivered Teams card to update"}
    if decision["status"] != "resolved":
        return {"decision_id": decision_id, "delivery_id": delivery["delivery_id"], "state": "update_skipped", "error": "decision is not resolved"}
    now = utc_now()
    try:
        conversation = _conversation_activity(delivery.get("conversation_ref"))
        activity_id = str(delivery.get("activity_id") or "")
        if not activity_id:
            raise DecisionRecordError("delivered Teams card activity_id is missing")
        updated_id = sender.update_decision_card(
            role_id=str(decision["owner_role"]),
            activity=conversation,
            activity_id=activity_id,
            card=render_decision_card(decision, detail_url=detail_url),
        )
    except Exception as exc:  # noqa: BLE001 - update failure must be projected without unresolving.
        with db.connection:
            db.connection.execute(
                """
                UPDATE decision_card_deliveries
                SET state='update_failed',
                    error=?,
                    attempt_count=attempt_count + 1,
                    updated_at=?
                WHERE delivery_id=?
                """,
                (str(exc), now, delivery["delivery_id"]),
            )
        return {"decision_id": decision_id, "delivery_id": delivery["delivery_id"], "state": "update_failed", "error": str(exc)}
    with db.connection:
        db.connection.execute(
            """
            UPDATE decision_card_deliveries
            SET state='updated',
                activity_id=?,
                error=NULL,
                attempt_count=attempt_count + 1,
                updated_at=?
            WHERE delivery_id=?
            """,
            (updated_id or delivery.get("activity_id"), now, delivery["delivery_id"]),
        )
    return {"decision_id": decision_id, "delivery_id": delivery["delivery_id"], "state": "updated"}


def retry_failed_card_updates(
    *,
    db: V4Database,
    sender: DecisionCardSender,
    limit: int = 20,
    detail_url: str | None = None,
) -> dict[str, Any]:
    rows = [
        {key: row[key] for key in row.keys()}
        for row in db.connection.execute(
            """
            SELECT decision_id FROM decision_card_deliveries
            WHERE state='update_failed'
            ORDER BY updated_at
            LIMIT ?
            """,
            (limit,),
        )
    ]
    results = [
        update_resolved_decision_card(
            db=db,
            decision_id=str(row["decision_id"]),
            sender=sender,
            detail_url=detail_url,
        )
        for row in rows
    ]
    return {
        "checked": len(rows),
        "updated": sum(1 for item in results if item.get("state") == "updated"),
        "update_failed": sum(1 for item in results if item.get("state") == "update_failed"),
        "results": results,
    }


def deliver_pending_decision_cards(
    *,
    db: V4Database,
    sender: DecisionCardSender,
    limit: int = 20,
    detail_url: str | None = None,
) -> dict[str, Any]:
    rows = [
        {key: row[key] for key in row.keys()}
        for row in db.connection.execute(
            """
            SELECT d.*, r.owner_role
            FROM decision_card_deliveries d
            JOIN decision_records r ON r.decision_id=d.decision_id
            WHERE d.channel='teams'
              AND d.state IN ('pending_delivery','delivery_failed')
              AND r.status IN ('pending','delivery_failed_pending','resolution_failed')
            ORDER BY d.updated_at
            LIMIT ?
            """,
            (limit,),
        )
    ]
    results: list[dict[str, Any]] = []
    for delivery in rows:
        decision = _decision(db, str(delivery["decision_id"]))
        now = utc_now()
        try:
            conversation = _conversation_activity(delivery.get("conversation_ref"))
            activity_id = sender.send_decision_card(
                role_id=str(decision["owner_role"]),
                activity=conversation,
                card=render_decision_card(decision, detail_url=detail_url),
            )
        except Exception as exc:  # noqa: BLE001 - background delivery failure must be durable.
            with db.connection:
                db.connection.execute(
                    """
                    UPDATE decision_card_deliveries
                    SET state='delivery_failed',
                        error=?,
                        attempt_count=attempt_count + 1,
                        updated_at=?
                    WHERE delivery_id=?
                    """,
                    (str(exc), now, delivery["delivery_id"]),
                )
                db.connection.execute(
                    """
                    UPDATE decision_records
                    SET status='delivery_failed_pending',
                        updated_at=?
                    WHERE decision_id=? AND status='pending'
                    """,
                    (now, delivery["decision_id"]),
                )
            results.append({"decision_id": delivery["decision_id"], "delivery_id": delivery["delivery_id"], "state": "delivery_failed", "error": str(exc)})
            continue
        with db.connection:
            db.connection.execute(
                """
                UPDATE decision_card_deliveries
                SET state='delivered',
                    activity_id=?,
                    error=NULL,
                    attempt_count=attempt_count + 1,
                    updated_at=?
                WHERE delivery_id=?
                """,
                (activity_id, now, delivery["delivery_id"]),
            )
            db.connection.execute(
                """
                UPDATE decision_records
                SET status='pending',
                    updated_at=?
                WHERE decision_id=? AND status='delivery_failed_pending'
                """,
                (now, delivery["decision_id"]),
            )
        results.append({"decision_id": delivery["decision_id"], "delivery_id": delivery["delivery_id"], "state": "delivered", "activity_id": activity_id})
    return {
        "checked": len(rows),
        "delivered": sum(1 for item in results if item.get("state") == "delivered"),
        "delivery_failed": sum(1 for item in results if item.get("state") == "delivery_failed"),
        "results": results,
    }


def recalculate_sla_states(
    *,
    db: V4Database,
    now: str | datetime | None = None,
    due_soon_seconds: int = 3600,
    escalate_after_seconds: int = 86400,
) -> dict[str, Any]:
    current = _parse_timestamp(now) if isinstance(now, str) else now or datetime.now(UTC)
    rows = [
        {key: row[key] for key in row.keys()}
        for row in db.connection.execute(
            """
            SELECT * FROM decision_records
            WHERE status NOT IN ('resolved','cancelled') AND sla_due_at IS NOT NULL
            ORDER BY updated_at
            """
        )
    ]
    updates: list[dict[str, str]] = []
    with db.connection:
        for row in rows:
            due = _parse_timestamp(row.get("sla_due_at"))
            state = _sla_state_for_due_at(
                due_at=due,
                now=current,
                due_soon_seconds=due_soon_seconds,
                escalate_after_seconds=escalate_after_seconds,
            )
            if state == row.get("sla_state"):
                continue
            db.connection.execute(
                "UPDATE decision_records SET sla_state=?, updated_at=? WHERE decision_id=?",
                (state, utc_now(), row["decision_id"]),
            )
            updates.append({"decision_id": str(row["decision_id"]), "from": str(row.get("sla_state")), "to": state})
    return {
        "checked": len(rows),
        "updated": len(updates),
        "due_soon": sum(1 for item in updates if item["to"] == "due_soon"),
        "overdue": sum(1 for item in updates if item["to"] == "overdue"),
        "escalated": sum(1 for item in updates if item["to"] == "escalated"),
        "resolved_by_sla": 0,
        "updates": updates,
    }


def link_decision(
    *,
    db: V4Database,
    decision_id: str,
    target_type: str,
    target_id: str,
    link_type: str,
    effect_summary: str,
    effect_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    link_id = f"link-{uuid4().hex}"
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO decision_links(
              link_id, decision_id, target_type, target_id, link_type,
              effect_summary, effect_payload_json, applied_at, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                link_id,
                decision_id,
                target_type,
                target_id,
                link_type,
                effect_summary,
                json.dumps(effect_payload or {}, sort_keys=True),
                None,
                utc_now(),
            ),
        )
    return {"decision_id": decision_id, "link_id": link_id}


def render_decision_card(decision: dict[str, Any], *, detail_url: str | None = None) -> dict[str, Any]:
    options = _json_list(decision.get("options_json"))
    title = str(decision.get("title") or "Decision needed")
    body: list[dict[str, object]] = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True, "size": "Medium"},
    ]
    if decision.get("status") in TERMINAL_DECISION_STATES:
        body.extend(
            [
                {
                    "type": "TextBlock",
                    "text": f"Decision recorded: **{_option_label(str(decision.get('selected_option') or decision.get('status') or ''))}**",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "text": (
                        "The requesting agent has been notified and is responsible for the next action."
                        if decision.get("status") == "resolved"
                        else "This decision has been cancelled. No agent notification was sent."
                    ),
                    "wrap": True,
                    "isSubtle": True,
                },
            ]
        )
    else:
        body.extend(
            [
                {"type": "TextBlock", "text": str(decision.get("question") or ""), "wrap": True},
                *(
                    [
                        {
                            "type": "TextBlock",
                            "text": f"Recommended: **{_option_label(str(decision['recommended_option']))}**",
                            "wrap": True,
                            "isSubtle": True,
                        }
                    ]
                    if decision.get("recommended_option")
                    else []
                ),
            ]
        )
    if detail_url:
        body.append({"type": "TextBlock", "text": f"[View supporting details]({detail_url})", "wrap": True})
    return {
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
        "actions": [] if decision.get("status") in TERMINAL_DECISION_STATES else [
            {
                "type": "Action.Submit",
                "title": _option_label(str(option)),
                "data": {"action": "decision_callback", "decision_id": decision.get("decision_id"), "selected_option": option},
            }
            for option in options
        ],
    }


def _validate_request(request: DecisionRequest) -> None:
    if request.decision_type not in DECISION_OPTIONS:
        raise DecisionRecordError(f"unsupported decision_type: {request.decision_type}")
    if request.authority_label not in AUTHORITY_LABELS:
        raise DecisionRecordError(f"unsupported authority_label: {request.authority_label}")
    if not request.authorized_responders:
        raise DecisionRecordError("authorized_responders is required")
    allowed = DECISION_OPTIONS[request.decision_type]
    if not request.options or not set(request.options).issubset(allowed):
        raise DecisionRecordError("options are not valid for decision_type")
    if request.recommended_option and request.recommended_option not in request.options:
        raise DecisionRecordError("recommended_option must be one of options")
    if not request.title.strip() or len(request.title.strip()) > MAX_DECISION_TITLE_LENGTH:
        raise DecisionRecordError(f"title must be plain English and no more than {MAX_DECISION_TITLE_LENGTH} characters")
    if not request.question.strip() or len(request.question.strip()) > MAX_DECISION_QUESTION_LENGTH:
        raise DecisionRecordError(
            f"question must be one clear plain-English decision request of no more than {MAX_DECISION_QUESTION_LENGTH} characters; link detailed governance evidence instead"
        )
    for effect in request.link_effects:
        target_type = str(effect.get("target_type") or "")
        if target_type not in SUPPORTED_EFFECT_TARGET_TYPES:
            raise DecisionRecordError(
                "each declared effect requires a supported target_type: "
                + ", ".join(sorted(SUPPORTED_EFFECT_TARGET_TYPES))
            )
        if not str(effect.get("target_id") or "") or not str(effect.get("effect_summary") or ""):
            raise DecisionRecordError("each declared effect requires target_id and effect_summary")


def _option_label(value: str) -> str:
    return value.replace("_", " ").strip().capitalize()


def _decision(db: V4Database, decision_id: str) -> dict[str, Any]:
    row = db.connection.execute("SELECT * FROM decision_records WHERE decision_id=?", (decision_id,)).fetchone()
    if row is None:
        raise DecisionRecordError(f"unknown decision_id: {decision_id}")
    return {key: row[key] for key in row.keys()}


def _normalize_option(*, decision: dict[str, Any], submitted_option: str) -> str:
    options = set(str(item) for item in _json_list(decision.get("options_json")))
    normalized = submitted_option.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized not in options:
        raise DecisionRecordError("submitted option is not valid for this decision")
    return normalized


def _authorized(*, decision: dict[str, Any], responder_ref: str) -> bool:
    responders = _json_object(decision.get("authorized_responders_json"))
    return responder_ref in responders or responder_ref in set(str(value) for value in responders.values())


def _record_failed_callback(
    *,
    db: V4Database,
    callback_id: str,
    decision_id: str,
    delivery_id: str | None,
    responder_ref: str,
    submitted_option: str,
    normalized_option: str,
    raw_payload_hash: str,
    idempotency_key: str,
    error: str,
) -> dict[str, Any]:
    now = utc_now()
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO decision_callbacks(
              callback_id, decision_id, delivery_id, responder_ref, submitted_option,
              normalized_option, raw_payload_hash, state, error, idempotency_key,
              created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                callback_id,
                decision_id,
                delivery_id,
                responder_ref,
                submitted_option,
                normalized_option,
                raw_payload_hash,
                "callback_failed",
                error,
                idempotency_key,
                now,
                now,
            ),
        )
    return {"decision_id": decision_id, "callback_id": callback_id, "state": "callback_failed", "error": error, "idempotent": False}


def _apply_declared_effects(*, db: V4Database, decision_id: str, selected_option: str, now: str) -> None:
    rows = [
        {key: row[key] for key in row.keys()}
        for row in db.connection.execute("SELECT * FROM decision_links WHERE decision_id=?", (decision_id,))
    ]
    for row in rows:
        payload = _json_object(row.get("effect_payload_json"))
        applies_to = payload.get("on_option")
        if applies_to is not None and applies_to != selected_option:
            continue
        applied = False
        target_type = str(row.get("target_type") or "")
        if target_type == "work_item":
            db.upsert_work_item(
                work_item_id=str(row["target_id"]),
                state=_optional_string(payload.get("state")),
                owner_role=_optional_string(payload.get("owner_role")),
                next_action=_optional_string(payload.get("next_action")),
            )
            applied = True
        elif target_type == "handoff":
            applied = _apply_handoff_effect(db=db, row=row, payload=payload, now=now)
        elif target_type == "preflight":
            applied = _apply_preflight_effect(db=db, row=row, payload=payload)
        elif target_type == "artifact":
            applied = _apply_artifact_effect(db=db, row=row, payload=payload)
        elif target_type == "release":
            applied = _apply_release_effect(db=db, row=row, payload=payload, now=now)
        if applied:
            db.connection.execute(
                "UPDATE decision_links SET applied_at=? WHERE link_id=?",
                (now, row["link_id"]),
            )


def _apply_handoff_effect(*, db: V4Database, row: dict[str, Any], payload: dict[str, Any], now: str) -> bool:
    target_id = str(row.get("target_id") or "")
    existing = db.connection.execute("SELECT * FROM handoffs WHERE handoff_id=?", (target_id,)).fetchone()
    if existing is not None:
        assignments = []
        values: list[Any] = []
        for column in ("state", "status", "to_role", "reason", "next_action"):
            value = _optional_string(payload.get(column))
            if value is not None:
                assignments.append(f"{column}=?")
                values.append(value)
        if not assignments:
            return False
        assignments.append("updated_at=?")
        values.extend([now, target_id])
        db.connection.execute(f"UPDATE handoffs SET {', '.join(assignments)} WHERE handoff_id=?", values)
        return True
    from_role = _optional_string(payload.get("from_role"))
    to_role = _optional_string(payload.get("to_role"))
    reason = _optional_string(payload.get("reason")) or _optional_string(row.get("effect_summary"))
    if not from_role or not to_role or not reason:
        return False
    db.record_handoff(
        handoff_id=target_id or None,
        work_item_id=_optional_string(payload.get("work_item_id")),
        from_role=from_role,
        to_role=to_role,
        reason=reason,
        status=_optional_string(payload.get("status")) or _optional_string(payload.get("state")) or "open",
    )
    return True


def _apply_preflight_effect(*, db: V4Database, row: dict[str, Any], payload: dict[str, Any]) -> bool:
    work_item_id = _optional_string(payload.get("work_item_id"))
    role_id = _optional_string(payload.get("role_id"))
    role_instance_id = _optional_string(payload.get("role_instance_id"))
    service_name = _optional_string(payload.get("service_name"))
    probe_type = _optional_string(payload.get("probe_type"))
    check_name = _optional_string(payload.get("check_name"))
    status = _optional_string(payload.get("status"))
    message_id = _optional_string(payload.get("message_id"))
    if not all([work_item_id, role_id, role_instance_id, service_name, probe_type, check_name, status, message_id]):
        return False
    db.record_preflight_result(
        message_id=message_id,
        work_item_id=work_item_id,
        role_id=role_id,
        role_instance_id=role_instance_id,
        service_name=service_name,
        probe_type=probe_type,
        check_name=check_name,
        status=status,
        handoff_id=_optional_string(payload.get("handoff_id")),
        lifecycle_state=_optional_string(payload.get("lifecycle_state")),
        required_path=_optional_string(payload.get("required_path")),
        canonical_path=_optional_string(payload.get("canonical_path")),
        exit_code=payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
        stdout_excerpt=_optional_string(payload.get("stdout_excerpt")),
        stderr_excerpt=_optional_string(payload.get("stderr_excerpt")),
        diagnostic=payload.get("diagnostic") if isinstance(payload.get("diagnostic"), dict) else {},
        remediation=_optional_string(payload.get("remediation")) or "",
        preflight_id=_optional_string(row.get("target_id")),
    )
    return True


def _apply_artifact_effect(*, db: V4Database, row: dict[str, Any], payload: dict[str, Any]) -> bool:
    work_item_id = _optional_string(payload.get("work_item_id"))
    path = _optional_string(payload.get("path")) or _optional_string(row.get("target_id"))
    title = _optional_string(payload.get("title")) or _optional_string(row.get("effect_summary"))
    if not work_item_id or not path or not title:
        return False
    db.record_artifact(work_item_id=work_item_id, path=path, title=title)
    return True


def _apply_release_effect(*, db: V4Database, row: dict[str, Any], payload: dict[str, Any], now: str) -> bool:
    release_id = str(row.get("target_id") or "")
    work_item_id = _optional_string(payload.get("work_item_id"))
    status = _optional_string(payload.get("status"))
    deployment_result = _optional_string(payload.get("deployment_result")) or ""
    rollback_plan = _optional_string(payload.get("rollback_plan")) or ""
    if not release_id or not work_item_id or not status:
        return False
    db.connection.execute(
        """
        INSERT INTO releases(release_id, work_item_id, status, deployment_result, rollback_plan, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(release_id) DO UPDATE SET
          work_item_id=excluded.work_item_id,
          status=excluded.status,
          deployment_result=excluded.deployment_result,
          rollback_plan=excluded.rollback_plan,
          updated_at=excluded.updated_at
        """,
        (release_id, work_item_id, status, deployment_result, rollback_plan, now, now),
    )
    return True


def _insert_decision_link(*, db: V4Database, decision_id: str, effect: dict[str, Any], applied_at: str | None) -> None:
    db.connection.execute(
        """
        INSERT INTO decision_links(
          link_id, decision_id, target_type, target_id, link_type,
          effect_summary, effect_payload_json, applied_at, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            f"link-{uuid4().hex}",
            decision_id,
            str(effect.get("target_type") or ""),
            str(effect.get("target_id") or ""),
            str(effect.get("link_type") or "declared_effect"),
            str(effect.get("effect_summary") or ""),
            json.dumps(effect.get("effect_payload") or {}, sort_keys=True),
            applied_at,
            utc_now(),
        ),
    )


def _sla_state(sla_due_at: str | None) -> str:
    return "pending" if sla_due_at else "pending"


def _sla_state_for_due_at(
    *,
    due_at: datetime,
    now: datetime,
    due_soon_seconds: int,
    escalate_after_seconds: int,
) -> str:
    if now >= due_at + timedelta(seconds=escalate_after_seconds):
        return "escalated"
    if now >= due_at:
        return "overdue"
    if due_at - now <= timedelta(seconds=due_soon_seconds):
        return "due_soon"
    return "pending"


def _sla_copy(decision: dict[str, Any]) -> str:
    due = decision.get("sla_due_at")
    state = decision.get("sla_state") or "pending"
    if due:
        return f"{state}; due by {due}; expiry does not automatically resolve."
    return f"{state}; no automatic approval, denial, or release on expiry."


def _effect_warning(decision: dict[str, Any]) -> str:
    effects = _json_list(decision.get("link_effects_json"))
    if not effects:
        return "Your response records the decision and creates follow-up visibility; it will not automatically advance the work."
    summaries = [str(item.get("effect_summary")) for item in effects if isinstance(item, dict) and item.get("effect_summary")]
    if not summaries:
        return "Your response records the decision and creates follow-up visibility; it will not automatically advance the work."
    return "Selecting a configured option will update: " + "; ".join(summaries)


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list[Any]:
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _latest_update_target(*, db: V4Database, decision_id: str, delivery_id: str | None) -> dict[str, Any] | None:
    if delivery_id:
        row = db.connection.execute(
            "SELECT * FROM decision_card_deliveries WHERE delivery_id=? AND decision_id=?",
            (delivery_id, decision_id),
        ).fetchone()
    else:
        row = db.connection.execute(
            """
            SELECT * FROM decision_card_deliveries
            WHERE decision_id=?
              AND channel='teams'
              AND state IN ('delivered','update_failed')
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (decision_id,),
        ).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _latest_conversation_delivery(*, db: V4Database, decision_id: str) -> dict[str, Any] | None:
    row = db.connection.execute(
        """
        SELECT * FROM decision_card_deliveries
        WHERE decision_id=?
          AND channel='teams'
          AND conversation_ref IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (decision_id,),
    ).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _conversation_activity(value: object) -> dict[str, object]:
    parsed = _json_object(value)
    if not parsed:
        raise DecisionRecordError("Teams conversation_ref is missing or is not a JSON activity reference")
    return parsed


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise DecisionRecordError("timestamp is required")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
