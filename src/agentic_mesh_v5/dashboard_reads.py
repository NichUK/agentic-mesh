from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.lifecycle import LifecycleNotFound


TrafficLight = Literal["green", "amber", "red"]
QUEUE_AMBER_SECONDS = 60.0
QUEUE_RED_SECONDS = 120.0
HEARTBEAT_AMBER_SECONDS = 60.0
HEARTBEAT_RED_SECONDS = 120.0


class DashboardReadError(DatabaseError):
    pass


def traffic_status(reasons: Iterable[tuple[TrafficLight, str]]) -> dict[str, object]:
    unique = sorted(
        set(reasons), key=lambda item: (0 if item[0] == "red" else 1, item[1])
    )
    light: TrafficLight = "green"
    if any(severity == "red" for severity, _code in unique):
        light = "red"
    elif unique:
        light = "amber"
    return {
        "light": light,
        "reasons": [
            {"severity": severity, "code": code} for severity, code in unique
        ],
    }


def usage_traffic(summary: dict[str, Any]) -> dict[str, object]:
    capacity = summary.get("capacity")
    if not isinstance(capacity, dict) or capacity.get("status") != "known":
        return traffic_status((("amber", "usage.capacity_unknown"),))
    remaining = []
    for key in ("primary", "secondary", "individual_limit"):
        value = capacity.get(key)
        if isinstance(value, dict) and isinstance(value.get("remaining_percent"), int):
            remaining.append(value["remaining_percent"])
    if not remaining:
        return traffic_status((("amber", "usage.remaining_unknown"),))
    lowest = min(remaining)
    if lowest <= 5:
        return traffic_status((("red", "usage.remaining_critical"),))
    if lowest <= 20:
        return traffic_status((("amber", "usage.remaining_low"),))
    return traffic_status(())


def operational_traffic(
    facts: dict[str, Any],
    *,
    queue_age: float | None = None,
    paused: bool = False,
    overdue_claims: int = 0,
    overdue_acceptances: int = 0,
) -> dict[str, object]:
    reasons: list[tuple[TrafficLight, str]] = _queue_reasons(queue_age)
    if facts.get("status") == "error" or facts.get("error_work"):
        reasons.append(("red", "work.error"))
    actionable = facts.get("status") != "completed"
    if actionable and facts.get("timed_out_gates"):
        reasons.append(("red", "gate.timed_out"))
    if actionable and facts.get("rejected_gates"):
        reasons.append(("red", "gate.rejected"))
    if facts.get("failed_recovery"):
        reasons.append(("red", "recovery.failed"))
    if facts.get("terminal_incidents"):
        reasons.append(("red", "incident.terminal"))
    if facts.get("failed_instances"):
        reasons.append(("red", "fleet.instance_failed"))
    if facts.get("stale_instances"):
        reasons.append(("red", "fleet.heartbeat_stale"))
    if overdue_claims:
        reasons.append(("red", "handoff.claim_overdue"))
    if overdue_acceptances:
        reasons.append(("red", "handoff.acceptance_overdue"))
    if paused:
        reasons.append(("amber", "project.paused"))
    if actionable and facts.get("pending_gates"):
        reasons.append(("amber", "gate.pending"))
    if facts.get("active_incidents"):
        reasons.append(("amber", "incident.active"))
    if facts.get("pending_recovery"):
        reasons.append(("amber", "recovery.pending"))
    if facts.get("delayed_instances"):
        reasons.append(("amber", "fleet.heartbeat_delayed"))
    return traffic_status(reasons)


def instance_traffic(
    *, status: str, last_error: str | None, heartbeat_age: float | None
) -> dict[str, object]:
    if status in {"failed", "error"} or last_error:
        return traffic_status((("red", "fleet.instance_failed"),))
    if (
        status == "running"
        and heartbeat_age is not None
        and heartbeat_age >= HEARTBEAT_RED_SECONDS
    ):
        return traffic_status((("red", "fleet.heartbeat_stale"),))
    if (
        status == "running"
        and heartbeat_age is not None
        and heartbeat_age >= HEARTBEAT_AMBER_SECONDS
    ):
        return traffic_status((("amber", "fleet.heartbeat_delayed"),))
    return traffic_status(())


