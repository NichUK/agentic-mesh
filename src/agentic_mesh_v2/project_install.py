from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class InstallOptions:
    apply: bool = False
    allow_create_team: bool = False
    allow_create_channel: bool = False
    allow_register_apps: bool = False
    allow_install_apps: bool = False
    allow_uninstall_stale: bool = False
    allow_secret_rotation: bool = False


@dataclass(frozen=True)
class InstallOperation:
    action: str
    target: str
    status: str
    detail: str
    required_permission: str | None = None
    external_id: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "action": self.action,
            "target": self.target,
            "status": self.status,
            "detail": self.detail,
            "required_permission": self.required_permission,
            "external_id": self.external_id,
        }


class GraphRequestError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AzureCliGraphClient:
    def __init__(self, *, az_path: str = "az") -> None:
        self.az_path = _resolve_az_path(az_path)
        self._token: str | None = None

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._access_token()
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{GRAPH_ROOT}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                if response.status == 204:
                    return {}
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")
            raise GraphRequestError(exc.code, payload) from exc

    def _access_token(self) -> str:
        if self._token is None:
            output = subprocess.check_output(
                [
                    self.az_path,
                    "account",
                    "get-access-token",
                    "--resource",
                    "https://graph.microsoft.com",
                    "-o",
                    "json",
                ],
                text=True,
            )
            self._token = str(json.loads(output)["accessToken"])
        return self._token


