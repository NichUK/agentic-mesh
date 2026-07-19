from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.import_questions import ImportQuestionResolution
from agentic_mesh_v5.project_manifest import ProjectManifest, validate_project_manifest


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SECRET_MARKERS = (
    "password=",
    "secret=",
    "token=",
    "authorization: bearer",
    "-----begin private key-----",
)


class ImportPreviewError(ValueError):
    pass


class ImportPreviewNotFound(ImportPreviewError):
    pass


class ImportPreviewConflict(ImportPreviewError):
    pass


class ImportActivationError(ImportPreviewError):
    pass


class ImportPreviewStoreError(DatabaseError):
    pass


@dataclass(frozen=True, slots=True)
class SourceSelection:
    source_key: str
    decision: Literal["include", "exclude"]
    rationale: str
    resource_kind: str | None = None
    resource_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_key", _source_key(self.source_key))
        if self.decision not in {"include", "exclude"}:
            raise ImportPreviewError("source decision must be include or exclude")
        object.__setattr__(self, "rationale", _safe_text(self.rationale, "rationale", 2_000))
        if self.decision == "include":
            if self.resource_kind is None or self.resource_key is None:
                raise ImportPreviewError(
                    "included source requires an exact manifest resource"
                )
            kind = _text(self.resource_kind, "resource_kind", 100)
            key = _text(self.resource_key, "resource_key", 2_000)
            object.__setattr__(self, "resource_kind", kind)
            object.__setattr__(self, "resource_key", key)
        elif self.resource_kind is not None or self.resource_key is not None:
            raise ImportPreviewError("excluded source cannot name a manifest resource")

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BacklogCandidate:
    candidate_id: str
    title: str
    description: str
    source_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _id(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "title", _safe_text(self.title, "title", 300))
        object.__setattr__(self, "description", _safe_text(self.description, "description", 4_000))
        if not isinstance(self.source_keys, tuple) or not self.source_keys:
            raise ImportPreviewError("candidate source_keys are required")
        normalized = tuple(_source_key(item) for item in self.source_keys)
        if len(normalized) != len(set(normalized)):
            raise ImportPreviewError("candidate source_keys must be unique")
        object.__setattr__(self, "source_keys", normalized)

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "source_keys": list(self.source_keys), "status": "candidate"}


@dataclass(frozen=True, slots=True)
class ImportPreview:
    import_id: str
    revision: int
    question_version: int
    resolution_digest: str
    preview_digest: str
    project_id: str
    manifest_digest: str
    manifest_snapshot: Mapping[str, object]
    source_selections: tuple[SourceSelection, ...]
    backlog_candidates: tuple[BacklogCandidate, ...]
    selected_candidate_ids: tuple[str, ...]
    status: str
    created_by: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ImportPreviewDecision:
    import_id: str
    revision: int
    sponsor_id: str
    decision: str
    rationale: str
    evidence: Mapping[str, object]
    decided_at: str


@dataclass(frozen=True, slots=True)
class ImportActivationRequest:
    import_id: str
    revision: int
    operation_id: str
    manifest: ProjectManifest
    sponsor_ids: tuple[str, ...]
    source_selections: tuple[SourceSelection, ...]
    selected_candidates: tuple[BacklogCandidate, ...]
    requested_by: str


@dataclass(frozen=True, slots=True)
class ImportActivationReceipt:
    project_id: str
    manifest_digest: str
    selected_candidate_ids: tuple[str, ...]
    activation_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _project_id(self.project_id))
        digest = _digest_value(self.manifest_digest, "manifest_digest")
        object.__setattr__(self, "manifest_digest", digest)
        selected = tuple(_id(item, "candidate_id") for item in self.selected_candidate_ids)
        if selected != tuple(sorted(set(selected))):
            raise ImportActivationError("receipt candidate ids must be sorted and unique")
        object.__setattr__(self, "selected_candidate_ids", selected)
        reference = _safe_text(self.activation_ref, "activation_ref", 1_000)
        object.__setattr__(self, "activation_ref", reference)

