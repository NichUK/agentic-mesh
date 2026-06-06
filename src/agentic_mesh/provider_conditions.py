from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from agentic_mesh.external_actions import safe_text
from agentic_mesh.external_actions import safe_token
from agentic_mesh.models import utc_now_iso


PROVIDER_CONDITION_SCHEMA_VERSION = "provider-condition-v1"
PROVIDER_PROBE_RECEIPT_SCHEMA_VERSION = "provider-condition-probe-receipt-v1"

CONDITION_CLASSES = {
    "healthy",
    "rate_limited",
    "capacity_exhausted",
    "credit_exhausted",
    "quota_exceeded",
    "auth_invalid",
    "auth_missing",
    "outage_suspected",
    "timeout_window_active",
    "operator_review_required",
    "unknown",
}
CONFIDENCE_VALUES = {"confirmed", "inferred", "unknown"}

_CODEX_FAILURE_MAP = {
    "usage_limit": "capacity_exhausted",
    "capacity_exhausted": "capacity_exhausted",
    "credit_exhausted": "credit_exhausted",
    "quota_exceeded": "quota_exceeded",
    "rate_limited": "rate_limited",
    "auth_failed": "auth_invalid",
    "auth_invalid": "auth_invalid",
    "auth_missing": "auth_missing",
    "timeout": "timeout_window_active",
    "operator_review": "operator_review_required",
    "operator_review_required": "operator_review_required",
}
_CONDITION_LABELS = {
    "healthy": "Provider healthy",
    "rate_limited": "Provider rate limit",
    "capacity_exhausted": "Provider capacity exhausted",
    "credit_exhausted": "Provider credit exhausted",
    "quota_exceeded": "Provider quota exceeded",
    "auth_invalid": "Provider authentication invalid",
    "auth_missing": "Provider authentication missing",
    "outage_suspected": "Provider outage suspected",
    "timeout_window_active": "Provider timeout window active",
    "operator_review_required": "Provider operator review required",
    "unknown": "Provider condition unknown",
}
_DIAGNOSTIC_CLASSES = {
    "provider_limit",
    "provider_credit",
    "provider_quota",
    "provider_rate_limit",
    "provider_auth",
    "provider_timeout",
    "provider_outage",
    "provider_unknown",
    "probe_skipped",
}


@dataclass(frozen=True)
class ProviderConditionResult:
    provider_adapter_id: str
    provider_display_label: str
    condition_class: str
    confidence: str
    evidence_freshness: str
    safe_human_label: str
    redacted_diagnostic_class: str
    retry_after: str | None = None
    next_check_at: str | None = None
    probe_receipt_id: str | None = None
    skipped: bool = False
    denial_reason: str | None = None
    schema_version: str = PROVIDER_CONDITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.condition_class not in CONDITION_CLASSES:
            raise ValueError(f"unsupported provider condition `{self.condition_class}`")
        if self.confidence not in CONFIDENCE_VALUES:
            raise ValueError(f"unsupported provider confidence `{self.confidence}`")
        if self.redacted_diagnostic_class not in _DIAGNOSTIC_CLASSES:
            raise ValueError(
                f"unsupported provider diagnostic `{self.redacted_diagnostic_class}`"
            )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "provider_adapter_id": safe_token(self.provider_adapter_id),
            "provider_display_label": safe_text(self.provider_display_label, 80),
            "condition_class": self.condition_class,
            "confidence": self.confidence,
            "evidence_freshness": self.evidence_freshness,
            "retry_after": self.retry_after,
            "next_check_at": self.next_check_at,
            "safe_human_label": safe_text(self.safe_human_label, 120),
            "redacted_diagnostic_class": self.redacted_diagnostic_class,
            "probe_receipt_id": safe_token(self.probe_receipt_id)
            if self.probe_receipt_id
            else None,
            "skipped": self.skipped,
            "denial_reason": safe_token(self.denial_reason)
            if self.denial_reason
            else None,
        }
        return {key: value for key, value in payload.items() if value is not None}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ProviderConditionResult":
        return ProviderConditionResult(
            provider_adapter_id=str(data["provider_adapter_id"]),
            provider_display_label=str(data["provider_display_label"]),
            condition_class=str(data["condition_class"]),
            confidence=str(data["confidence"]),
            evidence_freshness=str(data["evidence_freshness"]),
            safe_human_label=str(data["safe_human_label"]),
            redacted_diagnostic_class=str(data["redacted_diagnostic_class"]),
            retry_after=data.get("retry_after"),
            next_check_at=data.get("next_check_at"),
            probe_receipt_id=data.get("probe_receipt_id"),
            skipped=bool(data.get("skipped", False)),
            denial_reason=data.get("denial_reason"),
        )


