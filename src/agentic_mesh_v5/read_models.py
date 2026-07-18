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
        while self.advance(project_id, limit=1000) == 1000:
            pass
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
                rows = connection.execute(
                    f"""
                    SELECT domain, entity_id, payload
                    FROM {SCHEMA}.read_model_entities
                    WHERE project_id = %s
                    ORDER BY domain, entity_id
                    """,
                    (project_id,),
                ).fetchall()
        except Exception as exc:
            raise ReadModelError("read-model snapshot failed") from exc
        domains: dict[str, list[dict[str, Any]]] = {item: [] for item in DOMAINS}
        for domain, _entity_id, payload in rows:
            domains[domain].append(payload)
        return {
            "project_id": project_id,
            "last_event_id": 0 if cursor is None else cursor[0],
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
        self.require_project(project_id)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
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
