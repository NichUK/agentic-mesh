from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = (
    ROOT / "examples" / "projects" / "agentic-mesh-v5" / "deploy" / "compose"
)
COMPOSE = DEPLOYMENT / "compose.yaml"
FLEET = DEPLOYMENT / "fleet.json"

CORE_SERVICES = {"database", "migrate", "telemetry", "control"}
EXPECTED_PROFILES = {
    "business-analyst": "GENERAL",
    "delivery-manager": "GENERAL",
    "engineering-1": "DEVELOPMENT",
    "engineering-2": "DEVELOPMENT",
    "enterprise-architect": "GENERAL",
    "platform-engineer": "OPERATIONS",
    "product-manager": "GENERAL",
    "project-manager": "GENERAL",
    "prompt-engineer": "GENERAL",
    "qa-engineer": "QA",
    "release-manager": "OPERATIONS",
    "research-analyst": "GENERAL",
    "security-architect": "GENERAL",
    "solution-architect": "GENERAL",
    "technical-writer": "GENERAL",
    "ux-designer": "UX",
}


def _compose() -> dict[str, object]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _mount_text(service: dict[str, object]) -> str:
    return "\n".join(str(item) for item in service.get("volumes", []))


def test_linuxch_compose_contains_the_complete_project_core() -> None:
    data = _compose()
    services = data["services"]

    assert data["name"] == "agentic-mesh-v5"
    assert set(services) == CORE_SERVICES | set(EXPECTED_PROFILES)
    assert services["database"].get("ports") is None
    assert services["migrate"]["command"] == ["database-migrate"]
    assert (
        services["control"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert services["control"]["group_add"] == [
        "${AGENTIC_MESH_DOCKER_GID:?set the Linux host Docker group id}"
    ]
    control_mounts = _mount_text(services["control"])
    assert "/var/run/docker.sock" in control_mounts
    assert "/mesh/sources" not in control_mounts
    assert "/mesh/workspaces" not in control_mounts
    assert len(data["volumes"]) == 16
    assert all(volume["external"] is True for volume in data["volumes"].values())
    assert len(data["secrets"]) == 16


def test_linuxch_fleet_matches_every_role_service_and_tool_profile() -> None:
    data = _compose()
    services = data["services"]
    fleet = json.loads(FLEET.read_text(encoding="utf-8"))["projects"]
    instances = fleet["agentic-mesh-v5"]["instances"]

    assert len(instances) == len(EXPECTED_PROFILES) == 16
    for service_id, profile in EXPECTED_PROFILES.items():
        service = services[service_id]
        command = service["command"]
        role = command[command.index("--role") + 1]
        instance = command[command.index("--instance") + 1]
        binding = instances[instance]

        assert command[:3] == ["python", "-m", "agentic_mesh_v5"]
        assert binding == {
            "role_id": role,
            "container": service["container_name"],
        }
        assert service["image"].startswith(
            "${AGENTIC_MESH_V5_WORKER_" f"{profile}_IMAGE:?"
        )
        assert service["environment"]["CODEX_HOME"] == "/codex-home"
        assert service["environment"]["AGENTIC_MESH_V5_API_TOKEN_FILE"] == (
            "/run/secrets/api_token"
        )
        assert service["secrets"][0]["target"] == "api_token"
        if service_id == "project-manager":
            assert "profiles" not in service
        else:
            assert service["profiles"] == ["fleet"]
        mounts = _mount_text(service)
        for expected in (
            "/mesh/config:ro",
            "/mesh/config/state",
            "/mesh/runtime/sources.json:ro",
            "/mesh/sources/agentic-mesh",
            "/mesh/workspaces",
            "/codex-home",
        ):
            assert expected in mounts, (service_id, expected)


def test_only_engineering_receives_project_scoped_github_auth() -> None:
    services = _compose()["services"]

    for service_id in EXPECTED_PROFILES:
        service = services[service_id]
        environment = service["environment"]
        mounts = _mount_text(service)
        serialized = json.dumps(service, sort_keys=True)

        assert "GITHUB_TOKEN" not in serialized
        assert "GH_TOKEN" not in serialized
        if service_id.startswith("engineering-"):
            assert environment["GH_CONFIG_DIR"] == "/run/agentic-mesh/github"
            assert "./gitconfig:/etc/gitconfig:ro" in mounts
            assert "/run/agentic-mesh/github:ro" in mounts
        else:
            assert "GH_CONFIG_DIR" not in environment
            assert "/run/agentic-mesh/github" not in mounts


def test_project_deployment_inputs_are_secret_free_and_linux_first() -> None:
    environment = (DEPLOYMENT / ".env.example").read_text(encoding="utf-8")
    readme = (DEPLOYMENT / "README.md").read_text(encoding="utf-8")
    sources = json.loads((DEPLOYMENT / "sources.json").read_text(encoding="utf-8"))

    assert "LinuxCH" in readme
    assert "Windows" not in readme
    assert "--profile fleet create" in readme
    assert "not start all fleet-profile services" in readme
    assert "password=" not in environment.lower()
    assert "token=" not in environment.lower()
    assert "/home/nich/agentic-mesh-projects/agentic-mesh-v5" in environment
    assert sources["projects"]["agentic-mesh-v5"]["repositories"] == {
        "primary": "/mesh/sources/agentic-mesh"
    }
