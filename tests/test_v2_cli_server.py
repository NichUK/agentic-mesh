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
    assert snapshot["releases"][0]["status"] == "no_deployment_disposition"


def test_v2_cli_status_json_reports_runtime_state(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "v2.sqlite3"

    assert main(["--db", str(db_path), "init-db"]) == 0
    assert main(["--db", str(db_path), "status-json"]) == 0

    output = capsys.readouterr().out
    assert '"queue_items": 0' in output
    assert '"work_items": 0' in output


def test_v2_cli_recovers_stale_assignments_for_one_role(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "v2.sqlite3"
    db = V2Database(db_path)
    try:
        db.migrate()
        for role_id in ("product-manager", "engineering"):
            db.create_role_assignment(
                assignment_id=f"assignment-cli-stale-{role_id}",
                role_id=role_id,
                source_ref=f"msg-cli-{role_id}",
                title=f"CLI stale {role_id}",
                summary="Exercise operator stale recovery.",
                assignment_type="work_item_handoff",
                visibility_scope="project",
                payload={},
            )
            assert db.claim_role_assignment(
                role_id=role_id,
                role_instance_id=f"agentic-mesh-dev.{role_id}.old",
                lease_seconds=60,
            )
        with db.connection:
            db.connection.execute(
                """
                UPDATE role_assignments
                SET claim_expires_at = '2000-01-01 00:00:00'
                """
            )
    finally:
        db.close()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "recover-stale-assignments",
                "--role-id",
                "product-manager",
                "--reason",
                "Operator recovery test.",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"recovered_count": 1' in output
    assert '"assignment-cli-stale-product-manager"' in output
    db = V2Database(db_path)
    try:
        product = db.get_role_assignment("assignment-cli-stale-product-manager")
        engineering = db.get_role_assignment("assignment-cli-stale-engineering")
    finally:
        db.close()

    assert product is not None
    assert engineering is not None
    assert product["status"] == "queued"
    assert product["failure_reason"] == "Operator recovery test."
    assert product["recovery_count"] == 1
    assert engineering["status"] == "claimed"


def test_v2_cli_recovery_respects_limit(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "v2.sqlite3"
    db = V2Database(db_path)
    try:
        db.migrate()
        for index in range(2):
            db.create_role_assignment(
                assignment_id=f"assignment-cli-limit-{index}",
                role_id="product-manager",
                source_ref=f"msg-cli-limit-{index}",
                title=f"CLI limit {index}",
                summary="Exercise bounded operator stale recovery.",
                assignment_type="work_item_handoff",
                visibility_scope="project",
                payload={},
            )
            assert db.claim_role_assignment(
                role_id="product-manager",
                role_instance_id=f"agentic-mesh-dev.product-manager.{index}",
                lease_seconds=60,
            )
        with db.connection:
            db.connection.execute(
                """
                UPDATE role_assignments
                SET claim_expires_at = '2000-01-01 00:00:00'
                """
            )
    finally:
        db.close()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "recover-stale-assignments",
                "--role-id",
                "product-manager",
                "--limit",
                "1",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"recovered_count": 1' in output
    db = V2Database(db_path)
    try:
        statuses = [row["status"] for row in db.list_role_assignments()]
    finally:
        db.close()

    assert statuses.count("queued") == 1
    assert statuses.count("claimed") == 1


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
