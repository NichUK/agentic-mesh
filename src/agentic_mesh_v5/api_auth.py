from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from typing import Protocol

import jwt
from jwt import PyJWKClient


API_PRINCIPALS_FILE_ENV = "AGENTIC_MESH_V5_API_PRINCIPALS_FILE"
ENTRA_TENANT_ID_ENV = "AGENTIC_MESH_V5_ENTRA_TENANT_ID"
ENTRA_AUDIENCE_ENV = "AGENTIC_MESH_V5_ENTRA_AUDIENCE"
ENTRA_BINDINGS_FILE_ENV = "AGENTIC_MESH_V5_ENTRA_BINDINGS_FILE"
ALLOWED_SCOPES = frozenset({"project:create", "read", "write", "recovery:execute"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ENTRA_ROLE_SCOPES = {
    "AgenticMesh.Viewer": frozenset({"read"}),
    "AgenticMesh.Sponsor": frozenset({"read", "write"}),
    "AgenticMesh.Operator": ALLOWED_SCOPES,
}


class AuthenticationConfigurationError(RuntimeError):
    """Raised when the external bootstrap authentication file is unsafe."""


@dataclass(frozen=True)
class Principal:
    subject: str
    projects: frozenset[str]
    scopes: frozenset[str]
    roles: frozenset[str] = frozenset()

    def permits(self, *, project_id: str, scope: str) -> bool:
        return scope in self.scopes and (
            "*" in self.projects or project_id in self.projects
        )


@dataclass(frozen=True)
class _TokenRecord:
    token_sha256: str
    principal: Principal


class Authorizer(Protocol):
    def resolve(self, token: str) -> Principal | None: ...


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


@dataclass(frozen=True)
class _EntraBinding:
    object_id: str
    subject: str
    projects: frozenset[str]


class EntraAuthorizer:
    """Validate tenant-specific Entra access tokens and bind them to projects."""

    def __init__(
        self,
        *,
        tenant_id: str,
        audience: str,
        bindings: Sequence[Mapping[str, object]],
        decoder: Callable[[str], Mapping[str, object]] | None = None,
    ) -> None:
        if GUID_PATTERN.fullmatch(tenant_id) is None:
            raise AuthenticationConfigurationError("Entra tenant_id must be a GUID")
        self._tenant_id = tenant_id.lower()
        self._audience = _required_string(audience, "Entra audience")
        self._issuer = f"https://login.microsoftonline.com/{self._tenant_id}/v2.0"
        parsed = tuple(self._parse_binding(item) for item in bindings)
        if not parsed:
            raise AuthenticationConfigurationError(
                "at least one Entra principal binding is required"
            )
        object_ids = [item.object_id for item in parsed]
        if len(object_ids) != len(set(object_ids)):
            raise AuthenticationConfigurationError(
                "Entra object_id bindings must be unique"
            )
        self._bindings = {item.object_id: item for item in parsed}
        self._decoder = decoder or self._decode
        self._jwks = PyJWKClient(
            f"https://login.microsoftonline.com/{self._tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
        )

    @classmethod
    def from_file(
        cls,
        *,
        tenant_id: str,
        audience: str,
        path: Path,
        decoder: Callable[[str], Mapping[str, object]] | None = None,
    ) -> EntraAuthorizer:
        payload = _read_json_object(path, "Entra bindings")
        if set(payload) != {"principals"} or not isinstance(payload["principals"], list):
            raise AuthenticationConfigurationError(
                "the Entra bindings file must contain only a principals array"
            )
        return cls(
            tenant_id=tenant_id,
            audience=audience,
            bindings=payload["principals"],
            decoder=decoder,
        )

    @classmethod
    def from_environment(cls) -> EntraAuthorizer:
        tenant_id = os.environ.get(ENTRA_TENANT_ID_ENV, "").strip()
        audience = os.environ.get(ENTRA_AUDIENCE_ENV, "").strip()
        bindings = os.environ.get(ENTRA_BINDINGS_FILE_ENV, "").strip()
        if not tenant_id or not audience or not bindings:
            raise AuthenticationConfigurationError(
                f"{ENTRA_TENANT_ID_ENV}, {ENTRA_AUDIENCE_ENV}, and "
                f"{ENTRA_BINDINGS_FILE_ENV} are required together"
            )
        return cls.from_file(
            tenant_id=tenant_id,
            audience=audience,
            path=Path(bindings),
        )

    def resolve(self, token: str) -> Principal | None:
        if not token:
            return None
        try:
            claims = self._decoder(token)
            tenant_id = _claim_string(claims, "tid").lower()
            object_id = _claim_string(claims, "oid").lower()
            roles_value = claims.get("roles")
            if tenant_id != self._tenant_id or not isinstance(roles_value, list):
                return None
            roles = frozenset(
                role
                for role in roles_value
                if isinstance(role, str) and role in ENTRA_ROLE_SCOPES
            )
            if not roles:
                return None
            binding = self._bindings.get(object_id)
            if binding is None:
                return None
            scopes = frozenset(
                scope for role in roles for scope in ENTRA_ROLE_SCOPES[role]
            )
            return Principal(
                subject=binding.subject,
                projects=binding.projects,
                scopes=scopes,
                roles=roles,
            )
        except (jwt.PyJWTError, AuthenticationConfigurationError, KeyError, TypeError):
            return None

    def _decode(self, token: str) -> Mapping[str, object]:
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self._audience,
            issuer=self._issuer,
            options={"require": ["exp", "iat", "iss", "aud", "tid", "oid"]},
        )
        if not isinstance(claims, Mapping):
            raise jwt.InvalidTokenError("access token claims must be an object")
        return claims

    @staticmethod
    def _parse_binding(item: Mapping[str, object]) -> _EntraBinding:
        if not isinstance(item, Mapping) or set(item) != {
            "object_id",
            "subject",
            "projects",
        }:
            raise AuthenticationConfigurationError(
                "each Entra binding requires object_id, subject, and projects"
            )
        object_id = _required_string(item["object_id"], "object_id").lower()
        if GUID_PATTERN.fullmatch(object_id) is None:
            raise AuthenticationConfigurationError("object_id must be a GUID")
        return _EntraBinding(
            object_id=object_id,
            subject=_required_string(item["subject"], "subject"),
            projects=_string_set(item["projects"], "projects"),
        )


def authorizer_from_environment() -> Authorizer:
    entra_values = [
        os.environ.get(ENTRA_TENANT_ID_ENV, "").strip(),
        os.environ.get(ENTRA_AUDIENCE_ENV, "").strip(),
        os.environ.get(ENTRA_BINDINGS_FILE_ENV, "").strip(),
    ]
    if any(entra_values):
        return EntraAuthorizer.from_environment()
    return TokenAuthorizer.from_environment()


def _read_json_object(path: Path, label: str) -> Mapping[str, object]:
    if not path.exists() or not path.is_file():
        raise AuthenticationConfigurationError(
            f"the {label} path must be an existing file"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthenticationConfigurationError(
            f"the {label} file is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, Mapping):
        raise AuthenticationConfigurationError(f"the {label} file must be an object")
    return payload


def _claim_string(claims: Mapping[str, object], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationConfigurationError(f"Entra claim {name} is required")
    return value.strip()


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
