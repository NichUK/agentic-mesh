from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Mapping, Protocol

import psycopg
from psycopg.rows import dict_row

from agentic_mesh_v5.database import DatabaseConfigurationError, SCHEMA
from agentic_mesh_v5.teams_connector import ProjectTeamsConnector
from agentic_mesh_v5.teams_connector import TeamsConnectorError
from agentic_mesh_v5.teams_connector import TeamsRoleIdentityBinding


_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REJECTION_CODES = frozenset(
    {
        "invalid-activity",
        "unknown-route",
        "ambiguous-channel",
        "invalid-selection",
        "authority-changed",
        "routing-unavailable",
    }
)


class TeamsProjectAuthorizer(Protocol):
    def allows(self, *, sender_id: str, project_id: str) -> bool: ...


class TeamsRouteRejected(TeamsConnectorError):
    def __init__(self, code: str) -> None:
        if code not in _REJECTION_CODES:
            raise ValueError("Teams route rejection code is invalid")
        self.code = code
        super().__init__(f"Teams project route rejected: {code}")


@dataclass(frozen=True, slots=True)
class TeamsRouteCandidate:
    project_id: str
    display_name: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TeamsRouteDecision:
    status: str
    source: str
    project_id: str | None
    project_display_name: str | None
    role_id: str | None
    manifest_digest: str | None
    candidates: tuple[TeamsRouteCandidate, ...] = ()
    clarification_question: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _ActiveRoute:
    project_id: str
    display_name: str
    manifest_digest: str
    tenant_id: str
    team_id: str
    channels: Mapping[str, str]
    identities: Mapping[str, str]


