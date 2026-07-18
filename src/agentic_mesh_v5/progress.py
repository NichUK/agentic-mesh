from __future__ import annotations

from dataclasses import dataclass
import re

import psycopg

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA


_STORE_ERROR = "progress checkpoint operation failed"
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_STATUS = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SENSITIVE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}",
        r"\bsk-[A-Za-z0-9_-]{16,}",
        r"\b(?:access_token|refresh_token|client_secret|password)[\"']?\s*[:=]\s*"
        r"[\"']?[^\s\"']{8,}",
        r"\b(?:api[_-]?key|secret[_-]?key|token)[\"']?\s*[:=]\s*"
        r"[\"']?[^\s\"']{12,}",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{16,})",
        r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s/:@]+:[^\s/@]+@",
        r"</?(?:analysis|reasoning|chain[-_ ]of[-_ ]thought)>",
    )
)
_COLUMNS = """
    progress_id, checkpoint_id, work_item_id, role_instance_id, sequence,
    status, goal, step, completed_action, activity, blocker, next_action,
    safe_summary, recorded_at::text
"""


class ProgressError(DatabaseError):
    pass


class ProgressNotFound(ProgressError):
    pass


class ProgressConflict(ProgressError):
    pass


class ProgressSensitiveContent(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProgressDraft:
    project_id: str
    work_item_id: str
    role_instance_id: str
    checkpoint_id: str
    expected_previous_sequence: int
    status: str
    goal: str
    step: str
    completed_action: str | None
    activity: str | None
    blocker: str | None
    next_action: str
    safe_summary: str

    def __post_init__(self) -> None:
        for field_name in (
            "project_id",
            "work_item_id",
            "role_instance_id",
            "checkpoint_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        if (
            isinstance(self.expected_previous_sequence, bool)
            or not isinstance(self.expected_previous_sequence, int)
            or self.expected_previous_sequence < 0
        ):
            raise ValueError("expected_previous_sequence is invalid")
        status = _required_text(self.status, "status", 32)
        if _STATUS.fullmatch(status) is None:
            raise ValueError("status is invalid")
        object.__setattr__(self, "status", status)
        for field_name in ("goal", "step", "next_action"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name, 2000),
            )
        for field_name in ("completed_action", "activity", "blocker"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name, 2000),
            )
        object.__setattr__(
            self,
            "safe_summary",
            _required_text(self.safe_summary, "safe_summary", 1000),
        )


@dataclass(frozen=True, slots=True)
class ProgressRecord:
    project_id: str
    progress_id: int
    checkpoint_id: str
    work_item_id: str
    role_instance_id: str | None
    sequence: int
    status: str
    goal: str
    step: str
    completed_action: str | None
    activity: str | None
    blocker: str | None
    next_action: str
    safe_summary: str
    recorded_at: str


class ProgressStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def record(self, draft: ProgressDraft) -> ProgressRecord:
        if not isinstance(draft, ProgressDraft):
            raise ValueError("progress draft is invalid")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                with connection.transaction():
                    authorized = connection.execute(
                        f"""
                        SELECT 1
                        FROM {SCHEMA}.work_items AS work
                        JOIN {SCHEMA}.role_instances AS instance
                          ON instance.project_id = work.project_id
                         AND instance.instance_id = %s
                        WHERE work.project_id = %s AND work.work_item_id = %s
                        FOR UPDATE OF work
                        """,
                        (
                            draft.role_instance_id,
                            draft.project_id,
                            draft.work_item_id,
                        ),
                    ).fetchone()
                    if authorized is None:
                        raise ProgressNotFound(
                            "work item or project role instance not found"
                        )
                    existing_row = connection.execute(
                        f"""
                        SELECT {_COLUMNS}
                        FROM {SCHEMA}.progress
                        WHERE project_id = %s AND checkpoint_id = %s
                        """,
                        (draft.project_id, draft.checkpoint_id),
                    ).fetchone()
                    if existing_row is not None:
                        existing = _record(draft.project_id, existing_row)
                        if _same_checkpoint(existing, draft):
                            return existing
                        raise ProgressConflict(
                            "checkpoint id is already used by different progress"
                        )
                    current_sequence = connection.execute(
                        f"""
                        SELECT COALESCE(MAX(sequence), 0)
                        FROM {SCHEMA}.progress
                        WHERE project_id = %s AND work_item_id = %s
                        """,
                        (draft.project_id, draft.work_item_id),
                    ).fetchone()[0]
                    if current_sequence != draft.expected_previous_sequence:
                        raise ProgressConflict(
                            "progress checkpoint sequence is stale"
                        )
                    row = connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.progress
                            (project_id, checkpoint_id, work_item_id,
                             role_instance_id, sequence, status, goal, step,
                             completed_action, activity, blocker, next_action,
                             safe_summary)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s)
                        RETURNING {_COLUMNS}
                        """,
                        (
                            draft.project_id,
                            draft.checkpoint_id,
                            draft.work_item_id,
                            draft.role_instance_id,
                            current_sequence + 1,
                            draft.status,
                            draft.goal,
                            draft.step,
                            draft.completed_action,
                            draft.activity,
                            draft.blocker,
                            draft.next_action,
                            draft.safe_summary,
                        ),
                    ).fetchone()
                    return _record(draft.project_id, row)
        except (ProgressError, ValueError):
            raise
        except psycopg.errors.ForeignKeyViolation:
            raise ProgressNotFound(
                "work item or project role instance not found"
            ) from None
        except psycopg.errors.UniqueViolation:
            raise ProgressConflict("progress checkpoint conflicts") from None
        except Exception:
            raise ProgressError(_STORE_ERROR) from None

    def read(self, project_id: str, checkpoint_id: str) -> ProgressRecord:
        project_id = _identifier(project_id, "project_id")
        checkpoint_id = _identifier(checkpoint_id, "checkpoint_id")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                row = connection.execute(
                    f"""
                    SELECT {_COLUMNS}
                    FROM {SCHEMA}.progress
                    WHERE project_id = %s AND checkpoint_id = %s
                    """,
                    (project_id, checkpoint_id),
                ).fetchone()
            if row is None:
                raise ProgressNotFound("progress checkpoint not found")
            return _record(project_id, row)
        except ProgressError:
            raise
        except Exception:
            raise ProgressError(_STORE_ERROR) from None


def _record(project_id: str, row: object) -> ProgressRecord:
    if not isinstance(row, (tuple, list)) or len(row) != 14:
        raise ProgressError(_STORE_ERROR)
    return ProgressRecord(project_id, *row)


def _same_checkpoint(record: ProgressRecord, draft: ProgressDraft) -> bool:
    return (
        record.work_item_id == draft.work_item_id
        and record.role_instance_id == draft.role_instance_id
        and record.sequence == draft.expected_previous_sequence + 1
        and record.status == draft.status
        and record.goal == draft.goal
        and record.step == draft.step
        and record.completed_action == draft.completed_action
        and record.activity == draft.activity
        and record.blocker == draft.blocker
        and record.next_action == draft.next_action
        and record.safe_summary == draft.safe_summary
    )


def _identifier(value: object, field_name: str) -> str:
    value = _required_text(value, field_name, 128)
    if _ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _required_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    value = value.strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")
    reject_sensitive_content(value, field_name)
    return value


def _optional_text(value: object, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name, maximum)


def reject_sensitive_content(value: str, field_name: str) -> None:
    if any(pattern.search(value) is not None for pattern in _SENSITIVE_PATTERNS):
        raise ProgressSensitiveContent(
            f"{field_name} contains restricted content"
        )
