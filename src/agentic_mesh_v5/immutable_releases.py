from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import re
from typing import Mapping, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.database import SCHEMA


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_TERMINAL = frozenset({"deployed", "rejected", "rolled_back", "failed"})


class ImmutableReleaseError(DatabaseError):
    pass


class ImmutableReleaseConflict(ImmutableReleaseError):
    pass


@dataclass(frozen=True, slots=True)
class UpgradeRequest:
    project_id: str
    operation_id: str
    source_root: Path
    source_revision: str
    actor_id: str


@dataclass(frozen=True, slots=True)
class VerifiedImage:
    image_ref: str
    minimum_database_version: int
    maximum_database_version: int
    build_evidence_ref: str
    test_evidence_ref: str


@dataclass(frozen=True, slots=True)
class DeploymentObservation:
    image_ref: str | None
    healthy: bool
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class ReleaseAttempt:
    project_id: str
    operation_id: str
    source_revision: str
    candidate_release_id: str | None
    previous_image_ref: str | None
    previous_minimum_database_version: int | None
    previous_maximum_database_version: int | None
    status: str
    error: str | None
    evidence: Mapping[str, object]
    started_by: str
    started_at: str
    completed_at: str | None
    version: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ImageBuilder(Protocol):
    def build_and_verify(
        self, request: UpgradeRequest, *, release_id: str
    ) -> VerifiedImage: ...


class DeploymentDriver(Protocol):
    def validate(self, *, forbidden_source_root: Path) -> None: ...

    def observe(self, *, project_id: str) -> DeploymentObservation: ...

    def deploy(self, *, project_id: str, image_ref: str) -> None: ...