class ImportActivator(Protocol):
    """Idempotently activate one exact request, keyed by operation_id."""

    def activate(self, request: ImportActivationRequest) -> ImportActivationReceipt: ...


def preview_source_keys(resolution: ImportQuestionResolution) -> tuple[str, ...]:
    if not isinstance(resolution, ImportQuestionResolution):
        raise ImportPreviewError("question resolution is required")
    return _report_source_keys(resolution.discovery_report)


class ImportPreviewStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def create_preview(
        self,
        *,
        resolution: ImportQuestionResolution,
        manifest: ProjectManifest,
        source_selections: Sequence[SourceSelection],
        backlog_candidates: Sequence[BacklogCandidate],
        selected_candidate_ids: Sequence[str],
        expected_prior_revision: int,
        actor_id: str,
    ) -> ImportPreview:
        import_id = _uuid(resolution.import_id)
        actor_id = _actor(actor_id)
        if type(expected_prior_revision) is not int or expected_prior_revision < 0:
            raise ImportPreviewError("expected_prior_revision is invalid")
        manifest = _validated_manifest(manifest)
        selections = tuple(sorted(source_selections, key=lambda item: item.source_key))
        candidates = tuple(sorted(backlog_candidates, key=lambda item: item.candidate_id))
        selected = tuple(sorted(_id(item, "candidate_id") for item in selected_candidate_ids))
        if len(selected) != len(set(selected)):
            raise ImportPreviewError("selected candidate ids must be unique")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"import-preview:{import_id}",),
                    )
                    report, answers, _ = self._lock_resolution(
                        connection, resolution
                    )
                    self._validate_project_identity(report, answers, manifest)
                    source_keys = _report_source_keys(report)
                    self._validate_selections(source_keys, selections, manifest)
                    self._validate_candidates(
                        source_keys,
                        candidates,
                        selected,
                        {item.source_key for item in selections if item.decision == "include"},
                    )
                    body = {
                        "resolution_digest": resolution.resolution_digest,
                        "manifest_digest": manifest.digest,
                        "source_selections": [item.to_dict() for item in selections],
                        "backlog_candidates": [item.to_dict() for item in candidates],
                        "selected_candidate_ids": list(selected),
                    }
                    preview_digest = _digest(body)
                    latest = connection.execute(
                        f"""
                        SELECT revision, preview_digest, status
                        FROM {SCHEMA}.project_import_previews
                        WHERE import_id = %s ORDER BY revision DESC LIMIT 1
                        FOR UPDATE
                        """,
                        (import_id,),
                    ).fetchone()
                    actual = 0 if latest is None else latest[0]
                    if (
                        latest is not None
                        and actual == expected_prior_revision + 1
                        and latest[1] == preview_digest
                    ):
                        return self._read_preview(connection, import_id, actual)
                    if actual != expected_prior_revision:
                        raise ImportPreviewConflict(
                            f"preview revision changed: expected {expected_prior_revision}, "
                            f"found {actual}"
                        )
                    if latest is not None and latest[1] == preview_digest:
                        raise ImportPreviewConflict("new preview revision must change content")
                    if latest is not None and latest[2] in {"approved", "activated"}:
                        raise ImportPreviewConflict(
                            "approved or activated preview cannot be revised"
                        )
                    revision = actual + 1
                    if latest is not None:
                        connection.execute(
                            f"UPDATE {SCHEMA}.project_import_previews "
                            "SET status = 'superseded' "
                            "WHERE import_id = %s AND revision = %s",
                            (import_id, actual),
                        )
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.project_import_previews
                            (import_id, revision, question_version,
                             resolution_digest, preview_digest, project_id,
                             manifest_digest, manifest_snapshot,
                             source_selections, backlog_candidates,
                             selected_candidate_ids, created_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            import_id,
                            revision,
                            resolution.session_version,
                            resolution.resolution_digest,
                            preview_digest,
                            manifest.project_id,
                            manifest.digest,
                            Jsonb(dict(manifest.snapshot)),
                            Jsonb([item.to_dict() for item in selections]),
                            Jsonb([item.to_dict() for item in candidates]),
                            Jsonb(list(selected)),
                            actor_id,
                        ),
                    )
                    return self._read_preview(connection, import_id, revision)
        except ImportPreviewError:
            raise
        except Exception as exc:
            raise ImportPreviewStoreError("import preview creation failed") from exc

    def decide(
        self,
        *,
        import_id: str,
        revision: int,
        sponsor_id: str,
        decision: Literal["approved", "rejected"],
        rationale: str,
        evidence: Mapping[str, object] | None = None,
    ) -> ImportPreviewDecision:
        import_id = _uuid(import_id)
        revision = _positive(revision, "revision")
        sponsor_id = _actor(sponsor_id)
        if decision not in {"approved", "rejected"}:
            raise ImportPreviewError("decision must be approved or rejected")
        rationale = _safe_text(rationale, "rationale", 2_000)
        evidence_value = _safe_mapping(evidence or {}, "evidence")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    preview = self._read_preview(
                        connection, import_id, revision, for_update=True
                    )
                    existing = self._read_decision(connection, import_id, revision)
                    if existing is not None:
                        if (
                            existing.sponsor_id == sponsor_id
                            and existing.decision == decision
                            and existing.rationale == rationale
                            and dict(existing.evidence) == evidence_value
                        ):
                            return existing
                        raise ImportPreviewConflict("preview decision already exists")
                    latest = connection.execute(
                        f"SELECT max(revision) FROM {SCHEMA}.project_import_previews "
                        "WHERE import_id = %s",
                        (import_id,),
                    ).fetchone()[0]
                    if revision != latest or preview.status != "draft":
                        raise ImportPreviewConflict(
                            "only the latest draft preview can be decided"
                        )
                    self._ensure_preview_current(connection, preview)
                    sponsors = self._session_sponsors(connection, import_id, lock=True)
                    if sponsor_id not in sponsors:
                        raise ImportPreviewConflict("decision actor is not a project sponsor")
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.project_import_preview_decisions
                            (import_id, revision, sponsor_id, decision,
                             rationale, evidence)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            import_id,
                            revision,
                            sponsor_id,
                            decision,
                            rationale,
                            Jsonb(evidence_value),
                        ),
                    )
                    connection.execute(
                        f"UPDATE {SCHEMA}.project_import_previews SET status = %s "
                        "WHERE import_id = %s AND revision = %s",
                        (decision, import_id, revision),
                    )
                    return self._read_decision(connection, import_id, revision)
        except ImportPreviewError:
            raise
        except Exception as exc:
            raise ImportPreviewStoreError("import preview decision failed") from exc

    def activate(
        self,
        *,
        import_id: str,
        revision: int,
        operation_id: str,
        actor_id: str,
        activator: ImportActivator,
    ) -> ImportActivationReceipt:
        import_id = _uuid(import_id)
        revision = _positive(revision, "revision")
        operation_id = _id(operation_id, "operation_id")
        actor_id = _actor(actor_id)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                    (f"import-activation:{import_id}",),
                )
                try:
                    with connection.transaction():
                        request, completed = self._activation_request(
                            connection,
                            import_id,
                            revision,
                            operation_id,
                            actor_id,
                        )
                        if completed is not None:
                            return completed
                    try:
                        receipt = activator.activate(request)
                    except Exception:
                        with connection.transaction():
                            connection.execute(
                                f"UPDATE {SCHEMA}.project_import_activations "
                                "SET last_error = 'activation adapter failed' "
                                "WHERE import_id = %s AND operation_id = %s",
                                (import_id, operation_id),
                            )
                        raise ImportActivationError("activation adapter failed") from None
                    self._validate_receipt(request, receipt)
                    with connection.transaction():
                        connection.execute(
                            f"""
                            UPDATE {SCHEMA}.project_import_activations
                            SET status = 'activated', receipt = %s, last_error = NULL,
                                activated_at = clock_timestamp()
                            WHERE import_id = %s AND revision = %s
                              AND operation_id = %s AND status = 'pending'
                            """,
                            (Jsonb(asdict(receipt)), import_id, revision, operation_id),
                        )
                        connection.execute(
                            f"UPDATE {SCHEMA}.project_import_previews "
                            "SET status = 'activated' "
                            "WHERE import_id = %s AND revision = %s "
                            "AND status = 'approved'",
                            (import_id, revision),
                        )
                    return receipt
                finally:
                    connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (f"import-activation:{import_id}",),
                    )
        except ImportPreviewError:
            raise
        except Exception as exc:
            raise ImportPreviewStoreError("import activation failed") from exc

    def get_preview(self, import_id: str, revision: int) -> ImportPreview:
        import_id = _uuid(import_id)
        revision = _positive(revision, "revision")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                return self._read_preview(connection, import_id, revision)
        except ImportPreviewError:
            raise
        except Exception as exc:
            raise ImportPreviewStoreError("import preview read failed") from exc

    @staticmethod
    def _lock_resolution(connection, resolution: ImportQuestionResolution):
        row = connection.execute(
            f"""
            SELECT discovery_digest, discovery_report, questionnaire_digest,
                   status, version, answers
            FROM {SCHEMA}.project_import_question_sessions
            WHERE import_id = %s FOR UPDATE
            """,
            (resolution.import_id,),
        ).fetchone()
        if row is None:
            raise ImportPreviewNotFound("import question session was not found")
        report = dict(row[1])
        answers = dict(row[5])
        body = {
            "import_id": resolution.import_id,
            "discovery_digest": row[0],
            "discovery_report": report,
            "questionnaire_digest": row[2],
            "session_version": row[4],
            "answers": answers,
        }
        if (
            row[3] != "ready"
            or row[4] != resolution.session_version
            or row[0] != resolution.discovery_digest
            or report != dict(resolution.discovery_report)
            or row[2] != resolution.questionnaire_digest
            or answers != dict(resolution.answers)
            or _digest(body) != resolution.resolution_digest
        ):
            raise ImportPreviewConflict(
                "question resolution is stale, incomplete, or inconsistent"
            )
        return report, answers, _sponsors(answers)

    @staticmethod
    def _validate_project_identity(
        report: Mapping[str, object],
        answers: Mapping[str, object],
        manifest: ProjectManifest,
    ) -> None:
        project_id = report.get("project_id_hint") or answers.get("project.id")
        display_name = report.get("display_name_hint") or answers.get("project.display-name")
        if manifest.project_id != project_id or manifest.display_name != display_name:
            raise ImportPreviewConflict("manifest identity disagrees with resolved answers")

    @staticmethod
    def _validate_selections(
        source_keys: tuple[str, ...],
        selections: tuple[SourceSelection, ...],
        manifest: ProjectManifest,
    ) -> None:
        supplied = [item.source_key for item in selections]
        if supplied != list(source_keys):
            raise ImportPreviewConflict(
                "every discovered source requires exactly one ordered decision"
            )
        resources = {(item.kind, item.key) for item in manifest.resources}
        for item in selections:
            if item.decision == "include" and (
                item.resource_kind,
                item.resource_key,
            ) not in resources:
                raise ImportPreviewConflict(
                    f"included source {item.source_key} has no exact manifest resource"
                )

    @staticmethod
    def _validate_candidates(
        source_keys: tuple[str, ...],
        candidates: tuple[BacklogCandidate, ...],
        selected: tuple[str, ...],
        included_source_keys: set[str],
    ) -> None:
        ids = [item.candidate_id for item in candidates]
        if ids != sorted(set(ids)):
            raise ImportPreviewConflict("candidate ids must be sorted and unique")
        if selected != tuple(sorted(set(selected))):
            raise ImportPreviewConflict("selected candidate ids must be sorted and unique")
        allowed = set(source_keys)
        if any(not set(item.source_keys) <= allowed for item in candidates):
            raise ImportPreviewConflict("candidate references an unknown source")
        if not set(selected) <= set(ids):
            raise ImportPreviewConflict("selected candidate does not exist")
        candidates_by_id = {item.candidate_id: item for item in candidates}
        if any(
            not set(candidates_by_id[item].source_keys) <= included_source_keys
            for item in selected
        ):
            raise ImportPreviewConflict("selected candidate depends on an excluded source")

    def _session_sponsors(self, connection, import_id: str, *, lock: bool):
        row = connection.execute(
            f"SELECT answers FROM {SCHEMA}.project_import_question_sessions "
            f"WHERE import_id = %s {'FOR UPDATE' if lock else ''}",
            (import_id,),
        ).fetchone()
        if row is None:
            raise ImportPreviewNotFound("import question session was not found")
        return _sponsors(dict(row[0]))

    def _activation_request(
        self,
        connection,
        import_id: str,
        revision: int,
        operation_id: str,
        actor_id: str,
    ) -> tuple[ImportActivationRequest, ImportActivationReceipt | None]:
        preview = self._read_preview(connection, import_id, revision, for_update=True)
        decision = self._read_decision(connection, import_id, revision)
        if decision is None or decision.decision != "approved":
            raise ImportPreviewConflict("exact preview revision is not sponsor approved")
        existing = connection.execute(
            f"SELECT revision, operation_id, status, receipt, requested_by "
            f"FROM {SCHEMA}.project_import_activations "
            "WHERE import_id = %s FOR UPDATE",
            (import_id,),
        ).fetchone()
        request_actor_id = actor_id
        if existing is not None:
            if existing[0] != revision or existing[1] != operation_id:
                raise ImportPreviewConflict("import already has another activation")
            request_actor_id = existing[4]
            if existing[2] == "activated":
                return self._request(preview, operation_id, request_actor_id, connection), (
                    _receipt_from_dict(existing[3])
                )
        else:
            if preview.status != "approved":
                raise ImportPreviewConflict("preview is not ready for activation")
            connection.execute(
                f"""
                INSERT INTO {SCHEMA}.project_import_activations
                    (import_id, revision, operation_id, requested_by)
                VALUES (%s, %s, %s, %s)
                """,
                (import_id, revision, operation_id, actor_id),
            )
        self._ensure_preview_current(connection, preview)
        connection.execute(
            f"UPDATE {SCHEMA}.project_import_activations "
            "SET attempts = attempts + 1, last_error = NULL "
            "WHERE import_id = %s",
            (import_id,),
        )
        return self._request(preview, operation_id, request_actor_id, connection), None

    def _request(self, preview, operation_id, actor_id, connection):
        manifest = validate_project_manifest(dict(preview.manifest_snapshot))
        sponsors = self._session_sponsors(connection, preview.import_id, lock=False)
        selected = set(preview.selected_candidate_ids)
        return ImportActivationRequest(
            preview.import_id,
            preview.revision,
            operation_id,
            manifest,
            sponsors,
            preview.source_selections,
            tuple(
                item for item in preview.backlog_candidates if item.candidate_id in selected
            ),
            actor_id,
        )

    @staticmethod
    def _validate_receipt(request, receipt) -> None:
        if not isinstance(receipt, ImportActivationReceipt):
            raise ImportActivationError("activation adapter returned an invalid receipt")
        if (
            receipt.project_id != request.manifest.project_id
            or receipt.manifest_digest != request.manifest.digest
            or receipt.selected_candidate_ids
            != tuple(item.candidate_id for item in request.selected_candidates)
        ):
            raise ImportActivationError("activation receipt disagrees with its request")

    def _read_preview(self, connection, import_id, revision, *, for_update=False):
        row = connection.execute(
            f"""
            SELECT import_id::text, revision, question_version,
                   resolution_digest, preview_digest, project_id,
                   manifest_digest, manifest_snapshot, source_selections,
                   backlog_candidates, selected_candidate_ids, status,
                   created_by, created_at::text
            FROM {SCHEMA}.project_import_previews
            WHERE import_id = %s AND revision = %s
            {'FOR UPDATE' if for_update else ''}
            """,
            (import_id, revision),
        ).fetchone()
        if row is None:
            raise ImportPreviewNotFound("import preview was not found")
        manifest = validate_project_manifest(dict(row[7]))
        if manifest.digest != row[6] or manifest.project_id != row[5]:
            raise ImportPreviewStoreError("stored preview manifest is invalid")
        selections = tuple(_selection_from_dict(item) for item in row[8])
        candidates = tuple(_candidate_from_dict(item) for item in row[9])
        selected = tuple(row[10])
        report = connection.execute(
            f"SELECT discovery_report FROM {SCHEMA}.project_import_question_sessions "
            "WHERE import_id = %s",
            (import_id,),
        ).fetchone()
        if report is None:
            raise ImportPreviewStoreError("stored preview has no question session")
        self._validate_selections(
            _report_source_keys(dict(report[0])), selections, manifest
        )
        self._validate_candidates(
            _report_source_keys(dict(report[0])),
            candidates,
            selected,
            {item.source_key for item in selections if item.decision == "include"},
        )
        content = {
            "resolution_digest": row[3],
            "manifest_digest": row[6],
            "source_selections": [item.to_dict() for item in selections],
            "backlog_candidates": [item.to_dict() for item in candidates],
            "selected_candidate_ids": list(selected),
        }
        if _digest(content) != row[4]:
            raise ImportPreviewStoreError("stored preview digest is invalid")
        return ImportPreview(
            *row[:7],
            MappingProxyType(dict(row[7])),
            selections,
            candidates,
            selected,
            *row[11:],
        )

    @staticmethod
    def _ensure_preview_current(connection, preview: ImportPreview) -> None:
        row = connection.execute(
            f"""
            SELECT discovery_digest, discovery_report, questionnaire_digest,
                   status, version, answers
            FROM {SCHEMA}.project_import_question_sessions
            WHERE import_id = %s FOR UPDATE
            """,
            (preview.import_id,),
        ).fetchone()
        if row is None:
            raise ImportPreviewNotFound("import question session was not found")
        body = {
            "import_id": preview.import_id,
            "discovery_digest": row[0],
            "discovery_report": row[1],
            "questionnaire_digest": row[2],
            "session_version": row[4],
            "answers": row[5],
        }
        if row[3] != "ready" or row[4] != preview.question_version or (
            _digest(body) != preview.resolution_digest
        ):
            raise ImportPreviewConflict("preview question resolution is no longer current")

    @staticmethod
    def _read_decision(connection, import_id, revision):
        row = connection.execute(
            f"""
            SELECT import_id::text, revision, sponsor_id, decision,
                   rationale, evidence, decided_at::text
            FROM {SCHEMA}.project_import_preview_decisions
            WHERE import_id = %s AND revision = %s
            """,
            (import_id, revision),
        ).fetchone()
        return None if row is None else ImportPreviewDecision(*row)


