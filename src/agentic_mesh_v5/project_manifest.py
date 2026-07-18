from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.types.json import Jsonb
import yaml

from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.package_resolver import PackageReference, PackageResolutionError


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_CREDENTIAL_REF = re.compile(
    r"^(?:secret|mount|oauth-cache)://[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$"
)
_GRANT_REF = re.compile(r"^grant://[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SOURCE_PATH = "agentic-mesh/project.yaml"
_UNSET = object()


class ProjectManifestError(ValueError):
    pass


class ProjectManifestAuthorizationError(ProjectManifestError):
    pass


class ProjectManifestConflict(ProjectManifestError):
    pass


class ProjectManifestStoreError(DatabaseError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceBinding:
    kind: str
    key: str
    owner_project_id: str
    authorization_ref: str | None


@dataclass(frozen=True, slots=True)
class ResourceGrant:
    project_id: str
    owner_project_id: str
    resource_kind: str
    resource_key: str
    authorization_ref: str


class ResourceAuthorizer(Protocol):
    def allows(
        self,
        *,
        project_id: str,
        owner_project_id: str,
        resource_kind: str,
        resource_key: str,
        authorization_ref: str | None,
    ) -> bool: ...


class DenyForeignResources:
    def allows(self, **_: object) -> bool:
        return False


class StaticResourceAuthorizer:
    def __init__(self, grants: Sequence[ResourceGrant]) -> None:
        self._grants = frozenset(grants)

    def allows(
        self,
        *,
        project_id: str,
        owner_project_id: str,
        resource_kind: str,
        resource_key: str,
        authorization_ref: str | None,
    ) -> bool:
        if authorization_ref is None:
            return False
        return ResourceGrant(
            project_id,
            owner_project_id,
            resource_kind,
            resource_key,
            authorization_ref,
        ) in self._grants


@dataclass(frozen=True, slots=True)
class ProjectManifest:
    project_id: str
    display_name: str
    digest: str
    snapshot: Mapping[str, object]
    resources: tuple[ResourceBinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "digest": self.digest,
            "snapshot": self.snapshot,
            "resources": [asdict(item) for item in self.resources],
        }


@dataclass(frozen=True, slots=True)
class ProjectManifestActivation:
    project_id: str
    manifest_digest: str
    source_revision: str
    source_path: str
    activated_by: str
    activated_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def load_project_manifest(path: Path) -> ProjectManifest:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProjectManifestError("project manifest must be valid UTF-8 YAML") from exc
    return _normalize_project_manifest(value)


def _normalize_project_manifest(value: object) -> ProjectManifest:
    root = _mapping(
        value,
        "manifest",
        {
            "schema_version",
            "project_id",
            "display_name",
            "packages",
            "repositories",
            "documents",
            "teams",
            "ado",
            "credentials",
            "roles",
            "limits",
        },
    )
    if root["schema_version"] != 1:
        raise ProjectManifestError("schema_version must be 1")
    project_id = _identifier(root["project_id"], "project_id")
    display_name = _text(root["display_name"], "display_name", 200)
    credentials, credential_resources = _credentials(
        root["credentials"], project_id
    )
    credential_ids = frozenset(credentials)
    repositories, repository_resources = _repositories(
        root["repositories"], project_id, credential_ids
    )
    documents, document_resources = _documents(
        root["documents"], project_id, credential_ids
    )
    teams, teams_resources = _teams(root["teams"], project_id, credential_ids)
    ado, ado_resources = _ado(root["ado"], project_id, credential_ids)
    packages = _packages(root["packages"])
    roles, minimum, maximum = _roles(root["roles"])
    limits = _limits(root["limits"], minimum, maximum)
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "project_id": project_id,
        "display_name": display_name,
        "packages": packages,
        "repositories": repositories,
        "documents": documents,
        "teams": teams,
        "ado": ado,
        "credentials": credentials,
        "roles": roles,
        "limits": limits,
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    resources = tuple(
        sorted(
            (
                *credential_resources,
                *repository_resources,
                *document_resources,
                *teams_resources,
                *ado_resources,
            ),
            key=lambda item: (item.kind, item.key),
        )
    )
    identities = [(item.kind, item.key) for item in resources]
    if len(identities) != len(set(identities)):
        raise ProjectManifestError("project manifest contains duplicate resources")
    return ProjectManifest(
        project_id=project_id,
        display_name=display_name,
        digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        snapshot=snapshot,
        resources=resources,
    )


def _credentials(
    value: object, project_id: str
) -> tuple[dict[str, object], tuple[ResourceBinding, ...]]:
    entries = _named_mapping(value, "credentials", allow_empty=False)
    normalized: dict[str, object] = {}
    resources: list[ResourceBinding] = []
    for credential_id, item in sorted(entries.items()):
        record = _mapping(
            item,
            f"credentials.{credential_id}",
            {"scope", "provider", "reference"},
            {"authorization_ref"},
        )
        scope = record["scope"]
        if scope not in {"project", "system"}:
            raise ProjectManifestError(
                f"credentials.{credential_id}.scope must be project or system"
            )
        provider = _identifier(record["provider"], "credential provider")
        reference = _reference(record["reference"], "credential reference")
        authorization_ref = _grant_reference(record.get("authorization_ref"))
        if scope == "system" and authorization_ref is None:
            raise ProjectManifestAuthorizationError(
                f"system credential {credential_id} requires authorization_ref"
            )
        if scope == "project" and authorization_ref is not None:
            raise ProjectManifestError(
                f"project credential {credential_id} cannot use authorization_ref"
            )
        normalized[credential_id] = {
            "scope": scope,
            "provider": provider,
            "reference": reference,
            **(
                {"authorization_ref": authorization_ref}
                if authorization_ref is not None
                else {}
            ),
        }
        owner = project_id if scope == "project" else "system"
        namespace = project_id if scope == "project" else "system"
        resources.append(
            ResourceBinding(
                "credential",
                f"{namespace}|{provider}|{reference}",
                owner,
                authorization_ref,
            )
        )
    return normalized, tuple(resources)


def _repositories(
    value: object, project_id: str, credential_ids: frozenset[str]
) -> tuple[dict[str, object], tuple[ResourceBinding, ...]]:
    entries = _named_mapping(value, "repositories", allow_empty=False)
    normalized: dict[str, object] = {}
    resources: list[ResourceBinding] = []
    for repository_id, item in sorted(entries.items()):
        record = _mapping(
            item,
            f"repositories.{repository_id}",
            {"url", "default_branch"},
            {"credential", "owner_project_id", "authorization_ref"},
        )
        url = _git_url(record["url"])
        owner, authorization = _ownership(record, project_id)
        credential = _credential_link(record.get("credential"), credential_ids)
        normalized[repository_id] = {
            "url": url,
            "default_branch": _text(
                record["default_branch"], "default_branch", 200
            ),
            **({"credential": credential} if credential else {}),
            "owner_project_id": owner,
            **({"authorization_ref": authorization} if authorization else {}),
        }
        resources.append(ResourceBinding("repository", url, owner, authorization))
    return normalized, tuple(resources)


def _documents(
    value: object, project_id: str, credential_ids: frozenset[str]
) -> tuple[dict[str, object], tuple[ResourceBinding, ...]]:
    entries = _named_mapping(value, "documents", allow_empty=True)
    normalized: dict[str, object] = {}
    resources: list[ResourceBinding] = []
    for document_id, item in sorted(entries.items()):
        record = _mapping(
            item,
            f"documents.{document_id}",
            {"adapter", "drive_id", "root", "credential"},
            {"owner_project_id", "authorization_ref"},
        )
        adapter = _identifier(record["adapter"], "document adapter")
        drive_id = _external_id(record["drive_id"], "drive_id")
        root = _document_root(record["root"])
        owner, authorization = _ownership(record, project_id)
        credential = _credential_link(record["credential"], credential_ids)
        normalized[document_id] = {
            "adapter": adapter,
            "drive_id": drive_id,
            "root": root,
            "credential": credential,
            "owner_project_id": owner,
            **({"authorization_ref": authorization} if authorization else {}),
        }
        resources.append(
            ResourceBinding(
                "document-root",
                f"{adapter}|{drive_id}|{root}",
                owner,
                authorization,
            )
        )
    return normalized, tuple(resources)


def _teams(
    value: object, project_id: str, credential_ids: frozenset[str]
) -> tuple[dict[str, object], tuple[ResourceBinding, ...]]:
    record = _mapping(
        value,
        "teams",
        {"tenant_id", "team_id", "credential", "channels"},
        {"owner_project_id", "authorization_ref"},
    )
    tenant_id = _external_id(record["tenant_id"], "tenant_id")
    team_id = _external_id(record["team_id"], "team_id")
    credential = _credential_link(record["credential"], credential_ids)
    owner, authorization = _ownership(record, project_id)
    channels = _named_mapping(record["channels"], "teams.channels", allow_empty=False)
    normalized_channels: dict[str, str] = {}
    resources: list[ResourceBinding] = []
    for channel_name, channel_id in sorted(channels.items()):
        normalized_channel = _external_id(channel_id, "channel_id")
        normalized_channels[channel_name] = normalized_channel
        resources.append(
            ResourceBinding(
                "teams-channel",
                f"{tenant_id}|{team_id}|{normalized_channel}",
                owner,
                authorization,
            )
        )
    return (
        {
            "tenant_id": tenant_id,
            "team_id": team_id,
            "credential": credential,
            "channels": normalized_channels,
            "owner_project_id": owner,
            **({"authorization_ref": authorization} if authorization else {}),
        },
        tuple(resources),
    )


def _ado(
    value: object, project_id: str, credential_ids: frozenset[str]
) -> tuple[dict[str, object], tuple[ResourceBinding, ...]]:
    record = _mapping(
        value,
        "ado",
        {"organization", "project", "credential"},
        {"owner_project_id", "authorization_ref"},
    )
    organization = _ado_url(record["organization"])
    ado_project = _text(record["project"], "ado.project", 200)
    credential = _credential_link(record["credential"], credential_ids)
    owner, authorization = _ownership(record, project_id)
    key = f"{organization.casefold()}|{ado_project.casefold()}"
    return (
        {
            "organization": organization,
            "project": ado_project,
            "credential": credential,
            "owner_project_id": owner,
            **({"authorization_ref": authorization} if authorization else {}),
        },
        (ResourceBinding("ado-project", key, owner, authorization),),
    )


def _packages(value: object) -> dict[str, object]:
    record = _mapping(
        value, "packages", {"organization_release_digest", "overrides"}
    )
    digest = record["organization_release_digest"]
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ProjectManifestError("organization_release_digest is invalid")
    overrides = record["overrides"]
    if not isinstance(overrides, list) or not all(
        isinstance(item, str) for item in overrides
    ):
        raise ProjectManifestError("packages.overrides must be an array")
    parsed = [_package_reference(item, "project-override") for item in overrides]
    if len(parsed) != len(set(parsed)):
        raise ProjectManifestError("packages.overrides contains duplicates")
    return {"organization_release_digest": digest, "overrides": sorted(parsed)}


def _roles(value: object) -> tuple[dict[str, object], int, int]:
    entries = _named_mapping(value, "roles", allow_empty=False)
    normalized: dict[str, object] = {}
    minimum_total = 0
    maximum_total = 0
    for role_id, item in sorted(entries.items()):
        record = _mapping(
            item, f"roles.{role_id}", {"package", "tool_profile", "instances"}
        )
        instances = _mapping(
            record["instances"], f"roles.{role_id}.instances", {"minimum", "maximum"}
        )
        minimum = _integer(instances["minimum"], "instances.minimum", 0, 256)
        maximum = _integer(instances["maximum"], "instances.maximum", 1, 256)
        if minimum > maximum:
            raise ProjectManifestError(
                f"roles.{role_id} minimum instances exceed maximum"
            )
        minimum_total += minimum
        maximum_total += maximum
        normalized[role_id] = {
            "package": _package_reference(record["package"], "role"),
            "tool_profile": _package_reference(
                record["tool_profile"], "tool-profile"
            ),
            "instances": {"minimum": minimum, "maximum": maximum},
        }
    return normalized, minimum_total, maximum_total


def _limits(value: object, minimum: int, maximum: int) -> dict[str, int]:
    record = _mapping(value, "limits", {"max_total_instances"})
    total = _integer(
        record["max_total_instances"], "max_total_instances", 1, 256
    )
    if total < minimum or total > maximum:
        raise ProjectManifestError(
            "max_total_instances must be between role minimum and maximum totals"
        )
    return {"max_total_instances": total}


def _ownership(
    record: Mapping[str, object], project_id: str
) -> tuple[str, str | None]:
    owner = _identifier(record.get("owner_project_id", project_id), "owner_project_id")
    authorization = _grant_reference(record.get("authorization_ref"))
    if owner != project_id and authorization is None:
        raise ProjectManifestAuthorizationError(
            f"resource owned by {owner} requires authorization_ref"
        )
    return owner, authorization


def _package_reference(value: object, expected_kind: str) -> str:
    if not isinstance(value, str):
        raise ProjectManifestError("package reference must be a string")
    try:
        parsed = PackageReference.parse(value)
    except PackageResolutionError as exc:
        raise ProjectManifestError(str(exc)) from exc
    if parsed.kind != expected_kind:
        raise ProjectManifestError(
            f"package reference {value} must have kind {expected_kind}"
        )
    return str(parsed)


def _git_url(value: object) -> str:
    text = _text(value, "repository url", 1000)
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(".git")
    ):
        raise ProjectManifestError(
            "repository url must be a credential-free HTTPS Git URL"
        )
    host = parsed.hostname.lower()
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit(("https", netloc, parsed.path, "", ""))


def _ado_url(value: object) -> str:
    text = _text(value, "ado.organization", 500).rstrip("/")
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "dev.azure.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len([item for item in parsed.path.split("/") if item]) != 1
    ):
        raise ProjectManifestError("ado.organization must identify one Azure DevOps org")
    return f"https://dev.azure.com/{parsed.path.strip('/')}"