class ImmutableReleaseCoordinator:
    def __init__(
        self,
        database_url: str,
        *,
        builder: ImageBuilder,
        deployment: DeploymentDriver,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._builder = builder
        self._deployment = deployment

    def upgrade(self, request: UpgradeRequest) -> ReleaseAttempt:
        request = _request(request)
        release_id = hashlib.sha256(
            f"{request.project_id}:{request.source_revision}".encode("utf-8")
        ).hexdigest()
        with psycopg.connect(self._database_url, autocommit=True) as lock:
            lock.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (f"runtime-release:{request.project_id}",),
            )
            try:
                attempt = self._start(request)
                if attempt.status in _TERMINAL:
                    return attempt
                if attempt.status == "building":
                    attempt = self._build(request, release_id)
                if attempt.status in _TERMINAL:
                    return attempt
                return self._continue(request, attempt)
            finally:
                lock.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (f"runtime-release:{request.project_id}",),
                )

    def get(self, project_id: str, operation_id: str) -> ReleaseAttempt | None:
        project_id = _project_id(project_id)
        operation_id = _external_id(operation_id, "operation_id")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                return self._read(connection, project_id, operation_id)
        except ImmutableReleaseError:
            raise
        except Exception as exc:
            raise ImmutableReleaseError("release attempt read failed") from exc

    def _start(self, request: UpgradeRequest) -> ReleaseAttempt:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.project_runtime_deployment_attempts
                            (project_id, operation_id, source_revision, status,
                             started_by)
                        VALUES (%s, %s, %s, 'building', %s)
                        ON CONFLICT (project_id, operation_id) DO NOTHING
                        """,
                        (
                            request.project_id,
                            request.operation_id,
                            request.source_revision,
                            request.actor_id,
                        ),
                    )
                    attempt = self._required_read(
                        connection, request.project_id, request.operation_id
                    )
                    if attempt.source_revision != request.source_revision:
                        raise ImmutableReleaseConflict(
                            "operation_id belongs to another source revision"
                        )
                    return attempt
        except ImmutableReleaseError:
            raise
        except Exception as exc:
            raise ImmutableReleaseError("release attempt start failed") from exc

    def _build(self, request: UpgradeRequest, release_id: str) -> ReleaseAttempt:
        try:
            self._deployment.validate(forbidden_source_root=request.source_root)
            candidate = _candidate(
                self._builder.build_and_verify(request, release_id=release_id)
            )
            return self._prepare(request, release_id, candidate)
        except ImmutableReleaseError as exc:
            return self._finish(
                request,
                status="rejected",
                error=_safe_error(exc),
                evidence={"stage": "preflight"},
            )
        except Exception as exc:
            return self._finish(
                request,
                status="rejected",
                error=_safe_error(exc),
                evidence={"stage": "preflight"},
            )

    def _prepare(
        self, request: UpgradeRequest, release_id: str, candidate: VerifiedImage
    ) -> ReleaseAttempt:
        database_version = MigrationRunner(self._database_url).status().current_version
        if not (
            candidate.minimum_database_version
            <= database_version
            <= candidate.maximum_database_version
        ):
            raise ImmutableReleaseConflict(
                "candidate does not support the current database schema"
            )
        boundary = self._boundary(request.project_id)
        observed = _observation(
            self._deployment.observe(project_id=request.project_id)
        )
        if observed.image_ref != boundary["running_image_ref"]:
            raise ImmutableReleaseConflict(
                "observed deployment differs from the registered running image"
            )
        if not observed.healthy:
            raise ImmutableReleaseConflict("current deployment is not healthy")
        if candidate.image_ref == observed.image_ref:
            raise ImmutableReleaseConflict("candidate image is already running")
        previous_minimum, previous_maximum = self._previous_schema_range(
            request.project_id,
            observed.image_ref,
            database_version,
        )
        if not previous_minimum <= database_version <= previous_maximum:
            raise ImmutableReleaseConflict(
                "previous image cannot roll back on the current database schema"
            )
        evidence = {
            "build_evidence_ref": candidate.build_evidence_ref,
            "test_evidence_ref": candidate.test_evidence_ref,
            "database_version": database_version,
            "initial_observation_ref": observed.evidence_ref,
        }
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    current = connection.execute(
                        f"""
                        SELECT manifest_digest, running_image_ref
                        FROM {SCHEMA}.project_runtime_boundaries
                        WHERE project_id = %s FOR UPDATE
                        """,
                        (request.project_id,),
                    ).fetchone()
                    if current is None or current["running_image_ref"] != observed.image_ref:
                        raise ImmutableReleaseConflict(
                            "registered running image changed during preflight"
                        )
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.project_runtime_releases
                            (project_id, release_id, manifest_digest,
                             source_revision, image_ref,
                             minimum_database_version,
                             maximum_database_version, build_evidence_ref,
                             test_evidence_ref, status, created_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                'verified', %s)
                        ON CONFLICT (project_id, release_id) DO NOTHING
                        """,
                        (
                            request.project_id,
                            release_id,
                            current["manifest_digest"],
                            request.source_revision,
                            candidate.image_ref,
                            candidate.minimum_database_version,
                            candidate.maximum_database_version,
                            candidate.build_evidence_ref,
                            candidate.test_evidence_ref,
                            request.actor_id,
                        ),
                    )
                    release = connection.execute(
                        f"""
                        SELECT source_revision, image_ref,
                               minimum_database_version,
                               maximum_database_version, build_evidence_ref,
                               test_evidence_ref
                        FROM {SCHEMA}.project_runtime_releases
                        WHERE project_id = %s AND release_id = %s
                        """,
                        (request.project_id, release_id),
                    ).fetchone()
                    expected = (
                        request.source_revision,
                        candidate.image_ref,
                        candidate.minimum_database_version,
                        candidate.maximum_database_version,
                        candidate.build_evidence_ref,
                        candidate.test_evidence_ref,
                    )
                    actual = (
                        None
                        if release is None
                        else (
                            release["source_revision"],
                            release["image_ref"],
                            release["minimum_database_version"],
                            release["maximum_database_version"],
                            release["build_evidence_ref"],
                            release["test_evidence_ref"],
                        )
                    )
                    if actual != expected:
                        raise ImmutableReleaseConflict(
                            "immutable candidate release conflicts with existing record"
                        )
                    updated = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.project_runtime_deployment_attempts
                        SET candidate_release_id = %s,
                            previous_image_ref = %s,
                            previous_minimum_database_version = %s,
                            previous_maximum_database_version = %s,
                            status = 'deploying', evidence = %s,
                            version = version + 1
                        WHERE project_id = %s AND operation_id = %s
                          AND status = 'building'
                        """,
                        (
                            release_id,
                            observed.image_ref,
                            previous_minimum,
                            previous_maximum,
                            Jsonb(evidence),
                            request.project_id,
                            request.operation_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ImmutableReleaseConflict(
                            "release attempt changed during candidate preparation"
                        )
                    return self._required_read(
                        connection, request.project_id, request.operation_id
                    )
        except ImmutableReleaseError:
            raise
        except Exception as exc:
            raise ImmutableReleaseError("candidate release persistence failed") from exc

    def _continue(
        self, request: UpgradeRequest, attempt: ReleaseAttempt
    ) -> ReleaseAttempt:
        candidate = self._release_image(attempt)
        previous = _required_image(attempt.previous_image_ref, "previous_image_ref")
        if attempt.status == "deploying":
            observed, error = self._safe_observe(request.project_id)
            if observed is not None and observed.image_ref == candidate:
                if observed.healthy:
                    return self._deployed(request, attempt, observed)
                attempt = self._phase(request, "rolling_back", error="candidate unhealthy")
            elif observed is not None and observed.image_ref == previous and observed.healthy:
                try:
                    self._deployment.deploy(
                        project_id=request.project_id, image_ref=candidate
                    )
                    attempt = self._phase(request, "verifying")
                except Exception as exc:
                    attempt = self._phase(
                        request, "rolling_back", error=_safe_error(exc)
                    )
            else:
                attempt = self._phase(
                    request,
                    "rolling_back",
                    error=error or "deployment state drifted during upgrade",
                )
        if attempt.status == "verifying":
            observed, error = self._safe_observe(request.project_id)
            if (
                observed is not None
                and observed.image_ref == candidate
                and observed.healthy
            ):
                return self._deployed(request, attempt, observed)
            attempt = self._phase(
                request,
                "rolling_back",
                error=error or "candidate failed post-deployment health",
            )
        if attempt.status == "rolling_back":
            return self._rollback(request, attempt, previous)
        raise ImmutableReleaseConflict(f"unsupported deployment phase: {attempt.status}")

    def _rollback(
        self, request: UpgradeRequest, attempt: ReleaseAttempt, previous: str
    ) -> ReleaseAttempt:
        observed, first_error = self._safe_observe(request.project_id)
        if (
            observed is not None
            and observed.image_ref == previous
            and observed.healthy
        ):
            return self._finish_rollback(request, attempt, observed)
        deploy_error: str | None = None
        try:
            self._deployment.deploy(project_id=request.project_id, image_ref=previous)
        except Exception as exc:
            deploy_error = _safe_error(exc)
        observed, observe_error = self._safe_observe(request.project_id)
        if (
            observed is not None
            and observed.image_ref == previous
            and observed.healthy
        ):
            return self._finish_rollback(request, attempt, observed)
        return self._finish(
            request,
            status="failed",
            error=(
                deploy_error
                or observe_error
                or first_error
                or "rollback did not restore a healthy previous image"
            ),
            evidence={"stage": "rollback"},
        )

    def _deployed(
        self,
        request: UpgradeRequest,
        attempt: ReleaseAttempt,
        observed: DeploymentObservation,
    ) -> ReleaseAttempt:
        candidate = self._release_image(attempt)
        previous = _required_image(attempt.previous_image_ref, "previous_image_ref")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    changed = connection.execute(
                        f"""
                        UPDATE {SCHEMA}.project_runtime_boundaries
                        SET running_image_ref = %s, version = version + 1
                        WHERE project_id = %s AND running_image_ref = %s
                        """,
                        (candidate, request.project_id, previous),
                    )
                    if changed.rowcount != 1:
                        raise ImmutableReleaseConflict(
                            "registered image changed before deployment commit"
                        )
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.project_runtime_releases
                        SET status = 'retired'
                        WHERE project_id = %s AND image_ref = %s
                          AND status = 'deployed'
                        """,
                        (request.project_id, previous),
                    )
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.project_runtime_releases
                        SET status = 'deployed',
                            deployed_at = COALESCE(deployed_at, clock_timestamp())
                        WHERE project_id = %s AND release_id = %s
                        """,
                        (request.project_id, attempt.candidate_release_id),
                    )
                    result = self._finish_in_transaction(
                        connection,
                        request,
                        status="deployed",
                        error=None,
                        evidence={"final_observation_ref": observed.evidence_ref},
                    )
                    self._audit(connection, request, result, candidate)
                    return result
        except ImmutableReleaseError:
            raise
        except Exception as exc:
            raise ImmutableReleaseError("deployment commit failed") from exc

    def _finish_rollback(
        self,
        request: UpgradeRequest,
        attempt: ReleaseAttempt,
        observed: DeploymentObservation,
    ) -> ReleaseAttempt:
        result = self._finish(
            request,
            status="rolled_back",
            error=attempt.error or "candidate deployment was rolled back",
            evidence={"rollback_observation_ref": observed.evidence_ref},
        )
        return result

    def _phase(
        self,
        request: UpgradeRequest,
        status: str,
        *,
        error: str | None = None,
    ) -> ReleaseAttempt:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                connection.execute(
                    f"""
                    UPDATE {SCHEMA}.project_runtime_deployment_attempts
                    SET status = %s, error = COALESCE(%s, error),
                        version = version + 1
                    WHERE project_id = %s AND operation_id = %s
                      AND status NOT IN ('deployed', 'rejected', 'rolled_back', 'failed')
                    """,
                    (
                        status,
                        error,
                        request.project_id,
                        request.operation_id,
                    ),
                )
                return self._required_read(
                    connection, request.project_id, request.operation_id
                )
        except Exception as exc:
            raise ImmutableReleaseError("deployment phase update failed") from exc

    def _finish(
        self,
        request: UpgradeRequest,
        *,
        status: str,
        error: str | None,
        evidence: Mapping[str, object],
    ) -> ReleaseAttempt:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                with connection.transaction():
                    result = self._finish_in_transaction(
                        connection,
                        request,
                        status=status,
                        error=error,
                        evidence=evidence,
                    )
                    if result.candidate_release_id is not None and status != "deployed":
                        connection.execute(
                            f"""
                            UPDATE {SCHEMA}.project_runtime_releases
                            SET status = 'rejected'
                            WHERE project_id = %s AND release_id = %s
                              AND status = 'verified'
                            """,
                            (request.project_id, result.candidate_release_id),
                        )
                    self._audit(
                        connection,
                        request,
                        result,
                        result.candidate_release_id or request.source_revision,
                    )
                    return result
        except ImmutableReleaseError:
            raise
        except Exception as exc:
            raise ImmutableReleaseError("release attempt completion failed") from exc

    def _finish_in_transaction(
        self,
        connection,
        request: UpgradeRequest,
        *,
        status: str,
        error: str | None,
        evidence: Mapping[str, object],
    ) -> ReleaseAttempt:
        current = self._required_read(
            connection, request.project_id, request.operation_id
        )
        if current.status in _TERMINAL:
            return current
        merged = {**current.evidence, **evidence}
        connection.execute(
            f"""
            UPDATE {SCHEMA}.project_runtime_deployment_attempts
            SET status = %s, error = %s, evidence = %s,
                completed_at = clock_timestamp(), version = version + 1
            WHERE project_id = %s AND operation_id = %s
            """,
            (
                status,
                error,
                Jsonb(merged),
                request.project_id,
                request.operation_id,
            ),
        )
        return self._required_read(
            connection, request.project_id, request.operation_id
        )

    def _boundary(self, project_id: str) -> Mapping[str, object]:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                row = connection.execute(
                    f"""
                    SELECT manifest_digest, running_image_ref
                    FROM {SCHEMA}.project_runtime_boundaries
                    WHERE project_id = %s
                    """,
                    (project_id,),
                ).fetchone()
            if row is None:
                raise ImmutableReleaseConflict("project runtime boundary is missing")
            return row
        except ImmutableReleaseError:
            raise
        except Exception as exc:
            raise ImmutableReleaseError("project runtime boundary read failed") from exc

    def _previous_schema_range(
        self, project_id: str, image_ref: str, database_version: int
    ) -> tuple[int, int]:
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            row = connection.execute(
                f"""
                SELECT minimum_database_version, maximum_database_version
                FROM {SCHEMA}.project_runtime_releases
                WHERE project_id = %s AND image_ref = %s
                """,
                (project_id, image_ref),
            ).fetchone()
        return (database_version, database_version) if row is None else row

    def _release_image(self, attempt: ReleaseAttempt) -> str:
        if attempt.candidate_release_id is None:
            raise ImmutableReleaseConflict("deployment attempt has no candidate release")
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            row = connection.execute(
                f"""
                SELECT image_ref FROM {SCHEMA}.project_runtime_releases
                WHERE project_id = %s AND release_id = %s
                """,
                (attempt.project_id, attempt.candidate_release_id),
            ).fetchone()
        if row is None:
            raise ImmutableReleaseConflict("candidate release is missing")
        return _required_image(row[0], "candidate image_ref")

    def _safe_observe(
        self, project_id: str
    ) -> tuple[DeploymentObservation | None, str | None]:
        try:
            return _observation(
                self._deployment.observe(project_id=project_id)
            ), None
        except Exception as exc:
            return None, _safe_error(exc)

    def _audit(
        self,
        connection,
        request: UpgradeRequest,
        result: ReleaseAttempt,
        object_id: str,
    ) -> None:
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.audit_records
                (scope, project_id, actor_id, action, object_type,
                 object_id, details)
            VALUES ('project', %s, %s, %s, 'runtime-release', %s, %s)
            """,
            (
                request.project_id,
                request.actor_id,
                f"project.runtime_{result.status}",
                object_id,
                Jsonb(
                    {
                        "operation_id": request.operation_id,
                        "source_revision": request.source_revision,
                        "status": result.status,
                    }
                ),
            ),
        )

    @staticmethod
    def _read(connection, project_id: str, operation_id: str) -> ReleaseAttempt | None:
        row = connection.execute(
            f"""
            SELECT project_id, operation_id, source_revision,
                   candidate_release_id, previous_image_ref,
                   previous_minimum_database_version,
                   previous_maximum_database_version, status, error,
                   evidence, started_by, started_at::text,
                   completed_at::text, version
            FROM {SCHEMA}.project_runtime_deployment_attempts
            WHERE project_id = %s AND operation_id = %s
            """,
            (project_id, operation_id),
        ).fetchone()
        return None if row is None else ReleaseAttempt(**row)

    @classmethod
    def _required_read(
        cls, connection, project_id: str, operation_id: str
    ) -> ReleaseAttempt:
        attempt = cls._read(connection, project_id, operation_id)
        if attempt is None:
            raise ImmutableReleaseConflict("release attempt is missing")
        return attempt


