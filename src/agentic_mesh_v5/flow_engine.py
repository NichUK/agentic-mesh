from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.events import EventDraft, EventStore, OutboundDraft
from agentic_mesh_v5.flow_definition import FlowDefinition, FlowDefinitionError
from agentic_mesh_v5.flow_definition import FlowState, validate_flow
from agentic_mesh_v5.handoffs import HandoffOffer, HandoffStore
from agentic_mesh_v5.lifecycle import LifecycleConflict
from agentic_mesh_v5.lifecycle import LifecycleStore
from agentic_mesh_v5.routing import RouteDraft, Router


_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class FlowEngineError(DatabaseError):
    pass


class FlowEngineNotFound(FlowEngineError):
    pass


class FlowEngineConflict(FlowEngineError):
    pass


@dataclass(frozen=True, slots=True)
class FlowRun:
    project_id: str
    work_item_id: str
    flow_id: str
    flow_digest: str
    current_state: str
    owner_role_id: str
    status: str
    fields: Mapping[str, object]
    version: int
    pending_route_id: str | None
    pending_target_state: str | None
    pending_target_role_id: str | None
    pending_handoff_id: str | None


@dataclass(frozen=True, slots=True)
class FlowObligation:
    project_id: str
    work_item_id: str
    state: str
    entry_version: int
    kind: str
    obligation_id: str
    accountable_role_id: str
    payload: Mapping[str, object]
    status: str
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TransitionSource:
    lease_id: str
    lease_token: str

    def __post_init__(self) -> None:
        _identifier(self.lease_id, "lease_id")
        _required(self.lease_token, "lease_token", 512)


