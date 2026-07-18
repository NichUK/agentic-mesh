from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Literal, Protocol
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError


MemoryScope = Literal["project_role", "project", "organization_role"]
SourceState = Literal["current", "stale", "removed"]
MemoryStatus = Literal["active", "retired"]
SourceKind = Literal["document", "work_item", "event", "decision", "policy", "release"]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SUBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_SOURCE_SCHEMES: dict[str, str] = {
    "document": "document://",
    "work_item": "work-item://",
    "event": "event://",
    "decision": "decision://",
    "policy": "policy://",
    "release": "release://",
}


class SharedMemoryError(DatabaseError):
    pass


class SharedMemoryConflict(SharedMemoryError):
    pass


class SharedMemoryNotFound(SharedMemoryError):
    pass


class SharedMemoryAuthorizationError(SharedMemoryError):
    pass


class SharedMemorySourceError(SharedMemoryError):
    pass


@dataclass(frozen=True)
class MemoryContext:
    organization_id: str
    project_id: str
    role_id: str

    def __post_init__(self) -> None:
        _external_id(self.organization_id, "organization_id")
        _external_id(self.project_id, "project_id")
        _external_id(self.role_id, "role_id")


@dataclass(frozen=True)
class MemorySource:
    kind: SourceKind
    reference: str
    observed_version: str

    def __post_init__(self) -> None:
        expected = _SOURCE_SCHEMES.get(self.kind)
        if expected is None:
            raise SharedMemorySourceError("source kind is not authoritative")
        if not isinstance(self.reference, str) or not self.reference.startswith(expected):
            raise SharedMemorySourceError(
                f"{self.kind} source must use the {expected} reference scheme"
            )
        if (
            len(self.reference) > 1000
            or self.reference != self.reference.strip()
            or any(character.isspace() for character in self.reference)
            or not self.reference[len(expected) :]
        ):
            raise SharedMemorySourceError("source reference is invalid")
        _source_version(self.observed_version, "observed source version")


@dataclass(frozen=True)
class SourceCheck:
    state: SourceState
    current_version: str | None

    def __post_init__(self) -> None:
        if self.state not in {"current", "stale", "removed"}:
            raise SharedMemorySourceError("source verifier returned an invalid state")
        if self.state == "removed":
            if self.current_version is not None:
                raise SharedMemorySourceError("removed source cannot have a current version")
        else:
            _source_version(self.current_version, "current source version")


class MemorySourceVerifier(Protocol):
    def verify(self, source: MemorySource) -> SourceCheck: ...


@dataclass(frozen=True)
class MemoryEntry:
    memory_id: str
    scope: MemoryScope
    organization_id: str
    project_id: str | None
    role_id: str | None
    subject: str
    summary: str
    tags: tuple[str, ...]
    source: MemorySource
    source_state: SourceState
    current_source_version: str | None
    status: MemoryStatus
    version: int
    created_by: str
    updated_by: str
    created_at: datetime | None
    updated_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "scope": self.scope,
            "organization_id": self.organization_id,
            "project_id": self.project_id,
            "role_id": self.role_id,
            "subject": self.subject,
            "summary": self.summary,
            "tags": list(self.tags),
            "source": {
                "kind": self.source.kind,
                "reference": self.source.reference,
                "observed_version": self.source.observed_version,
            },
            "source_state": self.source_state,
            "current_source_version": self.current_source_version,
            "status": self.status,
            "version": self.version,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": _timestamp(self.created_at),
            "updated_at": _timestamp(self.updated_at),
        }


@dataclass(frozen=True)
class MemoryRevision:
    entry: MemoryEntry
    operation_id: str
    request_digest: str
    action: Literal["create", "update", "retire", "source_status"]
    actor_id: str
    recorded_at: datetime


