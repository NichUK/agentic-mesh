from __future__ import annotations

from pathlib import Path
from typing import Any
import zipfile

import pytest
import yaml

from agentic_mesh_v3.project_install import GraphRequestError
from agentic_mesh_v3.project_install import InstallOptions
from agentic_mesh_v3.project_install import ProjectInstaller
from agentic_mesh_v3.project_install import _load_yaml


class FakeGraphClient:
    def __init__(
        self,
        *,
        installed_apps: list[dict[str, Any]] | None = None,
        fail_personal_installs: bool = False,
    ) -> None:
        self.installed_apps = installed_apps or []
        self.fail_personal_installs = fail_personal_installs
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append((method, path, body))
        if path == "/me":
            return {"id": "person-nicholas", "userPrincipalName": "nich@quantauma.com"}
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
        if path == "/teams/team-dev-agentic-mesh/channels":
            return {"value": [{"id": "channel-project", "displayName": "project"}]}
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
        if path == "/applications" and method == "POST":
            return {
                "id": "app-created",
                "appId": "bot-created-app-id",
                "displayName": body["displayName"] if body is not None else "created",
            }
        if path.startswith("/servicePrincipals?"):
            return {
                "value": [
                    {
                        "id": "sp-product-manager",
                        "appId": "bot-product-manager-app-id",
                        "displayName": "AM-Product Manager",
                    }
                ]
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
            if method == "DELETE":
                return {}
            return {"value": self.installed_apps}
        if path.startswith("/me/teamwork/installedApps"):
            if self.fail_personal_installs:
                raise GraphRequestError(403, "Missing scope TeamsAppInstallation.ReadForUser")
            return {"value": []}
        if path.startswith("/users/") and "/teamwork/installedApps" in path:
            return {"value": []}
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
                "ingress": {"public_endpoint": "https://agentic-mesh.example/api/messages"},
                "role_bots": {
                    "product-manager": {
                        "display_name": "AM-Product Manager",
                        "bot_id_ref": "teams-bot-product-manager-app-id",
                        "secret_ref": "teams-bot-product-manager-secret",
                    },
                    "prompt-engineer": {
                        "display_name": "AM-Prompt Engineer",
                        "bot_id_ref": "teams-bot-prompt-engineer-app-id",
                        "secret_ref": "teams-bot-prompt-engineer-secret",
                    },
                },
                "people": [
                    {
                        "person_id": "person-nicholas",
                        "display_name": "Nicholas Overend",
                        "external_refs": ["aad-nicholas"],
                        "groups": ["sponsors"],
                    }
                ],
            }
        },
    }


def test_v3_project_installer_uses_v3_stale_agent_action_name() -> None:
    result = ProjectInstaller(
        graph_client=FakeGraphClient(
            installed_apps=[
                {
                    "id": "installed-old",
                    "teamsAppDefinition": {
                        "displayName": "AM-Old Agent",
                        "teamsAppId": "teams-app-old",
                    },
                }
            ]
        ),
        project_config=_project_config(),
        organization_config={},
        options=InstallOptions(apply=True, allow_uninstall_stale=True),
    ).run()

    stale_actions = [item for item in result["operations"] if item["action"] == "uninstall_stale_agents"]
    assert stale_actions
    assert not any(item["action"] == "uninstall_stale_v1_agents" for item in result["operations"])


def test_v3_project_installer_generates_teams_package_without_icon_assets(tmp_path: Path) -> None:
    graph = FakeGraphClient()
    result = ProjectInstaller(
        graph_client=graph,
        project_config=_project_config(),
        organization_config={},
        options=InstallOptions(apply=True, allow_register_apps=True, allow_install_apps=True),
        teams_app_package_root=tmp_path / "teams-apps",
    ).run()

    package_path = tmp_path / "teams-apps" / "prompt-engineer.zip"
    assert package_path.exists()
    with zipfile.ZipFile(package_path) as archive:
        assert sorted(archive.namelist()) == ["color.png", "manifest.json", "outline.png"]
        manifest = yaml.safe_load(archive.read("manifest.json"))
        assert manifest["version"] == "1.0.0"
        assert archive.read("color.png").startswith(b"\x89PNG")
        assert archive.read("outline.png").startswith(b"\x89PNG")
    assert any(
        item["action"] == "publish_teams_app"
        and item["target"] == "AM-Prompt Engineer"
        and item["status"] == "created"
        for item in result["operations"]
    )


def test_v3_project_installer_reuses_team_installed_app_id_for_personal_install() -> None:
    graph = FakeGraphClient(
        installed_apps=[
            {
                "id": "installed-product-manager",
                "teamsAppDefinition": {
                    "displayName": "AM-Product Manager",
                    "teamsAppId": "teams-app-product-manager",
                },
            }
        ]
    )

    ProjectInstaller(
        graph_client=graph,
        project_config=_project_config(),
        organization_config={},
        options=InstallOptions(apply=True, allow_install_apps=True),
    ).run()

    assert any(
        request[0] == "POST"
        and request[1] == "/users/aad-nicholas/teamwork/installedApps"
        and request[2] == {
            "teamsApp@odata.bind": "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/teams-app-product-manager"
        }
        for request in graph.requests
    )


def test_v3_dogfood_project_configures_all_role_bots() -> None:
    project = yaml.safe_load(
        Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v3.yaml").read_text(encoding="utf-8")
    )

    roles = set(project["roles"])
    role_bots = set(project["connectors"]["teams"]["role_bots"])

    assert role_bots == roles


def test_v3_project_install_load_yaml_expands_placeholder_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: agentic-mesh-dev
connectors:
  teams:
    tenant_id: ${AGENTIC_MESH_TENANT_ID}
    ingress:
      public_endpoint: ${AGENTIC_MESH_TEAMS_BOT_SERVICE_URL}
    team:
      id: ${AGENTIC_MESH_PROJECT_TEAM_ID}
      name: dev-agentic-mesh
    channels:
      project:
        id: ${AGENTIC_MESH_PROJECT_CHANNEL_ID}
        name: project
    role_bots:
      product-manager:
        display_name: AM-Product Manager
        bot_id_ref: teams-bot-product-manager-app-id
        secret_ref: teams-bot-product-manager-secret
    people:
      - person_id: nicholas-overend
        external_refs:
          - ${AGENTIC_MESH_SPONSOR_AAD_OBJECT_ID}
roles:
  product-manager:
    instances: 1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTIC_MESH_TENANT_ID", "tenant-dogfood")
    monkeypatch.setenv("AGENTIC_MESH_TEAMS_BOT_SERVICE_URL", "https://agentic-mesh.example/api/messages")
    monkeypatch.setenv("AGENTIC_MESH_PROJECT_TEAM_ID", "team-dogfood")
    monkeypatch.setenv("AGENTIC_MESH_PROJECT_CHANNEL_ID", "channel-dogfood")
    monkeypatch.setenv("AGENTIC_MESH_SPONSOR_AAD_OBJECT_ID", "aad-sponsor")

    project = _load_yaml(project_file)

    assert project["connectors"]["teams"]["tenant_id"] == "tenant-dogfood"
    assert project["connectors"]["teams"]["ingress"]["public_endpoint"] == "https://agentic-mesh.example/api/messages"
    assert project["connectors"]["teams"]["team"]["id"] == "team-dogfood"
    assert project["connectors"]["teams"]["channels"]["project"]["id"] == "channel-dogfood"
    assert project["connectors"]["teams"]["people"][0]["external_refs"] == ["aad-sponsor"]
