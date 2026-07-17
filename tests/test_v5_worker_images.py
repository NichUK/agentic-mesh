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


def test_core_worker_image_contract_is_valid() -> None:
    assert VERIFIER.validate_worker_images(ROOT) == []


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