class TeamsProjectRouter:
    def __init__(
        self,
        database_url: str,
        *,
        connector: ProjectTeamsConnector,
        authorizer: TeamsProjectAuthorizer,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("the V5 database URL must use Postgres")
        self._database_url = database_url
        self._connector = connector
        self._authorizer = authorizer

    def route_channel(
        self,
        *,
        sender_id: str,
        tenant_id: str,
        team_id: str,
        channel_id: str,
        recipient_application_id: str,
    ) -> TeamsRouteDecision:
        try:
            sender_id = _external_id(sender_id)
            tenant_id = _external_id(tenant_id)
            team_id = _external_id(team_id)
            channel_id = _external_id(channel_id)
            recipient = _recipient(recipient_application_id)
        except ValueError:
            raise TeamsRouteRejected("invalid-activity") from None
        routes = tuple(
            route
            for route in self._active_routes()
            if route.tenant_id == tenant_id
            and route.team_id == team_id
            and channel_id in route.channels.values()
        )
        if not routes:
            raise TeamsRouteRejected("unknown-route")
        if len(routes) != 1:
            raise TeamsRouteRejected("ambiguous-channel")
        route = routes[0]
        if not self._allowed(sender_id, route.project_id):
            raise TeamsRouteRejected("unknown-route")
        role_id = _matching_role(route, recipient)
        if role_id is None:
            raise TeamsRouteRejected("unknown-route")
        binding = self._verified_binding(
            route,
            role_id,
            recipient,
            channel_id=channel_id,
        )
        return _routed("channel", route, binding)

    def route_personal(
        self,
        *,
        sender_id: str,
        tenant_id: str,
        recipient_application_id: str,
        selected_project_id: str | None = None,
    ) -> TeamsRouteDecision:
        try:
            sender_id = _external_id(sender_id)
            tenant_id = _external_id(tenant_id)
            recipient = _recipient(recipient_application_id)
        except ValueError:
            raise TeamsRouteRejected("invalid-activity") from None
        try:
            selected = (
                _id(selected_project_id) if selected_project_id is not None else None
            )
        except ValueError:
            raise TeamsRouteRejected("invalid-selection") from None
        matches: list[tuple[_ActiveRoute, str]] = []
        for route in self._active_routes():
            if route.tenant_id != tenant_id or not self._allowed(
                sender_id, route.project_id
            ):
                continue
            role_id = _matching_role(route, recipient)
            if role_id is not None:
                matches.append((route, role_id))
        if not matches:
            raise TeamsRouteRejected("unknown-route")
        if len({role_id for _, role_id in matches}) != 1:
            raise TeamsRouteRejected("routing-unavailable")
        if selected is not None:
            selected_matches = [
                item for item in matches if item[0].project_id == selected
            ]
            if len(selected_matches) != 1:
                raise TeamsRouteRejected("invalid-selection")
            route, role_id = selected_matches[0]
        elif len(matches) == 1:
            route, role_id = matches[0]
        else:
            candidates = tuple(
                TeamsRouteCandidate(route.project_id, route.display_name)
                for route, _ in matches
            )
            choices = "; ".join(
                f"{item.display_name} ({item.project_id})" for item in candidates
            )
            return TeamsRouteDecision(
                "clarification_required",
                "personal",
                None,
                None,
                None,
                None,
                candidates,
                f"Which project is this about? Choose one: {choices}.",
            )
        binding = self._verified_binding(route, role_id, recipient)
        return _routed("personal", route, binding)

    def _allowed(self, sender_id: str, project_id: str) -> bool:
        try:
            return self._authorizer.allows(
                sender_id=sender_id,
                project_id=project_id,
            ) is True
        except Exception:
            return False

    def _verified_binding(
        self,
        route: _ActiveRoute,
        role_id: str,
        recipient: str,
        *,
        channel_id: str | None = None,
    ) -> TeamsRoleIdentityBinding:
        try:
            binding = self._connector.binding(
                project_id=route.project_id,
                role_id=role_id,
            )
        except TeamsConnectorError:
            raise TeamsRouteRejected("authority-changed") from None
        if (
            binding.manifest_digest != route.manifest_digest
            or binding.tenant_id != route.tenant_id
            or binding.team_id != route.team_id
            or binding.application_id.casefold() != recipient.casefold()
            or (
                channel_id is not None
                and channel_id not in binding.channels.values()
            )
        ):
            raise TeamsRouteRejected("authority-changed")
        return binding

    def _active_routes(self) -> tuple[_ActiveRoute, ...]:
        try:
            with psycopg.connect(
                self._database_url, autocommit=True, row_factory=dict_row
            ) as connection:
                rows = connection.execute(
                    f"""
                    SELECT active.project_id, project.display_name,
                           snapshot.manifest_digest, snapshot.snapshot
                    FROM {SCHEMA}.project_manifest_active AS active
                    JOIN {SCHEMA}.projects AS project
                      ON project.project_id = active.project_id
                    JOIN {SCHEMA}.project_manifest_snapshots AS snapshot
                      ON snapshot.project_id = active.project_id
                     AND snapshot.manifest_digest = active.manifest_digest
                    ORDER BY active.project_id
                    """
                ).fetchall()
            return tuple(_active_route(row) for row in rows)
        except TeamsRouteRejected:
            raise
        except Exception:
            raise TeamsRouteRejected("routing-unavailable") from None


def _active_route(row: Mapping[str, object]) -> _ActiveRoute:
    try:
        snapshot = _mapping(row["snapshot"])
        teams = _mapping(snapshot["teams"])
        channels = {
            _id(name): _external_id(value)
            for name, value in _mapping(teams["channels"]).items()
        }
        identities = {
            _id(role_id): _external_id(_mapping(value)["application_id"])
            for role_id, value in _mapping(teams["role_identities"]).items()
        }
        if not channels or not identities:
            raise ValueError("Teams route configuration is empty")
        return _ActiveRoute(
            _id(row["project_id"]),
            _display_name(row["display_name"]),
            _digest(row["manifest_digest"]),
            _external_id(teams["tenant_id"]),
            _external_id(teams["team_id"]),
            channels,
            identities,
        )
    except (KeyError, TypeError, ValueError):
        raise TeamsRouteRejected("routing-unavailable") from None


def _matching_role(route: _ActiveRoute, recipient: str) -> str | None:
    matches = [
        role_id
        for role_id, application_id in route.identities.items()
        if application_id.casefold() == recipient.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def _routed(
    source: str,
    route: _ActiveRoute,
    binding: TeamsRoleIdentityBinding,
) -> TeamsRouteDecision:
    return TeamsRouteDecision(
        "routed",
        source,
        route.project_id,
        route.display_name,
        binding.role_id,
        binding.manifest_digest,
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("mapping is invalid")
    return value


def _id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("identifier is invalid")
    return value


def _external_id(value: object) -> str:
    if not isinstance(value, str) or _EXTERNAL_ID.fullmatch(value) is None:
        raise ValueError("external identifier is invalid")
    return value


def _recipient(value: object) -> str:
    recipient = _external_id(value)
    if recipient.startswith("28:"):
        recipient = _external_id(recipient[3:])
    return recipient


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("manifest digest is invalid")
    return value


def _display_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError("project display name is invalid")
    return value.strip()
