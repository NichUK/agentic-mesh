from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.document_store import DocumentStore


_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ARCHITECTURE_IMPACTS = {"no-material", "material", "uncertain"}
_ARCHITECTURE_GATE_TYPES = {"architecture_impact", "architecture_impact_assessment"}
_SPONSOR_GATE_TYPES = {"human_response", "sponsor_approval"}


class GovernanceError(DatabaseError):
    pass


class GovernanceNotFound(GovernanceError):
    pass


class GovernanceConflict(GovernanceError):
    pass


@dataclass(frozen=True, slots=True)
class GovernanceRecord:
    project_id: str
    work_item_id: str
    record_id: str
    state: str
    entry_version: int
    obligation_kind: str
    obligation_id: str
    decision: str
    actor_role_id: str
    reason: str
    evidence: Mapping[str, object]
    document_path: str | None
    document_etag: str | None
    recorded_at: str


class GovernanceStore:
    """Durable evidence for obligations declared by the external flow."""

    def __init__(self, database_url: str, document_store: DocumentStore) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        if not isinstance(document_store, DocumentStore):
            raise ValueError("a DocumentStore implementation is required")
        self._database_url = database_url
        self._documents = document_store

    def verify_artifact(
        self,
        *,
        project_id: str,
        work_item_id: str,
        path: str,
        actor_role_id: str,
        record_id: str,
    ) -> GovernanceRecord:
        project_id, work_item_id, actor_role_id, record_id = _identifiers(
            project_id, work_item_id, actor_role_id, record_id
        )
        path = _required(path, "path", 4000)
        digest = _digest("verify-artifact", work_item_id, path, actor_role_id)
        replay = self._read_exact(project_id, record_id, digest)
        if replay is not None:
            return replay
        metadata = self._documents.stat(path)
        if metadata.is_folder:
            raise GovernanceConflict("flow artifact must be a document")
        evidence = {"document": metadata.to_dict()}
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    replay = self._replay(connection, project_id, record_id, digest)
                    if replay is not None:
                        return replay
                    run, obligation = self._lock_current(
                        connection, project_id, work_item_id, "artifact", "artifact"
                    )
                    if obligation["accountable_role_id"] != actor_role_id:
                        raise GovernanceConflict("artifact must be verified by its owner role")
                    expected_path = _materialize_path(
                        obligation["payload"].get("path"), project_id, work_item_id
                    )
                    if path != expected_path or metadata.path != path:
                        raise GovernanceConflict("document path does not match the flow artifact")
                    record = self._insert(
                        connection,
                        run,
                        obligation,
                        record_id=record_id,
                        request_digest=digest,
                        decision="verified",
                        actor_role_id=actor_role_id,
                        reason="",
                        evidence=evidence,
                        document_path=path,
                        document_etag=metadata.etag,
                    )
                    self._satisfy(
                        connection,
                        run,
                        obligation,
                        actor_role_id,
                        {
                            "governance_record_id": record_id,
                            "document_path": path,
                            "document_etag": metadata.etag,
                            "document_size": metadata.size,
                        },
                    )
                    return record
        except psycopg.errors.UniqueViolation:
            return self._replay_after_conflict(project_id, record_id, digest)
        except GovernanceError:
            raise
        except DatabaseError:
            raise
        except Exception as exc:
            raise GovernanceError("artifact verification failed") from exc

    def record_consultation(
        self,
        *,
        project_id: str,
        work_item_id: str,
        obligation_id: str,
        decision: str,
        actor_role_id: str,
        record_id: str,
        reason: str = "",
        evidence: Mapping[str, object] | None = None,
    ) -> GovernanceRecord:
        project_id, work_item_id, obligation_id, actor_role_id, record_id = _identifiers(
            project_id, work_item_id, obligation_id, actor_role_id, record_id
        )
        if decision not in {"responded", "exception"}:
            raise ValueError("consultation decision must be responded or exception")
        reason = _reason(reason, required=decision == "exception")
        evidence = _evidence(evidence)
        digest = _digest(
            "consultation", work_item_id, obligation_id, decision,
            actor_role_id, reason, evidence,
        )
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    replay = self._replay(connection, project_id, record_id, digest)
                    if replay is not None:
                        return replay
                    run, obligation = self._lock_current(
                        connection, project_id, work_item_id, "consult", obligation_id
                    )
                    if decision == "responded":
                        if obligation["status"] != "dispatched":
                            raise GovernanceConflict("consultation has not been dispatched")
                        if obligation["accountable_role_id"] != actor_role_id:
                            raise GovernanceConflict(
                                "consultation response must come from the consulted role"
                            )
                    else:
                        leader = run["flow_snapshot"].get("leader_role")
                        if actor_role_id not in {run["owner_role_id"], leader}:
                            raise GovernanceConflict(
                                "consultation exception requires the owner or flow leader"
                            )
                    return self._insert(
                        connection,
                        run,
                        obligation,
                        record_id=record_id,
                        request_digest=digest,
                        decision=decision,
                        actor_role_id=actor_role_id,
                        reason=reason,
                        evidence=evidence,
                    )
        except psycopg.errors.UniqueViolation:
            return self._replay_after_conflict(project_id, record_id, digest)
        except GovernanceError:
            raise
        except Exception as exc:
            raise GovernanceError("consultation evidence failed") from exc

    def decide_gate(
        self,
        *,
        project_id: str,
        work_item_id: str,
        obligation_id: str,
        decision: str,
        actor_role_id: str,
        record_id: str,
        reason: str = "",
        evidence: Mapping[str, object] | None = None,
        architecture_impact: str | None = None,
    ) -> GovernanceRecord:
        project_id, work_item_id, obligation_id, actor_role_id, record_id = _identifiers(
            project_id, work_item_id, obligation_id, actor_role_id, record_id
        )
        if decision not in {"approved", "rejected", "exception"}:
            raise ValueError("gate decision must be approved, rejected, or exception")
        reason = _reason(reason, required=decision == "exception")
        evidence = _evidence(evidence)
        if architecture_impact is not None and architecture_impact not in _ARCHITECTURE_IMPACTS:
            raise ValueError("architecture_impact is invalid")
        digest = _digest(
            "gate", work_item_id, obligation_id, decision, actor_role_id,
            reason, evidence, architecture_impact,
        )
        replay = self._read_exact(project_id, record_id, digest)
        if replay is not None:
            return replay
        document_path, document_etag = self._current_artifact_version(
            project_id, work_item_id
        )
        record_evidence = {
            **evidence,
            "reviewed_document": {
                "path": document_path,
                "etag": document_etag,
            },
        }
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    replay = self._replay(connection, project_id, record_id, digest)
                    if replay is not None:
                        return replay
                    run, obligation = self._lock_current(
                        connection, project_id, work_item_id, "gate", obligation_id
                    )
                    gate_type = obligation["payload"].get("type", "owner_review")
                    requested_from = obligation["payload"].get("requested_from")
                    if gate_type in _SPONSOR_GATE_TYPES or requested_from == "sponsor":
                        raise GovernanceConflict("sponsor gates are reserved for sponsor approval")
                    if obligation["accountable_role_id"] != actor_role_id:
                        raise GovernanceConflict("gate decision requires its accountable role")
                    if not self._artifact_is_verified(
                        connection, run, document_path, document_etag
                    ):
                        raise GovernanceConflict("gate decision requires a verified state artifact")
                    if gate_type in _ARCHITECTURE_GATE_TYPES:
                        if decision == "exception":
                            raise GovernanceConflict(
                                "architecture impact must be explicitly classified"
                            )
                        if decision == "approved" and architecture_impact is None:
                            raise GovernanceConflict(
                                "approved architecture impact gate requires a classification"
                            )
                    elif architecture_impact is not None:
                        raise GovernanceConflict(
                            "architecture impact belongs to the impact-assessment gate"
                        )
                    record = self._insert(
                        connection,
                        run,
                        obligation,
                        record_id=record_id,
                        request_digest=digest,
                        decision=decision,
                        actor_role_id=actor_role_id,
                        reason=reason,
                        evidence=record_evidence,
                    )
                    if decision in {"approved", "exception"}:
                        self._satisfy(
                            connection,
                            run,
                            obligation,
                            actor_role_id,
                            {
                                "governance_record_id": record_id,
                                "decision": decision,
                                "reason": reason,
                                **record_evidence,
                            },
                        )
                    if decision == "approved" and architecture_impact is not None:
                        connection.execute(
                            f"""
                            INSERT INTO {SCHEMA}.governance_context
                                (project_id, work_item_id, architecture_impact, updated_by)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (project_id, work_item_id) DO UPDATE
                            SET architecture_impact = EXCLUDED.architecture_impact,
                                updated_by = EXCLUDED.updated_by,
                                updated_at = clock_timestamp()
                            """,
                            (project_id, work_item_id, architecture_impact, actor_role_id),
                        )
                    return record
        except psycopg.errors.UniqueViolation:
            return self._replay_after_conflict(project_id, record_id, digest)
        except GovernanceError:
            raise
        except Exception as exc:
            raise GovernanceError("gate decision failed") from exc

    def records(self, project_id: str, work_item_id: str) -> tuple[GovernanceRecord, ...]:
        project_id, work_item_id = _identifiers(project_id, work_item_id)
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                rows = connection.execute(
                    f"""
                    SELECT project_id, work_item_id, record_id, state, entry_version,
                           obligation_kind, obligation_id, decision, actor_role_id,
                           reason, evidence, document_path, document_etag,
                           recorded_at::text
                    FROM {SCHEMA}.governance_records
                    WHERE project_id = %s AND work_item_id = %s
                    ORDER BY recorded_at, record_id
                    """,
                    (project_id, work_item_id),
                ).fetchall()
            return tuple(_record(row) for row in rows)
        except Exception as exc:
            raise GovernanceError("governance evidence read failed") from exc

    @staticmethod
    def _replay(connection, project_id: str, record_id: str, digest: str):
        row = GovernanceStore._select_record(connection, project_id, record_id)
        if row is None:
            return None
        if row["request_digest"] != digest:
            raise GovernanceConflict("governance record id conflicts with another request")
        return _record(row)

    def _read_exact(
        self, project_id: str, record_id: str, digest: str
    ) -> GovernanceRecord | None:
        with psycopg.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            return self._replay(connection, project_id, record_id, digest)

    def _replay_after_conflict(
        self, project_id: str, record_id: str, digest: str
    ) -> GovernanceRecord:
        with psycopg.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            replay = self._replay(connection, project_id, record_id, digest)
        if replay is None:
            raise GovernanceConflict("governance evidence conflicted with durable state")
        return replay

    @staticmethod
    def _select_record(connection, project_id: str, record_id: str):
        return connection.execute(
            f"""
            SELECT project_id, work_item_id, record_id, request_digest, state,
                   entry_version, obligation_kind, obligation_id, decision,
                   actor_role_id, reason, evidence, document_path, document_etag,
                   recorded_at::text
            FROM {SCHEMA}.governance_records
            WHERE project_id = %s AND record_id = %s
            """,
            (project_id, record_id),
        ).fetchone()

    @staticmethod
    def _lock_current(connection, project_id, work_item_id, kind, obligation_id):
        run = connection.execute(
            f"""
            SELECT project_id, work_item_id, current_state, owner_role_id, status,
                   version, flow_snapshot
            FROM {SCHEMA}.flow_runs
            WHERE project_id = %s AND work_item_id = %s FOR UPDATE
            """,
            (project_id, work_item_id),
        ).fetchone()
        if run is None:
            raise GovernanceNotFound("flow run was not found")
        if run["status"] != "active":
            raise GovernanceConflict("flow run is not active")
        obligation = connection.execute(
            f"""
            SELECT state, entry_version, obligation_kind, obligation_id,
                   accountable_role_id, payload, status, evidence
            FROM {SCHEMA}.flow_obligations
            WHERE project_id = %s AND work_item_id = %s AND state = %s
              AND entry_version = %s AND obligation_kind = %s
              AND obligation_id = %s FOR UPDATE
            """,
            (
                project_id, work_item_id, run["current_state"], run["version"],
                kind, obligation_id,
            ),
        ).fetchone()
        if obligation is None:
            raise GovernanceNotFound("current flow obligation was not found")
        return run, obligation

    def _current_artifact_version(
        self, project_id: str, work_item_id: str
    ) -> tuple[str, str]:
        with psycopg.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            row = connection.execute(
                f"""
                SELECT evidence.document_path, evidence.document_etag
                FROM {SCHEMA}.flow_runs AS run
                JOIN {SCHEMA}.flow_obligations AS obligation
                  ON obligation.project_id = run.project_id
                 AND obligation.work_item_id = run.work_item_id
                 AND obligation.state = run.current_state
                 AND obligation.entry_version = run.version
                 AND obligation.obligation_kind = 'artifact'
                 AND obligation.status = 'satisfied'
                JOIN {SCHEMA}.governance_records AS evidence
                  ON evidence.project_id = obligation.project_id
                 AND evidence.work_item_id = obligation.work_item_id
                 AND evidence.state = obligation.state
                 AND evidence.entry_version = obligation.entry_version
                 AND evidence.obligation_kind = obligation.obligation_kind
                 AND evidence.obligation_id = obligation.obligation_id
                 AND evidence.decision = 'verified'
                WHERE run.project_id = %s AND run.work_item_id = %s
                  AND run.status = 'active'
                """,
                (project_id, work_item_id),
            ).fetchone()
        if row is None:
            raise GovernanceConflict("gate decision requires a verified state artifact")
        metadata = self._documents.stat(row["document_path"])
        if (
            metadata.is_folder
            or metadata.path != row["document_path"]
            or metadata.etag != row["document_etag"]
        ):
            raise GovernanceConflict("verified state artifact has changed")
        return row["document_path"], row["document_etag"]

    @staticmethod
    def _artifact_is_verified(connection, run, document_path, document_etag) -> bool:
        row = connection.execute(
            f"""
            SELECT 1
            FROM {SCHEMA}.flow_obligations AS obligation
            JOIN {SCHEMA}.governance_records AS evidence
              ON evidence.project_id = obligation.project_id
             AND evidence.work_item_id = obligation.work_item_id
             AND evidence.state = obligation.state
             AND evidence.entry_version = obligation.entry_version
             AND evidence.obligation_kind = obligation.obligation_kind
             AND evidence.obligation_id = obligation.obligation_id
             AND evidence.decision = 'verified'
            WHERE obligation.project_id = %s AND obligation.work_item_id = %s
              AND obligation.state = %s AND obligation.entry_version = %s
              AND obligation.obligation_kind = 'artifact'
              AND obligation.status = 'satisfied'
              AND evidence.document_path = %s AND evidence.document_etag = %s
            """,
            (
                run["project_id"], run["work_item_id"], run["current_state"],
                run["version"], document_path, document_etag,
            ),
        ).fetchone()
        return row is not None

    @staticmethod
    def _insert(
        connection,
        run,
        obligation,
        *,
        record_id,
        request_digest,
        decision,
        actor_role_id,
        reason,
        evidence,
        document_path=None,
        document_etag=None,
    ) -> GovernanceRecord:
        row = connection.execute(
            f"""
            INSERT INTO {SCHEMA}.governance_records
                (project_id, work_item_id, record_id, request_digest, state,
                 entry_version, obligation_kind, obligation_id, decision,
                 actor_role_id, reason, evidence, document_path, document_etag)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING project_id, work_item_id, record_id, state, entry_version,
                      obligation_kind, obligation_id, decision, actor_role_id,
                      reason, evidence, document_path, document_etag,
                      recorded_at::text
            """,
            (
                run["project_id"], run["work_item_id"], record_id, request_digest,
                obligation["state"], obligation["entry_version"],
                obligation["obligation_kind"], obligation["obligation_id"],
                decision, actor_role_id, reason, Jsonb(dict(evidence)),
                document_path, document_etag,
            ),
        ).fetchone()
        return _record(row)

    @staticmethod
    def _satisfy(connection, run, obligation, actor_role_id, evidence) -> None:
        updated = connection.execute(
            f"""
            UPDATE {SCHEMA}.flow_obligations
            SET status = 'satisfied', evidence = %s, updated_by = %s,
                updated_at = clock_timestamp()
            WHERE project_id = %s AND work_item_id = %s AND state = %s
              AND entry_version = %s AND obligation_kind = %s
              AND obligation_id = %s AND status = 'pending'
            """,
            (
                Jsonb(evidence), actor_role_id, run["project_id"], run["work_item_id"],
                obligation["state"], obligation["entry_version"],
                obligation["obligation_kind"], obligation["obligation_id"],
            ),
        ).rowcount
        if updated != 1:
            raise GovernanceConflict("flow obligation is no longer pending")


