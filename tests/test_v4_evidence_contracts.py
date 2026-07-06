from __future__ import annotations

import shutil
from pathlib import Path

from agentic_mesh_v4.evidence_contracts import load_evidence_contracts


def test_load_evidence_contracts_resolves_from_system_root_when_cwd_has_no_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    system_root = tmp_path / "system"
    flow_dir = system_root / "config" / "flows"
    flow_dir.mkdir(parents=True)
    source_flow_dir = Path(__file__).resolve().parents[1] / "config" / "flows"
    shutil.copyfile(source_flow_dir / "sdlc.yaml", flow_dir / "sdlc.yaml")
    shutil.copyfile(source_flow_dir / "evidence-contracts.yaml", flow_dir / "evidence-contracts.yaml")
    cwd = tmp_path / "agent-workspace"
    cwd.mkdir()

    monkeypatch.chdir(cwd)
    monkeypatch.setenv("AGENTIC_MESH_SYSTEM_ROOT", str(system_root))

    contracts = load_evidence_contracts()

    assert contracts
