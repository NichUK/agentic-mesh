from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.import_discovery import ImportDiscoveryReport


_QUESTION_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,199}$")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EXTERNAL_REFERENCE = re.compile(
    r"^(?:secret|mount|oauth-cache)://[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$"
)
AnswerKind = Literal["text", "identifier", "string-list", "credential-policy"]
_ANSWER_KINDS = {"text", "identifier", "string-list", "credential-policy"}
_SECRET_MARKERS = (
    "password=",
    "secret=",
    "token=",
    "authorization: bearer",
    "-----begin private key-----",
)


class ImportQuestionError(ValueError):
    pass


class ImportQuestionNotFound(ImportQuestionError):
    pass


class ImportQuestionConflict(ImportQuestionError):
    pass


class ImportQuestionNotReady(ImportQuestionError):
    pass


class ImportQuestionStoreError(DatabaseError):
    pass


@dataclass(frozen=True, slots=True)
class ImportQuestion:
    question_id: str
    category: str
    prompt: str
    answer_kind: AnswerKind
    source_kind: str | None = None
    source_id: str | None = None
    issue_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "question_id", _question_id(self.question_id))
        object.__setattr__(self, "category", _identifier(self.category, "category"))
        object.__setattr__(
            self, "prompt", _persisted_text(self.prompt, "prompt", 1_000)
        )
        if self.answer_kind not in _ANSWER_KINDS:
            raise ImportQuestionError("answer_kind is invalid")
        for field in ("source_kind", "source_id", "issue_code"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _identifier(value, field))

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AnswerDraft:
    value: object
    replace: bool = False
    correction_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.replace) is not bool:
            raise ImportQuestionError("replace must be a boolean")
        if self.correction_reason is not None:
            object.__setattr__(
                self,
                "correction_reason",
                _persisted_text(
                    self.correction_reason, "correction_reason", 1_000
                ),
            )


@dataclass(frozen=True, slots=True)
class ImportQuestionState:
    import_id: str
    discovery_digest: str
    discovery_report: Mapping[str, object]
    questionnaire_digest: str
    status: str
    version: int
    questions: tuple[ImportQuestion, ...]
    answers: Mapping[str, object]
    created_by: str
    created_at: str
    updated_at: str
    ready_at: str | None

    @property
    def unanswered_question_ids(self) -> tuple[str, ...]:
        return tuple(
            item.question_id
            for item in self.questions
            if item.question_id not in self.answers
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "import_id": self.import_id,
            "discovery_digest": self.discovery_digest,
            "discovery_report": dict(self.discovery_report),
            "questionnaire_digest": self.questionnaire_digest,
            "status": self.status,
            "version": self.version,
            "questions": [item.to_dict() for item in self.questions],
            "answers": dict(self.answers),
            "unanswered_question_ids": list(self.unanswered_question_ids),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ready_at": self.ready_at,
        }


@dataclass(frozen=True, slots=True)
class ImportQuestionResolution:
    import_id: str
    discovery_digest: str
    discovery_report: Mapping[str, object]
    questionnaire_digest: str
    session_version: int
    answers: Mapping[str, object]
    resolution_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "import_id": self.import_id,
            "discovery_digest": self.discovery_digest,
            "discovery_report": dict(self.discovery_report),
            "questionnaire_digest": self.questionnaire_digest,
            "session_version": self.session_version,
            "answers": dict(self.answers),
            "resolution_digest": self.resolution_digest,
        }