def _report_source_keys(report: Mapping[str, object]) -> tuple[str, ...]:
    results = report.get("results")
    if not isinstance(results, list):
        raise ImportPreviewConflict("discovery report results are invalid")
    seen: dict[tuple[str, str, str], int] = {}
    keys: list[str] = []
    for result in results:
        if not isinstance(result, Mapping):
            raise ImportPreviewConflict("discovery source result is invalid")
        kind = _text(result.get("source_kind"), "source_kind", 100)
        source_id = _text(result.get("source_id"), "source_id", 128)
        digest = _digest_value(
            result.get("configuration_digest"), "configuration_digest"
        )
        identity = (kind, source_id, digest)
        seen[identity] = seen.get(identity, 0) + 1
        keys.append(f"{kind}/{source_id}/{digest}/{seen[identity]}")
    return tuple(sorted(keys))


def _validated_manifest(manifest: object) -> ProjectManifest:
    if not isinstance(manifest, ProjectManifest):
        raise ImportPreviewError("validated project manifest is required")
    validated = validate_project_manifest(dict(manifest.snapshot))
    if validated != manifest:
        raise ImportPreviewConflict("project manifest does not match its snapshot")
    return manifest


def _selection_from_dict(value: object) -> SourceSelection:
    if not isinstance(value, Mapping) or set(value) != {
        "source_key",
        "decision",
        "rationale",
        "resource_kind",
        "resource_key",
    }:
        raise ImportPreviewStoreError("stored source selection is invalid")
    return SourceSelection(**value)


