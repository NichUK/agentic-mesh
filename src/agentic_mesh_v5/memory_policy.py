from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, Protocol


MemoryScope = Literal["project_role", "project", "organization_role"]
Classification = Literal["project_specific", "organization_generic", "uncertain"]


class MemoryPolicyError(ValueError):
    pass


class MemorySensitiveContentError(MemoryPolicyError):
    pass


class PolicyContext(Protocol):
    organization_id: str
    project_id: str


class PolicySource(Protocol):
    kind: str
    reference: str
    observed_version: str


class MemoryPolicy(Protocol):
    def evaluate(
        self,
        context: PolicyContext,
        *,
        requested_scope: MemoryScope,
        subject: str,
        summary: str,
        tags: tuple[str, ...],
        source: PolicySource,
        project_identifiers: tuple[str, ...],
    ) -> MemoryPolicyDecision: ...


@dataclass(frozen=True)
class Redaction:
    kind: Literal["email", "telephone"]
    count: int

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "count": self.count}


@dataclass(frozen=True)
class MemoryPolicyDecision:
    classification: Classification
    effective_scope: MemoryScope
    sanitized_summary: str
    reasons: tuple[str, ...]
    redactions: tuple[Redaction, ...]
    policy_version: str = "deterministic-v1"

    def evidence(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "reasons": list(self.reasons),
        }

    def redaction_evidence(self) -> list[dict[str, object]]:
        return [item.to_dict() for item in self.redactions]


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(
        r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)"
        r"\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE),
    re.compile(r"[?&](?:sig|signature)=[^&\s]+", re.IGNORECASE),
    re.compile(
        r"\b(?:gh[pousr]_|github_pat_|sk-|xox[baprs]-|AKIA)[A-Za-z0-9_-]{8,}",
        re.IGNORECASE,
    ),
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?!\w)")
_TELEPHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")


class ConservativeMemoryPolicy:
    """Small deterministic policy; uncertain content never reaches organization memory."""

    def __init__(
        self, *, project_aliases: dict[str, tuple[str, ...] | list[str]] | None = None
    ) -> None:
        self._aliases = {
            project_id: tuple(_identifier(item) for item in aliases)
            for project_id, aliases in (project_aliases or {}).items()
        }

    def evaluate(
        self,
        context: PolicyContext,
        *,
        requested_scope: MemoryScope,
        subject: str,
        summary: str,
        tags: tuple[str, ...],
        source: PolicySource,
        project_identifiers: tuple[str, ...] = (),
    ) -> MemoryPolicyDecision:
        if requested_scope not in {"project_role", "project", "organization_role"}:
            raise MemoryPolicyError("requested memory scope is invalid")
        structured = (subject, *tags, source.reference, source.observed_version)
        self._reject_secrets((*structured, summary))
        if any(_EMAIL.search(item) or _TELEPHONE.search(item) for item in structured):
            raise MemorySensitiveContentError(
                "personal data in structured memory fields cannot be redacted safely"
            )
        sanitized, redactions = _redact(summary)
        identifiers = (
            context.project_id,
            *project_identifiers,
            *(alias for aliases in self._aliases.values() for alias in aliases),
        )
        combined = "\n".join((*structured, sanitized))
        project_specific = any(_contains_identifier(combined, item) for item in identifiers)
        organization_source = source.kind in {"policy", "release"} and source.reference.startswith(
            f"{source.kind}://{context.organization_id}/"
        )
        if project_specific:
            classification: Classification = "project_specific"
            reasons = ("project-identifier-present",)
        elif organization_source:
            classification = "organization_generic"
            reasons = ("approved-organization-source",)
        else:
            classification = "uncertain"
            reasons = ("no-positive-generic-evidence",)
        if redactions:
            reasons += ("personal-data-redacted",)
        effective_scope: MemoryScope = requested_scope
        if requested_scope == "organization_role" and classification != "organization_generic":
            effective_scope = "project_role"
            reasons += ("organization-promotion-denied",)
        return MemoryPolicyDecision(
            classification=classification,
            effective_scope=effective_scope,
            sanitized_summary=sanitized,
            reasons=reasons,
            redactions=redactions,
        )

    @staticmethod
    def _reject_secrets(values: tuple[str, ...]) -> None:
        if any(pattern.search(value) for value in values for pattern in _SECRET_PATTERNS):
            raise MemorySensitiveContentError("secret-like content cannot enter memory")


def _redact(value: str) -> tuple[str, tuple[Redaction, ...]]:
    value, email_count = _EMAIL.subn("[REDACTED:email]", value)
    value, phone_count = _TELEPHONE.subn("[REDACTED:telephone]", value)
    evidence = tuple(
        Redaction(kind, count)
        for kind, count in (("email", email_count), ("telephone", phone_count))
        if count
    )
    return value, evidence


def _contains_identifier(value: str, identifier: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9]){re.escape(identifier)}(?![A-Za-z0-9])"
    return re.search(pattern, value, re.IGNORECASE) is not None


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise MemoryPolicyError("project alias is invalid")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise MemoryPolicyError("project alias contains sensitive content")
    return value.strip()
