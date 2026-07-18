from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re


API_PRINCIPALS_FILE_ENV = "AGENTIC_MESH_V5_API_PRINCIPALS_FILE"
ALLOWED_SCOPES = frozenset({"project:create", "read", "write"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AuthenticationConfigurationError(RuntimeError):
    """Raised when the external bootstrap authentication file is unsafe."""


@dataclass(frozen=True)
class Principal:
    subject: str
    projects: frozenset[str]
    scopes: frozenset[str]

    def permits(self, *, project_id: str, scope: str) -> bool:
        return scope in self.scopes and (
            "*" in self.projects or project_id in self.projects
        )


@dataclass(frozen=True)
class _TokenRecord:
    token_sha256: str
    principal: Principal


class TokenAuthorizer:
    """Resolve opaque bearer tokens without retaining their plaintext value."""

    def __init__(self, records: Sequence[Mapping[str, object]]) -> None:
        parsed = tuple(self._parse_record(item) for item in records)
        if not parsed:
            raise AuthenticationConfigurationError(
                "at least one API principal is required"
            )
        digests = [item.token_sha256 for item in parsed]
        if len(digests) != len(set(digests)):
            raise AuthenticationConfigurationError("API token hashes must be unique")
        self._records = parsed

    @classmethod
    def from_file(cls, path: Path) -> TokenAuthorizer:
        if not path.exists() or not path.is_file():
            raise AuthenticationConfigurationError(
                "the API principals path must be an existing file"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthenticationConfigurationError(
                "the API principals file is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(payload, Mapping) or set(payload) != {"principals"}:
            raise AuthenticationConfigurationError(
                "the API principals file must contain only a principals array"
            )
        records = payload["principals"]
        if not isinstance(records, list):
            raise AuthenticationConfigurationError("principals must be an array")
        return cls(records)

    @classmethod
    def from_environment(cls) -> TokenAuthorizer:
        value = os.environ.get(API_PRINCIPALS_FILE_ENV, "").strip()
        if not value:
            raise AuthenticationConfigurationError(
                f"{API_PRINCIPALS_FILE_ENV} is required"
            )
        return cls.from_file(Path(value))

    def resolve(self, token: str) -> Principal | None:
        if not token:
            return None
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched: Principal | None = None
        for record in self._records:
            if hmac.compare_digest(candidate, record.token_sha256):
                matched = record.principal
        return matched

    @staticmethod
    def _parse_record(item: Mapping[str, object]) -> _TokenRecord:
        if not isinstance(item, Mapping):
            raise AuthenticationConfigurationError(
                "each API principal must be an object"
            )
        expected = {"subject", "token_sha256", "projects", "scopes"}
        if set(item) != expected:
            raise AuthenticationConfigurationError(
                "each API principal requires subject, token_sha256, projects, and scopes"
            )
        subject = _required_string(item["subject"], "subject")
        digest = _required_string(item["token_sha256"], "token_sha256")
        if SHA256_PATTERN.fullmatch(digest) is None:
            raise AuthenticationConfigurationError(
                "token_sha256 must be a lowercase SHA-256 digest"
            )
        projects = _string_set(item["projects"], "projects")
        scopes = _string_set(item["scopes"], "scopes")
        unknown = scopes - ALLOWED_SCOPES
        if unknown:
            raise AuthenticationConfigurationError(
                f"unsupported API scopes: {sorted(unknown)}"
            )
        return _TokenRecord(
            token_sha256=digest,
            principal=Principal(subject=subject, projects=projects, scopes=scopes),
        )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _string_set(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise AuthenticationConfigurationError(
            f"{field} must be a non-empty string array"
        )
    items = tuple(_required_string(item, field) for item in value)
    if len(items) != len(set(items)):
        raise AuthenticationConfigurationError(f"{field} must not contain duplicates")
    return frozenset(items)
