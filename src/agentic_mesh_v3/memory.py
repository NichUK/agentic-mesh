from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RoleMemoryRecord:
    role_instance_id: str
    summary: str
    source_ref: str


class SQLiteRoleMemory:
    """Small source-linked memory cache for one or more role instances."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS role_memory (
                  role_instance_id TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  source_ref TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(role_instance_id, summary, source_ref)
                )
                """
            )

    def load_summary(self, role_instance_id: str) -> str:
        rows = self.connection.execute(
            """
            SELECT summary, source_ref
            FROM role_memory
            WHERE role_instance_id=?
            ORDER BY created_at ASC, summary ASC
            """,
            (role_instance_id,),
        )
        return "\n".join(f"- {row['summary']} (source: {row['source_ref']})" for row in rows)

    def record_observation(self, role_instance_id: str, observation: str, *, source_ref: str = "agent-run") -> None:
        self.record(
            RoleMemoryRecord(
                role_instance_id=role_instance_id,
                summary=observation,
                source_ref=source_ref,
            )
        )

    def record(self, record: RoleMemoryRecord) -> None:
        if not record.summary.strip():
            raise ValueError("memory summary is required")
        if not record.source_ref.strip():
            raise ValueError("memory source_ref is required")
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO role_memory(role_instance_id, summary, source_ref)
                VALUES (?, ?, ?)
                """,
                (record.role_instance_id, record.summary, record.source_ref),
            )


class DatabaseRoleMemory:
    """Role memory adapter backed by the v3 runtime database."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def load_summary(self, role_instance_id: str) -> str:
        rows = self.db.list_role_memory(role_instance_id)
        return "\n".join(f"- {row['summary']} (source: {row['source_ref']})" for row in rows)

    def record_observation(self, role_instance_id: str, observation: str, *, source_ref: str = "agent-run") -> None:
        self.record(
            RoleMemoryRecord(
                role_instance_id=role_instance_id,
                summary=observation,
                source_ref=source_ref,
            )
        )

    def record(self, record: RoleMemoryRecord) -> None:
        if not record.summary.strip():
            raise ValueError("memory summary is required")
        if not record.source_ref.strip():
            raise ValueError("memory source_ref is required")
        self.db.record_role_memory(
            memory_id=f"memory-{_stable_memory_id(record)}",
            role_instance_id=record.role_instance_id,
            summary=record.summary,
            source_ref=record.source_ref,
        )


def _stable_memory_id(record: RoleMemoryRecord) -> str:
    payload = "\n".join((record.role_instance_id, record.summary, record.source_ref))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
