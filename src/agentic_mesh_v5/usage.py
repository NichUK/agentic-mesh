from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

import psycopg

from agentic_mesh_v5.database import DatabaseConfigurationError
from agentic_mesh_v5.database import DatabaseError
from agentic_mesh_v5.database import SCHEMA
from agentic_mesh_v5.worker_provider import ProviderCapacity
from agentic_mesh_v5.worker_provider import ProviderCredits
from agentic_mesh_v5.worker_provider import ProviderRateLimitWindow
from agentic_mesh_v5.worker_provider import ProviderSpendControl
from agentic_mesh_v5.worker_provider import ProviderUsage


_STORE_ERROR = "usage operation failed"
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_USAGE_COLUMNS = """
    work_item_id, role_instance_id, input_tokens, cached_input_tokens,
    output_tokens, reasoning_output_tokens, total_tokens,
    first_observed_at, updated_at
"""
_CAPACITY_COLUMNS = """
    observation_id, available, observed_at, limit_id, limit_name, plan_type,
    primary_used_percent, primary_resets_at, primary_window_minutes,
    secondary_used_percent, secondary_resets_at, secondary_window_minutes,
    credit_balance, has_credits, unlimited_credits,
    individual_limit, individual_used, individual_remaining_percent,
    individual_resets_at, reset_credits_available,
    reset_credits_earliest_expiry, updated_at
"""


class UsageError(DatabaseError):
    pass


class UsageNotFound(UsageError):
    pass


class UsageConflict(UsageError):
    pass


