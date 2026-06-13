from __future__ import annotations

from typing import Any

import pytest

from agentic_mesh_v2.project_install import GraphRequestError
from agentic_mesh_v2.project_install import InstallOptions
from agentic_mesh_v2.project_install import ProjectInstaller


class FakeGraphClient:
    def __init__(self, *, fail_personal_installs: bool = True) -> None:
        self.fail_personal_installs = fail_personal_installs
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
        if path.startswith("/teams/team-dev-agentic-mesh/installedApps"):
            return {"value": []}
        if path.startswith("/me/teamwork/installedApps"):
            if self.fail_personal_installs:
                raise GraphRequestError(403, "Missing scope TeamsAppInstallation.ReadForUser")
            return {"value": []}
        raise AssertionError(f"unexpected Graph request {method} {path}")


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


def _run_installer(options: InstallOptions | None = None, *, fail_personal_installs: bool = True) -> dict[str, Any]:
    return ProjectInstaller(
        graph_client=FakeGraphClient(fail_personal_installs=fail_personal_installs),
        project_config=_project_config(),
        organization_config={},
        options=options or InstallOptions(),
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
