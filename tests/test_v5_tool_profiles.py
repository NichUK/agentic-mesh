from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agentic_mesh_v5.tool_profiles import ToolProfileError
from agentic_mesh_v5.tool_profiles import ToolProfileRegistry


GENERAL_PROFILE: dict[str, object] = {
    "tool_profile": {
        "schema_version": 1,
        "profile_id": "general",
        "image": {
            "repository": "agentic-mesh/worker-general",
            "tag": "0.1.0",
            "platform": "linux/amd64",
        },
        "capabilities": [
            {"id": "filesystem.read", "required": True},
            {"id": "structured-output", "required": True},
        ],
        "mounts": [
            {
                "id": "workspace",
                "source": "project-workspace",
                "target": "/workspace",
                "access": "read-write",
                "required": True,
            }
        ],
        "credentials": [
            {
                "id": "codex-auth",
                "kind": "oauth-cache",
                "delivery": "mount",
                "target": "/mesh/credentials/codex",
                "required": True,
            }
        ],
        "health": {
            "command": ["agentic-mesh-worker-healthcheck"],
            "interval_seconds": 30,
            "timeout_seconds": 5,
            "failure_threshold": 3,
        },
        "resources": {
            "cpu_millis": 1000,
            "memory_mb": 2048,
            "ephemeral_storage_mb": 4096,
        },
    }
}


def _repository(tmp_path: Path) -> Path:
    schema = tmp_path / "schemas" / "package.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    return tmp_path


def _profile_package(
    root: Path,
    profile_id: str,
    version: str,
    content: dict[str, object],
) -> str:
    package_root = root / "packages" / "tool-profile" / profile_id / version
    package_root.mkdir(parents=True)
    (package_root / "tool-profile.json").write_text(
        json.dumps(content, sort_keys=True) + "\n", encoding="utf-8"
    )
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": profile_id,
                "kind": "tool-profile",
                "version": version,
                "content": ["tool-profile.json"],
                "dependencies": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return f"tool-profile/{profile_id}@{version}"


def test_loads_typed_general_profile_and_checks_capabilities(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    reference = _profile_package(root, "general", "0.2.0", GENERAL_PROFILE)

    profile = ToolProfileRegistry(root).load(reference)

    assert profile.reference == reference
    assert profile.profile_id == "general"
    assert profile.image.repository == "agentic-mesh/worker-general"
    assert profile.capability_ids == {"filesystem.read", "structured-output"}
    assert str(profile.mounts[0].target) == "/workspace"
    assert profile.credentials[0].kind == "oauth-cache"
    assert profile.health.command == ("agentic-mesh-worker-healthcheck",)
    assert profile.resources.memory_mb == 2048
    assert len(profile.configuration_digest) == 64
    profile.require_capabilities(["structured-output"])
    with pytest.raises(ToolProfileError, match="lacks capabilities.*browser.test"):
        profile.require_capabilities(["browser.test"])
    with pytest.raises(ToolProfileError, match="must not have surrounding whitespace"):
        profile.require_capabilities([" structured-output "])


def test_synthetic_finance_category_uses_the_same_registry(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    content = copy.deepcopy(GENERAL_PROFILE)
    profile = content["tool_profile"]
    assert isinstance(profile, dict)
    profile["profile_id"] = "finance"
    image = profile["image"]
    assert isinstance(image, dict)
    image["repository"] = "agentic-mesh/worker-finance"
    profile["capabilities"] = [
        {"id": "ledger.read", "required": True},
        {"id": "forecast.create", "required": False},
    ]
    reference = _profile_package(root, "finance", "0.1.0", content)

    loaded = ToolProfileRegistry(root).load(reference)

    assert loaded.profile_id == "finance"
    assert loaded.capability_ids == {"ledger.read", "forecast.create"}
    loaded.require_capabilities(["ledger.read"])


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda profile: profile.update(profile_id="different"),
            "identity does not match",
        ),
        (
            lambda profile: profile.update(schema_version=2),
            "unsupported tool-profile schema_version",
        ),
        (
            lambda profile: profile.update(profile_id="general.v2"),
            "profile_id is invalid",
        ),
        (
            lambda profile: profile["capabilities"].append(
                {"id": "filesystem.read", "required": True}
            ),
            "capability ids must be unique",
        ),
        (
            lambda profile: profile["mounts"][0].update(target="../escape"),
            "mount.target must be an absolute",
        ),
        (
            lambda profile: profile["mounts"][0].update(id="workspace.data"),
            "mount.id is invalid",
        ),
        (
            lambda profile: profile["credentials"][0].update(id="codex.auth"),
            "credential.id is invalid",
        ),
        (
            lambda profile: profile["credentials"][0].update(
                target="/mesh/../escape"
            ),
            "mounted credential target must be an absolute contained",
        ),
        (
            lambda profile: profile["image"].update(repository="user:secret@registry/image"),
            "must not contain @",
        ),
        (
            lambda profile: profile["image"].update(tag=" 0.1.0"),
            "image.tag must not have surrounding whitespace",
        ),
        (
            lambda profile: profile["health"].update(timeout_seconds=31),
            "timeout cannot exceed interval",
        ),
        (
            lambda profile: profile["resources"].update(cpu_millis=True),
            "cpu_millis must be a positive integer",
        ),
    ],
)
def test_invalid_profiles_are_rejected(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    root = _repository(tmp_path)
    content = copy.deepcopy(GENERAL_PROFILE)
    profile = content["tool_profile"]
    assert isinstance(profile, dict)
    assert callable(mutation)
    mutation(profile)
    reference = _profile_package(root, "general", "0.2.0", content)

    with pytest.raises(ToolProfileError, match=message):
        ToolProfileRegistry(root).load(reference)


def test_registry_rejects_non_tool_profile_reference(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    package_root = root / "packages" / "system" / "core" / "1.0.0"
    package_root.mkdir(parents=True)
    (package_root / "settings.json").write_text("{}\n", encoding="utf-8")
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "core",
                "kind": "system",
                "version": "1.0.0",
                "content": ["settings.json"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ToolProfileError, match="requires a tool-profile reference"):
        ToolProfileRegistry(root).load("system/core@1.0.0")
