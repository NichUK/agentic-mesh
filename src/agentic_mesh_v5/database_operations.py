from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import SCHEMA


MANIFEST_FORMAT = "agentic-mesh-v5-postgres-backup/v1"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PG_ENVIRONMENT = {
    "application_name": "PGAPPNAME",
    "channel_binding": "PGCHANNELBINDING",
    "client_encoding": "PGCLIENTENCODING",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "dbname": "PGDATABASE",
    "gssencmode": "PGGSSENCMODE",
    "gsslib": "PGGSSLIB",
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "keepalives": "PGKEEPALIVES",
    "keepalives_count": "PGKEEPALIVESCOUNT",
    "keepalives_idle": "PGKEEPALIVESIDLE",
    "keepalives_interval": "PGKEEPALIVESINTERVAL",
    "krbsrvname": "PGKRBSRVNAME",
    "load_balance_hosts": "PGLOADBALANCEHOSTS",
    "options": "PGOPTIONS",
    "passfile": "PGPASSFILE",
    "password": "PGPASSWORD",
    "port": "PGPORT",
    "require_auth": "PGREQUIREAUTH",
    "requirepeer": "PGREQUIREPEER",
    "service": "PGSERVICE",
    "servicefile": "PGSERVICEFILE",
    "sslcert": "PGSSLCERT",
    "sslcertmode": "PGSSLCERTMODE",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "sslkey": "PGSSLKEY",
    "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
    "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
    "sslmode": "PGSSLMODE",
    "sslpassword": "PGSSLPASSWORD",
    "sslrootcert": "PGSSLROOTCERT",
    "sslsni": "PGSSLSNI",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "tcp_user_timeout": "PGTCPUSER_TIMEOUT",
    "user": "PGUSER",
}


class DatabaseOperationsError(DatabaseError):
    """A safe, operator-facing database operation failure."""


@dataclass(frozen=True, slots=True)
class MaintenanceStatus:
    status: str
    reason: str
    changed_by: str
    changed_at: str
    changed: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BackupResult:
    archive: str
    manifest: str
    sha256: str
    size_bytes: int
    database_schema_version: int
    table_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RestoreResult:
    archive: str
    database_schema_version: int
    table_counts: dict[str, int]
    maintenance_status: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class MaintenanceStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def status(self) -> MaintenanceStatus:
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                row = connection.execute(
                    f"""
                    SELECT status, reason, changed_by, changed_at::text
                    FROM {SCHEMA}.database_maintenance
                    WHERE singleton
                    """
                ).fetchone()
        except Exception as exc:
            raise DatabaseOperationsError("database maintenance status failed") from exc
        if row is None:
            raise DatabaseOperationsError("database maintenance state is missing")
        return MaintenanceStatus(*row)

    def pause(self, *, actor: str, reason: str) -> MaintenanceStatus:
        actor = _required(actor, "actor")
        reason = _required(reason, "reason")
        try:
            with psycopg.connect(self._database_url) as connection:
                table_names = _protected_tables(connection)
                if table_names:
                    connection.execute(
                        sql.SQL("LOCK TABLE {} IN SHARE MODE").format(
                            sql.SQL(", ").join(
                                sql.Identifier(SCHEMA, name) for name in table_names
                            )
                        )
                    )
                row = connection.execute(
                    f"""
                    SELECT status, reason, changed_by, changed_at::text
                    FROM {SCHEMA}.database_maintenance
                    WHERE singleton
                    FOR UPDATE
                    """
                ).fetchone()
                if row is None:
                    raise DatabaseOperationsError(
                        "database maintenance state is missing"
                    )
                if row[0] == "paused":
                    return MaintenanceStatus(*row)
                connection.execute(
                    f"""
                    INSERT INTO {SCHEMA}.database_maintenance_history
                        (status, reason, changed_by)
                    VALUES ('paused', %s, %s)
                    """,
                    (reason, actor),
                )
                changed = connection.execute(
                    f"""
                    UPDATE {SCHEMA}.database_maintenance
                    SET status = 'paused', reason = %s, changed_by = %s,
                        changed_at = clock_timestamp()
                    WHERE singleton
                    RETURNING status, reason, changed_by, changed_at::text
                    """,
                    (reason, actor),
                ).fetchone()
            return MaintenanceStatus(*changed, changed=True)
        except DatabaseOperationsError:
            raise
        except Exception as exc:
            raise DatabaseOperationsError("database maintenance pause failed") from exc

    def resume(self, *, actor: str, reason: str) -> MaintenanceStatus:
        actor = _required(actor, "actor")
        reason = _required(reason, "reason")
        try:
            with psycopg.connect(self._database_url) as connection:
                row = connection.execute(
                    f"""
                    SELECT status, reason, changed_by, changed_at::text
                    FROM {SCHEMA}.database_maintenance
                    WHERE singleton
                    FOR UPDATE
                    """
                ).fetchone()
                if row is None:
                    raise DatabaseOperationsError(
                        "database maintenance state is missing"
                    )
                if row[0] == "active":
                    return MaintenanceStatus(*row)
                changed = connection.execute(
                    f"""
                    UPDATE {SCHEMA}.database_maintenance
                    SET status = 'active', reason = %s, changed_by = %s,
                        changed_at = clock_timestamp()
                    WHERE singleton
                    RETURNING status, reason, changed_by, changed_at::text
                    """,
                    (reason, actor),
                ).fetchone()
                connection.execute(
                    f"""
                    INSERT INTO {SCHEMA}.database_maintenance_history
                        (status, reason, changed_by)
                    VALUES ('active', %s, %s)
                    """,
                    (reason, actor),
                )
            return MaintenanceStatus(*changed, changed=True)
        except DatabaseOperationsError:
            raise
        except Exception as exc:
            raise DatabaseOperationsError("database maintenance resume failed") from exc


