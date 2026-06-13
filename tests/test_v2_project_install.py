from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentic_mesh_v2.project_install import GraphRequestError
from agentic_mesh_v2.project_install import InstallOptions
from agentic_mesh_v2.project_install import ProjectInstaller


class FakeGraphClient:
    def __init__(
        self,
        *,
        fail_personal_installs: bool = True,
        installed_apps: list[dict[str, Any]] | None = None,
        installed_personal_apps: dict[str, list[dict[str, Any]]] | None = None,
        service_principals: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.fail_personal_installs = fail_personal_installs
        self.installed_apps = installed_apps or []
        self.installed_personal_apps = installed_personal_apps or {}
        self.service_principals = (
            service_principals
            if service_principals is not None
            else {
                "bot-product-manager-app-id": {
                    "id": "sp-product-manager",
                    "appId": "bot-product-manager-app-id",
                    "displayName": "AM-Product Manager",
                }
            }
        )
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append((method, path, body))
        if path == "/me":
            return {"id": "person-nicholas", "userPrincipalName": "nich@quantauma.com"}
        if path.startswith("/teams/team-dev-agentic-mesh") and path.endswith("/channels"):
            return {
                "value": [
                    {
                        "id": "channel-project",
                        "displayName": "project",
                    }
                ]
            }
        if path == "/teams/team-dev-agentic-mesh":
            return {"id": "team-dev-agentic-mesh", "displayName": "dev-agentic-mesh"}
        if path.startswith("/groups?"):
            return {
                "value": [
                    {
                        "id": "team-dev-agentic-mesh",
                        "displayName": "dev-agentic-mesh",
                        "resourceProvisioningOptions": ["Team"],
                    }
                ]
            }
        if path.startswith("/applications?"):
            if "AM-Product+Manager" in path or "AM-Product%20Manager" in path:
                return {
                    "value": [
                        {
                            "id": "app-product-manager",
                            "appId": "bot-product-manager-app-id",
                            "displayName": "AM-Product Manager",
                        }
                    ]
                }
            return {"value": []}
        if path.startswith("/servicePrincipals?"):
            if "appId+eq+" in path:
                app_id = path.split("appId+eq+", 1)[1].split("&", 1)[0].strip("%27'")
                app_id = app_id.replace("%27", "").replace("'", "")
                principal = self.service_principals.get(app_id)
                return {"value": [] if principal is None else [principal]}
            return {"value": []}
        if path == "/servicePrincipals" and method == "POST":
            app_id = str((body or {}).get("appId") or "")
            principal = {
                "id": f"sp-{app_id}",
                "appId": app_id,
                "displayName": f"Service Principal {app_id}",
            }
            self.service_principals[app_id] = principal
            return principal
        if path == "/applications" and method == "POST":
            return {
                "id": "app-created",
                "appId": "bot-created-app-id",
                "displayName": body["displayName"] if body is not None else "created",
            }
        if path.startswith("/appCatalogs/teamsApps?"):
            return {"value": []}
        if path.startswith("/teams/team-dev-agentic-mesh/installedApps"):
            if method == "POST":
                self.installed_apps.append(
                    {
                        "id": f"installed-{len(self.installed_apps) + 1}",
                        "teamsAppDefinition": {
                            "displayName": "unknown",
                            "teamsAppId": str(body or {}),
                        },
                    }
                )
                return {}
            return {"value": self.installed_apps}
        if path.startswith("/me/teamwork/installedApps"):
            if self.fail_personal_installs:
                raise GraphRequestError(403, "Missing scope TeamsAppInstallation.ReadForUser")
            return {"value": []}
        if path.startswith("/users/") and "/teamwork/installedApps" in path:
            user_ref = path.split("/users/", 1)[1].split("/teamwork/", 1)[0]
            if method == "POST":
                app_id = str((body or {}).get("teamsApp@odata.bind", "")).rsplit("/", 1)[-1]
                self.installed_personal_apps.setdefault(user_ref, []).append(
                    {
                        "id": f"personal-{user_ref}-{len(self.installed_personal_apps.get(user_ref, [])) + 1}",
                        "teamsAppDefinition": {
                            "displayName": "unknown",
                            "teamsAppId": app_id,
                        },
                    }
                )
                return {}
            return {"value": self.installed_personal_apps.get(user_ref, [])}
        raise AssertionError(f"unexpected Graph request {method} {path}")

    def request_bytes(self, method: str, path: str, *, data: bytes, content_type: str) -> dict[str, Any]:
        self.requests.append((method, path, {"content_type": content_type, "bytes": len(data)}))
        if path == "/appCatalogs/teamsApps" and method == "POST":
            return {"id": "teams-app-generated"}
        raise AssertionError(f"unexpected Graph byte request {method} {path}")


def _project_config() -> dict[str, Any]:
    return {
        "project_id": "agentic-mesh-dev",
        "connectors": {
            "teams": {
                "tenant_id": "tenant-1",
                "team": {"id": "team-dev-agentic-mesh", "name": "dev-agentic-mesh"},
                "channels": {"project": {"id": "channel-project", "name": "project"}},
                "role_bots": {
                    "product-manager": {
                        "display_name": "AM-Product Manager",
                        "bot_id_ref": "teams-bot-product-manager-app-id",
                        "secret_ref": "teams-bot-product-manager-secret",
                    },
                    "engineering": {
                        "display_name": "AM-Engineering",
                        "bot_id_ref": "teams-bot-engineering-app-id",
                        "secret_ref": "teams-bot-engineering-secret",
                    },
                },
            }
        },
        "gateways": {
            "agentic-mesh": {
                "teams": {
                    "bot": {
                        "display_name": "AM-Agentic Mesh",
                        "bot_id_ref": "teams-bot-agentic-mesh-app-id",
                        "secret_ref": "teams-bot-agentic-mesh-secret",
                    }
                }
            }
        },
    }


def _project_config_with_people() -> dict[str, Any]:
    config = _project_config()
    teams = config["connectors"]["teams"]
    teams["people"] = [
        {
            "person_id": "person-nicholas",
            "display_name": "Nicholas Overend",
            "external_refs": ["aad-nicholas"],
            "groups": ["sponsors", "release-board"],
        }
    ]
    teams["authority_groups"] = {
        "sponsors": ["sponsor"],
        "release-board": ["release_approver"],
    }
    return config


def _package_root(tmp_path: Path) -> Path:
    root = tmp_path / "teams-apps"
    root.mkdir()
    icon_dir = root / "product-manager"
    icon_dir.mkdir()
    icon_dir.joinpath("color.png").write_bytes(b"color")
    icon_dir.joinpath("outline.png").write_bytes(b"outline")
    (root / "published-apps.json").write_text(
        json.dumps(
            [
                {
                    "role": "product-manager",
                    "displayName": "AM-Product Manager",
                    "teamsAppId": "teams-app-product-manager",
                    "package": "product-manager.zip",
                },
                {
                    "role": "engineering",
                    "displayName": "AM-Engineering",
                    "teamsAppId": "teams-app-engineering",
                    "package": "engineering.zip",
                },
            ]
        ),
        encoding="utf-8",
    )
    return root


def _package_root_with_gateway(tmp_path: Path) -> Path:
    root = _package_root(tmp_path)
    records = json.loads((root / "published-apps.json").read_text(encoding="utf-8"))
    records.append(
        {
            "role": "gateway:agentic-mesh",
            "displayName": "AM-Agentic Mesh",
            "teamsAppId": "teams-app-gateway-agentic-mesh",
            "package": "gateway-agentic-mesh.zip",
        }
    )
    (root / "published-apps.json").write_text(json.dumps(records), encoding="utf-8")
    return root


def _run_installer(
    options: InstallOptions | None = None,
    *,
    fail_personal_installs: bool = True,
    teams_app_package_root: Path | None = None,
    installed_apps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return ProjectInstaller(
        graph_client=FakeGraphClient(
            fail_personal_installs=fail_personal_installs,
            installed_apps=installed_apps,
        ),
        project_config=_project_config(),
        organization_config={},
        options=options or InstallOptions(),
        teams_app_package_root=teams_app_package_root,
    ).run()


def test_project_installer_reuses_team_channel_and_existing_entra_app() -> None:
    result = _run_installer()

    assert result["team_name"] == "dev-agentic-mesh"
    assert result["team_id"] == "team-dev-agentic-mesh"
    operations = result["operations"]
    assert any(
        item["action"] == "ensure_team" and item["status"] == "reused"
        for item in operations
    )
    assert any(
        item["action"] == "ensure_channel" and item["target"] == "project" and item["status"] == "reused"
        for item in operations
    )
    assert any(
        item["action"] == "ensure_entra_app"
        and item["target"] == "AM-Product Manager"
        and item["status"] == "reused"
        for item in operations
    )


def test_project_installer_plans_all_agent_installs_without_install_flag() -> None:
    result = _run_installer()

    install_targets = {
        item["target"]
        for item in result["operations"]
        if item["action"] == "install_agent_to_team"
    }
    assert install_targets == {"AM-Agentic Mesh", "AM-Engineering", "AM-Product Manager"}
    assert all(
        item["status"] == "planned"
        for item in result["operations"]
        if item["action"] == "install_agent_to_team"
    )


def test_project_installer_reports_personal_install_scope_gap() -> None:
    result = _run_installer()

    personal = next(item for item in result["operations"] if item["action"] == "check_personal_installs")
    assert personal["status"] == "blocked"
    assert personal["required_permission"] == (
        "TeamsAppInstallation.ReadForUser or TeamsAppInstallation.ReadWriteSelfForUser"
    )
    assert result["status"] == "blocked"


def test_project_installer_can_report_clean_scope_when_personal_install_read_is_granted() -> None:
    result = _run_installer(fail_personal_installs=False)

    personal = next(item for item in result["operations"] if item["action"] == "check_personal_installs")
    assert personal["status"] == "ok"


def test_project_installer_rejects_missing_required_config() -> None:
    with pytest.raises(ValueError, match="connectors.teams"):
        ProjectInstaller(
            graph_client=FakeGraphClient(),
            project_config={"project_id": "agentic-mesh-dev", "connectors": {}},
            organization_config={},
            options=InstallOptions(),
        ).run()


def test_project_installer_installs_published_team_apps(tmp_path: Path) -> None:
    graph = FakeGraphClient(fail_personal_installs=False)
    result = ProjectInstaller(
        graph_client=graph,
        project_config=_project_config(),
        organization_config={},
        options=InstallOptions(apply=True, allow_install_apps=True),
        teams_app_package_root=_package_root(tmp_path),
    ).run()

    install_posts = [
        request
        for request in graph.requests
        if request[0] == "POST" and request[1] == "/teams/team-dev-agentic-mesh/installedApps"
    ]
    assert len(install_posts) == 2
    assert {
        request[2]["teamsApp@odata.bind"].rsplit("/", 1)[-1]
        for request in install_posts
        if request[2] is not None
    } == {"teams-app-product-manager", "teams-app-engineering"}
    assert any(
        item["action"] == "install_agent_to_team"
        and item["target"] == "AM-Agentic Mesh"
        and item["status"] == "blocked"
        for item in result["operations"]
    )


def test_project_installer_reuses_existing_team_app_install(tmp_path: Path) -> None:
    result = _run_installer(
        InstallOptions(apply=True, allow_install_apps=True),
        fail_personal_installs=False,
        teams_app_package_root=_package_root(tmp_path),
        installed_apps=[
            {
                "id": "installed-product",
                "teamsAppDefinition": {
                    "displayName": "AM-Product Manager",
                    "teamsAppId": "teams-app-product-manager",
                },
            }
        ],
    )

    product = next(
        item
        for item in result["operations"]
        if item["action"] == "install_agent_to_team" and item["target"] == "AM-Product Manager"
    )
    assert product["status"] == "reused"


def test_project_installer_does_not_reuse_wrong_catalog_id_with_same_display_name(tmp_path: Path) -> None:
    graph = FakeGraphClient(
        fail_personal_installs=False,
        installed_apps=[
            {
                "id": "installed-old-product",
                "teamsAppDefinition": {
                    "displayName": "AM-Product Manager",
                    "teamsAppId": "teams-app-old-product-manager",
                },
            }
        ],
    )
    ProjectInstaller(
        graph_client=graph,
        project_config=_project_config(),
        organization_config={},
        options=InstallOptions(apply=True, allow_install_apps=True),
        teams_app_package_root=_package_root(tmp_path),
    ).run()

    assert any(
        request[0] == "POST"
        and request[1] == "/teams/team-dev-agentic-mesh/installedApps"
        and request[2] == {
            "teamsApp@odata.bind": "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/teams-app-product-manager"
        }
        for request in graph.requests
    )


def test_project_installer_plans_personal_installs_for_configured_people(tmp_path: Path) -> None:
    result = ProjectInstaller(
        graph_client=FakeGraphClient(fail_personal_installs=False),
        project_config=_project_config_with_people(),
        organization_config={},
        options=InstallOptions(),
        teams_app_package_root=_package_root_with_gateway(tmp_path),
    ).run()

    personal_installs = [
        item
        for item in result["operations"]
        if item["action"] == "install_agent_to_person"
    ]
    assert {item["target"] for item in personal_installs} == {
        "Nicholas Overend: AM-Agentic Mesh",
        "Nicholas Overend: AM-Engineering",
        "Nicholas Overend: AM-Product Manager",
    }
    assert all(item["status"] == "planned" for item in personal_installs)


def test_project_installer_installs_personal_apps_for_configured_people(tmp_path: Path) -> None:
    graph = FakeGraphClient(fail_personal_installs=False)
    result = ProjectInstaller(
        graph_client=graph,
        project_config=_project_config_with_people(),
        organization_config={},
        options=InstallOptions(apply=True, allow_install_apps=True),
        teams_app_package_root=_package_root_with_gateway(tmp_path),
    ).run()

    personal_posts = [
        request
        for request in graph.requests
        if request[0] == "POST" and request[1] == "/users/aad-nicholas/teamwork/installedApps"
    ]
    assert {
        request[2]["teamsApp@odata.bind"].rsplit("/", 1)[-1]
        for request in personal_posts
        if request[2] is not None
    } == {
        "teams-app-gateway-agentic-mesh",
        "teams-app-product-manager",
        "teams-app-engineering",
    }
    assert any(
        item["action"] == "install_agent_to_person"
        and item["target"] == "Nicholas Overend: AM-Product Manager"
        and item["status"] == "created"
        for item in result["operations"]
    )


def test_project_installer_creates_missing_service_principal_before_install(tmp_path: Path) -> None:
    graph = FakeGraphClient(
        fail_personal_installs=False,
        service_principals={},
    )
    result = ProjectInstaller(
        graph_client=graph,
        project_config=_project_config(),
        organization_config={},
        options=InstallOptions(apply=True, allow_register_apps=True, allow_install_apps=True),
        teams_app_package_root=_package_root(tmp_path),
    ).run()

    assert any(
        request[0] == "POST"
        and request[1] == "/servicePrincipals"
        and request[2] == {"appId": "bot-product-manager-app-id"}
        for request in graph.requests
    )
    assert any(
        item["action"] == "ensure_service_principal"
        and item["target"] == "AM-Product Manager"
        and item["status"] == "created"
        for item in result["operations"]
    )


def test_project_installer_does_not_delete_expected_agentic_apps(tmp_path: Path) -> None:
    result = _run_installer(
        InstallOptions(apply=True, allow_uninstall_stale=True),
        fail_personal_installs=False,
        teams_app_package_root=_package_root(tmp_path),
        installed_apps=[
            {
                "id": "installed-product",
                "teamsAppDefinition": {
                    "displayName": "AM-Product Manager",
                    "teamsAppId": "teams-app-product-manager",
                },
            },
            {
                "id": "installed-old",
                "teamsAppDefinition": {
                    "displayName": "AM-Old V1 Agent",
                    "teamsAppId": "teams-app-old",
                },
            },
        ],
    )

    stale_deletes = [
        item
        for item in result["operations"]
        if item["action"] == "uninstall_stale_v1_agents" and item["status"] == "deleted"
    ]
    assert [item["target"] for item in stale_deletes] == ["AM-Old V1 Agent"]


def test_project_installer_can_generate_and_publish_missing_gateway_package(tmp_path: Path) -> None:
    graph = FakeGraphClient(fail_personal_installs=False)
    result = ProjectInstaller(
        graph_client=graph,
        project_config=_project_config(),
        organization_config={},
        options=InstallOptions(apply=True, allow_register_apps=True, allow_install_apps=True),
        teams_app_package_root=_package_root(tmp_path),
    ).run()

    assert any(
        request[0] == "POST" and request[1] == "/appCatalogs/teamsApps"
        for request in graph.requests
    )
    assert any(
        item["action"] == "publish_teams_app"
        and item["target"] == "AM-Agentic Mesh"
        and item["status"] == "created"
        for item in result["operations"]
    )
    assert (tmp_path / "teams-apps" / "gateway-agentic-mesh.zip").exists()


def test_project_installer_reuses_published_gateway_when_package_manifest_is_missing(tmp_path: Path) -> None:
    class CatalogGraphClient(FakeGraphClient):
        def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
            if path.startswith("/appCatalogs/teamsApps?"):
                self.requests.append((method, path, body))
                return {
                    "value": [
                        {
                            "id": "teams-app-existing-gateway",
                            "appDefinitions": [{"displayName": "AM-Agentic Mesh"}],
                        }
                    ]
                }
            return super().request(method, path, body=body)

    graph = CatalogGraphClient(fail_personal_installs=False)
    root = _package_root(tmp_path)
    result = ProjectInstaller(
        graph_client=graph,
        project_config=_project_config(),
        organization_config={},
        options=InstallOptions(apply=True, allow_register_apps=True, allow_install_apps=True),
        teams_app_package_root=root,
    ).run()

    assert not any(
        request[0] == "POST" and request[1] == "/appCatalogs/teamsApps"
        for request in graph.requests
    )
    assert any(
        item["action"] == "publish_teams_app"
        and item["target"] == "AM-Agentic Mesh"
        and item["status"] == "reused"
        and item["external_id"] == "teams-app-existing-gateway"
        for item in result["operations"]
    )
