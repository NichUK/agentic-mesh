from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
import urllib.parse
import urllib.request
import zipfile
import struct
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import yaml


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_ENV_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class InstallOptions:
    apply: bool = False
    allow_create_team: bool = False
    allow_create_channel: bool = False
    allow_register_apps: bool = False
    allow_register_bot_services: bool = False
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


class AzureCliError(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)
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
                payload = response.read().decode("utf-8")
                if not payload.strip():
                    return {}
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")
            raise GraphRequestError(exc.code, payload) from exc

    def request_bytes(self, method: str, path: str, *, data: bytes, content_type: str) -> dict[str, Any]:
        token = self._access_token()
        request = urllib.request.Request(
            f"{GRAPH_ROOT}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                payload = response.read().decode("utf-8")
                if not payload.strip():
                    return {}
                return json.loads(payload)
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


class AzureCliBotServiceClient:
    def __init__(self, *, az_path: str = "az") -> None:
        self.az_path = _resolve_az_path(az_path)

    def show(self, *, resource_group: str, name: str) -> dict[str, Any] | None:
        result = subprocess.run(
            [
                self.az_path,
                "bot",
                "show",
                "--resource-group",
                resource_group,
                "--name",
                name,
                "-o",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}".casefold()
            if "could not be found" in combined or "was not found" in combined or "not found" in combined:
                return None
            raise AzureCliError((result.stderr or result.stdout or "az bot show failed").strip())
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    def create(
        self,
        *,
        resource_group: str,
        name: str,
        app_id: str,
        tenant_id: str,
        display_name: str,
        endpoint: str,
        location: str,
        sku: str,
    ) -> dict[str, Any]:
        return self._run_json(
            [
                self.az_path,
                "bot",
                "create",
                "--resource-group",
                resource_group,
                "--name",
                name,
                "--appid",
                app_id,
                "--app-type",
                "SingleTenant",
                "--tenant-id",
                tenant_id,
                "--display-name",
                display_name,
                "--endpoint",
                endpoint,
                "--sku",
                sku,
                "--location",
                location,
                "-o",
                "json",
            ]
        )

    def update_endpoint(self, *, resource_group: str, name: str, endpoint: str) -> dict[str, Any]:
        return self._run_json(
            [
                self.az_path,
                "bot",
                "update",
                "--resource-group",
                resource_group,
                "--name",
                name,
                "--endpoint",
                endpoint,
                "-o",
                "json",
            ]
        )

    def ensure_msteams_channel(self, *, resource_group: str, name: str) -> dict[str, Any]:
        return self._run_json(
            [
                self.az_path,
                "bot",
                "msteams",
                "create",
                "--resource-group",
                resource_group,
                "--name",
                name,
                "-o",
                "json",
            ]
        )

    def _run_json(self, args: list[str]) -> dict[str, Any]:
        result = subprocess.run(args, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise AzureCliError((result.stderr or result.stdout or f"{args[0]} failed").strip())
        if not result.stdout.strip():
            return {}
        return json.loads(result.stdout)


class TokenGraphClient:
    def __init__(self, *, access_token: str) -> None:
        if not access_token.strip():
            raise ValueError("access token must be non-empty")
        self._token = access_token.strip()

    @classmethod
    def from_file(cls, path: Path) -> "TokenGraphClient":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            token = raw.get("access_token") or raw.get("accessToken")
            if isinstance(token, str):
                return cls(access_token=token)
        raise ValueError(f"{path} must contain an access_token field")

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{GRAPH_ROOT}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                if response.status == 204:
                    return {}
                payload = response.read().decode("utf-8")
                if not payload.strip():
                    return {}
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")
            raise GraphRequestError(exc.code, payload) from exc

    def request_bytes(self, method: str, path: str, *, data: bytes, content_type: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{GRAPH_ROOT}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": content_type,
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                payload = response.read().decode("utf-8")
                if not payload.strip():
                    return {}
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")
            raise GraphRequestError(exc.code, payload) from exc


@dataclass(frozen=True)
class TeamsAppPackage:
    role_id: str
    display_name: str
    teams_app_id: str
    package: str | None = None


class ProjectInstaller:
    def __init__(
        self,
        *,
        graph_client: Any,
        project_config: dict[str, Any],
        organization_config: dict[str, Any] | None = None,
        options: InstallOptions,
        teams_app_package_root: Path | None = None,
        azure_bot_client: Any | None = None,
    ) -> None:
        self.graph = graph_client
        self.azure_bot = azure_bot_client or AzureCliBotServiceClient()
        self.project_config = project_config
        self.organization_config = organization_config or {}
        self.options = options
        self.teams_app_package_root = teams_app_package_root
        self.teams_app_packages = _load_published_teams_apps(teams_app_package_root)
        self.operations: list[InstallOperation] = []
        self.expected_team_app_names: set[str] = set()
        self.expected_team_app_ids: set[str] = set()

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
        people = _people_list(teams.get("people"))
        bot_service = _mapping_or_empty(teams.get("bot_service"), "connectors.teams.bot_service")
        gateway_bots = _gateway_bots(self.project_config)
        self._record_expected_team_apps(role_bots=role_bots, gateway_bots=gateway_bots)

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
                tenant_id=tenant_id,
                bot_service=bot_service,
            )
        for gateway_id, bot in sorted(gateway_bots.items()):
            self._ensure_agent_identity(
                role_id=f"gateway:{gateway_id}",
                display_name=_required_string(bot, "display_name"),
                bot_id_ref=_required_string(bot, "bot_id_ref"),
                secret_ref=_required_string(bot, "secret_ref"),
                team_id=resolved_team_id,
                tenant_id=tenant_id,
                bot_service=bot_service,
            )

        self._detect_stale_agent_installs(team_id=resolved_team_id)
        self._check_personal_install_scope()
        self._ensure_personal_app_installs(
            people=people,
            role_bots=role_bots,
            gateway_bots=gateway_bots,
        )

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
        tenant_id: str,
        bot_service: dict[str, Any],
    ) -> None:
        apps = self._find_applications_by_display_name(display_name)
        app_id: str | None = None
        if apps:
            app = apps[0]
            app_id = _optional_string(app.get("appId"))
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
                app_id = _optional_string(created.get("appId"))
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

        if app_id is not None:
            self._ensure_service_principal(app_id=app_id, display_name=display_name)
            self._ensure_bot_service(
                role_id=role_id,
                display_name=display_name,
                app_id=app_id,
                tenant_id=tenant_id,
                bot_service=bot_service,
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

        self._ensure_team_app_install(
            role_id=role_id,
            display_name=display_name,
            team_id=team_id,
            bot_app_id=app_id,
        )

    def _ensure_bot_service(
        self,
        *,
        role_id: str,
        display_name: str,
        app_id: str,
        tenant_id: str,
        bot_service: dict[str, Any],
    ) -> None:
        if not bot_service:
            self.operations.append(
                InstallOperation(
                    action="ensure_bot_service",
                    target=display_name,
                    status="skipped",
                    detail="No connectors.teams.bot_service block configured; Azure Bot Service reconciliation skipped.",
                    external_id=app_id,
                )
            )
            return
        resource_group = _required_string(bot_service, "resource_group")
        location = _optional_string(bot_service.get("location")) or "global"
        sku = _optional_string(bot_service.get("sku")) or "F0"
        endpoint = _optional_string(bot_service.get("endpoint")) or _teams_public_endpoint(self.project_config)
        name_template = _optional_string(bot_service.get("name_template")) or "am-{role_id}"
        bot_name = _bot_service_name(name_template=name_template, role_id=role_id, display_name=display_name)

        try:
            existing = self.azure_bot.show(resource_group=resource_group, name=bot_name)
        except AzureCliError as exc:
            self.operations.append(
                InstallOperation(
                    action="ensure_bot_service",
                    target=bot_name,
                    status="blocked",
                    detail=exc.message,
                    required_permission="Microsoft.BotService/botServices/read",
                    external_id=app_id,
                )
            )
            return

        if existing is None:
            if not self.options.allow_register_bot_services:
                self.operations.append(
                    InstallOperation(
                        action="ensure_bot_service",
                        target=bot_name,
                        status="planned",
                        detail="Azure Bot Service registration would be created when --allow-register-bot-services is supplied.",
                        required_permission="Microsoft.BotService/botServices/write",
                        external_id=app_id,
                    )
                )
                return
            if not self.options.apply:
                self.operations.append(
                    InstallOperation(
                        action="ensure_bot_service",
                        target=bot_name,
                        status="planned",
                        detail="Azure Bot Service creation planned; rerun with --apply to mutate Azure state.",
                        required_permission="Microsoft.BotService/botServices/write",
                        external_id=app_id,
                    )
                )
                return
            try:
                existing = self.azure_bot.create(
                    resource_group=resource_group,
                    name=bot_name,
                    app_id=app_id,
                    tenant_id=tenant_id,
                    display_name=display_name,
                    endpoint=endpoint,
                    location=location,
                    sku=sku,
                )
            except AzureCliError as exc:
                self.operations.append(
                    InstallOperation(
                        action="ensure_bot_service",
                        target=bot_name,
                        status="blocked",
                        detail=exc.message,
                        required_permission="Microsoft.BotService/botServices/write",
                        external_id=app_id,
                    )
                )
                return
            self.operations.append(
                InstallOperation(
                    action="ensure_bot_service",
                    target=bot_name,
                    status="created",
                    detail="Created Azure Bot Service registration for the role bot.",
                    required_permission="Microsoft.BotService/botServices/write",
                    external_id=_optional_string(existing.get("id")) or app_id,
                )
            )
        else:
            properties = _mapping(existing.get("properties"), "bot.properties")
            configured_app_id = _optional_string(properties.get("msaAppId"))
            configured_endpoint = _optional_string(properties.get("endpoint"))
            if configured_app_id and configured_app_id != app_id:
                self.operations.append(
                    InstallOperation(
                        action="ensure_bot_service",
                        target=bot_name,
                        status="blocked",
                        detail=(
                            f"Existing Azure Bot Service uses app id `{configured_app_id}`, "
                            f"but project role `{role_id}` expects `{app_id}`."
                        ),
                        required_permission="Microsoft.BotService/botServices/write",
                        external_id=_optional_string(existing.get("id")) or configured_app_id,
                    )
                )
                return
            if configured_endpoint and configured_endpoint != endpoint:
                if not self.options.allow_register_bot_services:
                    self.operations.append(
                        InstallOperation(
                            action="ensure_bot_service",
                            target=bot_name,
                            status="planned",
                            detail=(
                                f"Existing Azure Bot Service endpoint is `{configured_endpoint}`; "
                                f"it would be updated to `{endpoint}` when --allow-register-bot-services is supplied."
                            ),
                            required_permission="Microsoft.BotService/botServices/write",
                            external_id=_optional_string(existing.get("id")) or app_id,
                        )
                    )
                    return
                if not self.options.apply:
                    self.operations.append(
                        InstallOperation(
                            action="ensure_bot_service",
                            target=bot_name,
                            status="planned",
                            detail=(
                                f"Existing Azure Bot Service endpoint is `{configured_endpoint}`; "
                                f"update to `{endpoint}` is planned. Rerun with --apply to mutate Azure state."
                            ),
                            required_permission="Microsoft.BotService/botServices/write",
                            external_id=_optional_string(existing.get("id")) or app_id,
                        )
                    )
                    return
                try:
                    existing = self.azure_bot.update_endpoint(
                        resource_group=resource_group,
                        name=bot_name,
                        endpoint=endpoint,
                    )
                except AzureCliError as exc:
                    self.operations.append(
                        InstallOperation(
                            action="ensure_bot_service",
                            target=bot_name,
                            status="blocked",
                            detail=exc.message,
                            required_permission="Microsoft.BotService/botServices/write",
                            external_id=_optional_string(existing.get("id")) or app_id,
                        )
                    )
                    return
                self.operations.append(
                    InstallOperation(
                        action="ensure_bot_service",
                        target=bot_name,
                        status="updated",
                        detail=f"Updated Azure Bot Service endpoint from `{configured_endpoint}` to `{endpoint}`.",
                        required_permission="Microsoft.BotService/botServices/write",
                        external_id=_optional_string(existing.get("id")) or app_id,
                    )
                )
            else:
                self.operations.append(
                    InstallOperation(
                        action="ensure_bot_service",
                        target=bot_name,
                        status="reused",
                        detail="Found existing Azure Bot Service registration for the role bot.",
                        external_id=_optional_string(existing.get("id")) or app_id,
                    )
                )

        channels = _bot_service_channels(existing or {})
        if "msteams" in channels:
            self.operations.append(
                InstallOperation(
                    action="ensure_bot_service_channel",
                    target=bot_name,
                    status="reused",
                    detail="Azure Bot Service already has the Microsoft Teams channel enabled.",
                    external_id=app_id,
                )
            )
            return
        if not self.options.allow_register_bot_services:
            self.operations.append(
                InstallOperation(
                    action="ensure_bot_service_channel",
                    target=bot_name,
                    status="planned",
                    detail="Microsoft Teams channel would be enabled when --allow-register-bot-services is supplied.",
                    required_permission="Microsoft.BotService/botServices/channels/write",
                    external_id=app_id,
                )
            )
            return
        if not self.options.apply:
            self.operations.append(
                InstallOperation(
                    action="ensure_bot_service_channel",
                    target=bot_name,
                    status="planned",
                    detail="Microsoft Teams channel enablement planned; rerun with --apply to mutate Azure state.",
                    required_permission="Microsoft.BotService/botServices/channels/write",
                    external_id=app_id,
                )
            )
            return
        try:
            self.azure_bot.ensure_msteams_channel(resource_group=resource_group, name=bot_name)
        except AzureCliError as exc:
            self.operations.append(
                InstallOperation(
                    action="ensure_bot_service_channel",
                    target=bot_name,
                    status="blocked",
                    detail=exc.message,
                    required_permission="Microsoft.BotService/botServices/channels/write",
                    external_id=app_id,
                )
            )
            return
        self.operations.append(
            InstallOperation(
                action="ensure_bot_service_channel",
                target=bot_name,
                status="created",
                detail="Enabled Microsoft Teams channel on the Azure Bot Service registration.",
                required_permission="Microsoft.BotService/botServices/channels/write",
                external_id=app_id,
            )
        )

    def _ensure_service_principal(self, *, app_id: str, display_name: str) -> None:
        matches = self._find_service_principals_by_app_id(app_id, display_name=display_name)
        if matches:
            self.operations.append(
                InstallOperation(
                    action="ensure_service_principal",
                    target=display_name,
                    status="reused",
                    detail="Found existing service principal for the app registration.",
                    external_id=_optional_string(matches[0].get("id")),
                )
            )
            return
        if not self.options.allow_register_apps:
            self.operations.append(
                InstallOperation(
                    action="ensure_service_principal",
                    target=display_name,
                    status="planned",
                    detail="Service principal would be created when --allow-register-apps is supplied.",
                    required_permission="Application.ReadWrite.All",
                    external_id=app_id,
                )
            )
            return
        if not self.options.apply:
            self.operations.append(
                InstallOperation(
                    action="ensure_service_principal",
                    target=display_name,
                    status="planned",
                    detail="Service principal creation planned; rerun with --apply to mutate tenant state.",
                    required_permission="Application.ReadWrite.All",
                    external_id=app_id,
                )
            )
            return
        try:
            created = self.graph.request("POST", "/servicePrincipals", body={"appId": app_id})
            self.operations.append(
                InstallOperation(
                    action="ensure_service_principal",
                    target=display_name,
                    status="created",
                    detail="Created service principal for the app registration.",
                    required_permission="Application.ReadWrite.All",
                    external_id=_optional_string(created.get("id")),
                )
            )
        except GraphRequestError as exc:
            if exc.status_code == 409:
                self.operations.append(
                    InstallOperation(
                        action="ensure_service_principal",
                        target=display_name,
                        status="reused",
                        detail="Graph reported the service principal already exists.",
                        external_id=app_id,
                    )
                )
                return
            self.operations.append(
                InstallOperation(
                    action="ensure_service_principal",
                    target=display_name,
                    status="blocked",
                    detail=exc.message,
                    required_permission="Application.ReadWrite.All",
                    external_id=app_id,
                )
            )

    def _record_expected_team_apps(
        self,
        *,
        role_bots: dict[str, Any],
        gateway_bots: dict[str, dict[str, Any]],
    ) -> None:
        for role_id, bot in role_bots.items():
            if not isinstance(bot, dict):
                continue
            display_name = _optional_string(bot.get("display_name"))
            if display_name:
                self.expected_team_app_names.add(display_name)
            package = self._package_for_role(str(role_id))
            if package is not None:
                self.expected_team_app_ids.add(package.teams_app_id)
        for gateway_id, bot in gateway_bots.items():
            display_name = _optional_string(bot.get("display_name"))
            if display_name:
                self.expected_team_app_names.add(display_name)
            package = self._package_for_role(f"gateway:{gateway_id}")
            if package is not None:
                self.expected_team_app_ids.add(package.teams_app_id)

    def _ensure_team_app_install(
        self,
        *,
        role_id: str,
        display_name: str,
        team_id: str | None,
        bot_app_id: str | None,
    ) -> None:
        if team_id is None:
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_team",
                    target=display_name,
                    status="blocked",
                    detail="Cannot install Teams app until project Team is resolved.",
                )
            )
            return
        if not self.options.allow_install_apps:
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_team",
                    target=display_name,
                    status="planned",
                    detail="Teams app installation would run when --allow-install-apps is supplied.",
                    required_permission="TeamsAppInstallation.ReadWriteForTeam",
                    external_id=team_id,
                )
            )
            return
        if not self.options.apply:
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_team",
                    target=display_name,
                    status="planned",
                    detail="Teams app installation planned; rerun with --apply to mutate tenant state.",
                    required_permission="TeamsAppInstallation.ReadWriteForTeam",
                    external_id=team_id,
                )
            )
            return

        installed = self._list_installed_team_apps(team_id=team_id)
        existing = _find_installed_team_app(
            installed,
            display_name=display_name,
            teams_app_ids=self._package_ids_for_role(role_id),
        )
        if existing is not None:
            teams_app_id = _installed_teams_app_id(existing)
            if teams_app_id is not None:
                self.teams_app_packages[role_id] = TeamsAppPackage(
                    role_id=role_id,
                    display_name=display_name,
                    teams_app_id=teams_app_id,
                )
                self.expected_team_app_ids.add(teams_app_id)
                self.expected_team_app_names.add(display_name)
                if self.teams_app_package_root is not None:
                    self.teams_app_package_root.mkdir(parents=True, exist_ok=True)
                    _write_published_teams_apps(self.teams_app_package_root, self.teams_app_packages)
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_team",
                    target=display_name,
                    status="reused",
                    detail="Teams app is already installed in the project Team.",
                    external_id=_optional_string(existing.get("id")),
                )
            )
            return

        package = self._package_for_role(role_id)
        if package is None and bot_app_id is not None:
            package = self._publish_generated_teams_app(
                role_id=role_id,
                display_name=display_name,
                bot_app_id=bot_app_id,
            )
        if package is None:
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_team",
                    target=display_name,
                    status="blocked",
                    detail=(
                        "No published Teams app catalog id was found for this project agent. "
                        "Provide build/teams-apps/published-apps.json, publish the app package first, "
                        "or allow app registration creation so the installer can generate one."
                    ),
                    required_permission="AppCatalog.ReadWrite.All",
                    external_id=team_id,
                )
            )
            return

        try:
            self.graph.request(
                "POST",
                f"/teams/{team_id}/installedApps",
                body={
                    "teamsApp@odata.bind": (
                        f"https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/{package.teams_app_id}"
                    )
                },
            )
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_team",
                    target=display_name,
                    status="created",
                    detail="Installed the published Teams app into the project Team.",
                    required_permission="TeamsAppInstallation.ReadWriteForTeam",
                    external_id=package.teams_app_id,
                )
            )
        except GraphRequestError as exc:
            if exc.status_code == 409:
                self.operations.append(
                    InstallOperation(
                        action="install_agent_to_team",
                        target=display_name,
                        status="reused",
                        detail="Graph reported the Teams app is already installed.",
                        external_id=package.teams_app_id,
                    )
                )
                return
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_team",
                    target=display_name,
                    status="blocked",
                    detail=exc.message,
                    required_permission="TeamsAppInstallation.ReadWriteForTeam",
                    external_id=package.teams_app_id,
                )
            )

    def _package_for_role(self, role_id: str) -> TeamsAppPackage | None:
        if role_id in self.teams_app_packages:
            return self.teams_app_packages[role_id]
        if role_id.startswith("gateway:"):
            return self.teams_app_packages.get(role_id.removeprefix("gateway:"))
        return None

    def _package_ids_for_role(self, role_id: str) -> set[str]:
        package = self._package_for_role(role_id)
        return set() if package is None else {package.teams_app_id}

    def _ensure_personal_app_installs(
        self,
        *,
        people: list[dict[str, Any]],
        role_bots: dict[str, Any],
        gateway_bots: dict[str, dict[str, Any]],
    ) -> None:
        targets: list[tuple[str, str]] = []
        for role_id, bot in sorted(role_bots.items()):
            if isinstance(bot, dict):
                targets.append((str(role_id), _required_string(bot, "display_name")))
        for gateway_id, bot in sorted(gateway_bots.items()):
            targets.append((f"gateway:{gateway_id}", _required_string(bot, "display_name")))

        if not people:
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_person",
                    target="configured-people",
                    status="planned",
                    detail="No connectors.teams.people records are configured; personal DM installs cannot be reconciled.",
                )
            )
            return

        for person in people:
            person_label = _person_label(person)
            person_refs = _person_external_refs(person)
            if not person_refs:
                self.operations.append(
                    InstallOperation(
                        action="install_agent_to_person",
                        target=person_label,
                        status="blocked",
                        detail="Person record has no usable external_refs for Graph user installation.",
                    )
                )
                continue
            user_ref = person_refs[0]
            installed = self._list_installed_personal_apps(user_ref=user_ref, person_label=person_label)
            for role_id, display_name in targets:
                self._ensure_personal_app_install(
                    role_id=role_id,
                    display_name=display_name,
                    user_ref=user_ref,
                    person_label=person_label,
                    installed=installed,
                )

    def _ensure_personal_app_install(
        self,
        *,
        role_id: str,
        display_name: str,
        user_ref: str,
        person_label: str,
        installed: list[dict[str, Any]] | None,
    ) -> None:
        target = f"{person_label}: {display_name}"
        if not self.options.allow_install_apps:
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_person",
                    target=target,
                    status="planned",
                    detail="Personal Teams app installation would run when --allow-install-apps is supplied.",
                    required_permission="TeamsAppInstallation.ReadWriteForUser",
                    external_id=user_ref,
                )
            )
            return
        if not self.options.apply:
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_person",
                    target=target,
                    status="planned",
                    detail="Personal Teams app installation planned; rerun with --apply to mutate tenant state.",
                    required_permission="TeamsAppInstallation.ReadWriteForUser",
                    external_id=user_ref,
                )
            )
            return

        package = self._package_for_role(role_id)
        if package is None:
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_person",
                    target=target,
                    status="blocked",
                    detail=(
                        "No published Teams app catalog id was found for this project agent. "
                        "Run app publication/registration before personal installation."
                    ),
                    required_permission="AppCatalog.ReadWrite.All",
                    external_id=user_ref,
                )
            )
            return

        if installed is not None:
            existing = _find_installed_team_app(
                installed,
                display_name=display_name,
                teams_app_ids={package.teams_app_id},
            )
            if existing is not None:
                self.operations.append(
                    InstallOperation(
                        action="install_agent_to_person",
                        target=target,
                        status="reused",
                        detail="Teams app is already installed in the person's personal scope.",
                        external_id=_optional_string(existing.get("id")),
                    )
                )
                return

        try:
            self.graph.request(
                "POST",
                f"/users/{urllib.parse.quote(user_ref, safe='')}/teamwork/installedApps",
                body={
                    "teamsApp@odata.bind": (
                        f"https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/{package.teams_app_id}"
                    )
                },
            )
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_person",
                    target=target,
                    status="created",
                    detail="Installed the published Teams app into the person's personal Teams scope.",
                    required_permission="TeamsAppInstallation.ReadWriteForUser",
                    external_id=package.teams_app_id,
                )
            )
        except GraphRequestError as exc:
            if exc.status_code == 409:
                self.operations.append(
                    InstallOperation(
                        action="install_agent_to_person",
                        target=target,
                        status="reused",
                        detail="Graph reported the Teams app is already installed in the person's personal scope.",
                        external_id=package.teams_app_id,
                    )
                )
                return
            self.operations.append(
                InstallOperation(
                    action="install_agent_to_person",
                    target=target,
                    status="blocked",
                    detail=exc.message,
                    required_permission="TeamsAppInstallation.ReadWriteForUser",
                    external_id=package.teams_app_id,
                )
            )

    def _publish_generated_teams_app(
        self,
        *,
        role_id: str,
        display_name: str,
        bot_app_id: str,
    ) -> TeamsAppPackage | None:
        if self.teams_app_packages.get(role_id) is not None:
            return self.teams_app_packages[role_id]
        existing = self._find_published_teams_app_by_display_name(display_name)
        if existing is not None:
            package = TeamsAppPackage(
                role_id=role_id,
                display_name=display_name,
                teams_app_id=existing,
            )
            self.teams_app_packages[role_id] = package
            self.expected_team_app_ids.add(existing)
            self.expected_team_app_names.add(display_name)
            if self.teams_app_package_root is not None:
                self.teams_app_package_root.mkdir(parents=True, exist_ok=True)
                _write_published_teams_apps(self.teams_app_package_root, self.teams_app_packages)
            self.operations.append(
                InstallOperation(
                    action="publish_teams_app",
                    target=display_name,
                    status="reused",
                    detail="Found an existing published Teams app in the organization app catalog.",
                    external_id=existing,
                )
            )
            return package
        if self.teams_app_package_root is None:
            return None
        self.teams_app_package_root.mkdir(parents=True, exist_ok=True)
        package_name = f"{_safe_package_name(role_id)}.zip"
        package_path = self.teams_app_package_root / package_name
        package_bytes = _build_teams_app_package(
            project_config=self.project_config,
            role_id=role_id,
            display_name=display_name,
            bot_app_id=bot_app_id,
            package_root=self.teams_app_package_root,
        )
        package_path.write_bytes(package_bytes)
        try:
            published = self.graph.request_bytes(
                "POST",
                "/appCatalogs/teamsApps",
                data=package_bytes,
                content_type="application/zip",
            )
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="publish_teams_app",
                    target=display_name,
                    status="blocked",
                    detail=exc.message,
                    required_permission="AppCatalog.ReadWrite.All",
                )
            )
            return None
        teams_app_id = _optional_string(published.get("id")) or _optional_string(published.get("teamsAppId"))
        if teams_app_id is None:
            self.operations.append(
                InstallOperation(
                    action="publish_teams_app",
                    target=display_name,
                    status="blocked",
                    detail="Graph did not return a Teams app catalog id after publishing the package.",
                    required_permission="AppCatalog.ReadWrite.All",
                )
            )
            return None
        package = TeamsAppPackage(
            role_id=role_id,
            display_name=display_name,
            teams_app_id=teams_app_id,
            package=package_name,
        )
        self.teams_app_packages[role_id] = package
        self.expected_team_app_ids.add(teams_app_id)
        self.expected_team_app_names.add(display_name)
        _write_published_teams_apps(self.teams_app_package_root, self.teams_app_packages)
        self.operations.append(
            InstallOperation(
                action="publish_teams_app",
                target=display_name,
                status="created",
                detail="Published a generated Teams app package to the organization app catalog.",
                required_permission="AppCatalog.ReadWrite.All",
                external_id=teams_app_id,
            )
        )
        return package

    def _find_published_teams_app_by_display_name(self, display_name: str) -> str | None:
        query = urllib.parse.urlencode(
            {
                "$filter": "distributionMethod eq 'organization'",
                "$expand": "appDefinitions",
            }
        )
        try:
            catalog = self.graph.request("GET", f"/appCatalogs/teamsApps?{query}")
        except GraphRequestError:
            return None
        for item in catalog.get("value", []):
            definitions = item.get("appDefinitions")
            if not isinstance(definitions, list):
                continue
            for definition in definitions:
                if isinstance(definition, dict) and definition.get("displayName") == display_name:
                    return _optional_string(item.get("id"))
        return None

    def _find_service_principals_by_app_id(self, app_id: str, *, display_name: str) -> list[dict[str, Any]]:
        escaped = app_id.replace("'", "''")
        query = urllib.parse.urlencode(
            {
                "$filter": f"appId eq '{escaped}'",
                "$select": "id,appId,displayName,accountEnabled,servicePrincipalType",
            }
        )
        try:
            result = self.graph.request("GET", f"/servicePrincipals?{query}")
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="ensure_service_principal",
                    target=display_name,
                    status="blocked",
                    detail=exc.message,
                    required_permission="Application.Read.All or Application.ReadWrite.All",
                    external_id=app_id,
                )
            )
            return []
        return list(result.get("value", []))

    def _detect_stale_agent_installs(self, *, team_id: str | None) -> None:
        if team_id is None:
            self.operations.append(
                InstallOperation(
                    action="uninstall_stale_agents",
                    target="team",
                    status="blocked",
                    detail="Cannot inspect stale installs until project Team is resolved.",
                )
            )
            return
        installed = self._list_installed_team_apps(team_id=team_id)
        matches = []
        for item in installed:
            definition = item.get("teamsAppDefinition") or {}
            name = str(definition.get("displayName") or "")
            teams_app_id = _optional_string(definition.get("teamsAppId"))
            is_agentic_app = name.startswith("AM-") or "Agentic" in name
            is_expected = name in self.expected_team_app_names or (
                teams_app_id is not None and teams_app_id in self.expected_team_app_ids
            )
            if is_agentic_app and not is_expected:
                matches.append((str(item.get("id")), name))
        if not matches:
            self.operations.append(
                InstallOperation(
                    action="uninstall_stale_agents",
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
                        action="uninstall_stale_agents",
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
                        action="uninstall_stale_agents",
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
                        action="uninstall_stale_agents",
                        target=name,
                        status="deleted",
                        detail="Removed stale team-scoped install.",
                        external_id=installed_id,
                    )
                )
            except GraphRequestError as exc:
                self.operations.append(
                    InstallOperation(
                        action="uninstall_stale_agents",
                        target=name,
                        status="blocked",
                        detail=exc.message,
                        required_permission="TeamsAppInstallation.ReadWriteForTeam",
                        external_id=installed_id,
                    )
                )

    def _check_personal_install_scope(self) -> None:
        try:
            self.graph.request("GET", "/me/teamwork/installedApps")
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

    def _list_installed_personal_apps(self, *, user_ref: str, person_label: str) -> list[dict[str, Any]] | None:
        try:
            return list(
                self.graph.request(
                    "GET",
                    f"/users/{urllib.parse.quote(user_ref, safe='')}/teamwork/installedApps?$expand=teamsAppDefinition",
                ).get("value", [])
            )
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="list_personal_installs",
                    target=person_label,
                    status="blocked",
                    detail=exc.message,
                    required_permission="TeamsAppInstallation.ReadForUser",
                    external_id=user_ref,
                )
            )
            return None

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

    def _list_installed_team_apps(self, *, team_id: str) -> list[dict[str, Any]]:
        try:
            return list(
                self.graph.request("GET", f"/teams/{team_id}/installedApps?$expand=teamsAppDefinition").get(
                    "value", []
                )
            )
        except GraphRequestError as exc:
            self.operations.append(
                InstallOperation(
                    action="list_team_installs",
                    target=team_id,
                    status="blocked",
                    detail=exc.message,
                    required_permission="TeamsAppInstallation.ReadForTeam",
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
    graph_client: Any | None = None,
    graph_token_file: Path | None = None,
    teams_app_package_root: Path | None = None,
) -> dict[str, Any]:
    project_config = _load_yaml(project_file)
    organization_config = _load_yaml(organization_file) if organization_file is not None else {}
    if graph_client is None and graph_token_file is not None:
        graph_client = TokenGraphClient.from_file(graph_token_file)
    installer = ProjectInstaller(
        graph_client=graph_client or AzureCliGraphClient(),
        project_config=project_config,
        organization_config=organization_config,
        options=options,
        teams_app_package_root=teams_app_package_root,
    )
    return installer.run()


def _load_yaml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    expanded = _expand_env_refs(raw)
    if not isinstance(expanded, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return expanded


def _expand_env_refs(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.environ.get(name)
            if resolved is None:
                raise ValueError(f"Environment variable `{name}` is required by project or organization configuration")
            return resolved

        return _ENV_REF_RE.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env_refs(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env_refs(item) for key, item in value.items()}
    return value


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


def _people_list(value: object) -> list[dict[str, Any]]:
    if value in (None, ()):
        return []
    if not isinstance(value, list):
        raise ValueError("connectors.teams.people must be a list")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("connectors.teams.people entries must be mappings")
        result.append(item)
    return result


def _person_label(person: dict[str, Any]) -> str:
    return (
        _optional_string(person.get("display_name"))
        or _optional_string(person.get("person_id"))
        or _optional_string(person.get("id"))
        or "unnamed-person"
    )


def _person_external_refs(person: dict[str, Any]) -> list[str]:
    refs = person.get("external_refs")
    if refs in (None, ()):
        return []
    if not isinstance(refs, list):
        raise ValueError("connectors.teams.people external_refs must be lists")
    result: list[str] = []
    for ref in refs:
        text = str(ref).strip()
        if text and text not in result:
            result.append(text)
    return result


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping")
    return value


def _mapping_or_empty(value: object, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    return _mapping(value, field)


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
            try:
                if Path(candidate).exists():
                    return candidate
            except OSError:
                continue
    return az_path


def _load_published_teams_apps(package_root: Path | None) -> dict[str, TeamsAppPackage]:
    if package_root is None:
        return {}
    published_path = package_root / "published-apps.json"
    if not published_path.exists():
        return {}
    raw = json.loads(published_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{published_path} must contain a JSON list")
    packages: dict[str, TeamsAppPackage] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        role_id = _optional_string(item.get("role"))
        display_name = _optional_string(item.get("displayName"))
        teams_app_id = _optional_string(item.get("teamsAppId"))
        if role_id is None or display_name is None or teams_app_id is None:
            continue
        packages[role_id] = TeamsAppPackage(
            role_id=role_id,
            display_name=display_name,
            teams_app_id=teams_app_id,
            package=_optional_string(item.get("package")),
        )
    return packages


def _write_published_teams_apps(package_root: Path, packages: dict[str, TeamsAppPackage]) -> None:
    records = [
        {
            "role": package.role_id,
            "package": package.package,
            "status": "published",
            "teamsAppId": package.teams_app_id,
            "displayName": package.display_name,
        }
        for package in sorted(packages.values(), key=lambda item: item.role_id)
    ]
    (package_root / "published-apps.json").write_text(
        json.dumps(records, indent=2) + "\n",
        encoding="utf-8",
    )


def _build_teams_app_package(
    *,
    project_config: dict[str, Any],
    role_id: str,
    display_name: str,
    bot_app_id: str,
    package_root: Path,
) -> bytes:
    project_id = _required_string(project_config, "project_id")
    app_manifest_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentic-mesh:{project_id}:{role_id}:teams-app"))
    endpoint = _teams_public_endpoint(project_config)
    domain = urllib.parse.urlparse(endpoint).hostname or "agentic-mesh.local"
    manifest = {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
        "manifestVersion": "1.17",
        "version": "1.0.0",
        "id": app_manifest_id,
        "developer": {
            "name": "Seerstone Systems Ltd",
            "websiteUrl": "https://seerstone.systems",
            "privacyUrl": "https://seerstone.systems/privacy",
            "termsOfUseUrl": "https://seerstone.systems/terms",
        },
        "name": {"short": display_name[:30], "full": display_name},
        "description": {
            "short": f"{display_name} bot for Agentic Mesh.",
            "full": f"{display_name} is the Microsoft Teams bot identity for {role_id} in {project_id}.",
        },
        "icons": {"outline": "outline.png", "color": "color.png"},
        "accentColor": "#23579B",
        "bots": [
            {
                "botId": bot_app_id,
                "scopes": ["team", "personal"],
                "supportsFiles": False,
                "isNotificationOnly": False,
            }
        ],
        "validDomains": [domain],
    }
    color_icon, outline_icon = _teams_app_icon_bytes(package_root)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr("color.png", color_icon)
        archive.writestr("outline.png", outline_icon)
    return output.getvalue()


def _teams_public_endpoint(project_config: dict[str, Any]) -> str:
    connectors = project_config.get("connectors")
    if isinstance(connectors, dict):
        teams = connectors.get("teams")
        if isinstance(teams, dict):
            ingress = teams.get("ingress")
            if isinstance(ingress, dict):
                endpoint = ingress.get("public_endpoint")
                if isinstance(endpoint, str) and endpoint.strip():
                    return endpoint.strip()
    return "https://agentic-mesh.local/teams/activity"


def _teams_app_icon_bytes(package_root: Path) -> tuple[bytes, bytes]:
    for child in package_root.iterdir() if package_root.exists() else []:
        if not child.is_dir():
            continue
        color = child / "color.png"
        outline = child / "outline.png"
        if color.exists() and outline.exists():
            return color.read_bytes(), outline.read_bytes()
    return _default_teams_app_icons()


def _default_teams_app_icons() -> tuple[bytes, bytes]:
    color = _solid_png(width=192, height=192, rgba=(35, 87, 155, 255))
    outline = _solid_png(width=32, height=32, rgba=(255, 255, 255, 255))
    return color, outline


def _solid_png(*, width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    raw_row = b"\x00" + bytes(rgba) * width
    raw = raw_row * height
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(raw)),
            _png_chunk(b"IEND", b""),
        ]
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _safe_package_name(value: str) -> str:
    safe = []
    for character in value.lower():
        if character.isalnum():
            safe.append(character)
        elif character in {":", "-", "_", " "}:
            safe.append("-")
    return "-".join("".join(safe).split("-"))


def _bot_service_name(*, name_template: str, role_id: str, display_name: str) -> str:
    clean_role = role_id.removeprefix("gateway:")
    candidate = name_template.format(
        role_id=clean_role,
        role_id_safe=_safe_package_name(clean_role),
        display_name=display_name,
        display_name_safe=_safe_package_name(display_name),
    )
    safe = []
    for character in candidate:
        if character.isalnum() or character in {"-", "_"}:
            safe.append(character)
        elif character in {" ", ":", "."}:
            safe.append("-")
    name = "-".join("".join(safe).lower().split("-")).strip("-")
    if not 4 <= len(name) <= 42:
        raise ValueError(f"Azure Bot Service name `{name}` must be between 4 and 42 characters")
    return name


def _bot_service_channels(bot_service: dict[str, Any]) -> set[str]:
    properties = bot_service.get("properties")
    if not isinstance(properties, dict):
        return set()
    channels = properties.get("enabledChannels") or properties.get("configuredChannels") or []
    if not isinstance(channels, list):
        return set()
    return {str(channel).casefold() for channel in channels}


def _find_installed_team_app(
    installed: list[dict[str, Any]],
    *,
    display_name: str,
    teams_app_ids: set[str],
) -> dict[str, Any] | None:
    if teams_app_ids:
        for item in installed:
            definition = item.get("teamsAppDefinition") or {}
            teams_app_id = _optional_string(definition.get("teamsAppId"))
            if teams_app_id is not None and teams_app_id in teams_app_ids:
                return item
        return None
    for item in installed:
        definition = item.get("teamsAppDefinition") or {}
        if definition.get("displayName") == display_name:
            return item
    return None


def _installed_teams_app_id(installed_app: dict[str, Any]) -> str | None:
    definition = installed_app.get("teamsAppDefinition") or {}
    return _optional_string(definition.get("teamsAppId"))
