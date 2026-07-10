from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentic_mesh_v4.documents import _atomic_write_text


@pytest.mark.skipif(os.name == "nt", reason="POSIX document modes are enforced in Linux deployments")
def test_atomic_document_write_is_editable_by_the_shared_library_group(tmp_path: Path) -> None:
    target = tmp_path / "artifact.md"

    _atomic_write_text(target, "# Evidence\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o666
