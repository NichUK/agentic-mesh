from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA


DOMAINS = ("project", "work", "queue", "role", "instance", "progress")
READ_MODEL_LOCK_SEED = 5_019
SNAPSHOT_BATCH_SIZE = 1000
SNAPSHOT_MAX_BATCHES = 10


class ReadModelError(DatabaseError):
    pass


class ReadModelNotFound(ReadModelError):
    pass


@dataclass(frozen=True)
class ProjectionEvent:
    event_id: int
    project_id: str
    domain: str
    entity_id: str
    operation: str
    payload: dict[str, Any]
    occurred_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "project_id": self.project_id,
            "domain": self.domain,
            "entity_id": self.entity_id,
            "operation": self.operation,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
        }

    def to_sse(self) -> str:
        data = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return f"id: {self.event_id}\nevent: {self.domain}.{self.operation}\ndata: {data}\n\n"


class ReadModelStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def require_project(self, project_id: str) -> None:
        project_id = _required(project_id, "project_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                exists = connection.execute(
                    f"SELECT 1 FROM {SCHEMA}.projects WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
        except Exception as exc:
            raise ReadModelError("read-model project lookup failed") from exc
        if exists is None:
            raise ReadModelNotFound("project not found")

    def rebuild(self, project_id: str) -> int:
        project_id = _required(project_id, "project_id")
        self.require_project(project_id)
        try:
            with psycopg.connect(self._database_url) as connection:
                self._lock_project(connection, project_id)
                connection.execute(
                    f"DELETE FROM {SCHEMA}.read_model_entities WHERE project_id = %s",
                    (project_id,),
                )
                connection.execute(
                    f"DELETE FROM {SCHEMA}.read_model_cursors WHERE project_id = %s",
                    (project_id,),
                )
                return self._apply_available(connection, project_id, limit=None)
        except ReadModelError:
            raise
        except Exception as exc:
            raise ReadModelError("read-model rebuild failed") from exc

    def advance(self, project_id: str, *, limit: int = 1000) -> int:
        project_id = _required(project_id, "project_id")
        _limit(limit)
        self.require_project(project_id)
        try:
            with psycopg.connect(self._database_url) as connection:
                self._lock_project(connection, project_id)
                return self._apply_available(connection, project_id, limit=limit)
        except ReadModelError:
            raise
        except Exception as exc:
            raise ReadModelError("read-model advance failed") from exc

    def snapshot(self, project_id: str) -> dict[str, object]:
        project_id = _required(project_id, "project_id")
        for _batch in range(SNAPSHOT_MAX_BATCHES):
            if self.advance(project_id, limit=SNAPSHOT_BATCH_SIZE) < SNAPSHOT_BATCH_SIZE:
                break
        try:
            with psycopg.connect(self._database_url) as connection:
                self._lock_project(connection, project_id)
                cursor = connection.execute(
                    f"""
                    SELECT last_event_id FROM {SCHEMA}.read_model_cursors
                    WHERE project_id = %s
                    """,
                    (project_id,),
                ).fetchone()
                latest = connection.execute(
                    f"""
                    SELECT COALESCE(max(event_id), 0)
                    FROM {SCHEMA}.read_model_events WHERE project_id = %s
                    """,
                    (project_id,),
                ).fetchone()[0]
                rows = connection.execute(
                    f"""
                    SELECT domain, entity_id, payload
                    FROM {SCHEMA}.read_model_entities
                    WHERE project_id = %s
                    ORDER BY domain, entity_id
                    """,
                    (project_id,),
                ).fetchall()
                queue_rows = connection.execute(
                    f"""
                    WITH observed AS (SELECT clock_timestamp() AS now)
                    SELECT queue.queue_id,
                           count(item.queue_item_id) FILTER (
                               WHERE item.status IN ('ready', 'leased')
                           ) AS depth,
                           count(item.queue_item_id) FILTER (
                               WHERE item.status = 'ready'
                                 AND item.available_at <= (SELECT now FROM observed)
                           ) AS ready,
                           count(item.queue_item_id) FILTER (
                               WHERE item.status = 'ready'
                                 AND item.available_at > (SELECT now FROM observed)
                           ) AS delayed,
                           count(item.queue_item_id) FILTER (
                               WHERE item.status = 'leased'
                           ) AS leased,
                           EXTRACT(epoch FROM (SELECT now FROM observed)
                               - min(item.available_at) FILTER (
                                   WHERE item.status = 'ready'
                                     AND item.available_at <= (SELECT now FROM observed)
                               )) AS oldest_ready_age_seconds,
                           COALESCE(sum(item.attempt_count), 0) AS total_attempts
                    FROM {SCHEMA}.role_queues AS queue
                    LEFT JOIN {SCHEMA}.queue_items AS item
                      ON item.project_id = queue.project_id
                     AND item.queue_id = queue.queue_id
                    WHERE queue.project_id = %s
                    GROUP BY queue.queue_id
                    """,
                    (project_id,),
                ).fetchall()
        except Exception as exc:
            raise ReadModelError("read-model snapshot failed") from exc
        domains: dict[str, list[dict[str, Any]]] = {item: [] for item in DOMAINS}
        queue_metrics = {
            row[0]: {
                "depth": row[1],
                "ready": row[2],
                "delayed": row[3],
                "leased": row[4],
                "oldest_ready_age_seconds": (
                    None if row[5] is None else float(row[5])
                ),
                "total_attempts": row[6],
            }
            for row in queue_rows
        }
        for domain, _entity_id, payload in rows:
            if domain == "queue":
                payload = {**payload, **queue_metrics.get(_entity_id, {})}
            domains[domain].append(payload)
        last_event_id = 0 if cursor is None else cursor[0]
        return {
            "project_id": project_id,
            "last_event_id": last_event_id,
            "latest_event_id": latest,
            "caught_up": last_event_id >= latest,
            "domains": domains,
        }

    def events(
        self,
        project_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 100,
    ) -> tuple[ProjectionEvent, ...]:
        project_id = _required(project_id, "project_id")
        if not isinstance(after_event_id, int) or after_event_id < 0:
            raise ValueError("after_event_id must be a non-negative integer")
        _limit(limit)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                exists = connection.execute(
                    f"SELECT 1 FROM {SCHEMA}.projects WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
                if exists is None:
                    raise ReadModelNotFound("project not found")
                rows = connection.execute(
                    f"""
                    SELECT event_id, project_id, domain, entity_id, operation,
                           payload, occurred_at::text
                    FROM {SCHEMA}.read_model_events
                    WHERE project_id = %s AND event_id > %s
                    ORDER BY event_id
                    LIMIT %s
                    """,
                    (project_id, after_event_id, limit),
                ).fetchall()
        except ReadModelError:
            raise
        except Exception as exc:
            raise ReadModelError("read-model event read failed") from exc
        return tuple(ProjectionEvent(*row) for row in rows)

    @staticmethod
    def _lock_project(connection, project_id: str) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
            (project_id, READ_MODEL_LOCK_SEED),
        )

    @staticmethod
    def _apply_available(connection, project_id: str, *, limit: int | None) -> int:
        cursor = connection.execute(
            f"""
            SELECT last_event_id FROM {SCHEMA}.read_model_cursors
            WHERE project_id = %s FOR UPDATE
            """,
            (project_id,),
        ).fetchone()
        last_event_id = 0 if cursor is None else cursor[0]
        parameters: list[object] = [project_id, last_event_id]
        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT %s"
            parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT event_id, domain, entity_id, operation, payload
            FROM {SCHEMA}.read_model_events
            WHERE project_id = %s AND event_id > %s
            ORDER BY event_id
            {limit_sql}
            """,
            parameters,
        ).fetchall()
        for event_id, domain, entity_id, operation, payload in rows:
            if operation == "delete":
                connection.execute(
                    f"""
                    DELETE FROM {SCHEMA}.read_model_entities
                    WHERE project_id = %s AND domain = %s AND entity_id = %s
                    """,
                    (project_id, domain, entity_id),
                )
            else:
                connection.execute(
                    f"""
                    INSERT INTO {SCHEMA}.read_model_entities
                        (project_id, domain, entity_id, payload, source_event_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, domain, entity_id) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        source_event_id = EXCLUDED.source_event_id,
                        updated_at = clock_timestamp()
                    WHERE {SCHEMA}.read_model_entities.source_event_id
                        < EXCLUDED.source_event_id
                    """,
                    (project_id, domain, entity_id, Jsonb(payload), event_id),
                )
        if rows:
            last_event_id = rows[-1][0]
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.read_model_cursors(project_id, last_event_id)
            VALUES (%s, %s)
            ON CONFLICT (project_id) DO UPDATE
            SET last_event_id = EXCLUDED.last_event_id,
                updated_at = clock_timestamp()
            """,
            (project_id, last_event_id),
        )
        return len(rows)


def _required(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _limit(value: int) -> None:
    if not isinstance(value, int) or value < 1 or value > 1000:
        raise ValueError("limit must be between 1 and 1000")
