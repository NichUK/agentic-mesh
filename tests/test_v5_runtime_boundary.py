from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from agentic_mesh_v5 import __version__
from agentic_mesh_v5.boundary import InvalidPackageRootError
from agentic_mesh_v5.boundary import RuntimeBoundaryError
from agentic_mesh_v5.boundary import find_runtime_boundary_violations
from agentic_mesh_v5.boundary import require_clean_runtime_boundary


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
V5_PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "agentic_mesh_v5"


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(REPOSITORY_ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source_root, environment.get("PYTHONPATH")) if value
    )
    return environment


def test_v5_import_does_not_load_v4_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import agentic_mesh_v5; "
            "assert not any(name.startswith('agentic_mesh_v4') for name in sys.modules)",
        ],
        cwd=REPOSITORY_ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_v5_cli_starts_independently() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agentic_mesh_v5", "--json", "status"],
        cwd=REPOSITORY_ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "runtime": "agentic-mesh-v5",
        "status": "bootstrap-ready",
        "version": __version__,
    }


@pytest.mark.parametrize("path_kind", ["missing", "file"])
def test_v5_boundary_cli_rejects_invalid_package_root(
    tmp_path: Path, path_kind: str
) -> None:
    package_root = tmp_path / path_kind
    if path_kind == "file":
        package_root.write_text("not a package\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh_v5",
            "--json",
            "boundary-check",
            "--package-root",
            str(package_root),
        ],
        cwd=REPOSITORY_ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "error": "package root must be an existing directory",
        "path": str(package_root),
        "runtime": "agentic-mesh-v5",
        "status": "error",
    }

    with pytest.raises(InvalidPackageRootError, match="existing directory"):
        find_runtime_boundary_violations(package_root)


def test_current_v5_source_has_a_clean_runtime_boundary() -> None:
    assert find_runtime_boundary_violations(V5_PACKAGE_ROOT) == ()


def test_boundary_check_honours_python_source_encoding(tmp_path: Path) -> None:
    source = tmp_path / "encoded.py"
    source.write_bytes(
        "# -*- coding: cp1252 -*-\n# £\nimport agentic_mesh_v4.runtime\n".encode(
            "cp1252"
        )
    )

    violations = find_runtime_boundary_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].module == "agentic_mesh_v4.runtime"


@pytest.mark.parametrize(
    "statement,module",
    [
        ("import agentic_mesh_v4.runtime", "agentic_mesh_v4.runtime"),
        ("from agentic_mesh_v4 import runtime", "agentic_mesh_v4"),
        ("from agentic_mesh_v3.worker import Worker", "agentic_mesh_v3.worker"),
    ],
)
def test_boundary_check_rejects_superseded_runtime_imports(
    tmp_path: Path, statement: str, module: str
) -> None:
    source = tmp_path / "coupled.py"
    source.write_text(f"{statement}\n", encoding="utf-8")

    violations = find_runtime_boundary_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].module == module
    with pytest.raises(RuntimeBoundaryError, match="V5 runtime boundary violation"):
        require_clean_runtime_boundary(tmp_path)


def test_v5_console_entry_point_is_declared() -> None:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)

    assert project["project"]["scripts"]["agentic-mesh-v5"] == "agentic_mesh_v5.cli:main"
    assert __version__ == project["project"]["version"]
