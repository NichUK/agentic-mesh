from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


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

    def record_observation(self, role_instance_id: str, observation: str) -> None:
        self.record(
            RoleMemoryRecord(
                role_instance_id=role_instance_id,
                summary=observation,
                source_ref="agent-run",
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
