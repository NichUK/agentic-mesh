from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Mapping, Protocol

import psycopg
from psycopg.rows import dict_row

from agentic_mesh_v5.database import DatabaseConfigurationError, DatabaseError, SCHEMA
from agentic_mesh_v5.document_store import AccessTokenProvider


_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_CREDENTIAL_REF = re.compile(
    r"^(?:secret|mount|oauth-cache)://[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$"
)
_BLOCKER_CODES = frozenset(
    {
        "credential-unavailable",
        "missing-installation",
        "permission-revoked",
        "connector-unavailable",
    }
)


class TeamsConnectorError(DatabaseError):
    pass


class TeamsConnectorConfigurationError(TeamsConnectorError):
    pass


class TeamsInboundRejected(TeamsConnectorError):
    pass


class TeamsConnectorBlocked(TeamsConnectorError):
    def __init__(self, code: str, *, project_id: str, role_id: str) -> None:
        if code not in _BLOCKER_CODES:
            raise ValueError("Teams blocker code is invalid")
        self.code = code
        self.project_id = project_id
        self.role_id = role_id
        super().__init__(f"Teams role delivery is blocked: {code}")


@dataclass(frozen=True, slots=True)
class TeamsRoleIdentityBinding:
    project_id: str
    role_id: str
    manifest_digest: str
    tenant_id: str
    team_id: str
    application_id: str
    display_name: str
    credential_provider: str
    credential_reference: str
    channels: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TeamsInstallation:
    installed: bool
    can_send: bool


@dataclass(frozen=True, slots=True)
class TeamsRoleReadiness:
    project_id: str
    role_id: str
    application_id: str
    display_name: str
    status: str
    blocker_code: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TeamsDelivery:
    project_id: str
    role_id: str
    display_name: str
    channel_id: str
    external_delivery_id: str
    connector: str = "teams"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class TeamsTransport(Protocol):
    def check_installation(
        self,
        *,
        tenant_id: str,
        team_id: str,
        application_id: str,
        access_token: str,
    ) -> TeamsInstallation: ...

    def send_channel_message(
        self,
        *,
        tenant_id: str,
        team_id: str,
        channel_id: str,
        application_id: str,
        display_name: str,
        text: str,
        access_token: str,
    ) -> str: ...


