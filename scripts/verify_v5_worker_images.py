from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentic_mesh_v5.tool_profiles import ToolProfileError  # noqa: E402
from agentic_mesh_v5.tool_profiles import ToolProfileRegistry  # noqa: E402


PROFILE_IDS = ("general", "development", "qa", "operations", "ux")
MANIFEST_FIELDS = {
    "schema_version",
    "profile_id",
    "image",
    "target",
    "tool_profile_reference",
    "capabilities",
    "required_commands",
}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(
        rb"['\"]?(?:password|passwd|client_secret|access_token)['\"]?\s*[:=]\s*['\"]?[^\s,'\"}]+",
        re.IGNORECASE,
    ),
)


def _load_manifest(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{path.name}: invalid image manifest: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{path.name}: image manifest must be an object")
        return None
    return value


def _string_list(value: object, label: str, errors: list[str]) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        errors.append(f"{label} must be a non-empty unique string list")
        return []
    return value


def _dockerfile_stage(
    dockerfile_text: str, target: str, errors: list[str]
) -> str:
    match = re.search(
        rf"^FROM\s+[^\r\n]+\s+AS\s+{re.escape(target)}\s*$.*?(?=^FROM\s|\Z)",
        dockerfile_text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        errors.append(f"{target}: Dockerfile target is missing")
        return ""
    return match.group(0)


def _validate_external_profile(
    manifest: dict[str, Any], config_root: Path, errors: list[str]
) -> None:
    reference = manifest.get("tool_profile_reference")
    if not isinstance(reference, str):
        errors.append(f"{manifest.get('profile_id')}: missing tool-profile reference")
        return
    try:
        profile = ToolProfileRegistry(config_root).load(reference)
    except ToolProfileError as exc:
        errors.append(f"{manifest.get('profile_id')}: external profile failed: {exc}")
        return
    expected_image = f"{profile.image.repository}:{profile.image.tag}"
    if manifest.get("profile_id") != profile.profile_id:
        errors.append(f"{profile.profile_id}: embedded and external profile ids differ")
    if manifest.get("image") != expected_image:
        errors.append(f"{profile.profile_id}: embedded and external image names differ")
    if set(manifest.get("capabilities", [])) != profile.capability_ids:
        errors.append(f"{profile.profile_id}: embedded and external capabilities differ")
    if profile.health.command != ("agentic-mesh-worker-healthcheck",):
        errors.append(f"{profile.profile_id}: external health command is incompatible")


def validate_worker_images(root: Path, config_root: Path | None = None) -> list[str]:
    errors: list[str] = []
    context = root / "docker" / "v5"
    dockerfile = context / "Dockerfile"
    try:
        dockerfile_text = dockerfile.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"could not read V5 Dockerfile: {exc}"]
    if re.search(
        r"^ARG PYTHON_BASE_IMAGE=[^\s]+@sha256:[0-9a-f]{64}$",
        dockerfile_text,
        re.MULTILINE,
    ) is None:
        errors.append("shared Python base must be pinned by sha256 digest")
    if (
        re.search(
            r"^ARG CODEX_VERSION=[0-9]+\.[0-9]+\.[0-9]+$",
            dockerfile_text,
            re.MULTILINE,
        )
        is None
    ):
        errors.append("Codex CLI must use an exact semantic version")
    if (
        re.search(
            r"^ARG SOURCE_DATE_EPOCH=[0-9]+$", dockerfile_text, re.MULTILINE
        )
        is None
    ):
        errors.append("worker image creation time must use a fixed SOURCE_DATE_EPOCH")
    if "FROM v5-worker-base" not in dockerfile_text:
        errors.append("worker targets must share v5-worker-base")

    expected_ignore = [
        "*",
        "!Dockerfile",
        "!worker-healthcheck.py",
        "!ux-smoke-test.mjs",
        "!manifests/",
        "!manifests/*.json",
    ]
    try:
        actual_ignore = [
            line.strip()
            for line in (context / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError) as exc:
        actual_ignore = []
        errors.append(f"could not read V5 .dockerignore: {exc}")
    if actual_ignore != expected_ignore:
        errors.append("V5 build context allowlist does not match the project-neutral contract")

    try:
        compose_text = (context / "compose.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        compose_text = ""
        errors.append(f"could not read V5 Compose file: {exc}")
    if "provenance: false" not in compose_text or "SOURCE_DATE_EPOCH:" not in compose_text:
        errors.append("Compose must disable volatile provenance and pass SOURCE_DATE_EPOCH")
    manifests: dict[str, dict[str, Any]] = {}
    for profile_id in PROFILE_IDS:
        path = context / "manifests" / f"{profile_id}.json"
        manifest = _load_manifest(path, errors)
        if manifest is None:
            continue
        manifests[profile_id] = manifest
        if set(manifest) != MANIFEST_FIELDS or manifest.get("schema_version") != 1:
            errors.append(f"{profile_id}: fields do not match the image manifest contract")
            continue
        if manifest.get("profile_id") != profile_id:
            errors.append(f"{profile_id}: manifest identity does not match its filename")
        image = manifest.get("image")
        target = manifest.get("target")
        if not isinstance(image, str) or not image.endswith(":0.1.0"):
            errors.append(f"{profile_id}: image must use the 0.1.0 tag")
        stage_text = (
            _dockerfile_stage(dockerfile_text, target, errors)
            if isinstance(target, str)
            else ""
        )
        if isinstance(image, str) and f"image: {image}" not in compose_text:
            errors.append(f"{profile_id}: Compose image is missing")
        if isinstance(target, str) and f"target: {target}" not in compose_text:
            errors.append(f"{profile_id}: Compose target is missing")
        capabilities = _string_list(
            manifest.get("capabilities"), f"{profile_id}: capabilities", errors
        )
        _string_list(
            manifest.get("required_commands"),
            f"{profile_id}: required_commands",
            errors,
        )
        label = ",".join(sorted(capabilities))
        if f'io.agentic-mesh.profile="{profile_id}"' not in stage_text:
            errors.append(f"{profile_id}: Dockerfile profile label is missing")
        if f'io.agentic-mesh.capabilities="{label}"' not in stage_text:
            errors.append(f"{profile_id}: Dockerfile capability label differs")
        if config_root is not None:
            _validate_external_profile(manifest, config_root, errors)

    for path in sorted(context.rglob("*")):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if any(pattern.search(payload) for pattern in SECRET_PATTERNS):
            errors.append(f"{path.relative_to(root)}: possible credential material")
    return errors


def inspect_built_images(root: Path) -> list[str]:
    errors: list[str] = []
    context = root / "docker" / "v5"
    for profile_id in PROFILE_IDS:
        manifest = json.loads(
            (context / "manifests" / f"{profile_id}.json").read_text(encoding="utf-8")
        )
        image = manifest["image"]
        try:
            inspected = json.loads(
                subprocess.run(
                    ["docker", "image", "inspect", image],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )[0]
        except (
            OSError,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
            IndexError,
        ) as exc:
            errors.append(f"{profile_id}: could not inspect built image: {exc}")
            continue
        labels = inspected.get("Config", {}).get("Labels", {}) or {}
        if labels.get("io.agentic-mesh.profile") != profile_id:
            errors.append(f"{profile_id}: built image profile label differs")
        expected_capabilities = ",".join(sorted(manifest["capabilities"]))
        if labels.get("io.agentic-mesh.capabilities") != expected_capabilities:
            errors.append(f"{profile_id}: built image capability label differs")
        inspect_payload = json.dumps(inspected.get("Config", {})).encode("utf-8")
        if any(pattern.search(inspect_payload) for pattern in SECRET_PATTERNS):
            errors.append(
                f"{profile_id}: built image configuration contains credential material"
            )
        health = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "agentic-mesh-worker-healthcheck", image],
            check=False,
            capture_output=True,
            text=True,
        )
        if health.returncode != 0:
            errors.append(
                f"{profile_id}: built image healthcheck failed: "
                f"{health.stdout}{health.stderr}"
            )
        embedded = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "cat",
                image,
                "/opt/agentic-mesh/image-manifest.json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            embedded_manifest = json.loads(embedded.stdout)
        except json.JSONDecodeError:
            embedded_manifest = None
        if embedded.returncode != 0 or embedded_manifest != manifest:
            errors.append(f"{profile_id}: embedded image manifest differs")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--inspect-images", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    errors = validate_worker_images(
        root, args.config_root.resolve() if args.config_root else None
    )
    if args.inspect_images:
        errors.extend(inspect_built_images(root))
    if errors:
        print(json.dumps({"status": "rejected", "errors": errors}, indent=2))
        return 2
    print(json.dumps({"status": "valid", "profiles": list(PROFILE_IDS)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
