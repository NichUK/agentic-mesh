from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from typing import Protocol
import uuid

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.api_auth import Principal
from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.events import EventDraft, EventUnitOfWork, OutboundDraft
from agentic_mesh_v5.progress import reject_sensitive_content
from agentic_mesh_v5.reliability import ReliabilityStore
from agentic_mesh_v5.tool_profiles import ToolProfile, ToolProfileRegistry


DEFAULT_TIME_LIMIT_MINUTES = 120
DEFAULT_USAGE_LIMIT = 200_000
DEFAULT_LEASE_SECONDS = 60
LAUNCHER_ID = "recovery-supervisor"
RECOVERY_SCOPE = "recovery:execute"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_STORE_ERROR = "recovery supervisor operation failed"


class RecoverySupervisorError(DatabaseError):
    pass


class RecoveryAuthorizationError(RecoverySupervisorError):
    pass


class RecoveryConflict(RecoverySupervisorError):
    pass


class RecoveryExecutionError(RecoverySupervisorError):
    pass


@dataclass(frozen=True, slots=True)
class RecoveryRun:
    project_id: str
    recovery_request_id: str
    incident_id: str
    work_item_id: str
    exact_goal: str
    run_id: str
    status: str
    claim_count: int
    owner_id: str
    lease_token: str
    claimed_at: str
    lease_expires_at: str
    deadline_at: str
    time_limit_minutes: int
    usage_limit: int
    tool_profile_reference: str
    tool_profile_digest: str
    outcome: str | None
    safe_summary: str | None
    verification_ref: str | None
    usage_used: int | None
    reported_at: str | None
    result_applied_at: str | None

    def to_safe_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("lease_token")
        return value


@dataclass(frozen=True, slots=True)
class RecoveryJob:
    project_id: str
    recovery_request_id: str
    incident_id: str
    work_item_id: str
    run_id: str
    exact_goal: str
    deadline_at: str
    usage_limit: int
    tool_profile_reference: str
    tool_profile_digest: str
    image: str
    mount_references: tuple[str, ...]
    credential_references: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "mount_references": list(self.mount_references),
            "credential_references": list(self.credential_references),
        }


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    outcome: str
    safe_summary: str
    verification_ref: str
    usage_used: int


@dataclass(frozen=True, slots=True)
class RecoveryExecution:
    status: str
    reconciled_results: int
    run: RecoveryRun | None

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reconciled_results": self.reconciled_results,
            "run": None if self.run is None else self.run.to_safe_dict(),
        }


class RecoveryLauncher(Protocol):
    def launch(self, job: RecoveryJob) -> RecoveryResult: ...


