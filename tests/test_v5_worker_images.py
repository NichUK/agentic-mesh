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
        if profile_id == "recovery":
            profile["tool_profile"]["launch_policy"] = {
                "normal_routing": False,
                "allowed_launchers": ["recovery-supervisor"],
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


def test_worker_image_contract_is_valid() -> None:
    assert VERIFIER.validate_worker_images(ROOT) == []


def test_worker_images_match_external_tool_profiles(tmp_path: Path) -> None:
    config_root = _synthetic_config_repository(tmp_path / "config")

    assert VERIFIER.validate_worker_images(ROOT, config_root=config_root) == []


def test_every_worker_image_has_one_shared_base_and_exact_build_inputs() -> None:
    dockerfile = (ROOT / "docker" / "v5" / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.count("FROM v5-worker-base AS v5-") == 6
    assert "python:3.12.11-slim-bookworm@sha256:" in dockerfile
    assert "ARG CODEX_VERSION=0.144.4" in dockerfile
    assert "ARG SOURCE_DATE_EPOCH=1751328000" in dockerfile
    assert "latest" not in dockerfile.casefold()


def test_ux_image_pins_and_exercises_specialist_tools() -> None:
    dockerfile = (ROOT / "docker" / "v5" / "Dockerfile").read_text(encoding="utf-8")
    ux_stage = VERIFIER._dockerfile_stage(dockerfile, "v5-ux-worker", [])
    smoke = (ROOT / "docker" / "v5" / "ux-smoke-test.mjs").read_text(
        encoding="utf-8"
    )

    assert "ARG PLAYWRIGHT_VERSION=1.61.1" in dockerfile
    assert "ARG AXE_PLAYWRIGHT_VERSION=4.12.1" in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile
    assert "--mount=type=cache,target=/root/.npm" in ux_stage
    assert "agentic-mesh-ux-smoke" in dockerfile
    for operation in (
        "AxeBuilder",
        "page.screenshot",
        "page.pdf",
        "pixelmatch",
        "spawnSync",
        'execFileSync("identify"',
        '"compare",',
        'execFileSync("pdfinfo"',
    ):
        assert operation in smoke
    assert "baselineScreenshot" in smoke
    assert "candidateScreenshot" in smoke
    assert "accessibility.violations.length > 0" in smoke
    assert "changedPixels > 0" in smoke
    assert "imageMagickChangedPixels > 0" in smoke


def test_build_context_allowlist_excludes_project_material() -> None:
    ignore = (ROOT / "docker" / "v5" / ".dockerignore").read_text(
        encoding="utf-8"
    )

    assert ignore.splitlines()[0] == "*"
    for forbidden in ("src", "config", "examples", ".git", "state", "prompts"):
        assert f"!{forbidden}" not in ignore


def test_recovery_image_is_supervisor_only_and_drills_real_repair() -> None:
    dockerfile = (ROOT / "docker" / "v5" / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker" / "v5" / "compose.yaml").read_text(encoding="utf-8")
    drill = (ROOT / "docker" / "v5" / "recovery-drill.py").read_text(
        encoding="utf-8"
    )
    stage = VERIFIER._dockerfile_stage(dockerfile, "v5-recovery-worker", [])

    assert "ARG COMPOSE_VERSION=v5.3.1" in stage
    assert "ARG COMPOSE_SHA256=f9ebc6eb" in stage
    assert 'io.agentic-mesh.normal-routing="disabled"' in stage
    assert 'io.agentic-mesh.allowed-launcher="recovery-supervisor"' in stage
    assert 'profiles: ["recovery-supervisor"]' in compose
    for operation in (
        '"build"',
        '"inspect"',
        '"restart"',
        '"--force-recreate"',
        '"down"',
        'evidence["status"] = "passed"',
    ):
        assert operation in drill


def test_operations_and_recovery_images_include_native_postgres_tools() -> None:
    dockerfile = (ROOT / "docker" / "v5" / "Dockerfile").read_text(encoding="utf-8")
    operations = VERIFIER._dockerfile_stage(dockerfile, "v5-operations-worker", [])
    recovery = VERIFIER._dockerfile_stage(dockerfile, "v5-recovery-worker", [])

    for stage in (operations, recovery):
        assert "ARG POSTGRES_CLIENT_MAJOR=17" in stage
        assert '"postgresql-client-${POSTGRES_CLIENT_MAJOR}"' in stage
        assert "PGDG_SIGNING_SHA256=0144068502a1eddd" in stage
        assert "sha256sum --check --status" in stage
    for profile_id in ("operations", "recovery"):
        manifest = json.loads(
            (ROOT / "docker" / "v5" / "manifests" / f"{profile_id}.json").read_text(
                encoding="utf-8"
            )
        )
        assert {"pg_dump", "pg_restore", "psql"} <= set(
            manifest["required_commands"]
        )


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


def test_labels_are_validated_within_their_target_stage(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    (root / "docker").mkdir(parents=True)
    shutil.copytree(ROOT / "docker" / "v5", root / "docker" / "v5")
    dockerfile_path = root / "docker" / "v5" / "Dockerfile"
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    general_label = 'io.agentic-mesh.profile="general"'
    development_label = 'io.agentic-mesh.profile="development"'
    dockerfile = dockerfile.replace(general_label, "profile-swap-placeholder")
    dockerfile = dockerfile.replace(development_label, general_label)
    dockerfile = dockerfile.replace("profile-swap-placeholder", development_label)
    dockerfile_path.write_text(dockerfile, encoding="utf-8")

    errors = VERIFIER.validate_worker_images(root)

    assert "general: Dockerfile profile label is missing" in errors
    assert "development: Dockerfile profile label is missing" in errors
