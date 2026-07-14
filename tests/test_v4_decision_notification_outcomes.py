from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.decision_records import DecisionRecordError
from agentic_mesh_v4.decision_records import DecisionRequest
from agentic_mesh_v4.decision_records import reconcile_resolved_decision_notifications
from agentic_mesh_v4.decision_records import record_decision_notification_recovery_summary
from agentic_mesh_v4.decision_records import request_decision
from agentic_mesh_v4.decision_records import resolve_decision
from agentic_mesh_v4.decision_records import set_decision_notification_outcome
from v4_postgres import make_v4_db
from v4_postgres import make_v4_db_url


def _request(*, requester_role: str = "solution-architect", suffix: str = "one") -> DecisionRequest:
    return DecisionRequest(
        work_item_id=f"work-notification-{suffix}",
        requester_role=requester_role,
        owner_role="project-manager",
        authority_label="Sponsor",
        authorized_responders={"sponsor": "synthetic-sponsor"},
        decision_type="approval",
        title=f"Synthetic decision {suffix}",
        question="Approve this synthetic decision?",
        options=("approved", "changes_requested"),
        recommended_option="approved",
    )


def _resolve(db: V4Database, *, requester_role: str = "solution-architect", suffix: str = "one") -> tuple[str, str]:
    requested = request_decision(db=db, request=_request(requester_role=requester_role, suffix=suffix), deliver=False)
    decision_id = str(requested["decision_id"])
    resolved = resolve_decision(
        db=db,
        decision_id=decision_id,
        responder_ref="synthetic-sponsor",
        selected_option="approved",
        idempotency_key=f"synthetic-resolution-{suffix}",
    )
    return decision_id, str(resolved["requester_notification"]["message_id"])


def test_enqueue_message_journals_only_real_insert_then_truthful_no_transition() -> None:
    db = make_v4_db()
    try:
        message_id = "msg-truthful-enqueue"
        db.enqueue_message(
            target_role="solution-architect",
            text="first",
            source="decision_callback",
            message_id=message_id,
            correlation_id="corr-first",
        )
        db.enqueue_message(
            target_role="project-manager",
            text="second must not replace first",
            source="teams",
            message_id=message_id,
            correlation_id="corr-second",
        )

        message = db.connection.execute(
            "SELECT target_role, source, text, state, correlation_id FROM message_queue WHERE message_id=?",
            (message_id,),
        ).fetchone()
        journals = db.connection.execute(
            "SELECT stage, status, correlation_id, summary FROM message_journal WHERE message_id=? ORDER BY created_at",
            (message_id,),
        ).fetchall()

        assert dict(message) == {
            "target_role": "solution-architect",
            "source": "decision_callback",
            "text": "first",
            "state": "queued",
            "correlation_id": "corr-first",
        }
        assert [(row["stage"], row["status"]) for row in journals] == [
            ("received", "queued"),
            ("enqueue", "existing_no_transition"),
        ]
        assert journals[1]["correlation_id"] == "corr-first"
        assert "no queue transition occurred" in journals[1]["summary"]
    finally:
        db.close()


def test_retained_terminal_outcome_is_idempotent_and_watchdog_excluded() -> None:
    db = make_v4_db()
    try:
        decision_id, message_id = _resolve(db)
        terminal = set_decision_notification_outcome(
            db=db,
            decision_id=decision_id,
            outcome="do_not_retry",
            source="platform_containment",
            reason="terminal provider failure retained for evidence",
            actor_ref="platform-engineer",
            expected_version=1,
            idempotency_key=f"do-not-retry:{decision_id}",
        )
        repeated = set_decision_notification_outcome(
            db=db,
            decision_id=decision_id,
            outcome="do_not_retry",
            source="platform_containment",
            reason="terminal provider failure retained for evidence",
            actor_ref="platform-engineer",
            expected_version=1,
            idempotency_key=f"do-not-retry:{decision_id}",
        )
        before = db.connection.execute(
            "SELECT state, created_at, updated_at FROM message_queue WHERE message_id=?",
            (message_id,),
        ).fetchone()

        first = reconcile_resolved_decision_notifications(db=db)
        second = reconcile_resolved_decision_notifications(db=db)
        after = db.connection.execute(
            "SELECT state, created_at, updated_at FROM message_queue WHERE message_id=?",
            (message_id,),
        ).fetchone()

        assert terminal["version"] == 2
        assert repeated == {**terminal, "idempotent": True}
        assert first["terminal_excluded"] == second["terminal_excluded"] == 1
        assert first["queued"] == second["queued"] == 0
        assert dict(after) == dict(before)
        assert db.connection.execute(
            "SELECT COUNT(*) AS count FROM decision_notification_outcome_events WHERE decision_id=?",
            (decision_id,),
        ).fetchone()["count"] == 2
    finally:
        db.close()