@dataclass(frozen=True, slots=True)
class TurnUsageDraft:
    project_id: str
    work_item_id: str
    role_instance_id: str
    provider_id: str
    account_scope: str
    turn_id: str
    usage: ProviderUsage

    def __post_init__(self) -> None:
        for name in (
            "project_id",
            "work_item_id",
            "role_instance_id",
            "provider_id",
            "account_scope",
            "turn_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        _validate_usage(self.usage)


@dataclass(frozen=True, slots=True)
class TurnUsageRecord:
    project_id: str
    work_item_id: str
    role_instance_id: str
    provider_id: str
    account_scope: str
    turn_id: str
    usage: ProviderUsage
    first_observed_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CapacityDraft:
    project_id: str
    provider_id: str
    account_scope: str
    observation_id: str
    observed_at: datetime
    capacity: ProviderCapacity

    def __post_init__(self) -> None:
        for name in ("project_id", "provider_id", "account_scope", "observation_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "observed_at", _aware_time(self.observed_at))
        _validate_capacity(self.capacity)


@dataclass(frozen=True, slots=True)
class CapacityRecord:
    project_id: str
    provider_id: str
    account_scope: str
    observation_id: str
    observed_at: str
    capacity: ProviderCapacity
    updated_at: str


class UsageStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url

    def record_turn(self, draft: TurnUsageDraft) -> TurnUsageRecord:
        if not isinstance(draft, TurnUsageDraft):
            raise ValueError("turn usage draft is invalid")
        key = "\x1f".join(
            (draft.project_id, draft.provider_id, draft.account_scope, draft.turn_id)
        )
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (key,),
                    )
                    authorized = connection.execute(
                        f"""
                        SELECT 1
                        FROM {SCHEMA}.work_items AS work
                        JOIN {SCHEMA}.role_instances AS instance
                          ON instance.project_id = work.project_id
                         AND instance.instance_id = %s
                        WHERE work.project_id = %s AND work.work_item_id = %s
                        """,
                        (
                            draft.role_instance_id,
                            draft.project_id,
                            draft.work_item_id,
                        ),
                    ).fetchone()
                    if authorized is None:
                        raise UsageNotFound(
                            "work item or project role instance not found"
                        )
                    row = connection.execute(
                        f"""
                        SELECT {_USAGE_COLUMNS}
                        FROM {SCHEMA}.turn_usage
                        WHERE project_id = %s AND provider_id = %s
                          AND account_scope = %s AND turn_id = %s
                        FOR UPDATE
                        """,
                        (
                            draft.project_id,
                            draft.provider_id,
                            draft.account_scope,
                            draft.turn_id,
                        ),
                    ).fetchone()
                    if row is None:
                        row = connection.execute(
                            f"""
                            INSERT INTO {SCHEMA}.turn_usage
                                (project_id, provider_id, account_scope, turn_id,
                                 work_item_id, role_instance_id, input_tokens,
                                 cached_input_tokens, output_tokens,
                                 reasoning_output_tokens, total_tokens)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING {_USAGE_COLUMNS}
                            """,
                            (
                                draft.project_id,
                                draft.provider_id,
                                draft.account_scope,
                                draft.turn_id,
                                draft.work_item_id,
                                draft.role_instance_id,
                                *_usage_values(draft.usage),
                            ),
                        ).fetchone()
                    else:
                        current = _usage_record(draft, row)
                        _validate_update(current, draft)
                        if current.usage == draft.usage:
                            return current
                        row = connection.execute(
                            f"""
                            UPDATE {SCHEMA}.turn_usage
                            SET input_tokens = %s, cached_input_tokens = %s,
                                output_tokens = %s, reasoning_output_tokens = %s,
                                total_tokens = %s, updated_at = clock_timestamp()
                            WHERE project_id = %s AND provider_id = %s
                              AND account_scope = %s AND turn_id = %s
                            RETURNING {_USAGE_COLUMNS}
                            """,
                            (
                                *_usage_values(draft.usage),
                                draft.project_id,
                                draft.provider_id,
                                draft.account_scope,
                                draft.turn_id,
                            ),
                        ).fetchone()
                    return _usage_record(draft, row)
        except UsageError:
            raise
        except psycopg.errors.ForeignKeyViolation:
            raise UsageNotFound(
                "work item or project role instance not found"
            ) from None
        except Exception:
            raise UsageError(_STORE_ERROR) from None

    def record_capacity(self, draft: CapacityDraft) -> CapacityRecord:
        if not isinstance(draft, CapacityDraft):
            raise ValueError("capacity draft is invalid")
        key = "\x1f".join((draft.project_id, draft.provider_id, draft.account_scope))
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (key,),
                    )
                    exists = connection.execute(
                        f"SELECT 1 FROM {SCHEMA}.projects WHERE project_id = %s",
                        (draft.project_id,),
                    ).fetchone()
                    if exists is None:
                        raise UsageNotFound("project not found")
                    row = connection.execute(
                        f"""
                        SELECT {_CAPACITY_COLUMNS}
                        FROM {SCHEMA}.provider_capacity
                        WHERE project_id = %s AND provider_id = %s
                          AND account_scope = %s
                        FOR UPDATE
                        """,
                        (draft.project_id, draft.provider_id, draft.account_scope),
                    ).fetchone()
                    if row is not None:
                        current = _capacity_record(draft, row)
                        if current.observation_id == draft.observation_id:
                            if _same_capacity(current, draft):
                                return current
                            raise UsageConflict(
                                "capacity observation id is already used differently"
                            )
                        current_time = datetime.fromisoformat(current.observed_at)
                        if draft.observed_at <= current_time:
                            return current
                    values = _capacity_values(draft.capacity)
                    row = connection.execute(
                        f"""
                        INSERT INTO {SCHEMA}.provider_capacity
                            (project_id, provider_id, account_scope,
                             observation_id, available, observed_at,
                             limit_id, limit_name, plan_type,
                             primary_used_percent, primary_resets_at,
                             primary_window_minutes, secondary_used_percent,
                             secondary_resets_at, secondary_window_minutes,
                             credit_balance, has_credits, unlimited_credits,
                             individual_limit, individual_used,
                             individual_remaining_percent, individual_resets_at,
                             reset_credits_available,
                             reset_credits_earliest_expiry)
                        VALUES (%s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (project_id, provider_id, account_scope)
                        DO UPDATE SET
                            observation_id = EXCLUDED.observation_id,
                            available = EXCLUDED.available,
                            observed_at = EXCLUDED.observed_at,
                            limit_id = EXCLUDED.limit_id,
                            limit_name = EXCLUDED.limit_name,
                            plan_type = EXCLUDED.plan_type,
                            primary_used_percent = EXCLUDED.primary_used_percent,
                            primary_resets_at = EXCLUDED.primary_resets_at,
                            primary_window_minutes = EXCLUDED.primary_window_minutes,
                            secondary_used_percent = EXCLUDED.secondary_used_percent,
                            secondary_resets_at = EXCLUDED.secondary_resets_at,
                            secondary_window_minutes = EXCLUDED.secondary_window_minutes,
                            credit_balance = EXCLUDED.credit_balance,
                            has_credits = EXCLUDED.has_credits,
                            unlimited_credits = EXCLUDED.unlimited_credits,
                            individual_limit = EXCLUDED.individual_limit,
                            individual_used = EXCLUDED.individual_used,
                            individual_remaining_percent = EXCLUDED.individual_remaining_percent,
                            individual_resets_at = EXCLUDED.individual_resets_at,
                            reset_credits_available = EXCLUDED.reset_credits_available,
                            reset_credits_earliest_expiry = EXCLUDED.reset_credits_earliest_expiry,
                            updated_at = clock_timestamp()
                        RETURNING {_CAPACITY_COLUMNS}
                        """,
                        (
                            draft.project_id,
                            draft.provider_id,
                            draft.account_scope,
                            draft.observation_id,
                            draft.capacity.available,
                            draft.observed_at,
                            *values,
                        ),
                    ).fetchone()
                    return _capacity_record(draft, row)
        except UsageError:
            raise
        except psycopg.errors.UniqueViolation:
            raise UsageConflict("capacity observation id conflicts") from None
        except Exception:
            raise UsageError(_STORE_ERROR) from None

    def summary(
        self, project_id: str, *, provider_id: str, account_scope: str
    ) -> dict[str, object]:
        project_id = _identifier(project_id, "project_id")
        provider_id = _identifier(provider_id, "provider_id")
        account_scope = _identifier(account_scope, "account_scope")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, connect_timeout=5
            ) as connection:
                if connection.execute(
                    f"SELECT 1 FROM {SCHEMA}.projects WHERE project_id = %s",
                    (project_id,),
                ).fetchone() is None:
                    raise UsageNotFound("project not found")
                totals = connection.execute(
                    f"""
                    SELECT COUNT(*), COALESCE(SUM(input_tokens), 0),
                           COALESCE(SUM(cached_input_tokens), 0),
                           COALESCE(SUM(output_tokens), 0),
                           COALESCE(SUM(reasoning_output_tokens), 0),
                           COALESCE(SUM(total_tokens), 0), MAX(updated_at)
                    FROM {SCHEMA}.turn_usage
                    WHERE project_id = %s AND provider_id = %s
                      AND account_scope = %s
                    """,
                    (project_id, provider_id, account_scope),
                ).fetchone()
                capacity_row = connection.execute(
                    f"""
                    SELECT {_CAPACITY_COLUMNS}
                    FROM {SCHEMA}.provider_capacity
                    WHERE project_id = %s AND provider_id = %s
                      AND account_scope = %s
                    """,
                    (project_id, provider_id, account_scope),
                ).fetchone()
            turn_count = totals[0]
            total_tokens = totals[5]
            capacity = (
                None
                if capacity_row is None
                else _capacity_record_for_key(
                    project_id, provider_id, account_scope, capacity_row
                )
            )
            return {
                "project_id": project_id,
                "provider_id": provider_id,
                "account_scope": account_scope,
                "account_scope_shared": True,
                "turn_count": turn_count,
                "input_tokens": totals[1],
                "cached_input_tokens": totals[2],
                "output_tokens": totals[3],
                "reasoning_output_tokens": totals[4],
                "total_tokens": total_tokens,
                "average_tokens_per_turn": (
                    None if turn_count == 0 else total_tokens / turn_count
                ),
                "usage_updated_at": _iso(totals[6]),
                "capacity": _capacity_view(capacity),
            }
        except UsageError:
            raise
        except Exception:
            raise UsageError(_STORE_ERROR) from None


