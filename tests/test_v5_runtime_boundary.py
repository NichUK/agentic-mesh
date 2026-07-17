from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

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
    assert '"runtime": "agentic-mesh-v5"' in result.stdout
    assert '"status": "bootstrap-ready"' in result.stdout


def test_current_v5_source_has_a_clean_runtime_boundary() -> None:
    assert find_runtime_boundary_violations(V5_PACKAGE_ROOT) == ()


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
    project = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'agentic-mesh-v5 = "agentic_mesh_v5.cli:main"' in project