def _document_root(value: object) -> str:
    text = _text(value, "document root", 500).replace("\\", "/")
    path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts:
        raise ProjectManifestError("document root must be an absolute contained path")
    return path.as_posix()


def _credential_link(value: object, credential_ids: frozenset[str]) -> str | None:
    if value is None:
        return None
    credential_id = _identifier(value, "credential")
    if credential_id not in credential_ids:
        raise ProjectManifestError(f"unknown credential reference: {credential_id}")
    return credential_id


def _reference(value: object, label: str) -> str:
    if not isinstance(value, str) or _CREDENTIAL_REF.fullmatch(value) is None:
        raise ProjectManifestError(f"{label} must be an external reference")
    return value


def _grant_reference(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _GRANT_REF.fullmatch(value) is None:
        raise ProjectManifestError("authorization_ref must be an external grant reference")
    return value


def _named_mapping(
    value: object, label: str, *, allow_empty: bool
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or (not allow_empty and not value):
        raise ProjectManifestError(f"{label} must be a mapping")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        name = _identifier(key, f"{label} id")
        if name in normalized:
            raise ProjectManifestError(f"{label} contains duplicate ids")
        normalized[name] = item
    return normalized


def _mapping(
    value: object,
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> Mapping[str, object]:
    allowed = required | (optional or set())
    if not isinstance(value, Mapping) or set(value) != required | (
        set(value).intersection(optional or set())
    ):
        missing = sorted(required - set(value) if isinstance(value, Mapping) else required)
        unexpected = sorted(set(value) - allowed if isinstance(value, Mapping) else set())
        raise ProjectManifestError(
            f"{label} fields are invalid: missing={missing}, unexpected={unexpected}"
        )
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ProjectManifestError(f"{label} is invalid")
    return value


def _external_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _EXTERNAL_ID.fullmatch(value) is None:
        raise ProjectManifestError(f"{label} is invalid")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "${" in value
        or "\x00" in value
    ):
        raise ProjectManifestError(f"{label} is invalid")
    return value.strip()


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ProjectManifestError(f"{label} must be between {minimum} and {maximum}")
    return value


class ProjectManifestStore:
    def __init__(
        self,
        database_url: str,
        *,
        authorizer: ResourceAuthorizer | None = None,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._authorizer = authorizer or DenyForeignResources()

    def activate(
        self,
        manifest: ProjectManifest,
        *,
        source_revision: str,
        actor_id: str,
        source_path: str = _SOURCE_PATH,
        expected_active: str | None | object = _UNSET,
    ) -> ProjectManifestActivation:
        validated = _normalize_project_manifest(manifest.snapshot)
        if validated != manifest:
            raise ProjectManifestError("manifest does not match its normalized snapshot")
        source_revision = _source_revision(source_revision)
        actor_id = _external_id(actor_id, "actor_id")
        if source_path != _SOURCE_PATH:
            raise ProjectManifestError(
                f"source_path must be the canonical {_SOURCE_PATH}"
            )
        if expected_active is not _UNSET and expected_active is not None:
            if not isinstance(expected_active, str) or _DIGEST.fullmatch(
                expected_active
            ) is None:
                raise ProjectManifestError("expected_active is invalid")
        self._authorize_declared_owners(manifest)
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                with connection.transaction():
                    project = connection.execute(
                        f"SELECT display_name FROM {SCHEMA}.projects "
                        "WHERE project_id = %s FOR UPDATE",
                        (manifest.project_id,),
                    ).fetchone()
                    if project is None:
                        raise ProjectManifestConflict("project is not registered")
                    if project[0] != manifest.display_name:
                        raise ProjectManifestConflict(
                            "manifest display_name does not match the project"
                        )
                    current_row = connection.execute(
                        f"SELECT manifest_digest FROM {SCHEMA}.project_manifest_active "
                        "WHERE project_id = %s FOR UPDATE",
                        (manifest.project_id,),
                    ).fetchone()
                    current = None if current_row is None else current_row[0]
                    if expected_active is not _UNSET and expected_active != current:
                        raise ProjectManifestConflict(
                            f"active manifest changed: expected {expected_active}, "
                            f"found {current}"
                        )
                    for resource in manifest.resources:
                        connection.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (json.dumps([resource.kind, resource.key]),),
                        )
                    self._authorize_active_collisions(connection, manifest)
                    existing = connection.execute(
                        f"""
                        SELECT source_revision, source_path, snapshot
                        FROM {SCHEMA}.project_manifest_snapshots
                        WHERE project_id = %s AND manifest_digest = %s
                        """,
                        (manifest.project_id, manifest.digest),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            f"""
                            INSERT INTO {SCHEMA}.project_manifest_snapshots
                                (project_id, manifest_digest, source_revision,
                                 source_path, snapshot, registered_by)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (
                                manifest.project_id,
                                manifest.digest,
                                source_revision,
                                source_path,
                                Jsonb(dict(manifest.snapshot)),
                                actor_id,
                            ),
                        )
                        for resource in manifest.resources:
                            connection.execute(
                                f"""
                                INSERT INTO {SCHEMA}.project_manifest_resource_claims
                                    (project_id, manifest_digest, resource_kind,
                                     resource_key, owner_project_id,
                                     authorization_ref)
                                VALUES (%s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    manifest.project_id,
                                    manifest.digest,
                                    resource.kind,
                                    resource.key,
                                    resource.owner_project_id,
                                    resource.authorization_ref,
                                ),
                            )
                    elif (
                        existing[0] != source_revision
                        or existing[1] != source_path
                        or existing[2] != manifest.snapshot
                    ):
                        raise ProjectManifestConflict(
                            "immutable manifest snapshot conflicts with existing digest"
                        )
                    if current != manifest.digest:
                        connection.execute(
                            f"""
                            INSERT INTO {SCHEMA}.project_manifest_active
                                (project_id, manifest_digest, activated_by)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (project_id) DO UPDATE
                            SET manifest_digest = EXCLUDED.manifest_digest,
                                activated_by = EXCLUDED.activated_by,
                                activated_at = clock_timestamp()
                            """,
                            (manifest.project_id, manifest.digest, actor_id),
                        )
                        connection.execute(
                            f"""
                            INSERT INTO {SCHEMA}.audit_records
                                (scope, project_id, actor_id, action, object_type,
                                 object_id, details)
                            VALUES ('project', %s, %s, 'project.manifest_activated',
                                    'project-manifest', %s, %s)
                            """,
                            (
                                manifest.project_id,
                                actor_id,
                                manifest.digest,
                                Jsonb(
                                    {
                                        "source_revision": source_revision,
                                        "source_path": source_path,
                                        "resource_count": len(manifest.resources),
                                    }
                                ),
                            ),
                        )
                    return self._read_active(connection, manifest.project_id)
        except (ProjectManifestError, DatabaseError):
            raise
        except Exception as exc:
            raise ProjectManifestStoreError("project manifest operation failed") from exc

    def get_active(self, project_id: str) -> ProjectManifestActivation | None:
        project_id = _identifier(project_id, "project_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                row = connection.execute(
                    f"SELECT 1 FROM {SCHEMA}.project_manifest_active "
                    "WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
                return None if row is None else self._read_active(connection, project_id)
        except ProjectManifestError:
            raise
        except Exception as exc:
            raise ProjectManifestStoreError("project manifest operation failed") from exc

    def list_history(self, project_id: str) -> tuple[dict[str, object], ...]:
        project_id = _identifier(project_id, "project_id")
        try:
            with psycopg.connect(self._database_url, autocommit=True) as connection:
                rows = connection.execute(
                    f"""
                    SELECT manifest_digest, source_revision, source_path, snapshot,
                           registered_by, registered_at::text
                    FROM {SCHEMA}.project_manifest_snapshots
                    WHERE project_id = %s
                    ORDER BY registered_at, manifest_digest
                    """,
                    (project_id,),
                ).fetchall()
        except Exception as exc:
            raise ProjectManifestStoreError("project manifest operation failed") from exc
        names = (
            "manifest_digest",
            "source_revision",
            "source_path",
            "snapshot",
            "registered_by",
            "registered_at",
        )
        return tuple(dict(zip(names, row, strict=True)) for row in rows)

    def _authorize_declared_owners(self, manifest: ProjectManifest) -> None:
        for resource in manifest.resources:
            if resource.owner_project_id != manifest.project_id and not self._allows(
                manifest.project_id, resource.owner_project_id, resource
            ):
                raise ProjectManifestAuthorizationError(
                    f"foreign {resource.kind} resource is not authorized"
                )

    def _authorize_active_collisions(self, connection, manifest: ProjectManifest) -> None:
        rows = connection.execute(
            f"""
            SELECT claim.project_id, claim.owner_project_id,
                   claim.resource_kind, claim.resource_key
            FROM {SCHEMA}.project_manifest_resource_claims AS claim
            JOIN {SCHEMA}.project_manifest_active AS active
              ON active.project_id = claim.project_id
             AND active.manifest_digest = claim.manifest_digest
            WHERE claim.project_id <> %s
            """,
            (manifest.project_id,),
        ).fetchall()
        resources = {(item.kind, item.key): item for item in manifest.resources}
        collisions = [
            (claimant, owner, resources[(kind, key)])
            for claimant, owner, kind, key in rows
            if (kind, key) in resources
        ]
        for claimant, owner, resource in collisions:
            if resource.owner_project_id != owner:
                raise ProjectManifestAuthorizationError(
                    f"active {resource.kind} resource belongs to project {owner}, "
                    f"not {resource.owner_project_id}"
                )
            if owner != manifest.project_id and not self._allows(
                manifest.project_id, owner, resource
            ):
                raise ProjectManifestAuthorizationError(
                    f"active {resource.kind} resource belongs to project {owner} "
                    f"and is claimed by {claimant}"
                )

    def _allows(
        self, project_id: str, owner_project_id: str, resource: ResourceBinding
    ) -> bool:
        return self._authorizer.allows(
            project_id=project_id,
            owner_project_id=owner_project_id,
            resource_kind=resource.kind,
            resource_key=resource.key,
            authorization_ref=resource.authorization_ref,
        )

    @staticmethod
    def _read_active(connection, project_id: str) -> ProjectManifestActivation:
        row = connection.execute(
            f"""
            SELECT active.project_id, active.manifest_digest,
                   snapshot.source_revision, snapshot.source_path,
                   active.activated_by, active.activated_at::text
            FROM {SCHEMA}.project_manifest_active AS active
            JOIN {SCHEMA}.project_manifest_snapshots AS snapshot
              ON snapshot.project_id = active.project_id
             AND snapshot.manifest_digest = active.manifest_digest
            WHERE active.project_id = %s
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            raise ProjectManifestStoreError("active project manifest was not found")
        return ProjectManifestActivation(*row)


def _source_revision(value: object) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ProjectManifestError("source_revision must be an exact Git commit")
    return value.lower()
