from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import resources
import os
from pathlib import Path
import re
from typing import Iterable

import psycopg


DATABASE_URL_ENV = "AGENTIC_MESH_V5_DATABASE_URL"
MIGRATION_LOCK_ID = 5_013_202_607_17
MIGRATION_PATTERN = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
SCHEMA = "agentic_mesh_v5"


class DatabaseError(RuntimeError):
    """Base error for the V5 durable store."""


class DatabaseConfigurationError(DatabaseError):
    """Raised when the durable store is not configured."""


class MigrationError(DatabaseError):
    """Raised when migrations cannot be validated or applied safely."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str

    @classmethod
    def from_text(cls, *, version: int, name: str, sql: str) -> Migration:
        return cls(
            version=version,
            name=name,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            sql=sql,
        )


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "name": self.name,
            "checksum": self.checksum,
            "applied_at": self.applied_at,
        }


@dataclass(frozen=True)
class MigrationStatus:
    current_version: int
    available_version: int
    pending_versions: tuple[int, ...]
    applied: tuple[AppliedMigration, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "current_version": self.current_version,
            "available_version": self.available_version,
            "pending_versions": list(self.pending_versions),
            "applied": [item.to_dict() for item in self.applied],
        }


def database_url_from_environment() -> str:
    value = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not value:
        raise DatabaseConfigurationError(f"{DATABASE_URL_ENV} is required")
    if not value.startswith(("postgresql://", "postgres://")):
        raise DatabaseConfigurationError("the V5 database URL must use Postgres")
    return value


def load_migrations(directory: Path | None = None) -> tuple[Migration, ...]:
    if directory is None:
        root = resources.files("agentic_mesh_v5.migrations")
        entries = [item for item in root.iterdir() if item.name.endswith(".sql")]
        texts = [(item.name, item.read_text(encoding="utf-8")) for item in entries]
    else:
        entries = [item for item in directory.iterdir() if item.is_file() and item.suffix == ".sql"]
        texts = [(item.name, item.read_text(encoding="utf-8")) for item in entries]

    migrations: list[Migration] = []
    for filename, sql_text in sorted(texts):
        match = MIGRATION_PATTERN.fullmatch(filename)
        if match is None:
            raise MigrationError(f"invalid migration filename: {filename}")
        migrations.append(
            Migration.from_text(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=sql_text,
            )
        )
    return validate_migrations(migrations)


def validate_migrations(migrations: Iterable[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    if not ordered:
        raise MigrationError("at least one migration is required")
    expected = tuple(range(1, len(ordered) + 1))
    actual = tuple(item.version for item in ordered)
    if actual != expected:
        raise MigrationError(
            f"migration versions must be contiguous from 1; found {list(actual)}"
        )
    names = [item.name for item in ordered]
    if len(names) != len(set(names)):
        raise MigrationError("migration names must be unique")
    for item in ordered:
        expected_checksum = hashlib.sha256(item.sql.encode("utf-8")).hexdigest()
        if item.checksum != expected_checksum:
            raise MigrationError(f"migration {item.version} checksum is invalid")
    return ordered


class MigrationRunner:
    def __init__(
        self,
        database_url: str,
        *,
        migrations: Iterable[Migration] | None = None,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        selected = load_migrations() if migrations is None else migrations
        self._migrations = validate_migrations(selected)

    def migrate(self) -> MigrationStatus:
        active: Migration | None = None
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,)
                    )
                    self._ensure_history(connection)
                    applied = self._read_applied(connection)
                    self._validate_history(applied)
                    applied_versions = {item.version for item in applied}
                    for active in self._migrations:
                        if active.version in applied_versions:
                            continue
                        connection.execute(active.sql)
                        connection.execute(
                            f"""
                            INSERT INTO {SCHEMA}.schema_migrations
                                (version, name, checksum)
                            VALUES (%s, %s, %s)
                            """,
                            (active.version, active.name, active.checksum),
                        )
        except MigrationError:
            raise
        except Exception as exc:
            detail = (
                f"migration {active.version} ({active.name}) failed"
                if active is not None
                else "database migration failed"
            )
            raise MigrationError(detail) from exc
        return self.status()

    def status(self) -> MigrationStatus:
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                exists = connection.execute(
                    "SELECT to_regclass(%s)", (f"{SCHEMA}.schema_migrations",)
                ).fetchone()[0]
                applied = () if exists is None else self._read_applied(connection)
        except Exception as exc:
            raise DatabaseError("database status read failed") from exc
        self._validate_history(applied)
        applied_versions = {item.version for item in applied}
        return MigrationStatus(
            current_version=max(applied_versions, default=0),
            available_version=self._migrations[-1].version,
            pending_versions=tuple(
                item.version
                for item in self._migrations
                if item.version not in applied_versions
            ),
            applied=applied,
        )

    @staticmethod
    def _ensure_history(connection: psycopg.Connection[object]) -> None:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations (
                version integer PRIMARY KEY CHECK (version > 0),
                name text NOT NULL UNIQUE CHECK (btrim(name) <> ''),
                checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{{64}}$'),
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
            """
        )

    @staticmethod
    def _read_applied(
        connection: psycopg.Connection[object],
    ) -> tuple[AppliedMigration, ...]:
        rows = connection.execute(
            f"""
            SELECT version, name, checksum, applied_at::text
            FROM {SCHEMA}.schema_migrations
            ORDER BY version
            """
        ).fetchall()
        return tuple(AppliedMigration(*row) for row in rows)

    def _validate_history(self, applied: tuple[AppliedMigration, ...]) -> None:
        versions = tuple(item.version for item in applied)
        expected_versions = tuple(range(1, len(applied) + 1))
        if versions != expected_versions:
            raise MigrationError(
                f"applied migration history is not contiguous: {list(versions)}"
            )
        available = {item.version: item for item in self._migrations}
        for item in applied:
            migration = available.get(item.version)
            if migration is None:
                raise MigrationError(
                    f"database contains unknown migration version {item.version}"
                )
            if item.name != migration.name or item.checksum != migration.checksum:
                raise MigrationError(
                    f"applied migration {item.version} does not match packaged content"
                )