class ProjectInstaller:
    def __init__(
        self,
        *,
        graph_client: AzureCliGraphClient,
        project_config: dict[str, Any],
        organization_config: dict[str, Any] | None = None,
        options: InstallOptions,
    ) -> None:
        self.graph = graph_client
        self.project_config = project_config
        self.organization_config = organization_config or {}
        self.options = options
        self.operations: list[InstallOperation] = []

    def run(self) -> dict[str, Any]:
        project_id = _required_string(self.project_config, "project_id")
        connectors = _mapping(self.project_config.get("connectors"), "connectors")
        teams = _mapping(connectors.get("teams"), "connectors.teams")
        tenant_id = _required_string(teams, "tenant_id")
        team = _mapping(teams.get("team"), "connectors.teams.team")
        configured_team_name = _required_string(team, "name")
        configured_team_id = _optional_string(team.get("id"))
        channels = _mapping(teams.get("channels"), "connectors.teams.channels")
        role_bots = _mapping(teams.get("role_bots"), "connectors.teams.role_bots")
        gateway_bots = _gateway_bots(self.project_config)

        self._check_graph_identity(tenant_id=tenant_id)
        resolved_team_id = self._ensure_team(
            configured_team_id=configured_team_id,
            configured_team_name=configured_team_name,
        )
        self._ensure_channels(team_id=resolved_team_id, channels=channels)

        for role_id, bot in sorted(role_bots.items()):
            if not isinstance(bot, dict):
                raise ValueError(f"role bot `{role_id}` must be a mapping")
            self._ensure_agent_identity(
                role_id=role_id,
                display_name=_required_string(bot, "display_name"),
                bot_id_ref=_required_string(bot, "bot_id_ref"),
                secret_ref=_required_string(bot, "secret_ref"),
                team_id=resolved_team_id,
            )
        for gateway_id, bot in sorted(gateway_bots.items()):
            self._ensure_agent_identity(
                role_id=f"gateway:{gateway_id}",
                display_name=_required_string(bot, "display_name"),
                bot_id_ref=_required_string(bot, "bot_id_ref"),
                secret_ref=_required_string(bot, "secret_ref"),
                team_id=resolved_team_id,
            )

        self._detect_stale_v1_installs(team_id=resolved_team_id)
        self._check_personal_install_scope()

        return {
            "status": _summary_status(self.operations),
            "project_id": project_id,
            "tenant_id": tenant_id,
            "team_name": configured_team_name,
            "team_id": resolved_team_id,
            "apply": self.options.apply,
            "operation_counts": _count_statuses(self.operations),
            "operations": [operation.as_dict() for operation in self.operations],
        }

    def _check_graph_identity(self, *, tenant_id: str) -> None:
        try:
            me = self.graph.request("GET", "/me")
            self.operations.append(
                InstallOperation(
                    action="check_graph_identity",
                    target=str(me.get("userPrincipalName") or me.get("displayName") or "current-user"),
                    status="ok",
                    detail=f"Authenticated to Graph for tenant {tenant_id}.",
                    external_id=_optional_string(me.get("id")),
                )
            )
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="check_graph_identity",
                    target="current-user",
                    status="blocked",
                    detail=exc.message,
                    required_permission="User.Read",
                )
            )

    def _ensure_team(self, *, configured_team_id: str | None, configured_team_name: str) -> str | None:
        if configured_team_id:
            try:
                team = self.graph.request("GET", f"/teams/{configured_team_id}")
                display_name = _optional_string(team.get("displayName"))
                if display_name == configured_team_name:
                    self.operations.append(
                        InstallOperation(
                            action="ensure_team",
                            target=configured_team_name,
                            status="reused",
                            detail="Configured team id and name match.",
                            external_id=configured_team_id,
                        )
                    )
                    return configured_team_id
                self.operations.append(
                    InstallOperation(
                        action="ensure_team",
                        target=configured_team_name,
                        status="needs_update",
                        detail=f"Configured team id resolves to `{display_name}`, not `{configured_team_name}`.",
                        external_id=configured_team_id,
                    )
                )
            except GraphRequestError as exc:
                self.operations.append(
                    InstallOperation(
                        action="ensure_team",
                        target=configured_team_name,
                        status="needs_update",
                        detail=f"Configured team id `{configured_team_id}` could not be read: {exc.message}",
                        required_permission="Team.ReadBasic.All or Group.Read.All",
                        external_id=configured_team_id,
                    )
                )

        matches = self._find_teams_by_name(configured_team_name)
        if matches:
            team_id = _required_string(matches[0], "id")
            self.operations.append(
                InstallOperation(
                    action="ensure_team",
                    target=configured_team_name,
                    status="reused",
                    detail="Found existing Team by display name.",
                    external_id=team_id,
                )
            )
            return team_id

        if not self.options.allow_create_team:
            self.operations.append(
                InstallOperation(
                    action="ensure_team",
                    target=configured_team_name,
                    status="planned",
                    detail="Team would be created when --allow-create-team is supplied.",
                    required_permission="Group.ReadWrite.All",
                )
            )
            return configured_team_id

        if not self.options.apply:
            self.operations.append(
                InstallOperation(
                    action="ensure_team",
                    target=configured_team_name,
                    status="planned",
                    detail="Team creation planned; rerun with --apply to mutate tenant state.",
                    required_permission="Group.ReadWrite.All",
                )
            )
            return configured_team_id

        self.operations.append(
            InstallOperation(
                action="ensure_team",
                target=configured_team_name,
                status="blocked",
                detail="Team creation needs the project installer's Graph create-team implementation.",
                required_permission="Group.ReadWrite.All",
            )
        )
        return configured_team_id

    def _ensure_channels(self, *, team_id: str | None, channels: dict[str, Any]) -> None:
        if not team_id:
            for channel_key in sorted(channels):
                self.operations.append(
                    InstallOperation(
                        action="ensure_channel",
                        target=channel_key,
                        status="blocked",
                        detail="Cannot reconcile channels until the project Team is resolved.",
                    )
                )
            return
        existing = self._list_channels(team_id)
        for channel_key, channel in sorted(channels.items()):
            if not isinstance(channel, dict):
                raise ValueError(f"channel `{channel_key}` must be a mapping")
            name = _required_string(channel, "name")
            configured_id = _optional_string(channel.get("id"))
            match = next((item for item in existing if item.get("displayName") == name), None)
            if match:
                status = "reused" if not configured_id or configured_id == match.get("id") else "needs_update"
                detail = "Found existing channel by name."
                if status == "needs_update":
                    detail = "Configured channel id differs from the existing channel with the same name."
                self.operations.append(
                    InstallOperation(
                        action="ensure_channel",
                        target=name,
                        status=status,
                        detail=detail,
                        external_id=_optional_string(match.get("id")),
                    )
                )
                continue
            if not self.options.allow_create_channel:
                self.operations.append(
                    InstallOperation(
                        action="ensure_channel",
                        target=name,
                        status="planned",
                        detail="Channel would be created when --allow-create-channel is supplied.",
                        required_permission="Channel.Create",
                    )
                )
                continue
            if not self.options.apply:
                self.operations.append(
                    InstallOperation(
                        action="ensure_channel",
                        target=name,
                        status="planned",
                        detail="Channel creation planned; rerun with --apply to mutate tenant state.",
                        required_permission="Channel.Create",
                    )
                )
                continue
            try:
                created = self.graph.request(
                    "POST",
                    f"/teams/{team_id}/channels",
                    body={"displayName": name, "membershipType": "standard"},
                )
                self.operations.append(
                    InstallOperation(
                        action="ensure_channel",
                        target=name,
                        status="created",
                        detail="Created standard Teams channel.",
                        external_id=_optional_string(created.get("id")),
                    )
                )
            except GraphRequestError as exc:
                self.operations.append(
                    InstallOperation(
                        action="ensure_channel",
                        target=name,
                        status="blocked",
                        detail=exc.message,
                        required_permission="Channel.Create or Group.ReadWrite.All",
                    )
                )

    def _ensure_agent_identity(
        self,
        *,
        role_id: str,
        display_name: str,
        bot_id_ref: str,
        secret_ref: str,
        team_id: str | None,
    ) -> None:
        apps = self._find_applications_by_display_name(display_name)
        if apps:
            app = apps[0]
            self.operations.append(
                InstallOperation(
                    action="ensure_entra_app",
                    target=display_name,
                    status="reused",
                    detail=f"Found existing Entra app registration for {role_id}; secret ref `{secret_ref}` remains external.",
                    external_id=_optional_string(app.get("appId")),
                )
            )
        elif not self.options.allow_register_apps:
            self.operations.append(
                InstallOperation(
                    action="ensure_entra_app",
                    target=display_name,
                    status="planned",
                    detail=f"App registration would be created for {role_id} when --allow-register-apps is supplied.",
                    required_permission="Application.ReadWrite.All",
                )
            )
        elif not self.options.apply:
            self.operations.append(
                InstallOperation(
                    action="ensure_entra_app",
                    target=display_name,
                    status="planned",
                    detail="App registration creation planned; rerun with --apply to mutate tenant state.",
                    required_permission="Application.ReadWrite.All",
                )
            )
        else:
            try:
                created = self.graph.request(
                    "POST",
                    "/applications",
                    body={"displayName": display_name, "signInAudience": "AzureADMyOrg"},
                )
                self.operations.append(
                    InstallOperation(
                        action="ensure_entra_app",
                        target=display_name,
                        status="created",
                        detail=f"Created Entra app registration; store resulting app id under `{bot_id_ref}`.",
                        external_id=_optional_string(created.get("appId")),
                    )
                )
            except GraphRequestError as exc:
                self.operations.append(
                    InstallOperation(
                        action="ensure_entra_app",
                        target=display_name,
                        status="blocked",
                        detail=exc.message,
                        required_permission="Application.ReadWrite.All",
                    )
                )

        if self.options.allow_secret_rotation:
            self.operations.append(
                InstallOperation(
                    action="ensure_secret",
                    target=secret_ref,
                    status="planned" if not self.options.apply else "blocked",
                    detail="Secret rotation is intentionally not implemented until a secret provider is configured.",
                    required_permission="Application.ReadWrite.All",
                )
            )
        else:
            self.operations.append(
                InstallOperation(
                    action="ensure_secret",
                    target=secret_ref,
                    status="skipped",
                    detail="Secret value is managed externally; installer validated the reference only.",
                )
            )

        if team_id is None:
            status = "blocked"
            detail = "Cannot install Teams app until project Team is resolved."
        elif not self.options.allow_install_apps:
            status = "planned"
            detail = "Teams app installation would run when --allow-install-apps is supplied."
        elif not self.options.apply:
            status = "planned"
            detail = "Teams app installation planned; rerun with --apply to mutate tenant state."
        else:
            status = "blocked"
            detail = (
                "Teams app installation requires a Teams app catalog package id; "
                "app catalog inspection/upload is blocked without AppCatalog permissions."
            )
        self.operations.append(
            InstallOperation(
                action="install_agent_to_team",
                target=display_name,
                status=status,
                detail=detail,
                required_permission="TeamsAppInstallation.ReadWriteForTeam and AppCatalog.Read.All",
                external_id=team_id,
            )
        )

    def _detect_stale_v1_installs(self, *, team_id: str | None) -> None:
        if team_id is None:
            self.operations.append(
                InstallOperation(
                    action="uninstall_stale_v1_agents",
                    target="team",
                    status="blocked",
                    detail="Cannot inspect stale installs until project Team is resolved.",
                )
            )
            return
        try:
            installed = self.graph.request("GET", f"/teams/{team_id}/installedApps?$expand=teamsAppDefinition")
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="uninstall_stale_v1_agents",
                    target=team_id,
                    status="blocked",
                    detail=exc.message,
                    required_permission="TeamsAppInstallation.ReadForTeam",
                    external_id=team_id,
                )
            )
            return
        matches = []
        for item in installed.get("value", []):
            definition = item.get("teamsAppDefinition") or {}
            name = str(definition.get("displayName") or "")
            if name.startswith("AM-") or "Agentic" in name:
                matches.append((str(item.get("id")), name))
        if not matches:
            self.operations.append(
                InstallOperation(
                    action="uninstall_stale_v1_agents",
                    target=team_id,
                    status="ok",
                    detail="No team-scoped AM-* or Agentic app installs found.",
                    external_id=team_id,
                )
            )
            return
        for installed_id, name in matches:
            if not self.options.allow_uninstall_stale:
                self.operations.append(
                    InstallOperation(
                        action="uninstall_stale_v1_agents",
                        target=name,
                        status="planned",
                        detail="Stale install found; rerun with --allow-uninstall-stale to remove it.",
                        required_permission="TeamsAppInstallation.ReadWriteForTeam",
                        external_id=installed_id,
                    )
                )
                continue
            if not self.options.apply:
                self.operations.append(
                    InstallOperation(
                        action="uninstall_stale_v1_agents",
                        target=name,
                        status="planned",
                        detail="Stale install removal planned; rerun with --apply to mutate tenant state.",
                        required_permission="TeamsAppInstallation.ReadWriteForTeam",
                        external_id=installed_id,
                    )
                )
                continue
            try:
                self.graph.request("DELETE", f"/teams/{team_id}/installedApps/{installed_id}")
                self.operations.append(
                    InstallOperation(
                        action="uninstall_stale_v1_agents",
                        target=name,
                        status="deleted",
                        detail="Removed stale team-scoped install.",
                        external_id=installed_id,
                    )
                )
            except GraphRequestError as exc:
                self.operations.append(
                    InstallOperation(
                        action="uninstall_stale_v1_agents",
                        target=name,
                        status="blocked",
                        detail=exc.message,
                        required_permission="TeamsAppInstallation.ReadWriteForTeam",
                        external_id=installed_id,
                    )
                )

    def _check_personal_install_scope(self) -> None:
        try:
            self.graph.request("GET", "/me/teamwork/installedApps?$top=1")
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="check_personal_installs",
                    target="current-user",
                    status="blocked",
                    detail=exc.message,
                    required_permission="TeamsAppInstallation.ReadForUser or TeamsAppInstallation.ReadWriteSelfForUser",
                )
            )
            return
        self.operations.append(
            InstallOperation(
                action="check_personal_installs",
                target="current-user",
                status="ok",
                detail="Current token can inspect personal Teams app installs.",
            )
        )

    def _find_teams_by_name(self, display_name: str) -> list[dict[str, Any]]:
        escaped = display_name.replace("'", "''")
        query = urllib.parse.urlencode(
            {
                "$filter": f"displayName eq '{escaped}'",
                "$select": "id,displayName,resourceProvisioningOptions",
            }
        )
        try:
            groups = self.graph.request("GET", f"/groups?{query}")
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="find_team",
                    target=display_name,
                    status="blocked",
                    detail=exc.message,
                    required_permission="Group.Read.All",
                )
            )
            return []
        return [
            item
            for item in groups.get("value", [])
            if "Team" in item.get("resourceProvisioningOptions", [])
        ]

    def _list_channels(self, team_id: str) -> list[dict[str, Any]]:
        try:
            return list(self.graph.request("GET", f"/teams/{team_id}/channels").get("value", []))
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="list_channels",
                    target=team_id,
                    status="blocked",
                    detail=exc.message,
                    required_permission="Channel.ReadBasic.All",
                    external_id=team_id,
                )
            )
            return []

    def _find_applications_by_display_name(self, display_name: str) -> list[dict[str, Any]]:
        escaped = display_name.replace("'", "''")
        query = urllib.parse.urlencode(
            {"$filter": f"displayName eq '{escaped}'", "$select": "id,appId,displayName"}
        )
        try:
            return list(self.graph.request("GET", f"/applications?{query}").get("value", []))
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="find_entra_app",
                    target=display_name,
                    status="blocked",
                    detail=exc.message,
                    required_permission="Application.Read.All",
                )
            )
            return []