class FlowEngine:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._events = EventStore(database_url)
        self._handoffs = HandoffStore(database_url)
        self._lifecycle = LifecycleStore(database_url)
        self._router = Router(database_url)

    def start(
        self,
        *,
        project_id: str,
        work_item_id: str,
        flow: FlowDefinition,
        fields: Mapping[str, object],
        actor_id: str,
        operation_id: str,
    ) -> FlowRun:
        project_id, work_item_id, actor_id, operation_id = _identifiers(
            project_id, work_item_id, actor_id, operation_id
        )
        if not isinstance(flow, FlowDefinition):
            raise FlowDefinitionError("validated flow definition is required")
        fields = _fields(fields)
        state = flow.state(flow.entry_state)
        digest = _digest("start", flow.digest, flow.snapshot, fields, actor_id)
        try:
            with self._events.transaction() as transaction:
                replay = self._replay(transaction, project_id, operation_id, digest)
                if replay is not None:
                    return self._get_in(transaction, project_id, replay)
                work = transaction.execute(
                    f"""
                    SELECT status, assigned_role_id FROM {SCHEMA}.work_items
                    WHERE project_id = %s AND work_item_id = %s FOR UPDATE
                    """,
                    (project_id, work_item_id),
                ).fetchone()
                if work is None:
                    raise FlowEngineNotFound("work item was not found")
                replay = self._replay(transaction, project_id, operation_id, digest)
                if replay is not None:
                    return self._get_in(transaction, project_id, replay)
                if work != ("active", state.owner_role):
                    raise FlowEngineConflict(
                        "active work owner must match the flow entry owner"
                    )
                transaction.execute(
                    f"""
                    INSERT INTO {SCHEMA}.flow_runs
                        (project_id, work_item_id, flow_id, flow_digest, flow_snapshot,
                         current_state, owner_role_id, status, fields, version,
                         created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, 1, %s, %s)
                    """,
                    (
                        project_id,
                        work_item_id,
                        flow.flow_id,
                        flow.digest,
                        Jsonb(dict(flow.snapshot)),
                        state.state_id,
                        state.owner_role,
                        Jsonb(fields),
                        actor_id,
                        actor_id,
                    ),
                )
                self._insert_obligations(
                    transaction, project_id, work_item_id, state, fields, 1, actor_id
                )
                self._journal(
                    transaction,
                    project_id,
                    work_item_id,
                    operation_id,
                    digest,
                    "start",
                    None,
                    state.state_id,
                    None,
                    None,
                    actor_id,
                    {"flow_digest": flow.digest},
                )
                self._event(
                    transaction,
                    project_id,
                    work_item_id,
                    actor_id,
                    operation_id,
                    "flow.started",
                    {"state": state.state_id, "owner_role_id": state.owner_role},
                )
            return self.get(project_id, work_item_id)
        except (FlowEngineError, FlowDefinitionError):
            raise
        except psycopg.errors.UniqueViolation as exc:
            raise FlowEngineConflict("flow run already exists") from exc
        except DatabaseError:
            raise
        except Exception as exc:
            raise FlowEngineError("flow start failed") from exc

    def satisfy(
        self,
        *,
        project_id: str,
        work_item_id: str,
        kind: str,
        obligation_id: str,
        evidence: Mapping[str, object],
        actor_id: str,
    ) -> FlowObligation:
        project_id, work_item_id, actor_id, obligation_id = _identifiers(
            project_id, work_item_id, actor_id, obligation_id
        )
        if kind not in {"artifact", "gate"}:
            raise FlowEngineConflict("only artifact and gate obligations are satisfiable")
        evidence = _fields(evidence)
        if not evidence:
            raise ValueError("obligation evidence is required")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    run = self._lock(connection, project_id, work_item_id)
                    row = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.flow_obligations
                        SET status = 'satisfied', evidence = %s, updated_by = %s,
                            updated_at = clock_timestamp()
                        WHERE project_id = %s AND work_item_id = %s
                          AND state = %s AND entry_version = %s
                          AND obligation_kind = %s AND obligation_id = %s
                        RETURNING state, entry_version, accountable_role_id,
                                  payload, status, evidence
                        """,
                        (
                            Jsonb(evidence), actor_id, project_id, work_item_id,
                            run.current_state, run.version, kind, obligation_id,
                        ),
                    ).fetchone()
                    if row is None:
                        raise FlowEngineNotFound("current obligation was not found")
            return FlowObligation(
                project_id, work_item_id, row[0], row[1], kind, obligation_id,
                row[2], row[3], row[4], row[5]
            )
        except FlowEngineError:
            raise
        except Exception as exc:
            raise FlowEngineError("flow obligation update failed") from exc

    def dispatch(
        self,
        *,
        project_id: str,
        work_item_id: str,
        kind: str,
        obligation_id: str,
        actor_id: str,
    ) -> FlowObligation:
        project_id, work_item_id, actor_id, obligation_id = _identifiers(
            project_id, work_item_id, actor_id, obligation_id
        )
        if kind not in {"consult", "inform"}:
            raise FlowEngineConflict("only consult and inform obligations are dispatched")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    run = self._lock(connection, project_id, work_item_id)
                    if run.status != "active":
                        raise FlowEngineConflict("flow run is not active")
                    row = connection.execute(
                        f"""
                        SELECT accountable_role_id, payload, status, evidence
                        FROM {SCHEMA}.flow_obligations
                        WHERE project_id = %s AND work_item_id = %s AND state = %s
                          AND entry_version = %s AND obligation_kind = %s
                          AND obligation_id = %s
                          AND status IN ('pending', 'dispatched')
                        FOR UPDATE
                        """,
                        (
                            project_id, work_item_id, run.current_state, run.version,
                            kind, obligation_id,
                        ),
                    ).fetchone()
                    if row is None:
                        raise FlowEngineConflict("obligation is no longer current")
                    self._router.route_in_transaction(
                        connection,
                        RouteDraft(
                            project_id=project_id,
                            work_item_id=work_item_id,
                            target_role_id=row[0],
                            idempotency_key=(
                                f"flow-{kind}-{work_item_id}-{run.version}-"
                                f"{obligation_id}"
                            ),
                            payload={
                                "flow_action": dict(row[1]),
                                "state": run.current_state,
                                "requested_by": actor_id,
                            },
                        ),
                    )
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.flow_obligations
                        SET status = 'dispatched', updated_by = %s,
                            updated_at = clock_timestamp()
                        WHERE project_id = %s AND work_item_id = %s AND state = %s
                          AND entry_version = %s AND obligation_kind = %s
                          AND obligation_id = %s
                        """,
                        (
                            actor_id, project_id, work_item_id, run.current_state,
                            run.version, kind, obligation_id,
                        ),
                    )
            return FlowObligation(
                project_id, work_item_id, run.current_state, run.version,
                kind, obligation_id, row[0], row[1], "dispatched", row[3]
            )
        except FlowEngineError:
            raise
        except Exception as exc:
            raise FlowEngineError("flow action dispatch failed") from exc

    def prepare_transition(
        self,
        *,
        project_id: str,
        work_item_id: str,
        outcome: str,
        fields: Mapping[str, object],
        source: TransitionSource,
        expected_version: int,
        actor_id: str,
        operation_id: str,
    ) -> FlowRun:
        project_id, work_item_id, actor_id, operation_id = _identifiers(
            project_id, work_item_id, actor_id, operation_id
        )
        outcome = _identifier(outcome, "outcome")
        fields = _fields(fields)
        expected_version = _version(expected_version)
        if not isinstance(source, TransitionSource):
            raise ValueError("transition source is required")
        digest = _digest(
            "prepare", work_item_id, outcome, fields, expected_version, actor_id
        )
        route = None
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    replay = self._replay(connection, project_id, operation_id, digest)
                    if replay is not None:
                        return self._get_in(connection, project_id, replay)
                    run = self._lock(connection, project_id, work_item_id)
                    replay = self._replay(
                        connection, project_id, operation_id, digest
                    )
                    if replay is not None:
                        return self._get_in(connection, project_id, replay)
                    if run.status == "handoff_preparing":
                        self._same_pending(connection, run, operation_id, digest)
                        route = self._pinned_flow(connection, run).state(
                            run.current_state
                        ).select_route(outcome, run.fields)
                    else:
                        if run.status != "active" or run.version != expected_version:
                            raise FlowEngineConflict("flow run is not ready at expected version")
                        merged = {**run.fields, **fields}
                        flow = self._pinned_flow(connection, run)
                        state = flow.state(run.current_state)
                        if state.terminal:
                            raise FlowEngineConflict("terminal state has no transition")
                        self._require_ready(connection, run)
                        route = state.select_route(outcome, merged)
                        connection.execute(
                            f"""
                            UPDATE {SCHEMA}.flow_runs
                            SET status = 'handoff_preparing', fields = %s,
                                pending_route_id = %s, pending_target_state = %s,
                                pending_target_role_id = %s,
                                pending_operation_id = %s, pending_request_digest = %s,
                                updated_by = %s, updated_at = clock_timestamp()
                            WHERE project_id = %s AND work_item_id = %s
                            """,
                            (
                                Jsonb(merged), route.route_id, route.target_state,
                                route.target_role, operation_id, digest, actor_id,
                                project_id, work_item_id,
                            ),
                        )
            assert route is not None
            handoff = self._handoffs.offer(
                HandoffOffer(
                    project_id=project_id,
                    source_lease_id=source.lease_id,
                    source_lease_token=source.lease_token,
                    target_role_id=route.target_role,
                    idempotency_key=f"flow-{work_item_id}-{expected_version}-{route.route_id}",
                    summary=f"Flow transition to {route.target_state}",
                    payload={
                        "flow": {"target_state": route.target_state, "route_id": route.route_id}
                    },
                )
            )
            with self._events.transaction() as transaction:
                run = self._lock(transaction, project_id, work_item_id)
                replay = self._replay(transaction, project_id, operation_id, digest)
                if replay is not None:
                    return self._get_in(transaction, project_id, replay)
                self._same_pending(transaction, run, operation_id, digest)
                transaction.execute(
                    f"""
                    UPDATE {SCHEMA}.flow_runs
                    SET status = 'handoff_pending', pending_handoff_id = %s,
                        version = version + 1, updated_by = %s,
                        updated_at = clock_timestamp()
                    WHERE project_id = %s AND work_item_id = %s
                    """,
                    (handoff.handoff_id, actor_id, project_id, work_item_id),
                )
                self._journal(
                    transaction, project_id, work_item_id, operation_id, digest,
                    "prepare", run.current_state, route.target_state, route.route_id,
                    handoff.handoff_id, actor_id, {},
                )
                self._event(
                    transaction, project_id, work_item_id, actor_id, operation_id,
                    "flow.handoff_prepared",
                    {"route_id": route.route_id, "handoff_id": handoff.handoff_id},
                )
            return self.get(project_id, work_item_id)
        except (FlowEngineError, FlowDefinitionError):
            raise
        except DatabaseError:
            raise
        except Exception as exc:
            raise FlowEngineError("flow transition preparation failed") from exc

    def pickup_transition(
        self,
        *,
        project_id: str,
        work_item_id: str,
        expected_version: int,
        actor_id: str,
        operation_id: str,
    ) -> FlowRun:
        project_id, work_item_id, actor_id, operation_id = _identifiers(
            project_id, work_item_id, actor_id, operation_id
        )
        expected_version = _version(expected_version)
        digest = _digest("pickup", work_item_id, expected_version, actor_id)
        try:
            with self._events.transaction() as transaction:
                replay = self._replay(transaction, project_id, operation_id, digest)
                if replay is not None:
                    return self._get_in(transaction, project_id, replay)
                run = self._lock(transaction, project_id, work_item_id)
                replay = self._replay(
                    transaction, project_id, operation_id, digest
                )
                if replay is not None:
                    return self._get_in(transaction, project_id, replay)
                if (
                    run.status != "handoff_pending"
                    or run.version != expected_version
                    or run.pending_handoff_id is None
                ):
                    raise FlowEngineConflict("pending flow transition changed")
                handoff_status = transaction.execute(
                    f"""
                    SELECT status FROM {SCHEMA}.handoffs
                    WHERE project_id = %s AND handoff_id = %s
                    """,
                    (project_id, run.pending_handoff_id),
                ).fetchone()
                if handoff_status != ("accepted",):
                    raise FlowEngineConflict("target has not accepted the handoff")
                assert run.pending_target_state and run.pending_target_role_id
                flow = self._pinned_flow(transaction, run)
                target = flow.state(run.pending_target_state)
                if target.owner_role != run.pending_target_role_id:
                    raise FlowEngineConflict("pinned target owner changed")
                next_version = run.version + 1
                transaction.execute(
                    f"""
                    UPDATE {SCHEMA}.flow_runs
                    SET current_state = %s, owner_role_id = %s, status = 'active',
                        pending_route_id = NULL, pending_target_state = NULL,
                        pending_target_role_id = NULL, pending_handoff_id = NULL,
                        pending_operation_id = NULL, pending_request_digest = NULL,
                        version = %s, updated_by = %s,
                        updated_at = clock_timestamp()
                    WHERE project_id = %s AND work_item_id = %s
                    """,
                    (
                        target.state_id, target.owner_role, next_version, actor_id,
                        project_id, work_item_id,
                    ),
                )
                work_owner = transaction.execute(
                    f"""
                    UPDATE {SCHEMA}.work_items
                    SET assigned_role_id = %s, version = version + 1,
                        updated_at = clock_timestamp()
                    WHERE project_id = %s AND work_item_id = %s AND status = 'active'
                    RETURNING assigned_role_id
                    """,
                    (target.owner_role, project_id, work_item_id),
                ).fetchone()
                if work_owner != (target.owner_role,):
                    raise FlowEngineConflict("active work item ownership did not change")
                self._insert_obligations(
                    transaction, project_id, work_item_id, target, run.fields,
                    next_version, actor_id,
                )
                self._journal(
                    transaction, project_id, work_item_id, operation_id, digest,
                    "pickup", run.current_state, target.state_id,
                    run.pending_route_id, run.pending_handoff_id, actor_id, {},
                )
                self._event(
                    transaction, project_id, work_item_id, actor_id, operation_id,
                    "flow.transition_picked_up",
                    {"state": target.state_id, "owner_role_id": target.owner_role},
                )
            return self.get(project_id, work_item_id)
        except (FlowEngineError, FlowDefinitionError):
            raise
        except DatabaseError:
            raise
        except Exception as exc:
            raise FlowEngineError("flow transition pickup failed") from exc

    def complete(
        self,
        *,
        project_id: str,
        work_item_id: str,
        expected_version: int,
        actor_id: str,
        operation_id: str,
        evidence: Mapping[str, object],
    ) -> FlowRun:
        project_id, work_item_id, actor_id, operation_id = _identifiers(
            project_id, work_item_id, actor_id, operation_id
        )
        expected_version = _version(expected_version)
        evidence = _fields(evidence)
        if not evidence:
            raise ValueError("completion evidence is required")
        digest = _digest("complete", expected_version, actor_id, evidence)
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            with connection.transaction():
                replay = self._replay(connection, project_id, operation_id, digest)
                if replay is not None:
                    return self._get_in(connection, project_id, replay)
                run = self._lock(connection, project_id, work_item_id)
                replay = self._replay(connection, project_id, operation_id, digest)
                if replay is not None:
                    return self._get_in(connection, project_id, replay)
                if run.status == "completion_preparing":
                    self._same_pending(connection, run, operation_id, digest)
                else:
                    flow = self._pinned_flow(connection, run)
                    if (
                        run.status != "active"
                        or run.version != expected_version
                        or not flow.state(run.current_state).terminal
                    ):
                        raise FlowEngineConflict("flow is not ready for completion")
                    self._require_ready(connection, run)
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.flow_runs
                        SET status = 'completion_preparing', pending_operation_id = %s,
                            pending_request_digest = %s, updated_by = %s,
                            updated_at = clock_timestamp()
                        WHERE project_id = %s AND work_item_id = %s
                        """,
                        (operation_id, digest, actor_id, project_id, work_item_id),
                    )
        work = self._lifecycle.get_work_item(project_id, work_item_id)
        if work.status == "active":
            try:
                work = self._lifecycle.transition_work_item(
                    project_id=project_id,
                    work_item_id=work_item_id,
                    target_status="completed",
                    actor_id=actor_id,
                    correlation_id=operation_id,
                    expected_version=work.version,
                    reason="external flow completed",
                    evidence=evidence,
                )
            except LifecycleConflict:
                work = self._lifecycle.get_work_item(project_id, work_item_id)
        if (
            work.status != "completed"
            or work.terminal_reason != "external flow completed"
            or dict(work.terminal_evidence) != evidence
        ):
            raise FlowEngineConflict("kernel work item cannot complete")
        with self._events.transaction() as transaction:
            run = self._lock(transaction, project_id, work_item_id)
            replay = self._replay(transaction, project_id, operation_id, digest)
            if replay is not None:
                return self._get_in(transaction, project_id, replay)
            self._same_pending(transaction, run, operation_id, digest)
            transaction.execute(
                f"""
                UPDATE {SCHEMA}.flow_runs
                SET status = 'completed', pending_operation_id = NULL,
                    pending_request_digest = NULL, version = version + 1,
                    updated_by = %s, updated_at = clock_timestamp()
                WHERE project_id = %s AND work_item_id = %s
                """,
                (actor_id, project_id, work_item_id),
            )
            self._journal(
                transaction, project_id, work_item_id, operation_id, digest,
                "complete", run.current_state, run.current_state, None, None,
                actor_id, evidence,
            )
            self._event(
                transaction, project_id, work_item_id, actor_id, operation_id,
                "flow.completed", {"state": run.current_state},
            )
        return self.get(project_id, work_item_id)

    def get(self, project_id: str, work_item_id: str) -> FlowRun:
        project_id, work_item_id = _identifiers(project_id, work_item_id)
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                return self._get_in(connection, project_id, work_item_id)
        except FlowEngineError:
            raise
        except Exception as exc:
            raise FlowEngineError("flow run read failed") from exc

    def obligations(
        self, project_id: str, work_item_id: str, *, current_only: bool = True
    ) -> tuple[FlowObligation, ...]:
        project_id, work_item_id = _identifiers(project_id, work_item_id)
        run = self.get(project_id, work_item_id)
        clause = (
            f"""
                AND state = %s
                AND entry_version = (
                    SELECT max(current.entry_version)
                    FROM {SCHEMA}.flow_obligations AS current
                    WHERE current.project_id = %s AND current.work_item_id = %s
                      AND current.state = %s
                )
            """
            if current_only
            else ""
        )
        parameters: tuple[object, ...] = (project_id, work_item_id)
        if current_only:
            parameters += (
                run.current_state,
                project_id,
                work_item_id,
                run.current_state,
            )
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            rows = connection.execute(
                f"""
                SELECT state, entry_version, obligation_kind, obligation_id,
                       accountable_role_id, payload, status, evidence
                FROM {SCHEMA}.flow_obligations
                WHERE project_id = %s AND work_item_id = %s {clause}
                ORDER BY entry_version, obligation_kind, obligation_id
                """,
                parameters,
            ).fetchall()
        return tuple(
            FlowObligation(project_id, work_item_id, *row) for row in rows
        )

    @staticmethod
    def _insert_obligations(
        transaction, project_id: str, work_item_id: str, state: FlowState,
        fields: Mapping[str, object], entry_version: int, actor_id: str,
    ) -> None:
        for action in state.obligations(fields):
            transaction.execute(
                f"""
                INSERT INTO {SCHEMA}.flow_obligations
                    (project_id, work_item_id, state, entry_version,
                     obligation_kind, obligation_id, accountable_role_id,
                     payload, status, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                """,
                (
                    project_id, work_item_id, state.state_id, entry_version,
                    action.kind, action.action_id, action.role_id,
                    Jsonb(dict(action.payload)), actor_id,
                ),
            )

    @staticmethod
    def _require_ready(connection, run: FlowRun) -> None:
        pending = connection.execute(
            f"""
            SELECT obligation_kind, obligation_id FROM {SCHEMA}.flow_obligations
            WHERE project_id = %s AND work_item_id = %s AND state = %s
              AND entry_version = %s AND (
                  (obligation_kind IN ('artifact', 'gate') AND status <> 'satisfied')
                  OR (obligation_kind IN ('consult', 'inform')
                      AND status <> 'dispatched')
              )
            ORDER BY obligation_kind, obligation_id
            """,
            (run.project_id, run.work_item_id, run.current_state, run.version),
        ).fetchall()
        if pending:
            raise FlowEngineConflict("required flow obligations are not satisfied")

    @staticmethod
    def _pinned_flow(connection, run: FlowRun) -> FlowDefinition:
        row = connection.execute(
            f"""
            SELECT flow_snapshot FROM {SCHEMA}.flow_runs
            WHERE project_id = %s AND work_item_id = %s
            """,
            (run.project_id, run.work_item_id),
        ).fetchone()
        return validate_flow(row[0], digest=run.flow_digest)

    @staticmethod
    def _same_pending(connection, run: FlowRun, operation_id: str, digest: str) -> None:
        row = connection.execute(
            f"""
            SELECT pending_operation_id, pending_request_digest
            FROM {SCHEMA}.flow_runs WHERE project_id = %s AND work_item_id = %s
            """,
            (run.project_id, run.work_item_id),
        ).fetchone()
        if row != (operation_id, digest):
            raise FlowEngineConflict("another flow operation is pending")

    @staticmethod
    def _journal(
        transaction, project_id, work_item_id, operation_id, digest, action,
        from_state, to_state, route_id, handoff_id, actor_id, evidence,
    ) -> None:
        transaction.execute(
            f"""
            INSERT INTO {SCHEMA}.flow_transition_journal
                (project_id, work_item_id, sequence, operation_id, request_digest,
                 action, from_state, to_state, route_id, handoff_id, actor_id, evidence)
            SELECT %s, %s, COALESCE(max(sequence), 0) + 1, %s, %s, %s, %s,
                   %s, %s, %s, %s, %s
            FROM {SCHEMA}.flow_transition_journal
            WHERE project_id = %s AND work_item_id = %s
            """,
            (
                project_id, work_item_id, operation_id, digest, action, from_state,
                to_state, route_id, handoff_id, actor_id, Jsonb(dict(evidence)),
                project_id, work_item_id,
            ),
        )

    @staticmethod
    def _replay(connection, project_id: str, operation_id: str, digest: str):
        row = connection.execute(
            f"""
            SELECT work_item_id, request_digest
            FROM {SCHEMA}.flow_transition_journal
            WHERE project_id = %s AND operation_id = %s
            """,
            (project_id, operation_id),
        ).fetchone()
        if row is None:
            return None
        if row[1] != digest:
            raise FlowEngineConflict("operation id conflicts with another request")
        return row[0]

    @staticmethod
    def _event(
        transaction, project_id, work_item_id, actor_id, operation_id,
        event_type, payload,
    ) -> None:
        transaction.append(
            EventDraft(
                project_id=project_id, work_item_id=work_item_id,
                actor_id=actor_id, correlation_id=operation_id,
                aggregate_type="flow-run", aggregate_id=work_item_id,
                event_type=event_type, payload=payload,
            ),
            (OutboundDraft(topic=event_type, payload=payload),),
        )

    @staticmethod
    def _lock(connection, project_id: str, work_item_id: str) -> FlowRun:
        row = connection.execute(
            f"""
            SELECT project_id, work_item_id, flow_id, flow_digest, current_state,
                   owner_role_id, status, fields, version, pending_route_id,
                   pending_target_state, pending_target_role_id, pending_handoff_id
            FROM {SCHEMA}.flow_runs
            WHERE project_id = %s AND work_item_id = %s FOR UPDATE
            """,
            (project_id, work_item_id),
        ).fetchone()
        if row is None:
            raise FlowEngineNotFound("flow run was not found")
        return FlowRun(*row)

    @staticmethod
    def _get_in(connection, project_id: str, work_item_id: str) -> FlowRun:
        row = connection.execute(
            f"""
            SELECT project_id, work_item_id, flow_id, flow_digest, current_state,
                   owner_role_id, status, fields, version, pending_route_id,
                   pending_target_state, pending_target_role_id, pending_handoff_id
            FROM {SCHEMA}.flow_runs
            WHERE project_id = %s AND work_item_id = %s
            """,
            (project_id, work_item_id),
        ).fetchone()
        if row is None:
            raise FlowEngineNotFound("flow run was not found")
        if isinstance(row, dict):
            return FlowRun(**row)
        return FlowRun(*row)


def _identifiers(*values: str) -> tuple[str, ...]:
    return tuple(_identifier(value, "identifier") for value in values)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _required(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _fields(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("flow fields must be an object")
    payload = json.loads(json.dumps(value, sort_keys=True))
    if not isinstance(payload, dict) or len(json.dumps(payload)) > 32_000:
        raise ValueError("flow fields are invalid")
    return payload


def _version(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected_version must be positive")
    return value


def _digest(*values: object) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