class SharedMemoryStore:
    def __init__(self, database_url: str, *, verifier: MemorySourceVerifier) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._verifier = verifier

    def create(
        self,
        context: MemoryContext,
        *,
        scope: MemoryScope,
        subject: str,
        summary: str,
        tags: tuple[str, ...] | list[str],
        source: MemorySource,
        actor_id: str,
        operation_id: str,
    ) -> MemoryEntry:
        coordinates = _coordinates(context, scope)
        subject = _subject(subject)
        summary = _summary(summary)
        tags = _tags(tags)
        actor_id = _external_id(actor_id, "actor_id")
        operation_id = _operation_id(operation_id)
        digest = _digest(
            "create", coordinates, subject, summary, tags, _source_payload(source), actor_id
        )
        replay = self._find_replay(context, operation_id, digest)
        if replay is not None:
            return replay.entry
        self._require_current(source)
        memory_id = f"mem-{uuid.uuid4().hex}"
        try:
            with psycopg.connect(
                self._database_url, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    self._validate_context(connection, context)
                    replay = self._find_replay_in(
                        connection, context, operation_id, digest
                    )
                    if replay is not None:
                        return replay.entry
                    row = connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.shared_memory_entries
                            (memory_id, scope, organization_id, project_id, role_id,
                             subject, summary, tags, source_kind, source_ref,
                             observed_source_version, source_state,
                             current_source_version, status, version, created_by,
                             updated_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                'current', %s, 'active', 1, %s, %s)
                        RETURNING *
                        """,
                        (
                            memory_id,
                            scope,
                            *coordinates,
                            subject,
                            summary,
                            Jsonb(list(tags)),
                            source.kind,
                            source.reference,
                            source.observed_version,
                            source.observed_version,
                            actor_id,
                            actor_id,
                        ),
                    ).fetchone()
                    assert row is not None
                    entry = _entry(row)
                    self._insert_revision(
                        connection, entry, operation_id, digest, "create", actor_id
                    )
                    return entry
        except SharedMemoryError:
            raise
        except psycopg.errors.UniqueViolation:
            replay = self._find_replay(context, operation_id, digest)
            if replay is not None:
                return replay.entry
            raise SharedMemoryConflict("shared memory entry already exists")
        except psycopg.Error as exc:
            raise SharedMemoryError("shared memory create failed") from exc

    def update(
        self,
        context: MemoryContext,
        memory_id: str,
        *,
        expected_version: int,
        summary: str,
        tags: tuple[str, ...] | list[str],
        source: MemorySource,
        actor_id: str,
        operation_id: str,
    ) -> MemoryEntry:
        return self._mutate(
            context,
            memory_id,
            expected_version=expected_version,
            summary=_summary(summary),
            tags=_tags(tags),
            source=source,
            actor_id=_external_id(actor_id, "actor_id"),
            operation_id=_operation_id(operation_id),
            action="update",
        )

    def retire(
        self,
        context: MemoryContext,
        memory_id: str,
        *,
        expected_version: int,
        actor_id: str,
        operation_id: str,
    ) -> MemoryEntry:
        memory_id = _memory_id(memory_id)
        actor_id = _external_id(actor_id, "actor_id")
        operation_id = _operation_id(operation_id)
        expected_version = _version(expected_version)
        digest = _digest("retire", memory_id, expected_version, actor_id)
        replay = self._find_replay(context, operation_id, digest)
        if replay is not None:
            return replay.entry
        try:
            with psycopg.connect(
                self._database_url, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    self._validate_context(connection, context)
                    replay = self._find_replay_in(
                        connection, context, operation_id, digest
                    )
                    if replay is not None:
                        return replay.entry
                    current = self._lock_visible(connection, context, memory_id)
                    if current.status == "retired" or current.version != expected_version:
                        raise SharedMemoryConflict(
                            f"memory version changed: expected {expected_version}, "
                            f"found {current.version}"
                        )
                    row = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.shared_memory_entries
                        SET status = 'retired', version = version + 1,
                            updated_by = %s, updated_at = clock_timestamp()
                        WHERE memory_id = %s AND version = %s
                        RETURNING *
                        """,
                        (actor_id, memory_id, expected_version),
                    ).fetchone()
                    if row is None:
                        raise SharedMemoryConflict("memory changed concurrently")
                    entry = _entry(row)
                    self._insert_revision(
                        connection, entry, operation_id, digest, "retire", actor_id
                    )
                    return entry
        except SharedMemoryError:
            raise
        except psycopg.Error as exc:
            raise SharedMemoryError("shared memory retire failed") from exc

    def list_current(self, context: MemoryContext) -> tuple[MemoryEntry, ...]:
        return tuple(
            item
            for item in self.inspect(context)
            if item.status == "active" and item.source_state == "current"
        )

    def inspect(
        self, context: MemoryContext, *, include_retired: bool = False
    ) -> tuple[MemoryEntry, ...]:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                self._validate_context(connection, context)
                status = "" if include_retired else "AND status = 'active'"
                rows = connection.execute(
                    f"""
                    SELECT * FROM {SCHEMA}.shared_memory_entries
                    WHERE ({_VISIBILITY_SQL}) {status}
                    ORDER BY scope, subject, memory_id
                    """,
                    _visibility_parameters(context),
                ).fetchall()
            return tuple(self._refresh(context, _entry(row)) for row in rows)
        except SharedMemoryError:
            raise
        except psycopg.Error as exc:
            raise SharedMemoryError("shared memory inspection failed") from exc

    def history(
        self, context: MemoryContext, memory_id: str
    ) -> tuple[MemoryRevision, ...]:
        memory_id = _memory_id(memory_id)
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                self._validate_context(connection, context)
                self._read_visible(connection, context, memory_id)
                rows = connection.execute(
                    f"""
                    SELECT * FROM {SCHEMA}.shared_memory_revisions
                    WHERE memory_id = %s ORDER BY version
                    """,
                    (memory_id,),
                ).fetchall()
            return tuple(_revision(row) for row in rows)
        except SharedMemoryError:
            raise
        except psycopg.Error as exc:
            raise SharedMemoryError("shared memory history failed") from exc

    def _mutate(
        self,
        context: MemoryContext,
        memory_id: str,
        *,
        expected_version: int,
        summary: str,
        tags: tuple[str, ...],
        source: MemorySource,
        actor_id: str,
        operation_id: str,
        action: Literal["update"],
    ) -> MemoryEntry:
        memory_id = _memory_id(memory_id)
        expected_version = _version(expected_version)
        digest = _digest(
            action,
            memory_id,
            expected_version,
            summary,
            tags,
            _source_payload(source),
            actor_id,
        )
        replay = self._find_replay(context, operation_id, digest)
        if replay is not None:
            return replay.entry
        self._require_current(source)
        try:
            with psycopg.connect(
                self._database_url, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    self._validate_context(connection, context)
                    replay = self._find_replay_in(
                        connection, context, operation_id, digest
                    )
                    if replay is not None:
                        return replay.entry
                    current = self._lock_visible(connection, context, memory_id)
                    if current.status != "active":
                        raise SharedMemoryConflict("retired memory cannot be updated")
                    if current.version != expected_version:
                        raise SharedMemoryConflict(
                            f"memory version changed: expected {expected_version}, "
                            f"found {current.version}"
                        )
                    row = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.shared_memory_entries
                        SET summary = %s, tags = %s, source_kind = %s,
                            source_ref = %s, observed_source_version = %s,
                            source_state = 'current', current_source_version = %s,
                            version = version + 1, updated_by = %s,
                            updated_at = clock_timestamp()
                        WHERE memory_id = %s AND version = %s
                        RETURNING *
                        """,
                        (
                            summary,
                            Jsonb(list(tags)),
                            source.kind,
                            source.reference,
                            source.observed_version,
                            source.observed_version,
                            actor_id,
                            memory_id,
                            expected_version,
                        ),
                    ).fetchone()
                    if row is None:
                        raise SharedMemoryConflict("memory changed concurrently")
                    entry = _entry(row)
                    self._insert_revision(
                        connection, entry, operation_id, digest, action, actor_id
                    )
                    return entry
        except SharedMemoryError:
            raise
        except psycopg.errors.UniqueViolation as exc:
            raise SharedMemoryConflict("operation id conflicts with another request") from exc
        except psycopg.Error as exc:
            raise SharedMemoryError("shared memory update failed") from exc

    def _require_current(self, source: MemorySource) -> None:
        try:
            check = self._verifier.verify(source)
        except SharedMemorySourceError:
            raise
        except Exception as exc:
            raise SharedMemorySourceError("authoritative source verification failed") from exc
        if check.state == "removed":
            raise SharedMemorySourceError("authoritative source does not exist")
        if check.state != "current" or check.current_version != source.observed_version:
            raise SharedMemorySourceError("observed source version is stale")

    def _refresh(self, context: MemoryContext, entry: MemoryEntry) -> MemoryEntry:
        for _attempt in range(2):
            try:
                check = self._verifier.verify(entry.source)
            except SharedMemorySourceError:
                raise
            except Exception as exc:
                raise SharedMemorySourceError(
                    "authoritative source verification failed"
                ) from exc
            state: SourceState
            if check.state == "removed":
                state = "removed"
            elif (
                check.state == "current"
                and check.current_version == entry.source.observed_version
            ):
                state = "current"
            else:
                state = "stale"
            if (
                state == entry.source_state
                and check.current_version == entry.current_source_version
            ):
                return entry
            with psycopg.connect(
                self._database_url, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    self._validate_context(connection, context)
                    current = self._lock_visible(connection, context, entry.memory_id)
                    if current.source != entry.source:
                        entry = current
                        continue
                    if (
                        current.source_state == state
                        and current.current_source_version == check.current_version
                    ):
                        return current
                    operation_id = _source_operation_id(current, state, check.current_version)
                    digest = _digest(
                        "source_status",
                        current.memory_id,
                        current.version,
                        state,
                        check.current_version,
                    )
                    row = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.shared_memory_entries
                        SET source_state = %s, current_source_version = %s,
                            version = version + 1, updated_by = 'source-verifier',
                            updated_at = clock_timestamp()
                        WHERE memory_id = %s AND version = %s
                        RETURNING *
                        """,
                        (state, check.current_version, current.memory_id, current.version),
                    ).fetchone()
                    if row is None:
                        entry = self._read_visible(connection, context, entry.memory_id)
                        continue
                    entry = _entry(row)
                    self._insert_revision(
                        connection,
                        entry,
                        operation_id,
                        digest,
                        "source_status",
                        "source-verifier",
                    )
                    return entry
        return entry

    def _find_replay(
        self, context: MemoryContext, operation_id: str, digest: str
    ) -> MemoryRevision | None:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                self._validate_context(connection, context)
                return self._find_replay_in(connection, context, operation_id, digest)
        except SharedMemoryError:
            raise
        except psycopg.Error as exc:
            raise SharedMemoryError("shared memory idempotency check failed") from exc

    def _find_replay_in(
        self,
        connection: psycopg.Connection,
        context: MemoryContext,
        operation_id: str,
        digest: str,
    ) -> MemoryRevision | None:
        row = connection.execute(
            f"""
            SELECT * FROM {SCHEMA}.shared_memory_revisions
            WHERE organization_id = %s
              AND operation_id = %s
              AND request_digest = %s
            """,
            (context.organization_id, operation_id, digest),
        ).fetchone()
        if row is None:
            collision = connection.execute(
                f"""
                SELECT 1 FROM {SCHEMA}.shared_memory_revisions
                WHERE organization_id = %s AND operation_id = %s
                """,
                (context.organization_id, operation_id),
            ).fetchone()
            if collision is not None:
                raise SharedMemoryConflict("operation id conflicts with another request")
            return None
        revision = _revision(row)
        if not _visible(context, revision.entry):
            raise SharedMemoryConflict("operation id conflicts with another request")
        return revision

    @staticmethod
    def _validate_context(connection: psycopg.Connection, context: MemoryContext) -> None:
        row = connection.execute(
            f"""
            SELECT project.organization_id
            FROM {SCHEMA}.projects AS project
            JOIN {SCHEMA}.roles AS role
              ON role.project_id = project.project_id
             AND role.role_id = %s
             AND role.status = 'active'
            WHERE project.project_id = %s AND project.status = 'active'
            """,
            (context.role_id, context.project_id),
        ).fetchone()
        if row is None:
            raise SharedMemoryAuthorizationError("project role context is not active")
        organization_id = row["organization_id"] if isinstance(row, dict) else row[0]
        if organization_id != context.organization_id:
            raise SharedMemoryAuthorizationError("project organization does not match context")

    def _read_visible(
        self,
        connection: psycopg.Connection,
        context: MemoryContext,
        memory_id: str,
    ) -> MemoryEntry:
        row = connection.execute(
            f"""
            SELECT * FROM {SCHEMA}.shared_memory_entries
            WHERE memory_id = %s AND ({_VISIBILITY_SQL})
            """,
            (memory_id, *_visibility_parameters(context)),
        ).fetchone()
        if row is None:
            raise SharedMemoryNotFound("shared memory entry was not found")
        return _entry(row)

    def _lock_visible(
        self,
        connection: psycopg.Connection,
        context: MemoryContext,
        memory_id: str,
    ) -> MemoryEntry:
        row = connection.execute(
            f"""
            SELECT * FROM {SCHEMA}.shared_memory_entries
            WHERE memory_id = %s AND ({_VISIBILITY_SQL}) FOR UPDATE
            """,
            (memory_id, *_visibility_parameters(context)),
        ).fetchone()
        if row is None:
            raise SharedMemoryNotFound("shared memory entry was not found")
        return _entry(row)

    @staticmethod
    def _insert_revision(
        connection: psycopg.Connection,
        entry: MemoryEntry,
        operation_id: str,
        digest: str,
        action: str,
        actor_id: str,
    ) -> None:
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.shared_memory_revisions
                (memory_id, version, operation_id, request_digest, action, scope,
                 organization_id, project_id, role_id, subject, summary, tags,
                 source_kind, source_ref, observed_source_version, source_state,
                 current_source_version, status, created_by, updated_by,
                 created_at, updated_at, actor_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entry.memory_id,
                entry.version,
                operation_id,
                digest,
                action,
                entry.scope,
                entry.organization_id,
                entry.project_id,
                entry.role_id,
                entry.subject,
                entry.summary,
                Jsonb(list(entry.tags)),
                entry.source.kind,
                entry.source.reference,
                entry.source.observed_version,
                entry.source_state,
                entry.current_source_version,
                entry.status,
                entry.created_by,
                entry.updated_by,
                entry.created_at,
                entry.updated_at,
                actor_id,
            ),
        )