def run_project_install(
    *,
    project_file: Path,
    organization_file: Path | None,
    options: InstallOptions,
    graph_client: AzureCliGraphClient | None = None,
) -> dict[str, Any]:
    project_config = _load_yaml(project_file)
    organization_config = _load_yaml(organization_file) if organization_file is not None else {}
    installer = ProjectInstaller(
        graph_client=graph_client or AzureCliGraphClient(),
        project_config=project_config,
        organization_config=organization_config,
        options=options,
    )
    return installer.run()


def _load_yaml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return raw


def _gateway_bots(project_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    gateways = project_config.get("gateways")
    if not isinstance(gateways, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for gateway_id, gateway in gateways.items():
        if not isinstance(gateway, dict):
            continue
        teams = gateway.get("teams")
        if not isinstance(teams, dict):
            continue
        bot = teams.get("bot")
        if isinstance(bot, dict):
            result[str(gateway_id)] = bot
    return result


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping")
    return value


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _count_statuses(operations: list[InstallOperation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for operation in operations:
        counts[operation.status] = counts.get(operation.status, 0) + 1
    return dict(sorted(counts.items()))


def _summary_status(operations: list[InstallOperation]) -> str:
    statuses = {operation.status for operation in operations}
    if "blocked" in statuses:
        return "blocked"
    if "planned" in statuses or "needs_update" in statuses:
        return "planned"
    return "ok"


def _resolve_az_path(az_path: str) -> str:
    resolved = shutil.which(az_path)
    if resolved is not None:
        return resolved
    if az_path == "az":
        for candidate in (
            r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
            r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
        ):
            if Path(candidate).exists():
                return candidate
    return az_path