def _validate_update(current: TurnUsageRecord, draft: TurnUsageDraft) -> None:
    if (
        current.work_item_id != draft.work_item_id
        or current.role_instance_id != draft.role_instance_id
    ):
        raise UsageConflict("provider turn is already assigned differently")
    if any(
        new < old
        for old, new in zip(
            _usage_values(current.usage), _usage_values(draft.usage), strict=True
        )
    ):
        raise UsageConflict("provider turn usage counters cannot decrease")


def _usage_record(draft: TurnUsageDraft, row: object) -> TurnUsageRecord:
    if not isinstance(row, (tuple, list)) or len(row) != 9:
        raise UsageError(_STORE_ERROR)
    return TurnUsageRecord(
        project_id=draft.project_id,
        work_item_id=row[0],
        role_instance_id=row[1],
        provider_id=draft.provider_id,
        account_scope=draft.account_scope,
        turn_id=draft.turn_id,
        usage=ProviderUsage(*row[2:7]),
        first_observed_at=_iso(row[7]),
        updated_at=_iso(row[8]),
    )


def _capacity_record(draft: CapacityDraft, row: object) -> CapacityRecord:
    return _capacity_record_for_key(
        draft.project_id, draft.provider_id, draft.account_scope, row
    )


def _capacity_record_for_key(
    project_id: str, provider_id: str, account_scope: str, row: object
) -> CapacityRecord:
    if not isinstance(row, (tuple, list)) or len(row) != 22:
        raise UsageError(_STORE_ERROR)
    primary = _window(row[6], row[7], row[8])
    secondary = _window(row[9], row[10], row[11])
    credits = (
        None
        if row[13] is None
        else ProviderCredits(
            balance=None if row[12] is None else str(row[12]),
            has_credits=row[13],
            unlimited=row[14],
        )
    )
    individual = (
        None
        if row[15] is None
        else ProviderSpendControl(
            limit=str(row[15]),
            used=str(row[16]),
            remaining_percent=row[17],
            resets_at=row[18],
        )
    )
    capacity = ProviderCapacity(
        available=row[1],
        limit_id=row[3],
        limit_name=row[4],
        plan_type=row[5],
        primary=primary,
        secondary=secondary,
        credits=credits,
        individual_limit=individual,
        reset_credits_available=row[19],
        reset_credits_earliest_expiry=row[20],
    )
    return CapacityRecord(
        project_id=project_id,
        provider_id=provider_id,
        account_scope=account_scope,
        observation_id=row[0],
        observed_at=_iso(row[2]),
        capacity=capacity,
        updated_at=_iso(row[21]),
    )