class CodexProviderConditionProfile:
    provider_adapter_id = "codex-cli"
    provider_display_label = "Codex"

    def classify_safe_evidence(self, evidence: dict[str, Any]) -> ProviderConditionResult:
        if not evidence.get("safe_non_prompt_evidence", True):
            return self.unsafe_probe_denied("unsafe_prompt_or_capacity_probe")
        failure_class = str(evidence.get("failure_class") or "unknown_provider_failure")
        condition = _CODEX_FAILURE_MAP.get(failure_class, "unknown")
        confidence = str(evidence.get("confidence") or ("inferred" if condition != "unknown" else "unknown"))
        if confidence not in CONFIDENCE_VALUES:
            confidence = "unknown"
        diagnostic = _diagnostic_class(condition)
        freshness = str(evidence.get("evidence_freshness") or utc_now_iso())
        retry_after = evidence.get("retry_after") if condition == "rate_limited" else None
        next_check_at = evidence.get("next_check_at")
        return ProviderConditionResult(
            provider_adapter_id=self.provider_adapter_id,
            provider_display_label=self.provider_display_label,
            condition_class=condition,
            confidence=confidence,
            evidence_freshness=freshness,
            retry_after=str(retry_after) if retry_after else None,
            next_check_at=str(next_check_at) if next_check_at else None,
            safe_human_label=_CONDITION_LABELS[condition],
            redacted_diagnostic_class=diagnostic,
            probe_receipt_id=_probe_receipt_id(self.provider_adapter_id, failure_class, freshness),
        )

    def unsafe_probe_denied(self, denial_reason: str) -> ProviderConditionResult:
        return ProviderConditionResult(
            provider_adapter_id=self.provider_adapter_id,
            provider_display_label=self.provider_display_label,
            condition_class="operator_review_required",
            confidence="unknown",
            evidence_freshness=utc_now_iso(),
            safe_human_label=_CONDITION_LABELS["operator_review_required"],
            redacted_diagnostic_class="probe_skipped",
            probe_receipt_id=_probe_receipt_id(
                self.provider_adapter_id,
                denial_reason,
                utc_now_iso(),
            ),
            skipped=True,
            denial_reason=denial_reason,
        )


@dataclass(frozen=True)
class ProviderProbeCadence:
    minimum_interval_seconds: int = 300
    backoff_seconds: int = 900
    jitter_seconds: int = 30

    def next_allowed_at(
        self,
        *,
        project_id: str,
        provider_adapter_id: str,
        role_id: str,
        last_probe_at: str | None,
        retry_after: str | None = None,
        now: str | None = None,
        failed_probe: bool = False,
    ) -> str:
        current = _parse_time(now) or datetime.now(timezone.utc)
        candidates: list[datetime] = []
        if last_probe_at:
            base = _parse_time(last_probe_at)
            if base:
                interval = self.backoff_seconds if failed_probe else self.minimum_interval_seconds
                candidates.append(base + timedelta(seconds=interval))
        retry_after_time = _parse_time(retry_after)
        if retry_after_time:
            candidates.append(retry_after_time)
        base_next = max(candidates) if candidates else current
        jitter = _stable_jitter(
            project_id=project_id,
            provider_adapter_id=provider_adapter_id,
            role_id=role_id,
            max_seconds=self.jitter_seconds,
        )
        return (base_next + timedelta(seconds=jitter)).isoformat()

    def should_probe(
        self,
        *,
        next_allowed_at: str | None,
        now: str | None = None,
    ) -> bool:
        allowed = _parse_time(next_allowed_at)
        current = _parse_time(now) or datetime.now(timezone.utc)
        return allowed is None or current >= allowed


def _diagnostic_class(condition: str) -> str:
    return {
        "capacity_exhausted": "provider_limit",
        "credit_exhausted": "provider_credit",
        "quota_exceeded": "provider_quota",
        "rate_limited": "provider_rate_limit",
        "auth_invalid": "provider_auth",
        "auth_missing": "provider_auth",
        "timeout_window_active": "provider_timeout",
        "outage_suspected": "provider_outage",
        "operator_review_required": "probe_skipped",
    }.get(condition, "provider_unknown")


def _probe_receipt_id(provider_adapter_id: str, value: str, freshness: str) -> str:
    digest = hashlib.sha256(
        f"{provider_adapter_id}|{value}|{freshness}".encode("utf-8")
    ).hexdigest()
    return f"provider-probe-{digest[:24]}"


def _stable_jitter(
    *,
    project_id: str,
    provider_adapter_id: str,
    role_id: str,
    max_seconds: int,
) -> int:
    if max_seconds <= 0:
        return 0
    seed = hashlib.sha256(
        f"{project_id}|{provider_adapter_id}|{role_id}".encode("utf-8")
    ).hexdigest()
    return random.Random(seed).randint(0, max_seconds)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
