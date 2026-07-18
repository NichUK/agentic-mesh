from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
import re
from typing import Literal, Protocol, runtime_checkable
import uuid

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA


_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ACTOR = "fleet-scaler"
_FAILURE = "fleet supervisor operation failed"


class FleetError(DatabaseError):
    pass


class FleetNotFound(FleetError):
    pass


class FleetConflict(FleetError):
    pass


class FleetSupervisorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScalingPolicy:
    project_id: str
    role_id: str
    min_warm_instances: int
    max_instances: int
    scale_after_seconds: int = 60
    idle_grace_seconds: int = 300
    hibernation_enabled: bool = True
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class FleetAction:
    action_id: str
    project_id: str
    role_id: str
    instance_id: str
    action: Literal["wake", "hibernate"]
    reason: str


@dataclass(frozen=True, slots=True)
class FleetActionResult:
    action: FleetAction
    status: Literal["completed", "failed", "superseded"]
    detail: str


@dataclass(frozen=True, slots=True)
class FleetReconcileResult:
    project_id: str | None
    actions: tuple[FleetActionResult, ...]


@runtime_checkable
class FleetSupervisor(Protocol):
    """Apply the same action id safely more than once."""

    def apply(self, action: FleetAction) -> None: ...


class FleetScaler:
    def __init__(
        self, database_url: str, supervisor: FleetSupervisor | None = None
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        if supervisor is not None and not isinstance(supervisor, FleetSupervisor):
            raise ValueError("fleet supervisor is invalid")
        self._database_url = database_url
        self._supervisor = supervisor

    @property
    def database_url(self) -> str:
        return self._database_url

    def configure(self, policy: ScalingPolicy) -> ScalingPolicy:
        selected = _policy(policy)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    role = connection.execute(
                        f"""
                        SELECT role_id FROM {SCHEMA}.roles
                        WHERE project_id = %s AND role_id = %s
                        FOR UPDATE
                        """,
                        (selected.project_id, selected.role_id),
                    ).fetchone()
                    if role is None:
                        raise FleetNotFound("project role not found")
                    instance_count = connection.execute(
                        f"""
                        SELECT count(*) FROM {SCHEMA}.role_instances
                        WHERE project_id = %s AND role_id = %s
                        """,
                        (selected.project_id, selected.role_id),
                    ).fetchone()[0]
                    if instance_count < selected.max_instances:
                        raise FleetConflict(
                            "max_instances exceeds the configured instance pool"
                        )
                    row = connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.role_scaling_policies
                            (project_id, role_id, min_warm_instances,
                             max_instances, scale_after_seconds,
                             idle_grace_seconds, hibernation_enabled)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (project_id, role_id) DO UPDATE
                        SET min_warm_instances = EXCLUDED.min_warm_instances,
                            max_instances = EXCLUDED.max_instances,
                            scale_after_seconds = EXCLUDED.scale_after_seconds,
                            idle_grace_seconds = EXCLUDED.idle_grace_seconds,
                            hibernation_enabled = EXCLUDED.hibernation_enabled,
                            updated_at = clock_timestamp()
                        RETURNING project_id, role_id, min_warm_instances,
                                  max_instances, scale_after_seconds,
                                  idle_grace_seconds, hibernation_enabled,
                                  updated_at::text
                        """,
                        (
                            selected.project_id,
                            selected.role_id,
                            selected.min_warm_instances,
                            selected.max_instances,
                            selected.scale_after_seconds,
                            selected.idle_grace_seconds,
                            selected.hibernation_enabled,
                        ),
                    ).fetchone()
                    self._audit(
                        connection,
                        selected.project_id,
                        "fleet.policy_configured",
                        selected.role_id,
                        asdict(selected),
                    )
            return ScalingPolicy(*row)
        except FleetError:
            raise
        except Exception:
            raise FleetError("fleet policy operation failed") from None

    def policy(self, project_id: str, role_id: str) -> ScalingPolicy:
        project_id = _identifier(project_id, "project_id")
        role_id = _identifier(role_id, "role_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                row = connection.execute(
                    f"""
                    SELECT project_id, role_id, min_warm_instances,
                           max_instances, scale_after_seconds,
                           idle_grace_seconds, hibernation_enabled,
                           updated_at::text
                    FROM {SCHEMA}.role_scaling_policies
                    WHERE project_id = %s AND role_id = %s
                    """,
                    (project_id, role_id),
                ).fetchone()
            if row is None:
                raise FleetNotFound("scaling policy not found")
            return ScalingPolicy(*row)
        except FleetError:
            raise
        except Exception:
            raise FleetError("fleet policy operation failed") from None

    def policies(self, project_id: str) -> tuple[ScalingPolicy, ...]:
        project_id = _identifier(project_id, "project_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                exists = connection.execute(
                    f"SELECT 1 FROM {SCHEMA}.projects WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
                if exists is None:
                    raise FleetNotFound("project not found")
                rows = connection.execute(
                    f"""
                    SELECT project_id, role_id, min_warm_instances,
                           max_instances, scale_after_seconds,
                           idle_grace_seconds, hibernation_enabled,
                           updated_at::text
                    FROM {SCHEMA}.role_scaling_policies
                    WHERE project_id = %s ORDER BY role_id
                    """,
                    (project_id,),
                ).fetchall()
            return tuple(ScalingPolicy(*row) for row in rows)
        except FleetError:
            raise
        except Exception:
            raise FleetError("fleet policy operation failed") from None

    def reconcile(self, project_id: str | None = None) -> FleetReconcileResult:
        if self._supervisor is None:
            raise FleetSupervisorError("fleet supervisor is unavailable")
        selected_project = (
            None if project_id is None else _identifier(project_id, "project_id")
        )
        actions = self._plan(selected_project)
        results: list[FleetActionResult] = []
        for action in actions:
            try:
                self._supervisor.apply(action)
            except Exception:
                self._record_failure(action)
                results.append(FleetActionResult(action, "failed", _FAILURE))
                continue
            results.append(self._complete(action))
        return FleetReconcileResult(selected_project, tuple(results))

    def _plan(self, project_id: str | None) -> tuple[FleetAction, ...]:
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                clause = "" if project_id is None else "AND policy.project_id = %s"
                parameters = () if project_id is None else (project_id,)
                policies = connection.execute(
                    f"""
                    SELECT policy.project_id, policy.role_id,
                           policy.min_warm_instances, policy.max_instances,
                           policy.scale_after_seconds,
                           policy.idle_grace_seconds,
                           policy.hibernation_enabled, policy.updated_at::text
                    FROM {SCHEMA}.role_scaling_policies AS policy
                    JOIN {SCHEMA}.projects AS project USING (project_id)
                    WHERE project.status = 'active' {clause}
                    ORDER BY policy.project_id, policy.role_id
                    """,
                    parameters,
                ).fetchall()
                if project_id is not None:
                    exists = connection.execute(
                        f"SELECT 1 FROM {SCHEMA}.projects WHERE project_id = %s",
                        (project_id,),
                    ).fetchone()
                    if exists is None:
                        raise FleetNotFound("project not found")
                planned: list[FleetAction] = []
                for row in policies:
                    with connection.transaction():
                        policy = ScalingPolicy(*row)
                        connection.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (
                                f"fleet:{len(policy.project_id)}:"
                                f"{policy.project_id}:{policy.role_id}",
                            ),
                        )
                        planned.extend(self._plan_role(connection, policy))
                return tuple(planned)
        except FleetError:
            raise
        except Exception:
            raise FleetError("fleet reconciliation failed") from None

    def _plan_role(
        self, connection: psycopg.Connection[object], policy: ScalingPolicy
    ) -> tuple[FleetAction, ...]:
        pending = self._pending_actions(connection, policy)
        if pending:
            return pending
        self._refresh_idle_state(connection, policy)
        queue = connection.execute(
            f"""
            SELECT count(*) FILTER (
                       WHERE item.status = 'ready'
                         AND item.available_at <= clock_timestamp()
                   ),
                   EXTRACT(epoch FROM clock_timestamp() - min(item.available_at)
                       FILTER (WHERE item.status = 'ready'
                                      AND item.available_at <= clock_timestamp()))
            FROM {SCHEMA}.role_queues AS queue
            LEFT JOIN {SCHEMA}.queue_items AS item
              ON item.project_id = queue.project_id
             AND item.queue_id = queue.queue_id
            WHERE queue.project_id = %s AND queue.role_id = %s
              AND queue.paused = false
            """,
            (policy.project_id, policy.role_id),
        ).fetchone()
        ready_count = int(queue[0])
        oldest_ready = None if queue[1] is None else float(queue[1])
        counts = connection.execute(
            f"""
            SELECT count(*) FILTER (WHERE status = 'running'),
                   count(*) FILTER (WHERE status = 'starting'),
                   count(*) FILTER (
                       WHERE status = 'running' AND {self._busy_sql('instance')}
                   )
            FROM {SCHEMA}.role_instances AS instance
            WHERE instance.project_id = %s AND instance.role_id = %s
            """,
            (policy.project_id, policy.role_id),
        ).fetchone()
        running, starting, busy = (int(value) for value in counts)
        effective = running + starting
        wake_count = max(0, policy.min_warm_instances - effective)
        wake_reason = "minimum warm capacity"
        if ready_count > 0 and effective == 0:
            wake_count = max(wake_count, 1)
            wake_reason = "ready work requires zero-sized role wake"
        elif (
            ready_count > max(0, running - busy)
            and oldest_ready is not None
            and oldest_ready >= policy.scale_after_seconds
            and effective < policy.max_instances
        ):
            wake_count = max(wake_count, 1)
            wake_reason = "ready work exceeded scale-out wait threshold"
        wake_count = min(wake_count, policy.max_instances - effective)
        if wake_count > 0:
            return self._transition_candidates(
                connection,
                policy,
                statuses=("hibernated", "stopped", "configured"),
                action="wake",
                reason=wake_reason,
                limit=wake_count,
            )
        if not policy.hibernation_enabled or ready_count > 0:
            return ()
        surplus = max(0, running - policy.min_warm_instances)
        if surplus == 0:
            return ()
        return self._transition_candidates(
            connection,
            policy,
            statuses=("running",),
            action="hibernate",
            reason="idle grace period elapsed",
            limit=surplus,
            require_idle_seconds=policy.idle_grace_seconds,
        )

    def _pending_actions(
        self, connection: psycopg.Connection[object], policy: ScalingPolicy
    ) -> tuple[FleetAction, ...]:
        rows = connection.execute(
            f"""
            SELECT lifecycle_action_id, instance_id, status, lifecycle_reason
            FROM {SCHEMA}.role_instances
            WHERE project_id = %s AND role_id = %s
              AND status IN ('starting', 'hibernating')
            ORDER BY instance_id
            """,
            (policy.project_id, policy.role_id),
        ).fetchall()
        return tuple(
            FleetAction(
                action_id=row[0],
                project_id=policy.project_id,
                role_id=policy.role_id,
                instance_id=row[1],
                action="wake" if row[2] == "starting" else "hibernate",
                reason=row[3],
            )
            for row in rows
        )

    def _refresh_idle_state(
        self, connection: psycopg.Connection[object], policy: ScalingPolicy
    ) -> None:
        connection.execute(
            f"""
            UPDATE {SCHEMA}.role_instances AS instance
            SET idle_since = CASE
                    WHEN {self._busy_sql('instance')} THEN NULL
                    ELSE COALESCE(instance.idle_since, clock_timestamp())
                END
            WHERE instance.project_id = %s AND instance.role_id = %s
              AND instance.status = 'running'
            """,
            (policy.project_id, policy.role_id),
        )

    def _transition_candidates(
        self,
        connection: psycopg.Connection[object],
        policy: ScalingPolicy,
        *,
        statuses: tuple[str, ...],
        action: Literal["wake", "hibernate"],
        reason: str,
        limit: int,
        require_idle_seconds: int | None = None,
    ) -> tuple[FleetAction, ...]:
        idle_clause = ""
        if require_idle_seconds is not None:
            idle_clause = f"""
                AND instance.idle_since IS NOT NULL
                AND instance.idle_since <= clock_timestamp()
                    - make_interval(secs => %s)
                AND NOT ({self._busy_sql('instance')})
            """
        parameters: list[object] = [
            policy.project_id,
            policy.role_id,
            list(statuses),
        ]
        if require_idle_seconds is not None:
            parameters.append(require_idle_seconds)
        parameters.append(limit)
        candidates = connection.execute(
            f"""
            SELECT instance.instance_id
            FROM {SCHEMA}.role_instances AS instance
            WHERE instance.project_id = %s AND instance.role_id = %s
              AND instance.status = ANY(%s)
              {idle_clause}
            ORDER BY instance.idle_since NULLS LAST, instance.instance_id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            tuple(parameters),
        ).fetchall()
        planned: list[FleetAction] = []
        target_status = "starting" if action == "wake" else "hibernating"
        for (instance_id,) in candidates:
            action_id = f"fleet-{uuid.uuid4().hex}"
            connection.execute(
                f"""
                UPDATE {SCHEMA}.role_instances
                SET status = %s, lifecycle_action_id = %s,
                    lifecycle_reason = %s, last_lifecycle_error = NULL
                WHERE project_id = %s AND instance_id = %s
                """,
                (
                    target_status,
                    action_id,
                    reason,
                    policy.project_id,
                    instance_id,
                ),
            )
            self._audit(
                connection,
                policy.project_id,
                f"fleet.{action}_planned",
                instance_id,
                {"action_id": action_id, "role_id": policy.role_id, "reason": reason},
            )
            planned.append(
                FleetAction(
                    action_id,
                    policy.project_id,
                    policy.role_id,
                    instance_id,
                    action,
                    reason,
                )
            )
        return tuple(planned)

    def _complete(self, action: FleetAction) -> FleetActionResult:
        target = "running" if action.action == "wake" else "hibernated"
        transition = "starting" if action.action == "wake" else "hibernating"
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    row = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.role_instances
                        SET status = %s,
                            started_at = CASE WHEN %s = 'wake'
                                THEN COALESCE(started_at, clock_timestamp())
                                ELSE started_at END,
                            heartbeat_at = CASE WHEN %s = 'wake'
                                THEN clock_timestamp() ELSE heartbeat_at END,
                            last_wake_at = CASE WHEN %s = 'wake'
                                THEN clock_timestamp() ELSE last_wake_at END,
                            hibernated_at = CASE WHEN %s = 'hibernate'
                                THEN clock_timestamp() ELSE hibernated_at END,
                            idle_since = CASE WHEN %s = 'wake'
                                THEN clock_timestamp() ELSE NULL END,
                            lifecycle_action_id = NULL,
                            last_lifecycle_error = NULL
                        WHERE project_id = %s AND instance_id = %s
                          AND role_id = %s AND status = %s
                          AND lifecycle_action_id = %s
                        RETURNING instance_id
                        """,
                        (
                            target,
                            action.action,
                            action.action,
                            action.action,
                            action.action,
                            action.action,
                            action.project_id,
                            action.instance_id,
                            action.role_id,
                            transition,
                            action.action_id,
                        ),
                    ).fetchone()
                    if row is None:
                        return FleetActionResult(
                            action, "superseded", "fleet action was already reconciled"
                        )
                    event_type = f"fleet.instance_{'woken' if action.action == 'wake' else 'hibernated'}"
                    self._audit(
                        connection,
                        action.project_id,
                        event_type,
                        action.instance_id,
                        {"action_id": action.action_id, "role_id": action.role_id},
                    )
            return FleetActionResult(action, "completed", f"instance is {target}")
        except Exception:
            raise FleetError("fleet action completion failed") from None

    def _record_failure(self, action: FleetAction) -> None:
        try:
            with psycopg.connect(self._database_url) as connection:
                updated = connection.execute(
                    f"""
                    UPDATE {SCHEMA}.role_instances
                    SET last_lifecycle_error = %s
                    WHERE project_id = %s AND instance_id = %s
                      AND lifecycle_action_id = %s
                    RETURNING instance_id
                    """,
                    (_FAILURE, action.project_id, action.instance_id, action.action_id),
                ).fetchone()
                if updated is not None:
                    self._audit(
                        connection,
                        action.project_id,
                        "fleet.action_failed",
                        action.instance_id,
                        {"action_id": action.action_id, "detail": _FAILURE},
                    )
        except Exception:
            raise FleetError("fleet failure recording failed") from None

    @staticmethod
    def _busy_sql(alias: str) -> str:
        return f"""
            EXISTS (
                SELECT 1 FROM {SCHEMA}.leases AS lease
                WHERE lease.project_id = {alias}.project_id
                  AND lease.owner_instance_id = {alias}.instance_id
                  AND lease.released_at IS NULL
            ) OR EXISTS (
                SELECT 1 FROM {SCHEMA}.thread_affinities AS affinity
                WHERE affinity.project_id = {alias}.project_id
                  AND affinity.active_instance_id = {alias}.instance_id
                  AND affinity.active_operation_id IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM {SCHEMA}.outbox AS pending
                WHERE pending.project_id = {alias}.project_id
                  AND pending.dispatched_at IS NULL
                  AND (
                    pending.payload ->> 'role_instance_id' = {alias}.instance_id
                    OR pending.payload ->> 'source_instance_id' = {alias}.instance_id
                  )
            )
        """

    @staticmethod
    def _audit(connection, project_id, action, object_id, details) -> None:
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.audit_records
                (scope, project_id, actor_id, action, object_type,
                 object_id, details)
            VALUES ('project', %s, %s, %s, 'role-instance', %s, %s)
            """,
            (project_id, _ACTOR, action, object_id, Jsonb(details)),
        )


