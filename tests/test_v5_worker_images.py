from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "worker_image_verifier", ROOT / "scripts" / "verify_v5_worker_images.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load worker image verifier")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def _synthetic_config_repository(root: Path) -> Path:
    schema = root / "schemas" / "package.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    for profile_id in VERIFIER.PROFILE_IDS:
        manifest = json.loads(
            (ROOT / "docker" / "v5" / "manifests" / f"{profile_id}.json").read_text(
                encoding="utf-8"
            )
        )
        version = manifest["tool_profile_reference"].rsplit("@", 1)[1]
        package_root = root / "packages" / "tool-profile" / profile_id / version
        package_root.mkdir(parents=True)
        repository, tag = manifest["image"].rsplit(":", 1)
        profile = {
            "tool_profile": {
                "schema_version": 1,
                "profile_id": profile_id,
                "image": {
                    "repository": repository,
                    "tag": tag,
                    "platform": "linux/amd64",
                },
                "capabilities": [
                    {"id": item, "required": True}
                    for item in manifest["capabilities"]
                ],
                "mounts": [],
                "credentials": [],
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
        (package_root / "tool-profile.json").write_text(
            json.dumps(profile) + "\n", encoding="utf-8"
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
                }
            )
            + "\n",
            encoding="utf-8",
        )
    return root


def test_core_worker_image_contract_is_valid() -> None:
    assert VERIFIER.validate_worker_images(ROOT) == []


def test_core_worker_images_match_external_tool_profiles(tmp_path: Path) -> None:
    config_root = _synthetic_config_repository(tmp_path / "config")

    assert VERIFIER.validate_worker_images(ROOT, config_root=config_root) == []


def test_every_core_image_has_one_shared_base_and_exact_build_inputs() -> None:
    dockerfile = (ROOT / "docker" / "v5" / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.count("FROM v5-worker-base AS v5-") == 4
    assert "python:3.12.11-slim-bookworm@sha256:" in dockerfile
    assert "ARG CODEX_VERSION=0.144.3" in dockerfile
    assert "ARG SOURCE_DATE_EPOCH=1751328000" in dockerfile
    assert "latest" not in dockerfile.casefold()


def test_build_context_allowlist_excludes_project_material() -> None:
    ignore = (ROOT / "docker" / "v5" / ".dockerignore").read_text(
        encoding="utf-8"
    )

    assert ignore.splitlines()[0] == "*"
    for forbidden in ("src", "config", "examples", ".git", "state", "prompts"):
        assert f"!{forbidden}" not in ignore


def test_manifest_or_secret_regressions_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    (root / "docker").mkdir(parents=True)
    shutil.copytree(ROOT / "docker" / "v5", root / "docker" / "v5")
    manifest_path = root / "docker" / "v5" / "manifests" / "qa.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"].append("unexpected.capability")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (root / "docker" / "v5" / "credential.txt").write_text(
        "client_secret=do-not-commit-this", encoding="utf-8"
    )

    errors = VERIFIER.validate_worker_images(root)

    assert any("capability label differs" in item for item in errors)
    assert any("possible credential material" in item for item in errors)