class CommandRecoveryLauncher:
    """Invoke one external launcher without a shell or inherited secret values."""

    def __init__(self, argv: Sequence[str]) -> None:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("launcher command must be a non-empty string array")
        self._argv = tuple(argv)

    @classmethod
    def from_file(cls, path: Path) -> CommandRecoveryLauncher:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryExecutionError(
                "launcher command file must be valid UTF-8 JSON"
            ) from exc
        if not isinstance(value, Mapping) or set(value) != {"argv"}:
            raise RecoveryExecutionError(
                "launcher command file must contain only an argv array"
            )
        argv = value["argv"]
        if not isinstance(argv, list):
            raise RecoveryExecutionError("launcher argv must be an array")
        return cls(argv)

    def launch(self, job: RecoveryJob) -> RecoveryResult:
        deadline = datetime.fromisoformat(job.deadline_at)
        remaining = max(0.1, (deadline - datetime.now(timezone.utc)).total_seconds())
        safe_environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP"}
        }
        try:
            completed = subprocess.run(
                self._argv,
                input=json.dumps(job.to_dict(), sort_keys=True),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                env=safe_environment,
                shell=False,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RecoveryResult(
                "failed",
                "Recovery execution reached its configured time limit.",
                f"supervisor://time-limit/{job.run_id}",
                0,
            )
        except (OSError, UnicodeError) as exc:
            raise RecoveryExecutionError("external recovery launcher failed to start") from exc
        if completed.returncode != 0:
            return RecoveryResult(
                "failed",
                "The external recovery launcher returned a non-zero exit status.",
                f"launcher://exit/{job.run_id}/{completed.returncode}",
                0,
            )
        if len(completed.stdout.encode("utf-8")) > 65_536:
            raise RecoveryExecutionError("recovery launcher result is too large")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RecoveryExecutionError("recovery launcher returned invalid JSON") from exc
        if not isinstance(value, Mapping) or set(value) != {
            "outcome",
            "safe_summary",
            "usage_used",
            "verification_ref",
        }:
            raise RecoveryExecutionError("recovery launcher result contract is invalid")
        return RecoveryResult(
            outcome=str(value["outcome"]),
            safe_summary=str(value["safe_summary"]),
            verification_ref=str(value["verification_ref"]),
            usage_used=value["usage_used"],
        )


class RecoverySupervisorStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._reliability = ReliabilityStore(database_url)

    def claim(
        self,
        *,
        principal: Principal,
        owner_id: str,
        profile: ToolProfile,
        time_limit_minutes: int = DEFAULT_TIME_LIMIT_MINUTES,
        usage_limit: int = DEFAULT_USAGE_LIMIT,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> RecoveryRun | None:
        owner_id = _identifier(owner_id, "owner_id")
        time_limit_minutes = _positive_integer(
            time_limit_minutes, "time_limit_minutes", maximum=24 * 60
        )
        usage_limit = _positive_integer(usage_limit, "usage_limit")
        lease_seconds = _positive_integer(lease_seconds, "lease_seconds", maximum=3600)
        _authorize_scope(principal)
        allowed_all = "*" in principal.projects
        allowed_projects = sorted(principal.projects - {"*"})
        lease_token = uuid.uuid4().hex
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    row = connection.execute(
                        f"""
                        SELECT req.project_id, req.recovery_request_id,
                               req.incident_id, req.work_item_id, req.exact_goal,
                               run.run_id, run.status, run.claim_count,
                               run.deadline_at, run.time_limit_minutes,
                               run.usage_limit, run.tool_profile_reference,
                               run.tool_profile_digest
                        FROM {SCHEMA}.recovery_requests AS req
                        LEFT JOIN {SCHEMA}.recovery_supervisor_runs AS run
                          ON run.project_id = req.project_id
                         AND run.recovery_request_id = req.recovery_request_id
                        WHERE req.status = 'pending'
                          AND (%s OR req.project_id = ANY(%s::text[]))
                          AND (run.run_id IS NULL OR (
                              run.status = 'running'
                              AND run.lease_expires_at <= clock_timestamp()
                              AND run.deadline_at > clock_timestamp()
                          ))
                        ORDER BY req.requested_at, req.project_id,
                                 req.recovery_request_id
                        FOR UPDATE OF req SKIP LOCKED
                        LIMIT 1
                        """,
                        (allowed_all, allowed_projects),
                    ).fetchone()
                    if row is None:
                        return None
                    project_id, request_id, incident_id, work_item_id, exact_goal = row[:5]
                    if row[5] is None:
                        run_id = _derived_run_id(project_id, request_id)
                        result = connection.execute(
                            f"""
                            INSERT INTO {SCHEMA}.recovery_supervisor_runs
                                (project_id, recovery_request_id, run_id, owner_id,
                                 lease_token, lease_expires_at, deadline_at,
                                 time_limit_minutes, usage_limit,
                                 tool_profile_reference, tool_profile_digest)
                            VALUES (%s, %s, %s, %s, %s,
                                    LEAST(clock_timestamp() + (%s * interval '1 second'),
                                          clock_timestamp() + (%s * interval '1 minute')),
                                    clock_timestamp() + (%s * interval '1 minute'),
                                    %s, %s, %s, %s)
                            RETURNING claimed_at::text, lease_expires_at::text,
                                      deadline_at::text
                            """,
                            (
                                project_id,
                                request_id,
                                run_id,
                                owner_id,
                                lease_token,
                                lease_seconds,
                                time_limit_minutes,
                                time_limit_minutes,
                                time_limit_minutes,
                                usage_limit,
                                profile.reference,
                                profile.configuration_digest,
                            ),
                        ).fetchone()
                        claim_count = 1
                        deadline = result[2]
                        stored_time_limit = time_limit_minutes
                        stored_usage_limit = usage_limit
                        profile_reference = profile.reference
                        profile_digest = profile.configuration_digest
                        claimed_at, lease_expires_at = result[:2]
                    else:
                        if (row[11], row[12]) != (
                            profile.reference,
                            profile.configuration_digest,
                        ):
                            raise RecoveryConflict(
                                "recovery run is pinned to a different tool profile"
                            )
                        run_id = row[5]
                        claim_count = row[7] + 1
                        result = connection.execute(
                            f"""
                            UPDATE {SCHEMA}.recovery_supervisor_runs
                            SET owner_id = %s, lease_token = %s,
                                claimed_at = clock_timestamp(),
                                lease_expires_at = LEAST(
                                    deadline_at,
                                    clock_timestamp() + (%s * interval '1 second')
                                ),
                                claim_count = claim_count + 1
                            WHERE project_id = %s AND recovery_request_id = %s
                              AND status = 'running'
                              AND lease_expires_at <= clock_timestamp()
                              AND deadline_at > clock_timestamp()
                            RETURNING claimed_at::text, lease_expires_at::text
                            """,
                            (owner_id, lease_token, lease_seconds, project_id, request_id),
                        ).fetchone()
                        if result is None:
                            raise RecoveryConflict("recovery claim is no longer available")
                        claimed_at, lease_expires_at = result
                        deadline = (
                            row[8].isoformat()
                            if hasattr(row[8], "isoformat")
                            else str(row[8])
                        )
                        stored_time_limit = row[9]
                        stored_usage_limit = row[10]
                        profile_reference = row[11]
                        profile_digest = row[12]
                    self._record_event(
                        connection,
                        project_id=project_id,
                        work_item_id=work_item_id,
                        actor_id=principal.subject,
                        run_id=run_id,
                        event_type="recovery.supervisor_claimed",
                        payload={
                            "recovery_request_id": request_id,
                            "run_id": run_id,
                            "claim_count": claim_count,
                            "deadline_at": deadline,
                        },
                    )
                    return RecoveryRun(
                        project_id,
                        request_id,
                        incident_id,
                        work_item_id,
                        exact_goal,
                        run_id,
                        "running",
                        claim_count,
                        owner_id,
                        lease_token,
                        claimed_at,
                        lease_expires_at,
                        deadline,
                        stored_time_limit,
                        stored_usage_limit,
                        profile_reference,
                        profile_digest,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
        except RecoverySupervisorError:
            raise
        except Exception:
            raise RecoverySupervisorError(_STORE_ERROR) from None

    def heartbeat(
        self,
        *,
        principal: Principal,
        run: RecoveryRun,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> RecoveryRun:
        _authorize_project(principal, run.project_id)
        lease_seconds = _positive_integer(lease_seconds, "lease_seconds", maximum=3600)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                row = connection.execute(
                    f"""
                    UPDATE {SCHEMA}.recovery_supervisor_runs
                    SET lease_expires_at = LEAST(
                        deadline_at,
                        clock_timestamp() + (%s * interval '1 second')
                    )
                    WHERE project_id = %s AND recovery_request_id = %s
                      AND run_id = %s AND owner_id = %s AND lease_token = %s
                      AND status = 'running'
                      AND lease_expires_at > clock_timestamp()
                      AND deadline_at > clock_timestamp()
                    RETURNING lease_expires_at::text
                    """,
                    (
                        lease_seconds,
                        run.project_id,
                        run.recovery_request_id,
                        run.run_id,
                        run.owner_id,
                        run.lease_token,
                    ),
                ).fetchone()
                if row is None:
                    raise RecoveryConflict("recovery lease is not active")
                return _replace_run(run, lease_expires_at=row[0])
        except RecoverySupervisorError:
            raise
        except Exception:
            raise RecoverySupervisorError(_STORE_ERROR) from None

    def report(
        self,
        *,
        principal: Principal,
        run: RecoveryRun,
        result: RecoveryResult,
    ) -> RecoveryRun:
        _authorize_project(principal, run.project_id)
        outcome, summary, verification, usage = _validated_result(result)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    current = connection.execute(
                        f"""
                        SELECT status, owner_id, lease_token,
                               lease_expires_at > clock_timestamp(),
                               deadline_at <= clock_timestamp(), usage_limit,
                               result_fingerprint, outcome, safe_summary,
                               verification_ref, usage_used,
                               reported_at::text, result_applied_at::text
                        FROM {SCHEMA}.recovery_supervisor_runs
                        WHERE project_id = %s AND recovery_request_id = %s
                          AND run_id = %s
                        FOR UPDATE
                        """,
                        (run.project_id, run.recovery_request_id, run.run_id),
                    ).fetchone()
                    if current is None:
                        raise RecoveryConflict("recovery run was not found")
                    if current[1:3] != (run.owner_id, run.lease_token):
                        raise RecoveryAuthorizationError("recovery lease token is invalid")
                    if current[0] == "reported":
                        fingerprints = {
                            _result_fingerprint(outcome, summary, verification, usage)
                        }
                        if usage > current[5]:
                            fingerprints.add(
                                _result_fingerprint(
                                    "failed",
                                    "Recovery execution exceeded its configured usage limit.",
                                    f"supervisor://usage-limit/{run.run_id}",
                                    usage,
                                )
                            )
                        if current[4]:
                            fingerprints.add(
                                _result_fingerprint(
                                    "failed",
                                    "Recovery execution reached its configured time limit.",
                                    f"supervisor://time-limit/{run.run_id}",
                                    current[10],
                                )
                            )
                        if current[6] not in fingerprints:
                            raise RecoveryConflict(
                                "recovery run already has a different result"
                            )
                        return _replace_run(
                            run,
                            status="reported",
                            outcome=current[7],
                            safe_summary=current[8],
                            verification_ref=current[9],
                            usage_used=current[10],
                            reported_at=current[11],
                            result_applied_at=current[12],
                        )
                    if current[4]:
                        outcome = "failed"
                        summary = "Recovery execution reached its configured time limit."
                        verification = f"supervisor://time-limit/{run.run_id}"
                    elif not current[3]:
                        raise RecoveryConflict("recovery lease is not active")
                    if usage > current[5]:
                        outcome = "failed"
                        summary = "Recovery execution exceeded its configured usage limit."
                        verification = f"supervisor://usage-limit/{run.run_id}"
                    fingerprint = _result_fingerprint(outcome, summary, verification, usage)
                    reported = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.recovery_supervisor_runs
                        SET status = 'reported', outcome = %s,
                            safe_summary = %s, verification_ref = %s,
                            usage_used = %s, result_fingerprint = %s,
                            reported_at = clock_timestamp()
                        WHERE project_id = %s AND recovery_request_id = %s
                          AND run_id = %s AND status = 'running'
                        RETURNING reported_at::text
                        """,
                        (
                            outcome,
                            summary,
                            verification,
                            usage,
                            fingerprint,
                            run.project_id,
                            run.recovery_request_id,
                            run.run_id,
                        ),
                    ).fetchone()
                    self._record_event(
                        connection,
                        project_id=run.project_id,
                        work_item_id=run.work_item_id,
                        actor_id=principal.subject,
                        run_id=run.run_id,
                        event_type="recovery.result_reported",
                        payload={
                            "recovery_request_id": run.recovery_request_id,
                            "run_id": run.run_id,
                            "outcome": outcome,
                            "verification_ref": verification,
                            "usage_used": usage,
                        },
                    )
                    return _replace_run(
                        run,
                        status="reported",
                        outcome=outcome,
                        safe_summary=summary,
                        verification_ref=verification,
                        usage_used=usage,
                        reported_at=reported[0],
                    )
        except RecoverySupervisorError:
            raise
        except Exception:
            raise RecoverySupervisorError(_STORE_ERROR) from None

    def reconcile(self, principal: Principal) -> int:
        _authorize_scope(principal)
        self._expire_deadlines(principal)
        allowed_all = "*" in principal.projects
        allowed_projects = sorted(principal.projects - {"*"})
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                rows = connection.execute(
                    f"""
                    SELECT run.project_id, run.recovery_request_id, run.run_id,
                           req.incident_id, req.work_item_id,
                           run.outcome, run.safe_summary, run.verification_ref,
                           run.usage_used, run.usage_limit,
                           run.tool_profile_reference, run.tool_profile_digest,
                           run.result_fingerprint
                    FROM {SCHEMA}.recovery_supervisor_runs AS run
                    JOIN {SCHEMA}.recovery_requests AS req
                      ON req.project_id = run.project_id
                     AND req.recovery_request_id = run.recovery_request_id
                    WHERE run.status = 'reported'
                      AND run.result_applied_at IS NULL
                      AND (%s OR run.project_id = ANY(%s::text[]))
                    ORDER BY run.reported_at, run.project_id, run.run_id
                    """,
                    (allowed_all, allowed_projects),
                ).fetchall()
        except Exception:
            raise RecoverySupervisorError(_STORE_ERROR) from None
        applied = 0
        for row in rows:
            project_id, request_id, run_id, incident_id, work_item_id = row[:5]
            evidence = {
                "recovery_run_id": run_id,
                "safe_summary": row[6],
                "verification_ref": row[7],
                "usage_used": row[8],
                "usage_limit": row[9],
                "tool_profile_reference": row[10],
                "tool_profile_digest": row[11],
            }
            self._reliability.record_attempt(
                project_id=project_id,
                work_item_id=work_item_id,
                incident_id=incident_id,
                attempt_id=run_id,
                stage="recovery",
                attempt_number=1,
                outcome=row[5],
                actor_id=principal.subject,
                evidence=evidence,
            )
            try:
                with psycopg.connect(self._database_url, autocommit=True) as connection:
                    changed = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.recovery_supervisor_runs
                        SET result_applied_at = clock_timestamp()
                        WHERE project_id = %s AND recovery_request_id = %s
                          AND run_id = %s AND result_fingerprint = %s
                          AND result_applied_at IS NULL
                        """,
                        (project_id, request_id, run_id, row[12]),
                    ).rowcount
                    applied += int(changed > 0)
            except Exception:
                raise RecoverySupervisorError(_STORE_ERROR) from None
        return applied

    def list_project(self, project_id: str) -> tuple[dict[str, object], ...]:
        project_id = _identifier(project_id, "project_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                rows = connection.execute(
                    f"""
                    SELECT req.recovery_request_id, req.incident_id,
                           req.work_item_id, req.exact_goal, req.status,
                           req.requested_at::text, req.resolved_at::text,
                           run.run_id, run.status, run.claim_count,
                           run.owner_id, run.claimed_at::text,
                           run.lease_expires_at::text, run.deadline_at::text,
                           run.time_limit_minutes, run.usage_limit,
                           run.tool_profile_reference, run.tool_profile_digest,
                           run.outcome, run.safe_summary, run.verification_ref,
                           run.usage_used, run.reported_at::text,
                           run.result_applied_at::text
                    FROM {SCHEMA}.recovery_requests AS req
                    LEFT JOIN {SCHEMA}.recovery_supervisor_runs AS run
                      ON run.project_id = req.project_id
                     AND run.recovery_request_id = req.recovery_request_id
                    WHERE req.project_id = %s
                    ORDER BY req.requested_at DESC, req.recovery_request_id
                    """,
                    (project_id,),
                ).fetchall()
        except Exception:
            raise RecoverySupervisorError(_STORE_ERROR) from None
        names = (
            "recovery_request_id", "incident_id", "work_item_id", "exact_goal",
            "request_status", "requested_at", "resolved_at", "run_id",
            "run_status", "claim_count", "owner_id", "claimed_at",
            "lease_expires_at", "deadline_at", "time_limit_minutes",
            "usage_limit", "tool_profile_reference", "tool_profile_digest",
            "outcome", "safe_summary", "verification_ref", "usage_used",
            "reported_at", "result_applied_at",
        )
        return tuple(dict(zip(names, row, strict=True)) for row in rows)

    def _expire_deadlines(self, principal: Principal) -> None:
        allowed_all = "*" in principal.projects
        allowed_projects = sorted(principal.projects - {"*"})
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    rows = connection.execute(
                        f"""
                        SELECT run.project_id, run.recovery_request_id, run.run_id,
                               req.work_item_id
                        FROM {SCHEMA}.recovery_supervisor_runs AS run
                        JOIN {SCHEMA}.recovery_requests AS req
                          ON req.project_id = run.project_id
                         AND req.recovery_request_id = run.recovery_request_id
                        WHERE run.status = 'running'
                          AND run.deadline_at <= clock_timestamp()
                          AND (%s OR run.project_id = ANY(%s::text[]))
                        FOR UPDATE OF run SKIP LOCKED
                        """,
                        (allowed_all, allowed_projects),
                    ).fetchall()
                    for project_id, request_id, run_id, work_item_id in rows:
                        summary = "Recovery execution reached its configured time limit."
                        verification = f"supervisor://time-limit/{run_id}"
                        fingerprint = _result_fingerprint(
                            "failed", summary, verification, 0
                        )
                        connection.execute(
                            f"""
                            UPDATE {SCHEMA}.recovery_supervisor_runs
                            SET status = 'reported', outcome = 'failed',
                                safe_summary = %s, verification_ref = %s,
                                usage_used = 0, result_fingerprint = %s,
                                reported_at = clock_timestamp()
                            WHERE project_id = %s AND recovery_request_id = %s
                              AND status = 'running'
                            """,
                            (summary, verification, fingerprint, project_id, request_id),
                        )
                        self._record_event(
                            connection,
                            project_id=project_id,
                            work_item_id=work_item_id,
                            actor_id=principal.subject,
                            run_id=run_id,
                            event_type="recovery.deadline_reached",
                            payload={
                                "recovery_request_id": request_id,
                                "run_id": run_id,
                                "verification_ref": verification,
                            },
                        )
        except RecoverySupervisorError:
            raise
        except Exception:
            raise RecoverySupervisorError(_STORE_ERROR) from None

    @staticmethod
    def _record_event(
        connection,
        *,
        project_id: str,
        work_item_id: str,
        actor_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.audit_records
                (scope, project_id, actor_id, action, object_type, object_id, details)
            VALUES ('project', %s, %s, %s, 'recovery-run', %s, %s)
            """,
            (project_id, actor_id, event_type, run_id, Jsonb(payload)),
        )
        EventUnitOfWork(connection).append(
            EventDraft(
                project_id=project_id,
                work_item_id=work_item_id,
                actor_id=actor_id,
                correlation_id=run_id,
                aggregate_type="recovery-run",
                aggregate_id=run_id,
                event_type=event_type,
                payload=payload,
            ),
            (
                OutboundDraft(
                    topic=event_type,
                    payload=payload,
                ),
            ),
        )


class RecoverySupervisor:
    def __init__(
        self,
        *,
        database_url: str,
        principal: Principal,
        owner_id: str,
        registry: ToolProfileRegistry,
        tool_profile_reference: str,
        launcher: RecoveryLauncher,
        time_limit_minutes: int = DEFAULT_TIME_LIMIT_MINUTES,
        usage_limit: int = DEFAULT_USAGE_LIMIT,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.store = RecoverySupervisorStore(database_url)
        self.principal = principal
        self.owner_id = _identifier(owner_id, "owner_id")
        self.registry = registry
        self.tool_profile_reference = tool_profile_reference
        self.launcher = launcher
        self.time_limit_minutes = time_limit_minutes
        self.usage_limit = usage_limit
        self.lease_seconds = lease_seconds

    def execute_once(self) -> RecoveryExecution:
        reconciled = self.store.reconcile(self.principal)
        profile = self.registry.load_for_launch(
            self.tool_profile_reference,
            launcher=LAUNCHER_ID,
            via_normal_routing=False,
        )
        run = self.store.claim(
            principal=self.principal,
            owner_id=self.owner_id,
            profile=profile,
            time_limit_minutes=self.time_limit_minutes,
            usage_limit=self.usage_limit,
            lease_seconds=self.lease_seconds,
        )
        if run is None:
            return RecoveryExecution("idle", reconciled, None)
        job = _job(run, profile)
        stop = threading.Event()
        lease_lost: list[RecoverySupervisorError] = []

        def keep_alive() -> None:
            interval = max(1.0, self.lease_seconds / 3)
            current = run
            while not stop.wait(interval):
                try:
                    current = self.store.heartbeat(
                        principal=self.principal,
                        run=current,
                        lease_seconds=self.lease_seconds,
                    )
                except RecoverySupervisorError as exc:
                    lease_lost.append(exc)
                    return

        heartbeat = threading.Thread(target=keep_alive, daemon=True)
        heartbeat.start()
        try:
            result = self.launcher.launch(job)
        finally:
            stop.set()
            heartbeat.join(timeout=2)
        if lease_lost:
            raise RecoveryConflict("recovery lease was lost during execution")
        reported = self.store.report(
            principal=self.principal,
            run=run,
            result=result,
        )
        reconciled += self.store.reconcile(self.principal)
        return RecoveryExecution("completed", reconciled, reported)


def _job(run: RecoveryRun, profile: ToolProfile) -> RecoveryJob:
    if (run.tool_profile_reference, run.tool_profile_digest) != (
        profile.reference,
        profile.configuration_digest,
    ):
        raise RecoveryConflict("resolved recovery profile does not match the pinned run")
    reject_sensitive_content(run.exact_goal, "exact_goal")
    return RecoveryJob(
        project_id=run.project_id,
        recovery_request_id=run.recovery_request_id,
        incident_id=run.incident_id,
        work_item_id=run.work_item_id,
        run_id=run.run_id,
        exact_goal=run.exact_goal,
        deadline_at=run.deadline_at,
        usage_limit=run.usage_limit,
        tool_profile_reference=run.tool_profile_reference,
        tool_profile_digest=run.tool_profile_digest,
        image=f"{profile.image.repository}:{profile.image.tag}",
        mount_references=tuple(item.source for item in profile.mounts),
        credential_references=tuple(item.id for item in profile.credentials),
    )


def _authorize_scope(principal: Principal) -> None:
    if RECOVERY_SCOPE not in principal.scopes:
        raise RecoveryAuthorizationError(
            "identity does not have independent recovery permission"
        )


def _authorize_project(principal: Principal, project_id: str) -> None:
    _authorize_scope(principal)
    if "*" not in principal.projects and project_id not in principal.projects:
        raise RecoveryAuthorizationError(
            "identity is not authorized for the recovery project"
        )


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _positive_integer(value: object, label: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        raise ValueError(f"{label} is invalid")
    return value


def _validated_result(result: RecoveryResult) -> tuple[str, str, str, int]:
    if result.outcome not in {"succeeded", "failed"}:
        raise ValueError("recovery outcome is invalid")
    summary = _bounded(result.safe_summary, "safe_summary", 1000)
    verification = _bounded(result.verification_ref, "verification_ref", 1000)
    reject_sensitive_content(summary, "safe_summary")
    reject_sensitive_content(verification, "verification_ref")
    usage = result.usage_used
    if type(usage) is not int or usage < 0:
        raise ValueError("usage_used is invalid")
    return result.outcome, summary, verification, usage


def _bounded(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value.strip()


def _result_fingerprint(outcome: str, summary: str, verification: str, usage: int) -> str:
    canonical = json.dumps(
        {
            "outcome": outcome,
            "safe_summary": summary,
            "usage_used": usage,
            "verification_ref": verification,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _derived_run_id(project_id: str, request_id: str) -> str:
    digest = hashlib.sha256(f"{project_id}\x00{request_id}".encode("utf-8")).hexdigest()[:32]
    return f"recovery-run-{digest}"


def _replace_run(run: RecoveryRun, **changes: object) -> RecoveryRun:
    value = asdict(run)
    value.update(changes)
    return RecoveryRun(**value)