def _request(value: UpgradeRequest) -> UpgradeRequest:
    if not isinstance(value, UpgradeRequest):
        raise ValueError("UpgradeRequest is required")
    return UpgradeRequest(
        project_id=_project_id(value.project_id),
        operation_id=_external_id(value.operation_id, "operation_id"),
        source_root=_existing_root(value.source_root, "source_root"),
        source_revision=_commit(value.source_revision),
        actor_id=_external_id(value.actor_id, "actor_id"),
    )


def _candidate(value: VerifiedImage) -> VerifiedImage:
    if not isinstance(value, VerifiedImage):
        raise ImmutableReleaseConflict("builder returned an invalid candidate")
    minimum = _positive(value.minimum_database_version, "minimum_database_version")
    maximum = _positive(value.maximum_database_version, "maximum_database_version")
    if minimum > maximum:
        raise ImmutableReleaseConflict("candidate database range is invalid")
    return VerifiedImage(
        image_ref=_required_image(value.image_ref, "candidate image_ref"),
        minimum_database_version=minimum,
        maximum_database_version=maximum,
        build_evidence_ref=_reference(value.build_evidence_ref, "build_evidence_ref"),
        test_evidence_ref=_reference(value.test_evidence_ref, "test_evidence_ref"),
    )


def _observation(value: DeploymentObservation) -> DeploymentObservation:
    if not isinstance(value, DeploymentObservation):
        raise ImmutableReleaseConflict("deployment observation is invalid")
    image_ref = (
        None
        if value.image_ref is None
        else _required_image(value.image_ref, "observed image_ref")
    )
    if not isinstance(value.healthy, bool):
        raise ImmutableReleaseConflict("deployment health is invalid")
    return DeploymentObservation(
        image_ref=image_ref,
        healthy=value.healthy,
        evidence_ref=_reference(value.evidence_ref, "observation evidence_ref"),
    )


def _project_id(value: object) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise ValueError("project_id is invalid")
    return value


def _external_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _EXTERNAL_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _commit(value: object) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError("source_revision is invalid")
    return value


def _required_image(value: object, field: str) -> str:
    if not isinstance(value, str) or _IMAGE.fullmatch(value) is None:
        raise ImmutableReleaseConflict(f"{field} must be digest-pinned")
    return value


def _reference(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 500
        or any(character in value for character in ("\x00", "\n", "\r"))
    ):
        raise ImmutableReleaseConflict(f"{field} is invalid")
    return value.strip()


def _positive(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ImmutableReleaseConflict(f"{field} must be a positive integer")
    return value


def _existing_root(value: object, field: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or not value.is_dir():
        raise ValueError(f"{field} must be an existing absolute directory")
    return value.resolve()


def _safe_error(error: Exception) -> str:
    detail = " ".join(str(error).split())[:500]
    lowered = detail.casefold()
    if any(
        marker in lowered
        for marker in ("password=", "client_secret", "authorization: bearer")
    ):
        return "operation failed with a restricted diagnostic"
    return detail or error.__class__.__name__