class PostgresNativeTools:
    def __init__(
        self,
        *,
        pg_dump: Sequence[str] = ("pg_dump",),
        pg_restore: Sequence[str] = ("pg_restore",),
    ) -> None:
        self._pg_dump = _command(pg_dump, "pg_dump")
        self._pg_restore = _command(pg_restore, "pg_restore")

    def validate(self, server_major: int) -> None:
        dump_major = self._version(self._pg_dump, "pg_dump")
        restore_major = self._version(self._pg_restore, "pg_restore")
        if dump_major < server_major:
            raise DatabaseOperationsError(
                "pg_dump major version is older than the Postgres server"
            )
        if restore_major != dump_major:
            raise DatabaseOperationsError(
                "pg_dump and pg_restore major versions do not match"
            )

    def dump(self, database_url: str, archive: Path) -> None:
        environment, dbname = _postgres_environment(database_url)
        try:
            with archive.open("xb") as output:
                result = subprocess.run(
                    [
                        *self._pg_dump,
                        "--format=custom",
                        "--no-owner",
                        "--no-acl",
                        f"--schema={SCHEMA}",
                        "--dbname",
                        dbname,
                    ],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DatabaseOperationsError("pg_dump could not be executed") from exc
        if result.returncode != 0:
            raise DatabaseOperationsError(
                f"pg_dump failed with exit code {result.returncode}"
            )

    def verify(self, archive: Path) -> None:
        self._restore_command(
            archive,
            arguments=("--list",),
            database_url=None,
            operation="pg_restore verification",
        )

    def restore(self, database_url: str, archive: Path) -> None:
        _environment, dbname = _postgres_environment(database_url)
        self._restore_command(
            archive,
            arguments=(
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-acl",
                "--dbname",
                dbname,
            ),
            database_url=database_url,
            operation="pg_restore",
        )

    def _restore_command(
        self,
        archive: Path,
        *,
        arguments: Sequence[str],
        database_url: str | None,
        operation: str,
    ) -> None:
        environment = (
            _postgres_environment(database_url)[0]
            if database_url is not None
            else _clean_postgres_environment()
        )
        try:
            with archive.open("rb") as source:
                result = subprocess.run(
                    [*self._pg_restore, *arguments],
                    env=environment,
                    stdin=source,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DatabaseOperationsError(f"{operation} could not be executed") from exc
        if result.returncode != 0:
            raise DatabaseOperationsError(
                f"{operation} failed with exit code {result.returncode}"
            )

    @staticmethod
    def _version(command: Sequence[str], operation: str) -> int:
        try:
            result = subprocess.run(
                [*command, "--version"],
                env=_clean_postgres_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DatabaseOperationsError(f"{operation} could not be executed") from exc
        match = re.search(rb"\b(\d+)(?:\.\d+)?\b", result.stdout)
        if result.returncode != 0 or match is None:
            raise DatabaseOperationsError(f"{operation} version check failed")
        return int(match.group(1))


class DatabaseBackupService:
    def __init__(
        self,
        database_url: str,
        *,
        tools: PostgresNativeTools | None = None,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._maintenance = MaintenanceStore(database_url)
        self._tools = tools or PostgresNativeTools()

    def backup(self, archive: Path, *, actor: str, reason: str) -> BackupResult:
        archive = archive.resolve()
        manifest_path = _manifest_path(archive)
        if archive.exists() or manifest_path.exists():
            raise DatabaseOperationsError("backup output already exists")
        self._tools.validate(_server_major(self._database_url))
        archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            with archive.open("xb"):
                pass
            os.chmod(archive, 0o600)
        except FileExistsError as exc:
            raise DatabaseOperationsError("backup output already exists") from exc
        except OSError as exc:
            raise DatabaseOperationsError("backup output could not be reserved") from exc
        temporary_archive = archive.with_name(f".{archive.name}.{uuid4().hex}.tmp")
        temporary_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{uuid4().hex}.tmp"
        )
        initiated_pause = False
        try:
            before = self._maintenance.status()
            paused = self._maintenance.pause(actor=actor, reason=reason)
            initiated_pause = before.status == "active" and paused.changed
            self._tools.dump(self._database_url, temporary_archive)
            self._tools.verify(temporary_archive)
            schema_version, table_counts = _validated_snapshot(self._database_url)
            digest = _sha256(temporary_archive)
            size = temporary_archive.stat().st_size
            manifest = {
                "format": MANIFEST_FORMAT,
                "created_at": datetime.now(UTC).isoformat(),
                "archive_name": archive.name,
                "sha256": digest,
                "size_bytes": size,
                "database_schema_version": schema_version,
                "table_counts": table_counts,
            }
            temporary_manifest.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary_archive, 0o600)
            os.chmod(temporary_manifest, 0o600)
            os.replace(temporary_archive, archive)
            os.replace(temporary_manifest, manifest_path)
            return BackupResult(
                archive=str(archive),
                manifest=str(manifest_path),
                sha256=digest,
                size_bytes=size,
                database_schema_version=schema_version,
                table_counts=table_counts,
            )
        except DatabaseOperationsError:
            _remove_if_exists(temporary_archive)
            _remove_if_exists(temporary_manifest)
            _remove_if_exists(archive if not manifest_path.exists() else None)
            raise
        except Exception as exc:
            _remove_if_exists(temporary_archive)
            _remove_if_exists(temporary_manifest)
            _remove_if_exists(archive if not manifest_path.exists() else None)
            raise DatabaseOperationsError("database backup failed") from exc
        finally:
            if initiated_pause:
                self._maintenance.resume(
                    actor=actor, reason="backup operation finished"
                )

    def restore(self, archive: Path) -> RestoreResult:
        archive = archive.resolve()
        manifest = _read_manifest(archive)
        self._tools.validate(_server_major(self._database_url))
        _ensure_empty_database(self._database_url)
        self._tools.verify(archive)
        self._tools.restore(self._database_url, archive)
        schema_version, table_counts = _validated_snapshot(self._database_url)
        if schema_version != manifest["database_schema_version"]:
            raise DatabaseOperationsError("restored schema version does not match backup")
        if table_counts != manifest["table_counts"]:
            raise DatabaseOperationsError("restored table counts do not match backup")
        maintenance = self._maintenance.status()
        if maintenance.status != "paused":
            raise DatabaseOperationsError("restored database is not maintenance-paused")
        return RestoreResult(
            archive=str(archive),
            database_schema_version=schema_version,
            table_counts=table_counts,
            maintenance_status=maintenance.status,
        )


def _protected_tables(connection: psycopg.Connection[Any]) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = %s AND tablename <> 'database_maintenance'
        ORDER BY tablename
        """,
        (SCHEMA,),
    ).fetchall()
    return tuple(row[0] for row in rows)


def _validated_snapshot(database_url: str) -> tuple[int, dict[str, int]]:
    migration = MigrationRunner(database_url).status()
    if migration.pending_versions:
        raise DatabaseOperationsError("database has pending migrations")
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            counts = {
                name: connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(
                        sql.Identifier(SCHEMA, name)
                    )
                ).fetchone()[0]
                for name in _protected_tables(connection)
            }
            counts["database_maintenance"] = connection.execute(
                f"SELECT count(*) FROM {SCHEMA}.database_maintenance"
            ).fetchone()[0]
    except Exception as exc:
        raise DatabaseOperationsError("database snapshot validation failed") from exc
    return migration.current_version, dict(sorted(counts.items()))


def _ensure_empty_database(database_url: str) -> None:
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            count = connection.execute(
                """
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                """
            ).fetchone()[0]
    except Exception as exc:
        raise DatabaseOperationsError("restore target inspection failed") from exc
    if count:
        raise DatabaseOperationsError("restore target database is not empty")


def _server_major(database_url: str) -> int:
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            version = int(connection.execute("SHOW server_version_num").fetchone()[0])
    except Exception as exc:
        raise DatabaseOperationsError("Postgres server version check failed") from exc
    return version // 10000


def _read_manifest(archive: Path) -> dict[str, Any]:
    manifest_path = _manifest_path(archive)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatabaseOperationsError("backup manifest is missing or invalid") from exc
    required = {
        "format",
        "created_at",
        "archive_name",
        "sha256",
        "size_bytes",
        "database_schema_version",
        "table_counts",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise DatabaseOperationsError("backup manifest contract is invalid")
    if raw["format"] != MANIFEST_FORMAT or raw["archive_name"] != archive.name:
        raise DatabaseOperationsError("backup manifest does not match archive")
    if not archive.is_file():
        raise DatabaseOperationsError("backup archive is missing")
    if (
        type(raw["size_bytes"]) is not int
        or raw["size_bytes"] < 1
        or not isinstance(raw["sha256"], str)
        or SHA256_PATTERN.fullmatch(raw["sha256"]) is None
        or raw["size_bytes"] != archive.stat().st_size
        or raw["sha256"] != _sha256(archive)
    ):
        raise DatabaseOperationsError("backup archive checksum does not match manifest")
    try:
        created_at = datetime.fromisoformat(raw["created_at"])
    except (TypeError, ValueError) as exc:
        raise DatabaseOperationsError("backup manifest values are invalid") from exc
    if (
        created_at.tzinfo is None
        or type(raw["database_schema_version"]) is not int
        or raw["database_schema_version"] < 1
        or not isinstance(raw["table_counts"], dict)
        or not raw["table_counts"]
        or not all(
            isinstance(name, str) and name and type(count) is int and count >= 0
            for name, count in raw["table_counts"].items()
        )
    ):
        raise DatabaseOperationsError("backup manifest values are invalid")
    return raw


def _postgres_environment(database_url: str) -> tuple[dict[str, str], str]:
    try:
        values = conninfo_to_dict(database_url)
    except Exception as exc:
        raise DatabaseConfigurationError("the V5 database URL is invalid") from exc
    dbname = values.get("dbname", "").strip()
    if not dbname:
        raise DatabaseConfigurationError("the V5 database URL requires a database name")
    unsupported = sorted(set(values) - set(PG_ENVIRONMENT))
    if unsupported:
        raise DatabaseConfigurationError(
            "the V5 database URL contains connection options that native "
            f"Postgres tools cannot preserve: {', '.join(unsupported)}"
        )
    environment = _clean_postgres_environment()
    for key, value in values.items():
        variable = PG_ENVIRONMENT.get(key)
        if variable is not None and value is not None:
            environment[variable] = str(value)
    return environment, dbname


def _clean_postgres_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PG")
    }


def _manifest_path(archive: Path) -> Path:
    return archive.with_name(f"{archive.name}.manifest.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_if_exists(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


def _required(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _command(value: Sequence[str], name: str) -> tuple[str, ...]:
    selected = tuple(value)
    if not selected or any(not isinstance(item, str) or not item for item in selected):
        raise ValueError(f"{name} command is invalid")
    return selected