def _same_capacity(record: CapacityRecord, draft: CapacityDraft) -> bool:
    return (
        datetime.fromisoformat(record.observed_at) == draft.observed_at
        and record.capacity == draft.capacity
    )


def _capacity_values(capacity: ProviderCapacity) -> tuple[object, ...]:
    primary = capacity.primary
    secondary = capacity.secondary
    credits = capacity.credits
    individual = capacity.individual_limit
    return (
        capacity.limit_id,
        capacity.limit_name,
        capacity.plan_type,
        None if primary is None else primary.used_percent,
        None if primary is None else primary.resets_at,
        None if primary is None else primary.window_minutes,
        None if secondary is None else secondary.used_percent,
        None if secondary is None else secondary.resets_at,
        None if secondary is None else secondary.window_minutes,
        None if credits is None else _decimal(credits.balance, "credit balance"),
        None if credits is None else credits.has_credits,
        None if credits is None else credits.unlimited,
        None if individual is None else _decimal(individual.limit, "individual limit"),
        None if individual is None else _decimal(individual.used, "individual used"),
        None if individual is None else individual.remaining_percent,
        None if individual is None else individual.resets_at,
        capacity.reset_credits_available,
        capacity.reset_credits_earliest_expiry,
    )


def _capacity_view(record: CapacityRecord | None) -> dict[str, object]:
    if record is None:
        return {"status": "unknown", "observed_at": None}
    capacity = record.capacity
    if not capacity.available:
        return {"status": "unknown", "observed_at": record.observed_at}
    return {
        "status": "known",
        "observed_at": record.observed_at,
        "limit_id": capacity.limit_id,
        "limit_name": capacity.limit_name,
        "plan_type": capacity.plan_type,
        "primary": _window_view(capacity.primary),
        "secondary": _window_view(capacity.secondary),
        "credits": (
            None
            if capacity.credits is None
            else {
                "balance": capacity.credits.balance,
                "has_credits": capacity.credits.has_credits,
                "unlimited": capacity.credits.unlimited,
            }
        ),
        "individual_limit": (
            None
            if capacity.individual_limit is None
            else {
                "limit": capacity.individual_limit.limit,
                "used": capacity.individual_limit.used,
                "remaining_percent": capacity.individual_limit.remaining_percent,
                "resets_at": _iso(capacity.individual_limit.resets_at),
            }
        ),
        "reset_credits_available": capacity.reset_credits_available,
        "reset_credits_earliest_expiry": _iso(
            capacity.reset_credits_earliest_expiry
        ),
    }