def test_removed_terminal_row_survives_process_restart_without_second_turn_or_effect() -> None:
    database_url = make_v4_db_url()
    db = V4Database(database_url)
    db.migrate()
    decision_id, message_id = _resolve(db, suffix="restart")
    set_decision_notification_outcome(
        db=db,
        decision_id=decision_id,
        outcome="do_not_retry",
        source="qa_fixture",
        reason="prove queue retention is not retry authority",
        actor_ref="qa-engineer",
        expected_version=1,
        idempotency_key=f"do-not-retry:{decision_id}",
    )
    db.connection.execute("DELETE FROM message_queue WHERE message_id=?", (message_id,))
    counts_before = _effect_counts(db)
    db.close()

    restarted = V4Database(database_url)
    try:
        restarted.migrate()
        for _ in range(3):
            result = reconcile_resolved_decision_notifications(db=restarted)
            assert result["queued"] == 0
            assert result["terminal_excluded"] == 1
        assert restarted.connection.execute(
            "SELECT 1 FROM message_queue WHERE message_id=?",
            (message_id,),
        ).fetchone() is None
        assert _effect_counts(restarted) == counts_before
    finally:
        restarted.close()


def test_one_recovered_route_owner_summary_fences_three_notifications_without_resend() -> None:
    db = make_v4_db()
    provider_calls: list[str] = []
    try:
        pairs = [_resolve(db, suffix=f"summary-{index}") for index in range(3)]
        summary = record_decision_notification_recovery_summary(
            db=db,
            summary_id="summary-project-manager-1784019456579",
            route_owner_role="project-manager",
            activity_ref="1784019456579",
            source="project_manager_recovery",
            reason="one consolidated summary already delivered",
            actor_ref="project-manager",
            idempotency_key="summary-already-recovered:1784019456579",
        )
        repeated_summary = record_decision_notification_recovery_summary(
            db=db,
            summary_id="summary-project-manager-1784019456579",
            route_owner_role="project-manager",
            activity_ref="1784019456579",
            source="project_manager_recovery",
            reason="one consolidated summary already delivered",
            actor_ref="project-manager",
            idempotency_key="summary-already-recovered:1784019456579",
        )
        for decision_id, message_id in pairs:
            set_decision_notification_outcome(
                db=db,
                decision_id=decision_id,
                outcome="summary_already_recovered",
                source="project_manager_recovery",
                reason="covered by the route-owner consolidated summary",
                actor_ref="project-manager",
                expected_version=1,
                summary_id=str(summary["summary_id"]),
                idempotency_key=f"summary-already-recovered:{decision_id}",
            )
            db.connection.execute("DELETE FROM message_queue WHERE message_id=?", (message_id,))

        for _ in range(3):
            result = reconcile_resolved_decision_notifications(db=db)
            assert result["queued"] == 0
            assert result["terminal_excluded"] == 3

        assert repeated_summary == {"summary_id": summary["summary_id"], "idempotent": True}
        assert db.connection.execute(
            "SELECT COUNT(*) AS count FROM decision_notification_recovery_summaries"
        ).fetchone()["count"] == 1
        assert db.connection.execute(
            "SELECT COUNT(DISTINCT summary_id) AS count FROM decision_notification_outcomes"
        ).fetchone()["count"] == 1
        assert db.connection.execute("SELECT COUNT(*) AS count FROM message_queue").fetchone()["count"] == 0
        assert _effect_counts(db) == {"turns": 0, "safe_outputs": 0, "handoffs": 0, "memories": 0}
        assert provider_calls == []
    finally:
        db.close()


