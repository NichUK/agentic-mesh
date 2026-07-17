from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh_v5.cli import main
from agentic_mesh_v5.package_resolver import PackageResolutionError
from agentic_mesh_v5.package_resolver import resolve_packages


def _repository(tmp_path: Path) -> Path:
    schema = tmp_path / "schemas" / "package.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    return tmp_path


def _package(
    root: Path,
    reference: str,
    *,
    dependencies: list[str] | None = None,
    json_content: dict[str, object] | None = None,
    text_content: str | None = None,
) -> None:
    path, version = reference.split("@", 1)
    kind, package_id = path.split("/", 1)
    package_root = root / "packages" / kind / package_id / version
    package_root.mkdir(parents=True)
    content: list[str] = []
    if json_content is not None:
        content.append("settings.json")
        (package_root / "settings.json").write_text(
            json.dumps(json_content, sort_keys=True) + "\n", encoding="utf-8"
        )
    if text_content is not None:
        content.append("instructions.md")
        (package_root / "instructions.md").write_text(text_content, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "id": package_id,
        "kind": kind,
        "version": version,
        "content": content,
        "dependencies": dependencies or [],
    }
    (package_root / "package.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )


def _golden_repository(tmp_path: Path) -> Path:
    root = _repository(tmp_path)
    _package(
        root,
        "system/core@1.0.0",
        json_content={"limits": {"idle": 300, "workers": 1}, "source": "system"},
        text_content="Keep the runtime simple.\n",
    )
    _package(
        root,
        "role/developer@1.0.0",
        dependencies=["system/core@1.0.0"],
        json_content={"limits": {"workers": 2}, "role": "developer"},
        text_content="Implement the accepted slice.\n",
    )
    _package(
        root,
        "project-override/acme@1.0.0",
        dependencies=["role/developer@1.0.0"],
        json_content={"limits": {"workers": 3}, "project": "acme"},
    )
    return root


def test_golden_render_applies_precedence_and_records_provenance(tmp_path: Path) -> None:
    root = _golden_repository(tmp_path)

    resolved = resolve_packages(root, ["project-override/acme@1.0.0"])

    assert resolved.packages == (
        "system/core@1.0.0",
        "role/developer@1.0.0",
        "project-override/acme@1.0.0",
    )
    assert resolved.settings == {
        "limits": {"idle": 300, "workers": 3},
        "project": "acme",
        "role": "developer",
        "source": "system",
    }
    assert [section.text for section in resolved.text_sections] == [
        "Keep the runtime simple.\n",
        "Implement the accepted slice.\n",
    ]
    assert [source.package for source in resolved.provenance] == [
        "system/core@1.0.0",
        "system/core@1.0.0",
        "system/core@1.0.0",
        "role/developer@1.0.0",
        "role/developer@1.0.0",
        "role/developer@1.0.0",
        "project-override/acme@1.0.0",
        "project-override/acme@1.0.0",
    ]
    assert [source.path for source in resolved.provenance] == [
        "packages/system/core/1.0.0/package.json",
        "packages/system/core/1.0.0/settings.json",
        "packages/system/core/1.0.0/instructions.md",
        "packages/role/developer/1.0.0/package.json",
        "packages/role/developer/1.0.0/settings.json",
        "packages/role/developer/1.0.0/instructions.md",
        "packages/project-override/acme/1.0.0/package.json",
        "packages/project-override/acme/1.0.0/settings.json",
    ]
    assert all(len(source.sha256) == 64 for source in resolved.provenance)
    assert resolved.digest == "b303f0e381a6664f0bcc0f22434d09d46edace808e670ce4a375d57a09bd7867"


def test_resolution_is_independent_of_root_order_and_clone_location(tmp_path: Path) -> None:
    first = _golden_repository(tmp_path / "first")
    second = _golden_repository(tmp_path / "second")

    first_result = resolve_packages(
        first, ["role/developer@1.0.0", "project-override/acme@1.0.0"]
    )
    second_result = resolve_packages(
        second, ["project-override/acme@1.0.0", "role/developer@1.0.0"]
    )

    assert first_result.to_dict() == second_result.to_dict()


def test_missing_dependency_and_cycle_are_rejected(tmp_path: Path) -> None:
    missing_root = _repository(tmp_path / "missing")
    _package(
        missing_root,
        "role/developer@1.0.0",
        dependencies=["system/missing@1.0.0"],
        text_content="Developer\n",
    )
    with pytest.raises(PackageResolutionError, match="package not found"):
        resolve_packages(missing_root, ["role/developer@1.0.0"])

    cycle_root = _repository(tmp_path / "cycle")
    _package(
        cycle_root,
        "system/one@1.0.0",
        dependencies=["system/two@1.0.0"],
        text_content="One\n",
    )
    _package(
        cycle_root,
        "system/two@1.0.0",
        dependencies=["system/one@1.0.0"],
        text_content="Two\n",
    )
    with pytest.raises(PackageResolutionError, match="dependency cycle"):
        resolve_packages(cycle_root, ["system/one@1.0.0"])


def test_content_cannot_escape_package_directory(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    package_root = root / "packages" / "system" / "core" / "1.0.0"
    package_root.mkdir(parents=True)
    outside = package_root.parent / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "core",
                "kind": "system",
                "version": "1.0.0",
                "content": ["../outside.md"],
                "dependencies": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PackageResolutionError, match="invalid content path"):
        resolve_packages(root, ["system/core@1.0.0"])


def test_cli_emits_resolved_configuration_and_rejects_invalid_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _golden_repository(tmp_path)
    code = main(
        [
            "--json",
            "resolve-config",
            "--config-root",
            str(root),
            "--package",
            "project-override/acme@1.0.0",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["status"] == "resolved"
    assert output["digest"] == resolve_packages(
        root, ["project-override/acme@1.0.0"]
    ).digest

    code = main(
        [
            "--json",
            "resolve-config",
            "--config-root",
            str(root),
            "--package",
            "latest",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["status"] == "rejected"
    assert "invalid package reference" in output["error"]