def _record(row) -> GovernanceRecord:
    return GovernanceRecord(
        project_id=row["project_id"],
        work_item_id=row["work_item_id"],
        record_id=row["record_id"],
        state=row["state"],
        entry_version=row["entry_version"],
        obligation_kind=row["obligation_kind"],
        obligation_id=row["obligation_id"],
        decision=row["decision"],
        actor_role_id=row["actor_role_id"],
        reason=row["reason"],
        evidence=row["evidence"],
        document_path=row["document_path"],
        document_etag=row["document_etag"],
        recorded_at=row["recorded_at"],
    )


def _materialize_path(value: object, project_id: str, work_item_id: str) -> str:
    if not isinstance(value, str):
        raise GovernanceConflict("flow artifact path is invalid")
    return value.replace("{project_id}", project_id).replace("{work_item_id}", work_item_id)


def _identifiers(*values: str) -> tuple[str, ...]:
    return tuple(_identifier(value, "identifier") for value in values)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _required(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value.strip()


def _reason(value: object, *, required: bool) -> str:
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("reason is invalid")
    value = value.strip()
    if required and not value:
        raise ValueError("exception reason is required")
    return value


def _evidence(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("evidence must be an object")
    result = dict(value)
    try:
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > 1_000_000:
        raise ValueError("evidence is too large")
    return result


def _digest(*values: object) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