def _candidate_from_dict(value: object) -> BacklogCandidate:
    if not isinstance(value, Mapping) or set(value) != {
        "candidate_id",
        "title",
        "description",
        "source_keys",
        "status",
    } or value.get("status") != "candidate":
        raise ImportPreviewStoreError("stored backlog candidate is invalid")
    return BacklogCandidate(
        value["candidate_id"],
        value["title"],
        value["description"],
        tuple(value["source_keys"]),
    )


def _receipt_from_dict(value: object) -> ImportActivationReceipt:
    if not isinstance(value, Mapping):
        raise ImportPreviewStoreError("stored activation receipt is invalid")
    return ImportActivationReceipt(
        value.get("project_id"),
        value.get("manifest_digest"),
        tuple(value.get("selected_candidate_ids", ())),
        value.get("activation_ref"),
    )


def _sponsors(answers: Mapping[str, object]) -> tuple[str, ...]:
    values = answers.get("project.sponsors")
    if not isinstance(values, list) or not values:
        raise ImportPreviewConflict("resolved project sponsors are missing")
    sponsors = tuple(sorted(_actor(item) for item in values))
    if len(sponsors) != len(set(sponsors)):
        raise ImportPreviewConflict("resolved project sponsors are ambiguous")
    return sponsors


def _safe_mapping(value: Mapping[str, object], field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ImportPreviewError(f"{field} must be an object")
    normalized = dict(value)
    try:
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ImportPreviewError(f"{field} must be JSON serializable") from None
    if len(encoded) > 64_000 or any(
        marker in encoded.casefold() for marker in _SECRET_MARKERS
    ):
        raise ImportPreviewError(f"{field} is too large or contains secret material")
    return normalized


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ImportPreviewError("import_id must be a UUID")
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError):
        raise ImportPreviewError("import_id must be a UUID") from None
    if normalized != value.lower():
        raise ImportPreviewError("import_id must be a canonical UUID")
    return normalized


def _source_key(value: object) -> str:
    return _text(value, "source_key", 400)


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ImportPreviewError(f"{field} is invalid")
    return value


def _project_id(value: object) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise ImportPreviewError("project_id is invalid")
    return value


def _digest_value(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ImportPreviewError(f"{field} is invalid")
    return value


def _actor(value: object) -> str:
    return _safe_text(value, "actor identity", 256)


def _safe_text(value: object, field: str, maximum: int) -> str:
    text = _text(value, field, maximum)
    if any(marker in text.casefold() for marker in _SECRET_MARKERS):
        raise ImportPreviewError(f"{field} must not contain secret material")
    return text


def _text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(item) < 32 for item in value)
    ):
        raise ImportPreviewError(f"{field} is invalid")
    return value.strip()


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ImportPreviewError(f"{field} must be a positive integer")
    return value