def build_import_questions(report: ImportDiscoveryReport) -> tuple[ImportQuestion, ...]:
    _validate_report(report)
    questions: list[ImportQuestion] = []

    def add(
        question_id: str,
        category: str,
        prompt: str,
        answer_kind: AnswerKind,
        *,
        source_kind: str | None = None,
        source_id: str | None = None,
        issue_code: str | None = None,
    ) -> None:
        questions.append(
            ImportQuestion(
                question_id,
                category,
                prompt,
                answer_kind,
                source_kind,
                source_id,
                issue_code,
            )
        )

    if report.project_id_hint is None:
        add("project.id", "project", "What is the canonical project id?", "identifier")
    if report.display_name_hint is None:
        add(
            "project.display-name",
            "project",
            "What user-facing project name should be used?",
            "text",
        )
    if not report.results:
        add(
            "sources.declarations",
            "source",
            "Which repositories, document roots, and collaboration bindings must be imported?",
            "text",
        )
    add(
        "project.intent",
        "project",
        "What outcome, scope, and explicit exclusions define this project?",
        "text",
    )
    add(
        "project.owner",
        "ownership",
        "Who is accountable for the imported project?",
        "text",
    )
    add(
        "project.sponsors",
        "ownership",
        "Which sponsor identities may approve project gates?",
        "string-list",
    )
    result_counts: dict[tuple[str, str], int] = {}
    for result in report.results:
        key = (result.source_kind, result.source_id)
        result_counts[key] = result_counts.get(key, 0) + 1
    result_positions: dict[tuple[str, str], int] = {}
    for result in report.results:
        key = (result.source_kind, result.source_id)
        result_positions[key] = result_positions.get(key, 0) + 1
        declaration = result_positions[key]
        base = f"source.{result.source_kind}.{result.source_id}"
        label = f"{result.source_kind} {result.source_id}"
        if result_counts[key] > 1:
            base = f"{base}.declaration-{declaration}"
            label = f"{label} declaration {declaration}"
        add(
            f"{base}.purpose",
            "source",
            f"What project purpose does {label} serve?",
            "text",
            source_kind=result.source_kind,
            source_id=result.source_id,
        )
        add(
            f"{base}.owner",
            "ownership",
            f"Who owns and authorizes access to {label}?",
            "text",
            source_kind=result.source_kind,
            source_id=result.source_id,
        )
        add(
            f"{base}.credential-policy",
            "credential",
            f"Which external credential reference accesses {label}, or is it none-public?",
            "credential-policy",
            source_kind=result.source_kind,
            source_id=result.source_id,
        )
        if result.source_kind == "repository" and (
            result.summary.get("default_branch") is None
            or result.summary.get("default_branch_matches_hint") is False
        ):
            add(
                f"{base}.default-branch",
                "source",
                f"Which default branch should {label} use?",
                "text",
                source_kind=result.source_kind,
                source_id=result.source_id,
            )
    seen_issue_questions: set[str] = set()
    for issue in report.issues:
        if issue.source_kind is not None and issue.source_id is not None:
            question_id = (
                f"source.{issue.source_kind}.{issue.source_id}."
                f"disposition.{issue.code}"
            )
        else:
            question_id = f"discovery.disposition.{issue.code}"
        if question_id in seen_issue_questions:
            continue
        seen_issue_questions.add(question_id)
        add(
            question_id,
            "discovery",
            f"How must the discovery issue '{issue.code}' be resolved or dispositioned?",
            "text",
            source_kind=issue.source_kind,
            source_id=issue.source_id,
            issue_code=issue.code,
        )
    add(
        "deployment.strategy",
        "deployment",
        "How is the project built, released, and rolled back?",
        "text",
    )
    add(
        "deployment.environments",
        "deployment",
        "Which deployment environments are in scope?",
        "string-list",
    )
    add(
        "deployment.owner",
        "deployment",
        "Who owns deployment and operational acceptance?",
        "text",
    )
    ordered = tuple(sorted(questions, key=lambda item: item.question_id))
    ids = [item.question_id for item in ordered]
    if len(ids) != len(set(ids)):
        raise ImportQuestionConflict("generated question ids are ambiguous")
    return ordered


class ImportQuestionStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def start(
        self,
        *,
        import_id: str,
        report: ImportDiscoveryReport,
        actor_id: str,
    ) -> ImportQuestionState:
        import_id = _uuid(import_id)
        actor_id = _actor(actor_id)
        report_payload = _validate_report(report)
        questions = build_import_questions(report)
        question_payload = [item.to_dict() for item in questions]
        questionnaire_digest = _digest(question_payload)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.project_import_question_sessions
                            (import_id, discovery_digest, discovery_report,
                             questionnaire_digest, questions, created_by)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (import_id) DO NOTHING
                        """,
                        (
                            import_id,
                            report.digest,
                            Jsonb(report_payload),
                            questionnaire_digest,
                            Jsonb(question_payload),
                            actor_id,
                        ),
                    )
                    state = self._read(connection, import_id, for_update=True)
                    if (
                        state.discovery_digest != report.digest
                        or dict(state.discovery_report) != report_payload
                    ):
                        raise ImportQuestionConflict(
                            "import id is already pinned to another discovery report"
                        )
                    return state
        except ImportQuestionError:
            raise
        except Exception as exc:
            raise ImportQuestionStoreError("import question start failed") from exc

    def add_followups(
        self,
        *,
        import_id: str,
        questions: Sequence[ImportQuestion],
        actor_id: str,
        rationale: str,
        expected_version: int,
    ) -> ImportQuestionState:
        import_id = _uuid(import_id)
        actor_id = _actor(actor_id)
        rationale = _persisted_text(rationale, "rationale", 2_000)
        expected_version = _positive_integer(expected_version, "expected_version")
        additions = tuple(questions)
        if not additions or not all(isinstance(item, ImportQuestion) for item in additions):
            raise ImportQuestionError("follow-up questions are required")
        if len(additions) > 100:
            raise ImportQuestionError("too many follow-up questions")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    state = self._read(connection, import_id, for_update=True)
                    existing = {item.question_id: item for item in state.questions}
                    changed: list[ImportQuestion] = []
                    for item in additions:
                        prior = existing.get(item.question_id)
                        if prior is not None and prior != item:
                            raise ImportQuestionConflict(
                                f"question {item.question_id} conflicts with its definition"
                            )
                        if prior is None:
                            existing[item.question_id] = item
                            changed.append(item)
                    if not changed:
                        return state
                    self._expected_version(state, expected_version)
                    merged = tuple(sorted(existing.values(), key=lambda item: item.question_id))
                    payload = [item.to_dict() for item in merged]
                    next_version = state.version + 1
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.project_import_question_sessions
                        SET questions = %s, questionnaire_digest = %s,
                            status = 'questioning', version = %s,
                            updated_at = clock_timestamp(), ready_at = NULL
                        WHERE import_id = %s AND version = %s
                        """,
                        (
                            Jsonb(payload),
                            _digest(payload),
                            next_version,
                            import_id,
                            state.version,
                        ),
                    )
                    self._event(
                        connection,
                        import_id,
                        next_version,
                        "questions-added",
                        {"questions": [item.to_dict() for item in changed]},
                        actor_id,
                        rationale,
                    )
                    return self._read(connection, import_id)
        except ImportQuestionError:
            raise
        except Exception as exc:
            raise ImportQuestionStoreError("follow-up question operation failed") from exc

    def answer(
        self,
        *,
        import_id: str,
        answers: Mapping[str, AnswerDraft],
        actor_id: str,
        expected_version: int,
        rationale: str | None = None,
    ) -> ImportQuestionState:
        import_id = _uuid(import_id)
        actor_id = _actor(actor_id)
        expected_version = _positive_integer(expected_version, "expected_version")
        round_rationale = (
            None
            if rationale is None
            else _persisted_text(rationale, "rationale", 2_000)
        )
        drafts = dict(answers)
        if not drafts or len(drafts) > 100 or not all(
            isinstance(value, AnswerDraft) for value in drafts.values()
        ):
            raise ImportQuestionError("one to one hundred answer drafts are required")
        for question_id in drafts:
            _question_id(question_id)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    state = self._read(connection, import_id, for_update=True)
                    definitions = {item.question_id: item for item in state.questions}
                    current = dict(state.answers)
                    recorded: list[dict[str, object]] = []
                    for raw_id, draft in sorted(drafts.items()):
                        question_id = _question_id(raw_id)
                        question = definitions.get(question_id)
                        if question is None:
                            raise ImportQuestionConflict(
                                f"question {question_id} is not part of this session"
                            )
                        value = _answer_value(question.answer_kind, draft.value)
                        previous = current.get(question_id)
                        if question_id in current:
                            if previous == value:
                                continue
                            if not draft.replace or draft.correction_reason is None:
                                raise ImportQuestionConflict(
                                    f"question {question_id} already has a different answer"
                                )
                        elif draft.replace:
                            raise ImportQuestionConflict(
                                f"question {question_id} has no answer to replace"
                            )
                        current[question_id] = value
                        recorded.append(
                            {
                                "question_id": question_id,
                                "value": value,
                                "replaced_value": previous,
                                "correction_reason": draft.correction_reason,
                            }
                        )
                    if not recorded:
                        return state
                    self._expected_version(state, expected_version)
                    ready = len(current) == len(definitions)
                    next_version = state.version + 1
                    connection.execute(
                        f"""
                        UPDATE {SCHEMA}.project_import_question_sessions
                        SET answers = %s, status = %s, version = %s,
                            updated_at = clock_timestamp(),
                            ready_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                        WHERE import_id = %s AND version = %s
                        """,
                        (
                            Jsonb(current),
                            "ready" if ready else "questioning",
                            next_version,
                            ready,
                            import_id,
                            state.version,
                        ),
                    )
                    self._event(
                        connection,
                        import_id,
                        next_version,
                        "answers-recorded",
                        {"answers": recorded},
                        actor_id,
                        round_rationale,
                    )
                    return self._read(connection, import_id)
        except ImportQuestionError:
            raise
        except Exception as exc:
            raise ImportQuestionStoreError("import answer operation failed") from exc

    def get(self, import_id: str) -> ImportQuestionState:
        import_id = _uuid(import_id)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                return self._read(connection, import_id)
        except ImportQuestionError:
            raise
        except Exception as exc:
            raise ImportQuestionStoreError("import question read failed") from exc

    def history(self, import_id: str) -> tuple[dict[str, object], ...]:
        import_id = _uuid(import_id)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                if connection.execute(
                    f"SELECT 1 FROM {SCHEMA}.project_import_question_sessions "
                    "WHERE import_id = %s",
                    (import_id,),
                ).fetchone() is None:
                    raise ImportQuestionNotFound("import question session was not found")
                rows = connection.execute(
                    f"""
                    SELECT sequence, event_kind, payload, actor_id, rationale,
                           recorded_at::text
                    FROM {SCHEMA}.project_import_question_events
                    WHERE import_id = %s ORDER BY sequence
                    """,
                    (import_id,),
                ).fetchall()
                names = (
                    "sequence",
                    "event_kind",
                    "payload",
                    "actor_id",
                    "rationale",
                    "recorded_at",
                )
                return tuple(dict(zip(names, row, strict=True)) for row in rows)
        except ImportQuestionError:
            raise
        except Exception as exc:
            raise ImportQuestionStoreError("import question history read failed") from exc

    def require_preview_ready(
        self, *, import_id: str, expected_version: int
    ) -> ImportQuestionResolution:
        state = self.get(import_id)
        self._expected_version(
            state, _positive_integer(expected_version, "expected_version")
        )
        if state.status != "ready" or state.unanswered_question_ids:
            raise ImportQuestionNotReady(
                "import preview is blocked until every material question is answered"
            )
        body = {
            "import_id": state.import_id,
            "discovery_digest": state.discovery_digest,
            "discovery_report": dict(state.discovery_report),
            "questionnaire_digest": state.questionnaire_digest,
            "session_version": state.version,
            "answers": dict(state.answers),
        }
        return ImportQuestionResolution(
            state.import_id,
            state.discovery_digest,
            state.discovery_report,
            state.questionnaire_digest,
            state.version,
            state.answers,
            _digest(body),
        )

    @staticmethod
    def _expected_version(state: ImportQuestionState, expected: int) -> None:
        if state.version != expected:
            raise ImportQuestionConflict(
                f"import question version changed: expected {expected}, "
                f"found {state.version}"
            )

    @staticmethod
    def _event(
        connection,
        import_id: str,
        sequence: int,
        event_kind: str,
        payload: Mapping[str, object],
        actor_id: str,
        rationale: str | None,
    ) -> None:
        connection.execute(
            f"""
            INSERT INTO {SCHEMA}.project_import_question_events
                (import_id, sequence, event_kind, payload, actor_id, rationale)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (import_id, sequence, event_kind, Jsonb(dict(payload)), actor_id, rationale),
        )

    @staticmethod
    def _read(
        connection, import_id: str, *, for_update: bool = False
    ) -> ImportQuestionState:
        row = connection.execute(
            f"""
            SELECT import_id::text, discovery_digest, discovery_report,
                   questionnaire_digest, status, version, questions, answers, created_by,
                   created_at::text, updated_at::text, ready_at::text
            FROM {SCHEMA}.project_import_question_sessions
            WHERE import_id = %s {"FOR UPDATE" if for_update else ""}
            """,
            (import_id,),
        ).fetchone()
        if row is None:
            raise ImportQuestionNotFound("import question session was not found")
        report = dict(row[2])
        report_digest = report.pop("digest", None)
        if report_digest != row[1] or _digest(report) != row[1]:
            raise ImportQuestionStoreError("stored discovery report is invalid")
        report["digest"] = report_digest
        questions = tuple(_question_from_dict(item) for item in row[6])
        question_payload = [item.to_dict() for item in questions]
        if _digest(question_payload) != row[3]:
            raise ImportQuestionStoreError("stored questionnaire digest is invalid")
        definitions = {item.question_id: item for item in questions}
        if len(definitions) != len(questions) or not isinstance(row[7], Mapping):
            raise ImportQuestionStoreError("stored question state is invalid")
        raw_answers = dict(row[7])
        answers: dict[str, object] = {}
        for question_id, value in raw_answers.items():
            question = definitions.get(question_id)
            if question is None:
                raise ImportQuestionStoreError("stored answer has no question")
            try:
                normalized = _answer_value(question.answer_kind, value)
            except ImportQuestionError:
                raise ImportQuestionStoreError("stored answer is invalid") from None
            if normalized != value:
                raise ImportQuestionStoreError("stored answer is not canonical")
            answers[question_id] = value
        complete = len(answers) == len(questions)
        if (row[4] == "ready") != complete or (row[11] is not None) != complete:
            raise ImportQuestionStoreError("stored readiness state is inconsistent")
        return ImportQuestionState(
            row[0],
            row[1],
            MappingProxyType(report),
            row[3],
            row[4],
            row[5],
            questions,
            MappingProxyType(answers),
            *row[8:],
        )


def _question_from_dict(value: object) -> ImportQuestion:
    if not isinstance(value, Mapping) or set(value) != {
        "question_id",
        "category",
        "prompt",
        "answer_kind",
        "source_kind",
        "source_id",
        "issue_code",
    }:
        raise ImportQuestionStoreError("stored question is invalid")
    return ImportQuestion(**value)


def _answer_value(kind: AnswerKind, value: object) -> object:
    if kind == "identifier":
        return _identifier(value, "answer")
    if kind == "text":
        return _persisted_text(value, "answer", 4_000)
    if kind == "credential-policy":
        if value == "none-public":
            return value
        if not isinstance(value, str) or _EXTERNAL_REFERENCE.fullmatch(value) is None:
            raise ImportQuestionError(
                "credential answer must be an external reference or none-public"
            )
        return value
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 100:
        raise ImportQuestionError("string-list answer must contain one to 100 values")
    normalized = tuple(
        _persisted_text(item, "answer item", 200) for item in value
    )
    if len(normalized) != len(set(normalized)):
        raise ImportQuestionError("string-list answer values must be unique")
    return list(normalized)


def _validate_report(report: ImportDiscoveryReport) -> dict[str, object]:
    if not isinstance(report, ImportDiscoveryReport) or _DIGEST.fullmatch(
        getattr(report, "digest", "")
    ) is None:
        raise ImportQuestionError("discovery report is invalid")
    payload = report.to_dict()
    supplied = payload.pop("digest", None)
    if supplied != report.digest or _digest(payload) != report.digest:
        raise ImportQuestionConflict("discovery report digest does not match its content")
    payload["digest"] = report.digest
    return payload


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ImportQuestionError("import_id must be a UUID")
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError):
        raise ImportQuestionError("import_id must be a UUID") from None
    if normalized != value.lower():
        raise ImportQuestionError("import_id must be a canonical UUID")
    return normalized


def _question_id(value: object) -> str:
    if not isinstance(value, str) or _QUESTION_ID.fullmatch(value) is None:
        raise ImportQuestionError("question_id is invalid")
    return value


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ImportQuestionError(f"{field} is invalid")
    return value


def _actor(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or any(ord(item) < 32 for item in value)
    ):
        raise ImportQuestionError("actor_id is invalid")
    return value.strip()


def _text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(item) < 32 for item in value)
    ):
        raise ImportQuestionError(f"{field} is invalid")
    return value.strip()


def _persisted_text(value: object, field: str, maximum: int) -> str:
    text = _text(value, field, maximum)
    if any(marker in text.casefold() for marker in _SECRET_MARKERS):
        raise ImportQuestionError(f"{field} must not contain a secret value")
    return text


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ImportQuestionError(f"{field} must be a positive integer")
    return value
