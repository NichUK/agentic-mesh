from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.events import EventDraft
from agentic_mesh_v5.events import EventUnitOfWork
from agentic_mesh_v5.events import OutboundDraft
from agentic_mesh_v5.routing import RouteDraft
from agentic_mesh_v5.routing import Router
from agentic_mesh_v5.routing import RoutingConflict
from agentic_mesh_v5.routing import RoutingNotFound


Stage = Literal["technical", "pm_correction", "recovery"]
Outcome = Literal["succeeded", "failed"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_STORE_ERROR = "reliability policy operation failed"
_PM_ROLE = "project-manager"


class ReliabilityError(DatabaseError):
    pass


class ReliabilityNotFound(ReliabilityError):
    pass


class ReliabilityConflict(ReliabilityError):
    pass


@dataclass(frozen=True, slots=True)
class FailureIncident:
    project_id: str
    incident_id: str
    work_item_id: str
    owner_role_id: str
    failure_category: str
    safe_summary: str
    source_ref: str
    status: str
    next_stage: str
    next_attempt_number: int | None
    started_by: str
    started_at: str
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class FailureAttempt:
    project_id: str
    attempt_id: str
    incident_id: str
    ordinal: int
    stage: str
    stage_attempt: int
    outcome: str
    correction_instruction: str | None
    actor_id: str
    evidence: dict[str, Any]
    recorded_at: str


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    project_id: str
    recovery_request_id: str
    incident_id: str
    work_item_id: str
    exact_goal: str
    status: str
    requested_at: str
    resolved_at: str | None
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReliabilityStatus:
    incident: FailureIncident
    attempts: tuple[FailureAttempt, ...]
    recovery_request: RecoveryRequest | None


class ReliabilityStore:
    """Durable, fixed 3/3/1 retry and recovery policy."""

    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._router = Router(database_url)

    def start(
        self,
        *,
        project_id: str,
        work_item_id: str,
        idempotency_key: str,
        failure_category: str,
        safe_summary: str,
        source_ref: str,
        actor_id: str,
    ) -> ReliabilityStatus:
        project_id = _identifier(project_id, "project_id")
        work_item_id = _identifier(work_item_id, "work_item_id")
        idempotency_key = _identifier(idempotency_key, "idempotency_key")
        failure_category = _identifier(failure_category, "failure_category")
        safe_summary = _bounded(safe_summary, "safe_summary", 1000)
        source_ref = _bounded(source_ref, "source_ref", 1000)
        actor_id = _bounded(actor_id, "actor_id", 512)
        request = {
            "project_id": project_id,
            "work_item_id": work_item_id,
            "failure_category": failure_category,
            "safe_summary": safe_summary,
            "source_ref": source_ref,
        }
        fingerprint = _fingerprint(request)
        incident_id = _derived_id("incident", project_id, idempotency_key)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    work = connection.execute(
                        f"""
                        SELECT status, assigned_role_id
                        FROM {SCHEMA}.work_items
                        WHERE project_id = %s AND work_item_id = %s
                        FOR UPDATE
                        """,
                        (project_id, work_item_id),
                    ).fetchone()
                    if work is None:
                        raise ReliabilityNotFound("work item not found")
                    existing = connection.execute(
                        f"""
                        SELECT incident_id, request_fingerprint
                        FROM {SCHEMA}.failure_incidents
                        WHERE project_id = %s AND idempotency_key = %s
                        """,
                        (project_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if existing[1] != fingerprint:
                            raise ReliabilityConflict(
                                "idempotency key is already used by different failure work"
                            )
                        return self._status(connection, project_id, existing[0])
                    if work[0] != "active" or work[1] is None:
                        raise ReliabilityConflict(
                            "only active work with an owning role can start recovery"
                        )
                    if connection.execute(
                        f"""
                        SELECT 1 FROM {SCHEMA}.failure_incidents
                        WHERE project_id = %s AND work_item_id = %s
                          AND status IN ('active', 'terminal_eligible')
                        """,
                        (project_id, work_item_id),
                    ).fetchone() is not None:
                        raise ReliabilityConflict(
                            "work item already has an unresolved failure incident"
                        )
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.failure_incidents
                            (project_id, incident_id, work_item_id, owner_role_id,
                             idempotency_key, request_fingerprint,
                             failure_category, safe_summary, source_ref,
                             next_stage, next_attempt_number, started_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                'technical', 1, %s)
                        """,
                        (
                            project_id,
                            incident_id,
                            work_item_id,
                            work[1],
                            idempotency_key,
                            fingerprint,
                            failure_category,
                            safe_summary,
                            source_ref,
                            actor_id,
                        ),
                    )
                    self._route(
                        connection,
                        project_id=project_id,
                        work_item_id=work_item_id,
                        target_role_id=work[1],
                        incident_id=incident_id,
                        stage="technical",
                        attempt_number=1,
                        safe_summary=safe_summary,
                        source_ref=source_ref,
                    )
                    self._audit(
                        connection,
                        project_id,
                        actor_id,
                        "reliability.incident_started",
                        incident_id,
                        {
                            "work_item_id": work_item_id,
                            "next_stage": "technical",
                            "next_attempt_number": 1,
                        },
                    )
                    self._journal(
                        connection,
                        project_id=project_id,
                        work_item_id=work_item_id,
                        actor_id=actor_id,
                        correlation_id=idempotency_key,
                        incident_id=incident_id,
                        event_type="reliability.incident_started",
                        payload={
                            "incident_id": incident_id,
                            "next_stage": "technical",
                            "next_attempt_number": 1,
                            "failure_category": failure_category,
                        },
                    )
                    return self._status(connection, project_id, incident_id)
        except ReliabilityError:
            raise
        except (RoutingNotFound, RoutingConflict):
            raise ReliabilityConflict("required continuation route is unavailable") from None
        except Exception:
            raise ReliabilityError(_STORE_ERROR) from None

    def record_attempt(
        self,
        *,
        project_id: str,
        work_item_id: str,
        incident_id: str,
        attempt_id: str,
        stage: Stage,
        attempt_number: int,
        outcome: Outcome,
        actor_id: str,
        correction_instruction: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> ReliabilityStatus:
        project_id = _identifier(project_id, "project_id")
        work_item_id = _identifier(work_item_id, "work_item_id")
        incident_id = _identifier(incident_id, "incident_id")
        attempt_id = _identifier(attempt_id, "attempt_id")
        actor_id = _bounded(actor_id, "actor_id", 512)
        if stage not in {"technical", "pm_correction", "recovery"}:
            raise ValueError("stage is invalid")
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("outcome is invalid")
        maximum = 1 if stage == "recovery" else 3
        if type(attempt_number) is not int or not 1 <= attempt_number <= maximum:
            raise ValueError("attempt_number is invalid")
        correction = None
        correction_digest = None
        if stage == "pm_correction":
            correction = _bounded(
                correction_instruction, "correction_instruction", 2000
            )
            correction_digest = hashlib.sha256(correction.encode("utf-8")).hexdigest()
        elif correction_instruction is not None:
            raise ValueError("correction_instruction is only valid for PM correction")
        evidence_value = _json_object(evidence or {}, "evidence")
        request = {
            "project_id": project_id,
            "work_item_id": work_item_id,
            "incident_id": incident_id,
            "stage": stage,
            "attempt_number": attempt_number,
            "outcome": outcome,
            "correction_instruction": correction,
            "evidence": evidence_value,
        }
        fingerprint = _fingerprint(request)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    work = connection.execute(
                        f"""
                        SELECT status FROM {SCHEMA}.work_items
                        WHERE project_id = %s AND work_item_id = %s
                        FOR UPDATE
                        """,
                        (project_id, work_item_id),
                    ).fetchone()
                    if work is None:
                        raise ReliabilityNotFound("work item not found")
                    incident = connection.execute(
                        f"""
                        SELECT work_item_id, owner_role_id, status, next_stage,
                               next_attempt_number, safe_summary, source_ref
                        FROM {SCHEMA}.failure_incidents
                        WHERE project_id = %s AND incident_id = %s
                        FOR UPDATE
                        """,
                        (project_id, incident_id),
                    ).fetchone()
                    if incident is None or incident[0] != work_item_id:
                        raise ReliabilityNotFound("failure incident not found")
                    existing = connection.execute(
                        f"""
                        SELECT request_fingerprint FROM {SCHEMA}.failure_attempts
                        WHERE project_id = %s AND attempt_id = %s
                        """,
                        (project_id, attempt_id),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != fingerprint:
                            raise ReliabilityConflict(
                                "attempt id is already used by a different result"
                            )
                        return self._status(connection, project_id, incident_id)
                    if incident[2] != "active":
                        raise ReliabilityConflict("failure incident is not accepting attempts")
                    if work[0] != "active":
                        raise ReliabilityConflict("work item is not active for retry")
                    if (incident[3], incident[4]) != (stage, attempt_number):
                        raise ReliabilityConflict(
                            "attempt does not match the required retry stage"
                        )
                    if correction_digest is not None and connection.execute(
                        f"""
                        SELECT 1 FROM {SCHEMA}.failure_attempts
                        WHERE project_id = %s AND incident_id = %s
                          AND correction_digest = %s
                        """,
                        (project_id, incident_id, correction_digest),
                    ).fetchone() is not None:
                        raise ReliabilityConflict(
                            "PM correction instruction must be distinct"
                        )
                    ordinal = _ordinal(stage, attempt_number)
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.failure_attempts
                            (project_id, attempt_id, incident_id, ordinal, stage,
                             stage_attempt, outcome, correction_instruction,
                             correction_digest, actor_id, evidence,
                             request_fingerprint)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            project_id,
                            attempt_id,
                            incident_id,
                            ordinal,
                            stage,
                            attempt_number,
                            outcome,
                            correction,
                            correction_digest,
                            actor_id,
                            Jsonb(evidence_value),
                            fingerprint,
                        ),
                    )
                    if outcome == "succeeded":
                        self._succeed(
                            connection,
                            project_id,
                            incident_id,
                            work_item_id,
                            incident[1],
                            stage,
                            evidence_value,
                            incident[5],
                            incident[6],
                        )
                        action = "reliability.incident_recovered"
                    else:
                        action = self._advance_failed(
                            connection,
                            project_id=project_id,
                            incident_id=incident_id,
                            work_item_id=work_item_id,
                            owner_role_id=incident[1],
                            stage=stage,
                            attempt_number=attempt_number,
                            safe_summary=incident[5],
                            source_ref=incident[6],
                            evidence=evidence_value,
                        )
                    self._audit(
                        connection,
                        project_id,
                        actor_id,
                        action,
                        incident_id,
                        {
                            "attempt_id": attempt_id,
                            "stage": stage,
                            "attempt_number": attempt_number,
                            "outcome": outcome,
                        },
                    )
                    self._journal(
                        connection,
                        project_id=project_id,
                        work_item_id=work_item_id,
                        actor_id=actor_id,
                        correlation_id=attempt_id,
                        incident_id=incident_id,
                        event_type="reliability.attempt_recorded",
                        payload={
                            "incident_id": incident_id,
                            "attempt_id": attempt_id,
                            "stage": stage,
                            "attempt_number": attempt_number,
                            "outcome": outcome,
                            "result": action,
                        },
                    )
                    return self._status(connection, project_id, incident_id)
        except ReliabilityError:
            raise
        except (RoutingNotFound, RoutingConflict):
            raise ReliabilityConflict("required continuation route is unavailable") from None
        except Exception:
            raise ReliabilityError(_STORE_ERROR) from None

    def status(
        self, project_id: str, work_item_id: str, incident_id: str | None = None
    ) -> ReliabilityStatus:
        project_id = _identifier(project_id, "project_id")
        work_item_id = _identifier(work_item_id, "work_item_id")
        if incident_id is not None:
            incident_id = _identifier(incident_id, "incident_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                if incident_id is None:
                    row = connection.execute(
                        f"""
                        SELECT incident_id FROM {SCHEMA}.failure_incidents
                        WHERE project_id = %s AND work_item_id = %s
                        ORDER BY started_at DESC, incident_id DESC LIMIT 1
                        """,
                        (project_id, work_item_id),
                    ).fetchone()
                    if row is None:
                        raise ReliabilityNotFound("failure incident not found")
                    incident_id = row[0]
                selected = self._status(connection, project_id, incident_id)
                if selected.incident.work_item_id != work_item_id:
                    raise ReliabilityNotFound("failure incident not found")
                return selected
        except ReliabilityError:
            raise
        except Exception:
            raise ReliabilityError(_STORE_ERROR) from None

    def _advance_failed(
        self,
        connection,
        *,
        project_id: str,
        incident_id: str,
        work_item_id: str,
        owner_role_id: str,
        stage: Stage,
        attempt_number: int,
        safe_summary: str,
        source_ref: str,
        evidence: dict[str, Any],
    ) -> str:
        if stage == "technical":
            next_stage = "technical" if attempt_number < 3 else "pm_correction"
            next_number = attempt_number + 1 if attempt_number < 3 else 1
            target_role = owner_role_id if next_stage == "technical" else _PM_ROLE
            self._set_next(connection, project_id, incident_id, next_stage, next_number)
            self._route(
                connection,
                project_id=project_id,
                work_item_id=work_item_id,
                target_role_id=target_role,
                incident_id=incident_id,
                stage=next_stage,
                attempt_number=next_number,
                safe_summary=safe_summary,
                source_ref=source_ref,
            )
            return "reliability.retry_routed"
        if stage == "pm_correction" and attempt_number < 3:
            self._set_next(
                connection, project_id, incident_id, "pm_correction", attempt_number + 1
            )
            self._route(
                connection,
                project_id=project_id,
                work_item_id=work_item_id,
                target_role_id=_PM_ROLE,
                incident_id=incident_id,
                stage="pm_correction",
                attempt_number=attempt_number + 1,
                safe_summary=safe_summary,
                source_ref=source_ref,
            )
            return "reliability.pm_correction_routed"
        if stage == "pm_correction":
            recovery_id = _derived_id("recovery", project_id, incident_id)
            goal = (
                f"Restore Agentic Mesh work item {work_item_id} after its technical "
                f"retries and PM corrections were exhausted. Resolve this verified "
                f"failure: {safe_summary} Source: {source_ref}. Leave the Mesh in a "
                "verified working state and record the repair evidence."
            )
            connection.execute(
                f"""
                INSERT INTO {SCHEMA}.recovery_requests
                    (project_id, recovery_request_id, incident_id, work_item_id,
                     exact_goal)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (project_id, recovery_id, incident_id, work_item_id, goal),
            )
            self._set_next(connection, project_id, incident_id, "recovery", 1)
            return "reliability.recovery_requested"
        connection.execute(
            f"""
            UPDATE {SCHEMA}.recovery_requests
            SET status = 'failed', resolved_at = clock_timestamp(), evidence = %s
            WHERE project_id = %s AND incident_id = %s AND status = 'pending'
            """,
            (Jsonb(evidence), project_id, incident_id),
        )
        connection.execute(
            f"""
            UPDATE {SCHEMA}.failure_incidents
            SET status = 'terminal_eligible', next_stage = 'none',
                next_attempt_number = NULL
            WHERE project_id = %s AND incident_id = %s
            """,
            (project_id, incident_id),
        )
        return "reliability.terminal_error_eligible"

    def _succeed(
        self,
        connection,
        project_id: str,
        incident_id: str,
        work_item_id: str,
        owner_role_id: str,
        stage: Stage,
        evidence: dict[str, Any],
        safe_summary: str,
        source_ref: str,
    ) -> None:
        if stage == "recovery":
            connection.execute(
                f"""
                UPDATE {SCHEMA}.recovery_requests
                SET status = 'succeeded', resolved_at = clock_timestamp(), evidence = %s
                WHERE project_id = %s AND incident_id = %s AND status = 'pending'
                """,
                (Jsonb(evidence), project_id, incident_id),
            )
            self._route(
                connection,
                project_id=project_id,
                work_item_id=work_item_id,
                target_role_id=owner_role_id,
                incident_id=incident_id,
                stage="recovery",
                attempt_number=1,
                safe_summary=safe_summary,
                source_ref=source_ref,
                resumed=True,
            )
        connection.execute(
            f"""
            UPDATE {SCHEMA}.failure_incidents
            SET status = 'recovered', next_stage = 'none',
                next_attempt_number = NULL, resolved_at = clock_timestamp()
            WHERE project_id = %s AND incident_id = %s
            """,
            (project_id, incident_id),
        )

    @staticmethod
    def _set_next(connection, project_id, incident_id, stage, number) -> None:
        connection.execute(
            f"""
            UPDATE {SCHEMA}.failure_incidents
            SET next_stage = %s, next_attempt_number = %s
            WHERE project_id = %s AND incident_id = %s AND status = 'active'
            """,
            (stage, number, project_id, incident_id),
        )

    def _route(
        self,
        connection,
        *,
        project_id: str,
        work_item_id: str,
        target_role_id: str,
        incident_id: str,
        stage: str,
        attempt_number: int,
        safe_summary: str,
        source_ref: str,
        resumed: bool = False,
    ) -> None:
        suffix = "resume" if resumed else f"{stage}-{attempt_number}"
        if resumed:
            required_action = "Resume normal work after verified independent recovery."
        elif stage == "pm_correction":
            required_action = (
                "Produce and execute one distinct, minimal corrective instruction; "
                "do not repeat an earlier correction or over-engineer the repair."
            )
        else:
            required_action = (
                "Retry the same bounded operation using the recorded evidence."
            )
        self._router.route_in_transaction(
            connection,
            RouteDraft(
                project_id=project_id,
                work_item_id=work_item_id,
                target_role_id=target_role_id,
                idempotency_key=f"reliability:{incident_id}:{suffix}",
                priority=200,
                payload={
                    "reliability": {
                        "incident_id": incident_id,
                        "stage": stage,
                        "attempt_number": attempt_number,
                        "safe_summary": safe_summary,
                        "source_ref": source_ref,
                        "resumed_after_recovery": resumed,
                        "required_action": required_action,
                    }
                },
            ),
        )

    @staticmethod
    def _status(connection, project_id: str, incident_id: str) -> ReliabilityStatus:
        row = connection.execute(
            f"""
            SELECT project_id, incident_id, work_item_id, owner_role_id,
                   failure_category, safe_summary, source_ref, status,
                   next_stage, next_attempt_number, started_by,
                   started_at::text, resolved_at::text
            FROM {SCHEMA}.failure_incidents
            WHERE project_id = %s AND incident_id = %s
            """,
            (project_id, incident_id),
        ).fetchone()
        if row is None:
            raise ReliabilityNotFound("failure incident not found")
        attempts = tuple(
            FailureAttempt(*item)
            for item in connection.execute(
                f"""
                SELECT project_id, attempt_id, incident_id, ordinal, stage,
                       stage_attempt, outcome, correction_instruction,
                       actor_id, evidence, recorded_at::text
                FROM {SCHEMA}.failure_attempts
                WHERE project_id = %s AND incident_id = %s
                ORDER BY ordinal
                """,
                (project_id, incident_id),
            ).fetchall()
        )
        recovery = connection.execute(
            f"""
            SELECT project_id, recovery_request_id, incident_id, work_item_id,
                   exact_goal, status, requested_at::text, resolved_at::text,
                   evidence
            FROM {SCHEMA}.recovery_requests
            WHERE project_id = %s AND incident_id = %s
            """,
            (project_id, incident_id),
        ).fetchone()
        return ReliabilityStatus(
            FailureIncident(*row),
            attempts,
            None if recovery is None else RecoveryRequest(*recovery),
        )

    @staticmethod
    def _audit(connection, project_id, actor_id, action, object_id, details) -> None:
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.audit_records
                (scope, project_id, actor_id, action, object_type, object_id, details)
            VALUES ('project', %s, %s, %s, 'failure-incident', %s, %s)
            """,
            (project_id, actor_id, action, object_id, Jsonb(details)),
        )

    @staticmethod
    def _journal(
        connection,
        *,
        project_id,
        work_item_id,
        actor_id,
        correlation_id,
        incident_id,
        event_type,
        payload,
    ) -> None:
        EventUnitOfWork(connection).append(
            EventDraft(
                project_id=project_id,
                work_item_id=work_item_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                aggregate_type="failure-incident",
                aggregate_id=incident_id,
                event_type=event_type,
                payload=payload,
            ),
            (OutboundDraft("reliability.changed", payload),),
        )


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    selected = value.strip()
    if _IDENTIFIER.fullmatch(selected) is None:
        raise ValueError(f"{field_name} is invalid")
    return selected


def _bounded(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    selected = value.strip()
    if not selected or len(selected) > maximum:
        raise ValueError(f"{field_name} is invalid")
    return selected


def _json_object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    selected = dict(value)
    try:
        json.dumps(selected, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must contain JSON values") from None
    return selected


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _derived_id(prefix: str, project_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{project_id}\x00{key}".encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _ordinal(stage: Stage, attempt_number: int) -> int:
    if stage == "technical":
        return attempt_number
    if stage == "pm_correction":
        return 3 + attempt_number
    return 7
