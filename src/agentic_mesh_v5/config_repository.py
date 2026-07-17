from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class ConfigRepository:
    repository_url: str
    default_ref: str
    mount_path: PurePosixPath
    secret_resolution: str


class ConfigRepositoryError(ValueError):
    pass


def load_config_repository(path: Path) -> ConfigRepository:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigRepositoryError(f"invalid configuration repository record: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigRepositoryError("configuration repository record must be a JSON object")
    required = {"repository_url", "default_ref", "mount_path", "secret_resolution"}
    missing = sorted(required - payload.keys())
    if missing:
        raise ConfigRepositoryError(f"missing configuration repository fields: {missing}")
    repository_url = str(payload["repository_url"])
    parsed = urlparse(repository_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or not repository_url.endswith(".git")
    ):
        raise ConfigRepositoryError("repository_url must be an HTTPS Git URL")
    default_ref = str(payload["default_ref"]).strip()
    if not default_ref:
        raise ConfigRepositoryError("default_ref must not be empty")
    mount_path = PurePosixPath(str(payload["mount_path"]))
    if not mount_path.is_absolute():
        raise ConfigRepositoryError("mount_path must be an absolute container path")
    secret_resolution = str(payload["secret_resolution"])
    if secret_resolution != "external-references-only":
        raise ConfigRepositoryError("configuration repositories cannot contain resolved secrets")
    return ConfigRepository(
        repository_url=repository_url,
        default_ref=default_ref,
        mount_path=mount_path,
        secret_resolution=secret_resolution,
    )


def validate_mounted_repository(root: Path) -> None:
    if not root.is_dir():
        raise ConfigRepositoryError(f"configuration mount is not a directory: {root}")
    if not (root / "schemas" / "package.schema.json").is_file():
        raise ConfigRepositoryError("configuration mount has no package schema")
    if not any((root / "packages").glob("*/*/*/package.json")):
        raise ConfigRepositoryError("configuration mount has no versioned packages")