_VISIBILITY_SQL = """
    (scope = 'project_role' AND organization_id = %s
     AND project_id = %s AND role_id = %s)
 OR (scope = 'project' AND organization_id = %s AND project_id = %s)
 OR (scope = 'organization_role' AND organization_id = %s AND role_id = %s)
"""


def _visibility_parameters(context: MemoryContext) -> tuple[str, ...]:
    return (
        context.organization_id,
        context.project_id,
        context.role_id,
        context.organization_id,
        context.project_id,
        context.organization_id,
        context.role_id,
    )


def _coordinates(
    context: MemoryContext, scope: MemoryScope
) -> tuple[str, str | None, str | None]:
    if scope == "project_role":
        return context.organization_id, context.project_id, context.role_id
    if scope == "project":
        return context.organization_id, context.project_id, None
    if scope == "organization_role":
        return context.organization_id, None, context.role_id
    raise SharedMemoryAuthorizationError("memory scope is invalid")


def _visible(context: MemoryContext, entry: MemoryEntry) -> bool:
    if entry.organization_id != context.organization_id:
        return False
    if entry.scope == "project_role":
        return entry.project_id == context.project_id and entry.role_id == context.role_id
    if entry.scope == "project":
        return entry.project_id == context.project_id
    return entry.role_id == context.role_id


