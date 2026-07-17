from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh_v5.config_repository import ConfigRepositoryError
from agentic_mesh_v5.config_repository import load_config_repository
from agentic_mesh_v5.config_repository import validate_mounted_repository


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "v5" / "organization-config-repository.json"


def test_v5_references_the_external_secret_free_configuration_repository() -> None:
    repository = load_config_repository(REGISTRY)

    assert repository.repository_url == "https://github.com/NichUK/agentic-mesh-config.git"
    assert repository.default_ref == "develop"
    assert repository.mount_path.as_posix() == "/mesh/config"
    assert repository.secret_resolution == "external-references-only"


def test_mounted_repository_requires_schema_and_versioned_packages(tmp_path: Path) -> None:
    with pytest.raises(ConfigRepositoryError, match="package schema"):
        validate_mounted_repository(tmp_path)
    schema = tmp_path / "schemas" / "package.schema.json"
    schema.parent.mkdir()
    schema.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ConfigRepositoryError, match="versioned packages"):
        validate_mounted_repository(tmp_path)
    package = tmp_path / "packages" / "system" / "core" / "0.1.0" / "package.json"
    package.parent.mkdir(parents=True)
    package.mkdir()
    with pytest.raises(ConfigRepositoryError, match="versioned packages"):
        validate_mounted_repository(tmp_path)
    package.rmdir()
    package.write_text("{}\n", encoding="utf-8")

    validate_mounted_repository(tmp_path)


def test_repository_record_rejects_resolved_secret_mode(tmp_path: Path) -> None:
    payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
    payload["secret_resolution"] = "embedded"
    record = tmp_path / "repository.json"
    record.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigRepositoryError, match="cannot contain resolved secrets"):
        load_config_repository(record)


def test_repository_record_must_be_an_object(tmp_path: Path) -> None:
    record = tmp_path / "repository.json"
    record.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ConfigRepositoryError, match="must be a JSON object"):
        load_config_repository(record)


def test_repository_url_cannot_embed_credentials(tmp_path: Path) -> None:
    payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
    payload["repository_url"] = "https://token@github.com/NichUK/agentic-mesh-config.git"
    record = tmp_path / "repository.json"
    record.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigRepositoryError, match="must be an HTTPS Git URL"):
        load_config_repository(record)
