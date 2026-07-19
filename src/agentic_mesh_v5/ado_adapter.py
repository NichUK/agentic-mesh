from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import time
from urllib.parse import quote, urlencode

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.document_store import AccessTokenProvider, HttpResponse, HttpTransport


_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_FIELDS: dict[str, tuple[int, bool]] = {
    "System.Title": (400, False),
    "System.State": (100, False),
    "System.Description": (100_000, True),
    "System.Tags": (4_000, True),
    "Microsoft.VSTS.Common.AcceptanceCriteria": (100_000, True),
}


class AdoAdapterError(DatabaseError):
    pass


class AdoConfigurationError(AdoAdapterError):
    pass


class AdoAuthenticationError(AdoAdapterError):
    pass


class AdoPermissionDenied(AdoAdapterError):
    pass


class AdoNotFound(AdoAdapterError):
    pass


class AdoConflict(AdoAdapterError):
    pass


class AdoForeignProject(AdoAdapterError):
    pass


class AdoInvalidResponse(AdoAdapterError):
    pass


class AdoUnavailable(AdoAdapterError):
    def __init__(self, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class AdoBinding:
    project_id: str
    manifest_digest: str
    organization_url: str
    ado_project: str
    credential_provider: str
    credential_reference: str


@dataclass(frozen=True, slots=True)
class AdoWorkItem:
    external_work_item_id: int
    revision: int
    team_project: str
    fields: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AdoLink:
    project_id: str
    work_item_id: str
    manifest_digest: str
    organization_url: str
    ado_project: str
    external_work_item_id: int
    external_url: str
    linked_by: str
    linked_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AdoUpdateOperation:
    project_id: str
    operation_id: str
    work_item_id: str
    request_digest: str
    requested_fields: Mapping[str, str]
    status: str
    external_revision: int | None
    attempt_count: int
    last_error: str | None
    started_by: str
    started_at: str
    completed_at: str | None
    version: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ProjectAdoAdapter:
    def __init__(
        self,
        database_url: str,
        *,
        token_provider: AccessTokenProvider,
        transport: HttpTransport,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if max_attempts < 1 or max_attempts > 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self._database_url = database_url
        self._tokens = token_provider
        self._transport = transport
        self._max_attempts = max_attempts
        self._sleep = sleeper

    def link(
        self,
        *,
        project_id: str,
        work_item_id: str,
        external_work_item_id: int,
        actor_id: str,
    ) -> AdoLink:
        project_id = _project_id(project_id)
        work_item_id = _external_id(work_item_id, "work_item_id")
        actor_id = _external_id(actor_id, "actor_id")
        external_work_item_id = _positive_id(external_work_item_id)
        binding = self._binding(project_id)
        remote, _ = self._fetch(binding, external_work_item_id)
        return self._persist_link(
            binding=binding,
            work_item_id=work_item_id,
            remote=remote,
            actor_id=actor_id,
        )

    def read(self, *, project_id: str, work_item_id: str) -> AdoWorkItem:
        link, binding = self._link_and_binding(project_id, work_item_id)
        remote, _ = self._fetch(binding, link.external_work_item_id)
        return remote

    def update(
        self,
        *,
        project_id: str,
        work_item_id: str,
        operation_id: str,
        fields: Mapping[str, str],
        actor_id: str,
    ) -> AdoUpdateOperation:
        project_id = _project_id(project_id)
        work_item_id = _external_id(work_item_id, "work_item_id")
        operation_id = _external_id(operation_id, "operation_id")
        actor_id = _external_id(actor_id, "actor_id")
        requested = _requested_fields(fields)
        digest = hashlib.sha256(
            json.dumps(requested, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with psycopg.connect(self._database_url, autocommit=True) as lock:
            lock.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (f"ado-update:{project_id}:{operation_id}",),
            )
            try:
                link, binding = self._link_and_binding(project_id, work_item_id)
                operation = self._start_operation(
                    project_id=project_id,
                    work_item_id=work_item_id,
                    operation_id=operation_id,
                    actor_id=actor_id,
                    requested=requested,
                    digest=digest,
                )
                if operation.status == "succeeded":
                    return operation
                return self._apply_update(operation, link, binding)
            finally:
                lock.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (f"ado-update:{project_id}:{operation_id}",),
                )

    def get_link(self, *, project_id: str, work_item_id: str) -> AdoLink:
        return self._read_link(
            _project_id(project_id), _external_id(work_item_id, "work_item_id")
        )

    def get_operation(
        self, *, project_id: str, operation_id: str
    ) -> AdoUpdateOperation | None:
        project_id = _project_id(project_id)
        operation_id = _external_id(operation_id, "operation_id")
        with psycopg.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            return self._read_operation(connection, project_id, operation_id)

    def _apply_update(
        self, operation: AdoUpdateOperation, link: AdoLink, binding: AdoBinding
    ) -> AdoUpdateOperation:
        try:
            remote, read_attempts = self._fetch(
                binding, link.external_work_item_id
            )
        except AdoUnavailable as exc:
            self._record_pending(operation, exc.attempts, "ADO is unavailable")
            raise
        if _matches(remote, operation.requested_fields):
            return self._complete(operation, remote.revision, read_attempts)
        try:
            updated, patch_attempts = self._patch(
                binding,
                link.external_work_item_id,
                remote.revision,
                operation.requested_fields,
            )
        except AdoUnavailable as exc:
            self._record_pending(
                operation, read_attempts + exc.attempts, "ADO is unavailable"
            )
            raise
        if not _matches(updated, operation.requested_fields):
            self._record_pending(
                operation,
                read_attempts + patch_attempts,
                "ADO did not retain the requested fields",
            )
            raise AdoConflict("ADO did not retain the requested fields")
        return self._complete(
            operation, updated.revision, read_attempts + patch_attempts
        )

    def _fetch(
        self, binding: AdoBinding, external_work_item_id: int
    ) -> tuple[AdoWorkItem, int]:
        selected = tuple(sorted({"System.TeamProject", *_FIELDS}))
        query = urlencode({"fields": ",".join(selected), "api-version": "7.1"})
        response, attempts = self._request(
            binding,
            "GET",
            f"{self._item_url(binding, external_work_item_id)}?{query}",
            content=None,
        )
        return self._decode(response, binding, external_work_item_id), attempts

    def _patch(
        self,
        binding: AdoBinding,
        external_work_item_id: int,
        revision: int,
        fields: Mapping[str, str],
    ) -> tuple[AdoWorkItem, int]:
        patch: list[dict[str, object]] = [
            {"op": "test", "path": "/rev", "value": revision}
        ]
        patch.extend(
            {"op": "add", "path": f"/fields/{field}", "value": value}
            for field, value in sorted(fields.items())
        )
        response, attempts = self._request(
            binding,
            "PATCH",
            f"{self._item_url(binding, external_work_item_id)}?api-version=7.1",
            content=json.dumps(patch, separators=(",", ":")).encode(),
        )
        return self._decode(response, binding, external_work_item_id), attempts

    def _request(
        self,
        binding: AdoBinding,
        method: str,
        url: str,
        *,
        content: bytes | None,
    ) -> tuple[HttpResponse, int]:
        try:
            token = self._tokens.access_token(
                provider=binding.credential_provider,
                reference=binding.credential_reference,
            )
        except Exception:
            raise AdoAuthenticationError("ADO credential is unavailable") from None
        if not isinstance(token, str) or not token:
            raise AdoAuthenticationError("ADO credential is unavailable")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if content is not None:
            headers["Content-Type"] = "application/json-patch+json"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._transport.request(
                    method,
                    url,
                    headers=headers,
                    content=content,
                    follow_redirects=False,
                )
            except Exception:
                response = None
            if response is None or response.status_code == 429 or response.status_code >= 500:
                if attempt == self._max_attempts:
                    raise AdoUnavailable("ADO is unavailable", attempts=attempt)
                self._sleep(_retry_seconds(response, attempt))
                continue
            if response.status_code == 401:
                raise AdoAuthenticationError("ADO authentication failed")
            if response.status_code == 403:
                raise AdoPermissionDenied("ADO permission denied")
            if response.status_code == 404:
                raise AdoNotFound("ADO work item was not found")
            if response.status_code in {409, 412}:
                raise AdoConflict("ADO work item revision changed")
            if response.status_code != 200:
                raise AdoInvalidResponse("ADO returned an unexpected status")
            return response, attempt
        raise AssertionError("unreachable")

    @staticmethod
    def _decode(
        response: HttpResponse,
        binding: AdoBinding,
        external_work_item_id: int,
    ) -> AdoWorkItem:
        if len(response.content) > 1024 * 1024:
            raise AdoInvalidResponse("ADO response is too large")
        try:
            payload = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AdoInvalidResponse("ADO response is invalid") from None
        if not isinstance(payload, Mapping):
            raise AdoInvalidResponse("ADO response is invalid")
        remote_id = payload.get("id")
        revision = payload.get("rev")
        fields = payload.get("fields")
        if (
            isinstance(remote_id, bool)
            or remote_id != external_work_item_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(fields, Mapping)
        ):
            raise AdoInvalidResponse("ADO response is invalid")
        team_project = fields.get("System.TeamProject")
        if not isinstance(team_project, str):
            raise AdoInvalidResponse("ADO response omits System.TeamProject")
        if team_project != binding.ado_project:
            raise AdoForeignProject("ADO work item belongs to another project")
        normalized: dict[str, str] = {}
        for name in _FIELDS:
            value = fields.get(name)
            if value is not None:
                if not isinstance(value, str):
                    raise AdoInvalidResponse("ADO work item fields are invalid")
                normalized[name] = value
        return AdoWorkItem(remote_id, revision, team_project, normalized)

    def _binding(self, project_id: str) -> AdoBinding:
        with psycopg.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            row = connection.execute(
                f"""
                SELECT active.manifest_digest, snapshot.snapshot
                FROM {SCHEMA}.project_manifest_active AS active
                JOIN {SCHEMA}.project_manifest_snapshots AS snapshot
                  ON snapshot.project_id = active.project_id
                 AND snapshot.manifest_digest = active.manifest_digest
                WHERE active.project_id = %s
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            raise AdoConfigurationError("active project manifest is missing")
        snapshot = row["snapshot"]
        try:
            ado = snapshot["ado"]
            credential = snapshot["credentials"][ado["credential"]]
            organization = ado["organization"]
            ado_project = ado["project"]
            provider = credential["provider"]
            reference = credential["reference"]
        except (KeyError, TypeError):
            raise AdoConfigurationError("active ADO binding is invalid") from None
        values = (organization, ado_project, provider, reference)
        if not all(isinstance(item, str) and item for item in values):
            raise AdoConfigurationError("active ADO binding is invalid")
        return AdoBinding(
            project_id,
            row["manifest_digest"],
            organization.rstrip("/"),
            ado_project,
            provider,
            reference,
        )

    def _link_and_binding(
        self, project_id: str, work_item_id: str
    ) -> tuple[AdoLink, AdoBinding]:
        project_id = _project_id(project_id)
        work_item_id = _external_id(work_item_id, "work_item_id")
        link = self._read_link(project_id, work_item_id)
        binding = self._binding(project_id)
        if (
            link.manifest_digest != binding.manifest_digest
            or link.organization_url != binding.organization_url
            or link.ado_project != binding.ado_project
        ):
            raise AdoConfigurationError(
                "linked ADO item does not match the active project manifest"
            )
        return link, binding

    def _persist_link(
        self,
        *,
        binding: AdoBinding,
        work_item_id: str,
        remote: AdoWorkItem,
        actor_id: str,
    ) -> AdoLink:
        canonical_url = (
            f"{binding.organization_url}/{quote(binding.ado_project, safe='')}"
            f"/_workitems/edit/{remote.external_work_item_id}"
        )
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    current = connection.execute(
                        f"""
                        SELECT manifest_digest
                        FROM {SCHEMA}.project_manifest_active
                        WHERE project_id = %s FOR SHARE
                        """,
                        (binding.project_id,),
                    ).fetchone()
                    if current is None or current["manifest_digest"] != binding.manifest_digest:
                        raise AdoConfigurationError(
                            "active project manifest changed during ADO link"
                        )
                    if connection.execute(
                        f"SELECT 1 FROM {SCHEMA}.work_items "
                        "WHERE project_id = %s AND work_item_id = %s FOR SHARE",
                        (binding.project_id, work_item_id),
                    ).fetchone() is None:
                        raise AdoNotFound("V5 work item was not found")
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.work_item_ado_links
                            (project_id, work_item_id, manifest_digest,
                             organization_url, ado_project,
                             external_work_item_id, external_url, linked_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (project_id, work_item_id) DO NOTHING
                        """,
                        (
                            binding.project_id,
                            work_item_id,
                            binding.manifest_digest,
                            binding.organization_url,
                            binding.ado_project,
                            remote.external_work_item_id,
                            canonical_url,
                            actor_id,
                        ),
                    )
                    link = self._read_link_connection(
                        connection, binding.project_id, work_item_id
                    )
                    expected = (
                        binding.manifest_digest,
                        binding.organization_url,
                        binding.ado_project,
                        remote.external_work_item_id,
                        canonical_url,
                    )
                    actual = (
                        link.manifest_digest,
                        link.organization_url,
                        link.ado_project,
                        link.external_work_item_id,
                        link.external_url,
                    )
                    if actual != expected:
                        raise AdoConflict("V5 work item has another ADO link")
                    return link
        except AdoAdapterError:
            raise
        except psycopg.errors.UniqueViolation as exc:
            raise AdoConflict("ADO work item is linked elsewhere") from exc
        except Exception as exc:
            raise AdoAdapterError("ADO link persistence failed") from exc

    def _start_operation(
        self,
        *,
        project_id: str,
        work_item_id: str,
        operation_id: str,
        actor_id: str,
        requested: Mapping[str, str],
        digest: str,
    ) -> AdoUpdateOperation:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.work_item_ado_update_operations
                            (project_id, operation_id, work_item_id,
                             request_digest, requested_fields, started_by)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (project_id, operation_id) DO NOTHING
                        """,
                        (
                            project_id,
                            operation_id,
                            work_item_id,
                            digest,
                            Jsonb(dict(requested)),
                            actor_id,
                        ),
                    )
                    operation = self._required_operation(
                        connection, project_id, operation_id
                    )
                    if (
                        operation.work_item_id != work_item_id
                        or operation.request_digest != digest
                        or dict(operation.requested_fields) != dict(requested)
                    ):
                        raise AdoConflict(
                            "operation_id belongs to another ADO update"
                        )
                    return operation
        except AdoAdapterError:
            raise
        except Exception as exc:
            raise AdoAdapterError("ADO update operation persistence failed") from exc

    def _record_pending(
        self, operation: AdoUpdateOperation, attempts: int, error: str
    ) -> None:
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            connection.execute(
                f"""
                UPDATE {SCHEMA}.work_item_ado_update_operations
                SET attempt_count = attempt_count + %s, last_error = %s,
                    version = version + 1
                WHERE project_id = %s AND operation_id = %s
                  AND status = 'pending'
                """,
                (attempts, error, operation.project_id, operation.operation_id),
            )

    def _complete(
        self, operation: AdoUpdateOperation, revision: int, attempts: int
    ) -> AdoUpdateOperation:
        with psycopg.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            with connection.transaction():
                connection.execute(
                    f"""
                    UPDATE {SCHEMA}.work_item_ado_update_operations
                    SET status = 'succeeded', external_revision = %s,
                        attempt_count = attempt_count + %s, last_error = NULL,
                        completed_at = clock_timestamp(), version = version + 1
                    WHERE project_id = %s AND operation_id = %s
                      AND status = 'pending'
                    """,
                    (
                        revision,
                        attempts,
                        operation.project_id,
                        operation.operation_id,
                    ),
                )
                return self._required_operation(
                    connection, operation.project_id, operation.operation_id
                )

    def _read_link(self, project_id: str, work_item_id: str) -> AdoLink:
        with psycopg.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        ) as connection:
            return self._read_link_connection(connection, project_id, work_item_id)

    @staticmethod
    def _read_link_connection(connection, project_id: str, work_item_id: str) -> AdoLink:
        row = connection.execute(
            f"""
            SELECT project_id, work_item_id, manifest_digest,
                   organization_url, ado_project, external_work_item_id,
                   external_url, linked_by, linked_at::text
            FROM {SCHEMA}.work_item_ado_links
            WHERE project_id = %s AND work_item_id = %s
            """,
            (project_id, work_item_id),
        ).fetchone()
        if row is None:
            raise AdoNotFound("V5 work item has no ADO link")
        return AdoLink(**row)

    @staticmethod
    def _read_operation(
        connection, project_id: str, operation_id: str
    ) -> AdoUpdateOperation | None:
        row = connection.execute(
            f"""
            SELECT project_id, operation_id, work_item_id, request_digest,
                   requested_fields, status, external_revision, attempt_count,
                   last_error, started_by, started_at::text,
                   completed_at::text, version
            FROM {SCHEMA}.work_item_ado_update_operations
            WHERE project_id = %s AND operation_id = %s
            """,
            (project_id, operation_id),
        ).fetchone()
        return None if row is None else AdoUpdateOperation(**row)

    @classmethod
    def _required_operation(
        cls, connection, project_id: str, operation_id: str
    ) -> AdoUpdateOperation:
        operation = cls._read_operation(connection, project_id, operation_id)
        if operation is None:
            raise AdoConflict("ADO update operation is missing")
        return operation

    @staticmethod
    def _item_url(binding: AdoBinding, external_work_item_id: int) -> str:
        project = quote(binding.ado_project, safe="")
        return (
            f"{binding.organization_url}/{project}/_apis/wit/workitems/"
            f"{external_work_item_id}"
        )


def _requested_fields(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("fields must be a non-empty mapping")
    normalized: dict[str, str] = {}
    for name, field_value in value.items():
        if name not in _FIELDS:
            raise ValueError(f"ADO field is not supported: {name}")
        if not isinstance(field_value, str):
            raise ValueError(f"ADO field must be a string: {name}")
        maximum, allow_empty = _FIELDS[name]
        if len(field_value) > maximum or (not allow_empty and not field_value.strip()):
            raise ValueError(f"ADO field value is invalid: {name}")
        normalized[name] = field_value
    return dict(sorted(normalized.items()))


def _matches(remote: AdoWorkItem, fields: Mapping[str, str]) -> bool:
    return all(remote.fields.get(name) == value for name, value in fields.items())


def _retry_seconds(response: HttpResponse | None, attempt: int) -> float:
    if response is not None:
        value = next(
            (value for key, value in response.headers.items() if key.casefold() == "retry-after"),
            None,
        )
        if isinstance(value, str) and value.isdigit():
            return min(float(value), 5.0)
    return min(0.1 * (2 ** (attempt - 1)), 2.0)


def _project_id(value: object) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise ValueError("project_id is invalid")
    return value


def _external_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _EXTERNAL_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _positive_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("external_work_item_id must be a positive integer")
    return value