def queue_traffic(
    *, age: float | None, paused: bool, depth: int
) -> dict[str, object]:
    reasons = _queue_reasons(age)
    if paused and depth:
        reasons.append(("amber", "queue.paused_with_work"))
    return traffic_status(reasons)


def recovery_traffic(status: str) -> dict[str, object]:
    if status == "failed":
        return traffic_status((("red", "recovery.failed"),))
    if status == "pending":
        return traffic_status((("amber", "recovery.pending"),))
    return traffic_status(())


class DashboardReadStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def portfolio(self, allowed_projects: frozenset[str]) -> dict[str, object]:
        clause = "" if "*" in allowed_projects else "WHERE p.project_id = ANY(%s)"
        parameters: tuple[object, ...] = (
            () if "*" in allowed_projects else (list(allowed_projects),)
        )
        rows = self._all(
            f"""
            WITH observed AS (SELECT clock_timestamp() AS now)
            SELECT p.project_id, p.display_name, p.status, p.updated_at,
                   work.total_work, work.active_work, work.completed_work,
                   work.error_work, gates.pending_gates, gates.rejected_gates,
                   gates.timed_out_gates, queues.ready_items,
                   queues.oldest_ready_age_seconds, incidents.active_incidents,
                   incidents.terminal_incidents, recovery.pending_recovery,
                   recovery.failed_recovery, fleet.failed_instances,
                   fleet.stale_instances, fleet.delayed_instances,
                   handoffs.overdue_claims, handoffs.overdue_acceptances
            FROM {SCHEMA}.projects AS p
            CROSS JOIN LATERAL (
                SELECT count(*) AS total_work,
                       count(*) FILTER (WHERE status IN ('new', 'active')) AS active_work,
                       count(*) FILTER (WHERE status = 'completed') AS completed_work,
                       count(*) FILTER (WHERE status = 'error') AS error_work
                FROM {SCHEMA}.work_items WHERE project_id = p.project_id
            ) AS work
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE gate.status = 'pending') AS pending_gates,
                       count(*) FILTER (WHERE gate.status = 'rejected') AS rejected_gates,
                       count(*) FILTER (WHERE gate.status = 'timed_out') AS timed_out_gates
                FROM {SCHEMA}.gates AS gate
                JOIN {SCHEMA}.work_items AS item USING (project_id, work_item_id)
                WHERE gate.project_id = p.project_id
                  AND item.status IN ('new', 'active')
            ) AS gates
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (
                           WHERE status = 'ready'
                             AND available_at <= (SELECT now FROM observed)
                       ) AS ready_items,
                       EXTRACT(epoch FROM (SELECT now FROM observed) - min(available_at) FILTER (
                           WHERE status = 'ready'
                             AND available_at <= (SELECT now FROM observed)
                       )) AS oldest_ready_age_seconds
                FROM {SCHEMA}.queue_items
                WHERE project_id = p.project_id
            ) AS queues
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE status = 'active') AS active_incidents,
                       count(*) FILTER (
                           WHERE status IN ('terminal_eligible', 'terminal')
                       ) AS terminal_incidents
                FROM {SCHEMA}.failure_incidents WHERE project_id = p.project_id
            ) AS incidents
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE status = 'pending') AS pending_recovery,
                       count(*) FILTER (WHERE status = 'failed') AS failed_recovery
                FROM {SCHEMA}.recovery_requests WHERE project_id = p.project_id
            ) AS recovery
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (
                    WHERE status IN ('failed', 'error') OR last_lifecycle_error IS NOT NULL
                ) AS failed_instances,
                count(*) FILTER (
                    WHERE status = 'running' AND heartbeat_at IS NOT NULL
                      AND heartbeat_at + interval '120 seconds'
                          <= (SELECT now FROM observed)
                ) AS stale_instances,
                count(*) FILTER (
                    WHERE status = 'running' AND heartbeat_at IS NOT NULL
                      AND heartbeat_at + interval '60 seconds'
                          <= (SELECT now FROM observed)
                      AND heartbeat_at + interval '120 seconds'
                          > (SELECT now FROM observed)
                ) AS delayed_instances
                FROM {SCHEMA}.role_instances WHERE project_id = p.project_id
            ) AS fleet
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (
                           WHERE status = 'offered'
                             AND queued_at + interval '90 seconds'
                                 < (SELECT now FROM observed)
                       ) AS overdue_claims,
                       count(*) FILTER (
                           WHERE status = 'claimed'
                             AND claimed_at + interval '120 seconds'
                                 < (SELECT now FROM observed)
                       ) AS overdue_acceptances
                FROM {SCHEMA}.handoffs WHERE project_id = p.project_id
            ) AS handoffs
            {clause}
            ORDER BY p.project_id
            """,
            parameters,
        )
        projects = [self._portfolio_item(row) for row in rows]
        return {"projects": projects}

    def work(self, project_id: str, *, limit: int, offset: int) -> dict[str, object]:
        self._require_project(project_id)
        limit, offset = _page(limit, offset)
        with self._connection() as connection:
            total = connection.execute(
                f"SELECT count(*) AS total FROM {SCHEMA}.work_items WHERE project_id = %s",
                (project_id,),
            ).fetchone()["total"]
            rows = list(
                connection.execute(
                    f"""
                    WITH observed AS (SELECT clock_timestamp() AS now)
                    SELECT work.work_item_id, work.parent_work_item_id, work.title,
                           work.status, work.priority,
                           work.assigned_role_id AS owner_role_id, work.version,
                           work.updated_at, progress.latest_progress,
                           gates.pending_gates, gates.rejected_gates,
                           gates.timed_out_gates, queues.oldest_ready_age_seconds,
                           incidents.active_incidents, incidents.terminal_incidents,
                           recovery.pending_recovery, recovery.failed_recovery,
                           handoffs.overdue_claims, handoffs.overdue_acceptances
                    FROM {SCHEMA}.work_items AS work
                    LEFT JOIN LATERAL (
                        SELECT to_jsonb(item) - 'project_id' AS latest_progress
                        FROM {SCHEMA}.progress AS item
                        WHERE item.project_id = work.project_id
                          AND item.work_item_id = work.work_item_id
                        ORDER BY item.progress_id DESC LIMIT 1
                    ) AS progress ON TRUE
                    CROSS JOIN LATERAL (
                        SELECT count(*) FILTER (WHERE status = 'pending') AS pending_gates,
                               count(*) FILTER (WHERE status = 'rejected') AS rejected_gates,
                               count(*) FILTER (WHERE status = 'timed_out') AS timed_out_gates
                        FROM {SCHEMA}.gates
                        WHERE project_id = work.project_id
                          AND work_item_id = work.work_item_id
                    ) AS gates
                    CROSS JOIN LATERAL (
                        SELECT EXTRACT(
                                   epoch FROM (SELECT now FROM observed)
                                   - min(available_at) FILTER (
                                       WHERE status = 'ready'
                                         AND available_at <= (SELECT now FROM observed)
                                   )
                               ) AS oldest_ready_age_seconds
                        FROM {SCHEMA}.queue_items
                        WHERE project_id = work.project_id
                          AND work_item_id = work.work_item_id
                    ) AS queues
                    CROSS JOIN LATERAL (
                        SELECT count(*) FILTER (WHERE status = 'active') AS active_incidents,
                               count(*) FILTER (
                                   WHERE status IN ('terminal_eligible', 'terminal')
                               ) AS terminal_incidents
                        FROM {SCHEMA}.failure_incidents
                        WHERE project_id = work.project_id
                          AND work_item_id = work.work_item_id
                    ) AS incidents
                    CROSS JOIN LATERAL (
                        SELECT count(*) FILTER (WHERE status = 'pending') AS pending_recovery,
                               count(*) FILTER (WHERE status = 'failed') AS failed_recovery
                        FROM {SCHEMA}.recovery_requests
                        WHERE project_id = work.project_id
                          AND work_item_id = work.work_item_id
                    ) AS recovery
                    CROSS JOIN LATERAL (
                        SELECT count(*) FILTER (
                                   WHERE status = 'offered'
                                     AND queued_at + interval '90 seconds'
                                         < (SELECT now FROM observed)
                               ) AS overdue_claims,
                               count(*) FILTER (
                                   WHERE status = 'claimed'
                                     AND claimed_at + interval '120 seconds'
                                         < (SELECT now FROM observed)
                               ) AS overdue_acceptances
                        FROM {SCHEMA}.handoffs
                        WHERE project_id = work.project_id
                          AND work_item_id = work.work_item_id
                    ) AS handoffs
                    WHERE work.project_id = %s
                    ORDER BY work.priority DESC, work.updated_at DESC, work.work_item_id
                    LIMIT %s OFFSET %s
                    """,
                    (project_id, limit, offset),
                ).fetchall()
            )
        return {
            "project_id": project_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [self._work_item(row) for row in rows],
        }

    def fleet(self, project_id: str) -> dict[str, object]:
        self._require_project(project_id)
        with self._connection() as connection:
            roles = list(
                connection.execute(
                    f"""
                    SELECT role.role_id, role.template_id, role.status,
                           count(DISTINCT instance.instance_id) AS instance_count,
                           count(DISTINCT queue.queue_id) AS queue_count
                    FROM {SCHEMA}.roles AS role
                    LEFT JOIN {SCHEMA}.role_instances AS instance USING (project_id, role_id)
                    LEFT JOIN {SCHEMA}.role_queues AS queue USING (project_id, role_id)
                    WHERE role.project_id = %s
                    GROUP BY role.role_id, role.template_id, role.status
                    ORDER BY role.role_id
                    """,
                    (project_id,),
                ).fetchall()
            )
            instances = list(
                connection.execute(
                    f"""
                    SELECT instance_id, role_id, status, provider_ref, started_at,
                           heartbeat_at, hibernated_at, idle_since,
                           EXTRACT(epoch FROM clock_timestamp() - heartbeat_at)
                               AS heartbeat_age_seconds,
                           lifecycle_reason, last_wake_at, last_lifecycle_error
                    FROM {SCHEMA}.role_instances
                    WHERE project_id = %s ORDER BY role_id, instance_id
                    """,
                    (project_id,),
                ).fetchall()
            )
            queues = list(
                connection.execute(
                    f"""
                    WITH observed AS (SELECT clock_timestamp() AS now)
                    SELECT queue.queue_id, queue.role_id, queue.capability, queue.paused,
                           count(item.queue_item_id) FILTER (
                               WHERE item.status IN ('ready', 'leased')
                           ) AS depth,
                           count(item.queue_item_id) FILTER (
                               WHERE item.status = 'ready'
                                 AND item.available_at <= (SELECT now FROM observed)
                           ) AS ready,
                           count(item.queue_item_id) FILTER (
                               WHERE item.status = 'leased'
                           ) AS leased,
                           EXTRACT(
                               epoch FROM (SELECT now FROM observed)
                               - min(item.available_at) FILTER (
                                   WHERE item.status = 'ready'
                                     AND item.available_at <= (SELECT now FROM observed)
                               )
                           ) AS oldest_ready_age_seconds
                    FROM {SCHEMA}.role_queues AS queue
                    LEFT JOIN {SCHEMA}.queue_items AS item USING (project_id, queue_id)
                    WHERE queue.project_id = %s
                    GROUP BY queue.queue_id, queue.role_id, queue.capability,
                             queue.paused
                    ORDER BY queue.role_id, queue.queue_id
                    """,
                    (project_id,),
                ).fetchall()
            )
        instances = [self._instance(item) for item in instances]
        queues = [self._queue(item) for item in queues]
        fleet_reasons = [
            (reason["severity"], reason["code"])
            for item in (*instances, *queues)
            for reason in item["traffic"]["reasons"]
        ]
        return {
            "project_id": project_id,
            "traffic": traffic_status(fleet_reasons),
            "roles": roles,
            "instances": instances,
            "queues": queues,
        }

    def recovery(self, project_id: str, *, limit: int, offset: int) -> dict[str, object]:
        return self._paged_records(
            project_id,
            limit=limit,
            offset=offset,
            table="recovery_requests",
            order="requested_at DESC, recovery_request_id",
            columns=(
                "recovery_request_id, incident_id, work_item_id, exact_goal, status, "
                "requested_at, resolved_at, evidence"
            ),
            mapper=self._recovery_item,
        )

    def audit(self, project_id: str, *, limit: int, offset: int) -> dict[str, object]:
        return self._paged_records(
            project_id,
            limit=limit,
            offset=offset,
            table="audit_records",
            order="audit_id DESC",
            columns=(
                "audit_id, actor_id, action, object_type, object_id, details, recorded_at"
            ),
            extra_where="scope = 'project'",
        )

    def _paged_records(
        self,
        project_id: str,
        *,
        limit: int,
        offset: int,
        table: str,
        order: str,
        columns: str,
        extra_where: str = "TRUE",
        mapper=None,
    ) -> dict[str, object]:
        self._require_project(project_id)
        limit, offset = _page(limit, offset)
        with self._connection() as connection:
            total = connection.execute(
                f"SELECT count(*) AS total FROM {SCHEMA}.{table} "
                f"WHERE project_id = %s AND {extra_where}",
                (project_id,),
            ).fetchone()["total"]
            rows = list(
                connection.execute(
                    f"SELECT {columns} FROM {SCHEMA}.{table} "
                    f"WHERE project_id = %s AND {extra_where} "
                    f"ORDER BY {order} LIMIT %s OFFSET %s",
                    (project_id, limit, offset),
                ).fetchall()
            )
        return {
            "project_id": project_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": rows if mapper is None else [mapper(row) for row in rows],
        }

    @staticmethod
    def _portfolio_item(row: dict[str, Any]) -> dict[str, Any]:
        age = _number(row.pop("oldest_ready_age_seconds"))
        result = dict(row)
        result["oldest_ready_age_seconds"] = age
        result["traffic"] = operational_traffic(
            row,
            queue_age=age,
            paused=row["status"] == "paused",
            overdue_claims=row["overdue_claims"],
            overdue_acceptances=row["overdue_acceptances"],
        )
        return result

    @staticmethod
    def _work_item(row: dict[str, Any]) -> dict[str, Any]:
        age = _number(row.pop("oldest_ready_age_seconds"))
        result = dict(row)
        result["oldest_ready_age_seconds"] = age
        result["traffic"] = operational_traffic(
            row,
            queue_age=age,
            overdue_claims=row["overdue_claims"],
            overdue_acceptances=row["overdue_acceptances"],
        )
        return result

    @staticmethod
    def _instance(row: dict[str, Any]) -> dict[str, Any]:
        age = _number(row.get("heartbeat_age_seconds"))
        row["heartbeat_age_seconds"] = age
        row["traffic"] = instance_traffic(
            status=row["status"],
            last_error=row["last_lifecycle_error"],
            heartbeat_age=age,
        )
        return row

    @staticmethod
    def _queue(row: dict[str, Any]) -> dict[str, Any]:
        age = _number(row.get("oldest_ready_age_seconds"))
        row["oldest_ready_age_seconds"] = age
        row["traffic"] = queue_traffic(
            age=age, paused=row["paused"], depth=row["depth"]
        )
        return row

    @staticmethod
    def _recovery_item(row: dict[str, Any]) -> dict[str, Any]:
        row["traffic"] = recovery_traffic(row["status"])
        return row

    def _require_project(self, project_id: str) -> None:
        with self._connection() as connection:
            exists = connection.execute(
                f"SELECT 1 FROM {SCHEMA}.projects WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        if exists is None:
            raise LifecycleNotFound("project not found")

    def _all(self, query: str, parameters: tuple[object, ...]) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return list(connection.execute(query, parameters).fetchall())

    def _connection(self):
        try:
            return psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            )
        except Exception as exc:
            raise DashboardReadError("dashboard read failed") from exc


def _queue_reasons(age: float | None) -> list[tuple[TrafficLight, str]]:
    if age is not None and age >= QUEUE_RED_SECONDS:
        return [("red", "queue.wait_over_120_seconds")]
    if age is not None and age >= QUEUE_AMBER_SECONDS:
        return [("amber", "queue.wait_over_60_seconds")]
    return []


def _number(value: object) -> float | None:
    return None if value is None else float(value)


def _page(limit: int, offset: int) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    return limit, offset