def _entry(row: dict) -> MemoryEntry:
    return MemoryEntry(
        memory_id=row["memory_id"],
        scope=row["scope"],
        organization_id=row["organization_id"],
        project_id=row["project_id"],
        role_id=row["role_id"],
        subject=row["subject"],
        summary=row["summary"],
        tags=tuple(row["tags"]),
        source=MemorySource(
            row["source_kind"], row["source_ref"], row["observed_source_version"]
        ),
        source_state=row["source_state"],
        current_source_version=row["current_source_version"],
        status=row["status"],
        version=row["version"],
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _revision(row: dict) -> MemoryRevision:
    entry = _entry(row)
    return MemoryRevision(
        entry=entry,
        operation_id=row["operation_id"],
        request_digest=row["request_digest"],
        action=row["action"],
        actor_id=row["actor_id"],
        recorded_at=row["recorded_at"],
    )


def _source_payload(source: MemorySource) -> tuple[str, str, str]:
    return source.kind, source.reference, source.observed_version


def _source_operation_id(
    entry: MemoryEntry, state: SourceState, current_version: str | None
) -> str:
    suffix = hashlib.sha256(
        json.dumps([entry.version, state, current_version]).encode()
    ).hexdigest()[:24]
    return f"source-check-{entry.memory_id[4:]}-{suffix}"


def _digest(*values: object) -> str:
    payload = json.dumps(values, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _external_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise SharedMemoryAuthorizationError(f"{label} is invalid")
    return value


def _operation_id(value: str) -> str:
    return _external_id(value, "operation_id")


def _memory_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"mem-[0-9a-f]{32}", value) is None:
        raise SharedMemoryNotFound("memory id is invalid")
    return value


def _subject(value: str) -> str:
    if not isinstance(value, str) or _SUBJECT.fullmatch(value) is None:
        raise SharedMemoryError("memory subject is invalid")
    return value


def _summary(value: str) -> str:
    return _nonempty(value, "memory summary", maximum=4000)


def _tags(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > 32:
        raise SharedMemoryError("memory tags must be a list of at most 32 values")
    normalized = tuple(sorted({_nonempty(item, "memory tag", maximum=64) for item in values}))
    if len(normalized) != len(values):
        raise SharedMemoryError("memory tags must be unique")
    return normalized


def _nonempty(value, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SharedMemoryError(f"{label} is invalid")
    return value.strip()


def _version(value: int) -> int:
    if type(value) is not int or value < 1:
        raise SharedMemoryConflict("expected version must be a positive integer")
    return value


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _source_version(value: str | None, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(character.isspace() for character in value)
    ):
        raise SharedMemorySourceError(f"{label} is invalid")
    return value