def test_notification_outcome_cas_and_idempotency_conflicts_fail_closed() -> None:
    db = make_v4_db()
    try:
        decision_id, _ = _resolve(db, suffix="cas")
        set_decision_notification_outcome(
            db=db,
            decision_id=decision_id,
            outcome="do_not_retry",
            source="qa_fixture",
            reason="terminal",
            actor_ref="qa-engineer",
            expected_version=1,
            idempotency_key=f"terminal:{decision_id}",
        )
        with pytest.raises(DecisionRecordError, match="CAS conflict"):
            set_decision_notification_outcome(
                db=db,
                decision_id=decision_id,
                outcome="recover_if_missing",
                source="watchdog",
                reason="stale retry request",
                actor_ref="watchdog",
                expected_version=1,
                idempotency_key=f"stale:{decision_id}",
            )
        with pytest.raises(DecisionRecordError, match="different fields"):
            set_decision_notification_outcome(
                db=db,
                decision_id=decision_id,
                outcome="do_not_retry",
                source="qa_fixture",
                reason="changed reason",
                actor_ref="qa-engineer",
                expected_version=1,
                idempotency_key=f"terminal:{decision_id}",
            )
    finally:
        db.close()


def test_resolved_decision_without_authoritative_policy_fails_closed() -> None:
    db = make_v4_db()
    try:
        requested = request_decision(db=db, request=_request(suffix="missing-policy"), deliver=False)
        decision_id = str(requested["decision_id"])
        db.connection.execute(
            "UPDATE decision_records SET status='resolved', resolved_at=updated_at WHERE decision_id=?",
            (decision_id,),
        )

        result = reconcile_resolved_decision_notifications(db=db)

        assert result["checked"] == 0
        assert result["queued"] == 0
        assert result["policy_missing"] == 1
        assert db.connection.execute("SELECT COUNT(*) AS count FROM message_queue").fetchone()["count"] == 0
    finally:
        db.close()


def test_concurrent_notification_outcome_cas_has_one_winner() -> None:
    database_url = make_v4_db_url()
    setup = V4Database(database_url)
    setup.migrate()
    decision_id, _ = _resolve(setup, suffix="concurrent")
    setup.close()
    barrier = Barrier(2)

    def attempt(label: str) -> str:
        connection = V4Database(database_url)
        try:
            barrier.wait()
            set_decision_notification_outcome(
                db=connection,
                decision_id=decision_id,
                outcome="do_not_retry",
                source="concurrency_fixture",
                reason=f"concurrent terminal request {label}",
                actor_ref=f"qa-{label}",
                expected_version=1,
                idempotency_key=f"concurrent:{decision_id}:{label}",
            )
            return "updated"
        except DecisionRecordError as exc:
            assert "CAS conflict" in str(exc)
            return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(attempt, ("a", "b")))

    verified = V4Database(database_url)
    try:
        assert results == ["conflict", "updated"]
        current = verified.connection.execute(
            "SELECT outcome, version FROM decision_notification_outcomes WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        assert dict(current) == {"outcome": "do_not_retry", "version": 2}
        assert verified.connection.execute(
            "SELECT COUNT(*) AS count FROM decision_notification_outcome_events WHERE decision_id=?",
            (decision_id,),
        ).fetchone()["count"] == 2
    finally:
        verified.close()


def _effect_counts(db: V4Database) -> dict[str, int]:
    return {
        "turns": int(db.connection.execute("SELECT COUNT(*) AS count FROM codex_turns").fetchone()["count"]),
        "safe_outputs": int(db.connection.execute("SELECT COUNT(*) AS count FROM safe_output_calls").fetchone()["count"]),
        "handoffs": int(db.connection.execute("SELECT COUNT(*) AS count FROM handoffs").fetchone()["count"]),
        "memories": int(db.connection.execute("SELECT COUNT(*) AS count FROM project_memory").fetchone()["count"]),
    }