def _window_view(window: ProviderRateLimitWindow | None) -> dict[str, object] | None:
    if window is None:
        return None
    return {
        "used_percent": window.used_percent,
        "remaining_percent": 100 - window.used_percent,
        "resets_at": _iso(window.resets_at),
        "window_minutes": window.window_minutes,
    }


def _window(
    used_percent: int | None, resets_at: datetime | None, minutes: int | None
) -> ProviderRateLimitWindow | None:
    if used_percent is None:
        return None
    return ProviderRateLimitWindow(used_percent, resets_at, minutes)


def _validate_usage(usage: object) -> None:
    if not isinstance(usage, ProviderUsage):
        raise ValueError("provider usage is invalid")
    if any(type(value) is not int or value < 0 for value in _usage_values(usage)):
        raise ValueError("provider usage is invalid")


def _validate_capacity(capacity: object) -> None:
    if not isinstance(capacity, ProviderCapacity) or type(capacity.available) is not bool:
        raise ValueError("provider capacity is invalid")
    if not capacity.available:
        if capacity != ProviderCapacity.unknown():
            raise ValueError("unknown provider capacity must not contain values")
        return
    for value in (capacity.limit_id, capacity.limit_name, capacity.plan_type):
        if value is not None:
            _safe_label(value)
    for window in (capacity.primary, capacity.secondary):
        if window is not None:
            if not isinstance(window, ProviderRateLimitWindow):
                raise ValueError("provider capacity window is invalid")
            _percent(window.used_percent)
            if window.resets_at is not None:
                _aware_time(window.resets_at)
            if window.window_minutes is not None and (
                type(window.window_minutes) is not int or window.window_minutes <= 0
            ):
                raise ValueError("provider capacity window is invalid")
    if capacity.credits is not None:
        if not isinstance(capacity.credits, ProviderCredits):
            raise ValueError("provider credits are invalid")
        if type(capacity.credits.has_credits) is not bool or type(
            capacity.credits.unlimited
        ) is not bool:
            raise ValueError("provider credits are invalid")
        _decimal(capacity.credits.balance, "credit balance")
    if capacity.individual_limit is not None:
        item = capacity.individual_limit
        if not isinstance(item, ProviderSpendControl):
            raise ValueError("provider spend control is invalid")
        _decimal(item.limit, "individual limit")
        _decimal(item.used, "individual used")
        _percent(item.remaining_percent)
        _aware_time(item.resets_at)
    if capacity.reset_credits_available is not None and (
        type(capacity.reset_credits_available) is not int
        or capacity.reset_credits_available < 0
    ):
        raise ValueError("reset credit count is invalid")
    if capacity.reset_credits_earliest_expiry is not None:
        _aware_time(capacity.reset_credits_earliest_expiry)


def _usage_values(usage: ProviderUsage) -> tuple[int, int, int, int, int]:
    return (
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        usage.reasoning_output_tokens,
        usage.total_tokens,
    )


def _decimal(value: str | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{field_name} is invalid")
    try:
        selected = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{field_name} is invalid") from None
    if not selected.is_finite() or selected < 0:
        raise ValueError(f"{field_name} is invalid")
    return selected


def _percent(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError("provider percentage is invalid")
    return value


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    value = value.strip()
    if _ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _safe_label(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("provider capacity label is invalid")
    selected = value.strip()
    if (
        not selected
        or len(selected) > 128
        or any(ord(character) < 32 for character in selected)
    ):
        raise ValueError("provider capacity label is invalid")
    return selected


def _aware_time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
