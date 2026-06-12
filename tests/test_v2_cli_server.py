from pathlib import Path

from agentic_mesh_v2.cli import main
from agentic_mesh_v2.db import V2Database


def test_v2_cli_demo_slice_creates_closed_release(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "v2.sqlite3"

    assert main(["--db", str(db_path), "demo-slice"]) == 0
    output = capsys.readouterr().out
    assert "work-v2-demo-slice" in output

    db = V2Database(db_path)
    try:
        snapshot = db.status_snapshot()
    finally:
        db.close()

    assert snapshot["counts"]["work_items"] == 1
    assert snapshot["counts"]["releases"] == 1
    assert snapshot["work_items"][0]["state"] == "closed"
    assert snapshot["releases"][0]["status"] == "deployed"


def test_v2_cli_status_json_reports_runtime_state(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "v2.sqlite3"

    assert main(["--db", str(db_path), "init-db"]) == 0
    assert main(["--db", str(db_path), "status-json"]) == 0

    output = capsys.readouterr().out
    assert '"queue_items": 0' in output
    assert '"work_items": 0' in output


def test_v2_cli_validate_topology_accepts_distinct_roots(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    deployed = tmp_path / "deployed"
    state = tmp_path / "state"
    project = tmp_path / "project"
    docs = project / "docs"
    for path in (source, deployed, state, docs):
        path.mkdir(parents=True)

    assert (
        main(
            [
                "validate-topology",
                "--source-repo",
                str(source),
                "--deployed-runtime",
                str(deployed),
                "--runtime-state",
                str(state),
                "--project-repo",
                f"demo={project}|{docs}",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"status": "ok"' in output
    assert '"repo_id": "demo"' in output