class ProjectTeamsConnector:
    def __init__(
        self,
        database_url: str,
        *,
        token_provider: AccessTokenProvider,
        transport: TeamsTransport,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._tokens = token_provider
        self._transport = transport

    def binding(self, *, project_id: str, role_id: str) -> TeamsRoleIdentityBinding:
        project_id = _id(project_id, "project_id")
        role_id = _id(role_id, "role_id")
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                row = connection.execute(
                    f"""
                    SELECT snapshot.manifest_digest, snapshot.snapshot,
                           binding.collaboration_identity
                    FROM {SCHEMA}.project_manifest_active AS active
                    JOIN {SCHEMA}.project_manifest_snapshots AS snapshot
                      ON snapshot.project_id = active.project_id
                     AND snapshot.manifest_digest = active.manifest_digest
                    JOIN {SCHEMA}.role_bindings AS binding
                      ON binding.project_id = active.project_id
                     AND binding.manifest_digest = active.manifest_digest
                    WHERE active.project_id = %s AND binding.role_id = %s
                    """,
                    (project_id, role_id),
                ).fetchone()
            if row is None:
                raise TeamsConnectorConfigurationError(
                    "Teams role identity is not active"
                )
            return _binding(project_id, role_id, row)
        except TeamsConnectorError:
            raise
        except Exception as exc:
            raise TeamsConnectorError("Teams role identity read failed") from exc

    def identify_inbound(
        self,
        *,
        project_id: str,
        tenant_id: str,
        team_id: str,
        recipient_application_id: str,
    ) -> TeamsRoleIdentityBinding:
        project_id = _id(project_id, "project_id")
        tenant_id = _external_id(tenant_id, "tenant_id")
        team_id = _external_id(team_id, "team_id")
        recipient = _external_id(
            recipient_application_id, "recipient_application_id"
        )
        if recipient.startswith("28:"):
            recipient = _external_id(
                recipient[3:], "recipient_application_id"
            )
        snapshot = self._active_snapshot(project_id)
        teams = _mapping(snapshot.get("teams"), "Teams configuration")
        if teams.get("tenant_id") != tenant_id or teams.get("team_id") != team_id:
            raise TeamsInboundRejected("Teams inbound project authority rejected")
        identities = _mapping(
            teams.get("role_identities"), "Teams role identities"
        )
        matches = [
            role_id
            for role_id, value in identities.items()
            if isinstance(value, Mapping)
            and isinstance(value.get("application_id"), str)
            and value["application_id"].casefold() == recipient.casefold()
        ]
        if len(matches) != 1:
            raise TeamsInboundRejected("Teams inbound recipient is not configured")
        binding = self.binding(project_id=project_id, role_id=matches[0])
        if (
            binding.tenant_id != tenant_id
            or binding.team_id != team_id
            or binding.application_id.casefold() != recipient.casefold()
        ):
            raise TeamsInboundRejected(
                "Teams inbound authority changed during identity resolution"
            )
        return binding

    def readiness(self, *, project_id: str, role_id: str) -> TeamsRoleReadiness:
        binding = self.binding(project_id=project_id, role_id=role_id)
        try:
            self._ready(binding)
        except TeamsConnectorBlocked as exc:
            return TeamsRoleReadiness(
                binding.project_id,
                binding.role_id,
                binding.application_id,
                binding.display_name,
                "blocked",
                exc.code,
            )
        return TeamsRoleReadiness(
            binding.project_id,
            binding.role_id,
            binding.application_id,
            binding.display_name,
            "ready",
            None,
        )

    def send_channel(
        self,
        *,
        project_id: str,
        role_id: str,
        channel: str,
        text: str,
    ) -> TeamsDelivery:
        channel = _id(channel, "channel")
        text = _message(text)
        binding = self.binding(project_id=project_id, role_id=role_id)
        channel_id = binding.channels.get(channel)
        if channel_id is None:
            raise TeamsConnectorConfigurationError(
                "Teams project channel is not configured"
            )
        access_token = self._ready(binding)
        try:
            external_id = self._transport.send_channel_message(
                tenant_id=binding.tenant_id,
                team_id=binding.team_id,
                channel_id=channel_id,
                application_id=binding.application_id,
                display_name=binding.display_name,
                text=text,
                access_token=access_token,
            )
        except Exception:
            raise TeamsConnectorBlocked(
                "connector-unavailable",
                project_id=binding.project_id,
                role_id=binding.role_id,
            ) from None
        try:
            external_id = _external_id(external_id, "external_delivery_id")
        except ValueError:
            raise TeamsConnectorBlocked(
                "connector-unavailable",
                project_id=binding.project_id,
                role_id=binding.role_id,
            ) from None
        return TeamsDelivery(
            binding.project_id,
            binding.role_id,
            binding.display_name,
            channel_id,
            external_id,
        )

    def _ready(self, binding: TeamsRoleIdentityBinding) -> str:
        try:
            token = self._tokens.access_token(
                provider=binding.credential_provider,
                reference=binding.credential_reference,
            )
        except Exception:
            raise TeamsConnectorBlocked(
                "credential-unavailable",
                project_id=binding.project_id,
                role_id=binding.role_id,
            ) from None
        if not isinstance(token, str) or not token.strip():
            raise TeamsConnectorBlocked(
                "credential-unavailable",
                project_id=binding.project_id,
                role_id=binding.role_id,
            )
        try:
            installation = self._transport.check_installation(
                tenant_id=binding.tenant_id,
                team_id=binding.team_id,
                application_id=binding.application_id,
                access_token=token,
            )
        except Exception:
            raise TeamsConnectorBlocked(
                "connector-unavailable",
                project_id=binding.project_id,
                role_id=binding.role_id,
            ) from None
        if not isinstance(installation, TeamsInstallation):
            raise TeamsConnectorBlocked(
                "connector-unavailable",
                project_id=binding.project_id,
                role_id=binding.role_id,
            )
        if not installation.installed:
            raise TeamsConnectorBlocked(
                "missing-installation",
                project_id=binding.project_id,
                role_id=binding.role_id,
            )
        if not installation.can_send:
            raise TeamsConnectorBlocked(
                "permission-revoked",
                project_id=binding.project_id,
                role_id=binding.role_id,
            )
        return token.strip()

    def _active_snapshot(self, project_id: str) -> Mapping[str, object]:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                row = connection.execute(
                    f"""
                    SELECT snapshot.snapshot
                    FROM {SCHEMA}.project_manifest_active AS active
                    JOIN {SCHEMA}.project_manifest_snapshots AS snapshot
                      ON snapshot.project_id = active.project_id
                     AND snapshot.manifest_digest = active.manifest_digest
                    WHERE active.project_id = %s
                    """,
                    (project_id,),
                ).fetchone()
            if row is None or not isinstance(row["snapshot"], Mapping):
                raise TeamsConnectorConfigurationError(
                    "Teams project binding is not active"
                )
            return row["snapshot"]
        except TeamsConnectorError:
            raise
        except Exception as exc:
            raise TeamsConnectorError("Teams project binding read failed") from exc


def _binding(
    project_id: str, role_id: str, row: Mapping[str, object]
) -> TeamsRoleIdentityBinding:
    try:
        manifest_digest = row["manifest_digest"]
        snapshot = _mapping(row["snapshot"], "project manifest")
        teams = _mapping(snapshot["teams"], "Teams configuration")
        identities = _mapping(teams["role_identities"], "Teams role identities")
        identity = _mapping(identities[role_id], "Teams role identity")
        credentials = _mapping(snapshot["credentials"], "project credentials")
        credential = _mapping(
            credentials[identity["credential"]], "Teams role credential"
        )
        application_id = identity["application_id"]
        if row["collaboration_identity"] != application_id:
            raise TeamsConnectorConfigurationError(
                "Teams role identity disagrees with role activation"
            )
        return TeamsRoleIdentityBinding(
            project_id,
            role_id,
            _digest(manifest_digest),
            _external_id(teams["tenant_id"], "tenant_id"),
            _external_id(teams["team_id"], "team_id"),
            _external_id(application_id, "application_id"),
            _display_name(identity["display_name"]),
            _id(credential["provider"], "credential_provider"),
            _credential_reference(credential["reference"]),
            {
                _id(name, "channel"): _external_id(value, "channel_id")
                for name, value in _mapping(
                    teams["channels"], "Teams channels"
                ).items()
            },
        )
    except TeamsConnectorError:
        raise
    except (KeyError, TypeError, ValueError):
        raise TeamsConnectorConfigurationError(
            "active Teams role identity is invalid"
        ) from None


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TeamsConnectorConfigurationError(f"{field} is invalid")
    return value


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _external_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _EXTERNAL_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("manifest digest is invalid")
    return value


def _display_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 100
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError("display_name is invalid")
    return value.strip()


def _credential_reference(value: object) -> str:
    if (
        not isinstance(value, str) or _CREDENTIAL_REF.fullmatch(value) is None
    ):
        raise ValueError("credential reference is invalid")
    return value


def _message(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 20_000
        or "\x00" in value
    ):
        raise ValueError("Teams message is invalid")
    return value.strip()