def _policy(value: object) -> ScalingPolicy:
    if not isinstance(value, ScalingPolicy):
        raise ValueError("scaling policy is invalid")
    project_id = _identifier(value.project_id, "project_id")
    role_id = _identifier(value.role_id, "role_id")
    for field, selected, minimum, maximum in (
        ("min_warm_instances", value.min_warm_instances, 0, 1000),
        ("max_instances", value.max_instances, 1, 1000),
        ("scale_after_seconds", value.scale_after_seconds, 1, 3600),
        ("idle_grace_seconds", value.idle_grace_seconds, 1, 86400),
    ):
        if type(selected) is not int or not minimum <= selected <= maximum:
            raise ValueError(f"{field} is invalid")
    if value.min_warm_instances > value.max_instances:
        raise ValueError("min_warm_instances cannot exceed max_instances")
    if type(value.hibernation_enabled) is not bool:
        raise ValueError("hibernation_enabled is invalid")
    if role_id == "project-manager" and (
        value.min_warm_instances < 1 or value.hibernation_enabled
    ):
        raise ValueError(
            "project-manager requires one warm instance and disabled hibernation"
        )
    return ScalingPolicy(
        project_id,
        role_id,
        value.min_warm_instances,
        value.max_instances,
        value.scale_after_seconds,
        value.idle_grace_seconds,
        value.hibernation_enabled,
    )


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value
