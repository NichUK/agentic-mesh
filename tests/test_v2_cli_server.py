import json
import sys
from pathlib import Path

import pytest

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


def test_v2_cli_run_role_service_tick_processes_assignment_from_safe_output_file(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    safe_output_path = tmp_path / "calls.json"
    safe_output_path.write_text(
        """
        {
          "calls": [
            {
              "tool_name": "status.complete",
              "payload": {"message": "Assignment complete from file worker."},
              "terminal": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    db = V2Database(db_path)
    try:
        db.migrate()
        db.create_role_assignment(
            assignment_id="assignment-cli-runner",
            role_id="product-manager",
            source_ref="msg-cli-runner",
            title="CLI runner assignment",
            summary="Run a role-service tick through a worker adapter.",
            assignment_type="direct_conversation",
            visibility_scope="project",
            payload={},
        )
    finally:
        db.close()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "run-role-service-tick",
                "--role-id",
                "product-manager",
                "--role-instance-id",
                "agentic-mesh-dev.product-manager.1",
                "--worker",
                "safe-output-file",
                "--safe-output-file",
                str(safe_output_path),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"processed_count": 1' in output
    assert '"terminal_tool": "status.complete"' in output
    db = V2Database(db_path)
    try:
        assignment = db.get_role_assignment("assignment-cli-runner")
        snapshot = db.status_snapshot()
    finally:
        db.close()

    assert assignment is not None
    assert assignment["status"] == "completed"
    assert assignment["terminal_tool"] == "status.complete"
    assert snapshot["role_instance_statuses"][0]["status"] == "idle"


def test_v2_cli_run_role_service_tick_rejects_mismatched_safe_output_role(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    safe_output_path = tmp_path / "calls.json"
    safe_output_path.write_text(
        """
        {
          "calls": [
            {
              "role_id": "engineering",
              "tool_name": "status.complete",
              "payload": {"message": "Wrong role."},
              "terminal": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    db = V2Database(db_path)
    try:
        db.migrate()
        db.create_role_assignment(
            assignment_id="assignment-cli-runner-mismatch",
            role_id="product-manager",
            source_ref="msg-cli-runner-mismatch",
            title="CLI runner mismatch",
            summary="Reject mismatched role output.",
            assignment_type="direct_conversation",
            visibility_scope="project",
            payload={},
        )
    finally:
        db.close()

    with pytest.raises(ValueError, match="does not match assignment role"):
        main(
            [
                "--db",
                str(db_path),
                "run-role-service-tick",
                "--role-id",
                "product-manager",
                "--role-instance-id",
                "agentic-mesh-dev.product-manager.1",
                "--worker",
                "safe-output-file",
                "--safe-output-file",
                str(safe_output_path),
            ]
        )

    db = V2Database(db_path)
    try:
        assignment = db.get_role_assignment("assignment-cli-runner-mismatch")
    finally:
        db.close()

    assert assignment is not None
    assert assignment["status"] == "failed"
    assert "does not match assignment role" in assignment["failure_reason"]


def test_v2_cli_run_role_service_tick_rejects_malformed_safe_output_file(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    safe_output_path = tmp_path / "calls.json"
    safe_output_path.write_text('{"calls": {"not": "a list"}}', encoding="utf-8")
    db = V2Database(db_path)
    try:
        db.migrate()
        db.create_role_assignment(
            assignment_id="assignment-cli-runner-malformed",
            role_id="product-manager",
            source_ref="msg-cli-runner-malformed",
            title="CLI runner malformed",
            summary="Reject malformed worker output.",
            assignment_type="direct_conversation",
            visibility_scope="project",
            payload={},
        )
    finally:
        db.close()

    with pytest.raises(ValueError, match="calls list"):
        main(
            [
                "--db",
                str(db_path),
                "run-role-service-tick",
                "--role-id",
                "product-manager",
                "--role-instance-id",
                "agentic-mesh-dev.product-manager.1",
                "--worker",
                "safe-output-file",
                "--safe-output-file",
                str(safe_output_path),
            ]
        )

    db = V2Database(db_path)
    try:
        assignment = db.get_role_assignment("assignment-cli-runner-malformed")
    finally:
        db.close()

    assert assignment is not None
    assert assignment["status"] == "failed"
    assert "calls list" in assignment["failure_reason"]


def test_v2_cli_run_role_service_tick_processes_assignment_from_subprocess_worker(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    worker_path = tmp_path / "worker.py"
    worker_path.write_text(
        """
import json
import sys

assignment = json.loads(sys.stdin.read())
assert assignment["assignment_id"] == "assignment-cli-subprocess"
print(json.dumps({
    "calls": [
        {
            "tool_name": "status.complete",
            "payload": {"message": f"Processed {assignment['title']}"},
            "terminal": True,
        }
    ]
}))
""",
        encoding="utf-8",
    )
    db = V2Database(db_path)
    try:
        db.migrate()
        db.create_role_assignment(
            assignment_id="assignment-cli-subprocess",
            role_id="product-manager",
            source_ref="msg-cli-subprocess",
            title="CLI subprocess assignment",
            summary="Run a role-service tick through a subprocess worker adapter.",
            assignment_type="direct_conversation",
            visibility_scope="project",
            payload={},
        )
    finally:
        db.close()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "run-role-service-tick",
                "--role-id",
                "product-manager",
                "--role-instance-id",
                "agentic-mesh-dev.product-manager.1",
                "--worker",
                "safe-output-subprocess",
                "--worker-command-json",
                json.dumps([sys.executable, str(worker_path)]),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"processed_count": 1' in output
    assert '"terminal_tool": "status.complete"' in output
    db = V2Database(db_path)
    try:
        assignment = db.get_role_assignment("assignment-cli-subprocess")
        safe_outputs = db.list_safe_output_calls()
    finally:
        db.close()

    assert assignment is not None
    assert assignment["status"] == "completed"
    assert safe_outputs[0]["payload"]["message"] == "Processed CLI subprocess assignment"


def test_v2_cli_run_role_service_tick_records_subprocess_failure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    worker_path = tmp_path / "worker.py"
    worker_path.write_text(
        """
import sys

print("worker exploded", file=sys.stderr)
raise SystemExit(17)
""",
        encoding="utf-8",
    )
    db = V2Database(db_path)
    try:
        db.migrate()
        db.create_role_assignment(
            assignment_id="assignment-cli-subprocess-fail",
            role_id="product-manager",
            source_ref="msg-cli-subprocess-fail",
            title="CLI subprocess failure",
            summary="Record subprocess worker failure.",
            assignment_type="direct_conversation",
            visibility_scope="project",
            payload={},
        )
    finally:
        db.close()

    with pytest.raises(RuntimeError, match="exited with code 17"):
        main(
            [
                "--db",
                str(db_path),
                "run-role-service-tick",
                "--role-id",
                "product-manager",
                "--role-instance-id",
                "agentic-mesh-dev.product-manager.1",
                "--worker",
                "safe-output-subprocess",
                "--worker-command-json",
                json.dumps([sys.executable, str(worker_path)]),
            ]
        )

    db = V2Database(db_path)
    try:
        assignment = db.get_role_assignment("assignment-cli-subprocess-fail")
    finally:
        db.close()

    assert assignment is not None
    assert assignment["status"] == "failed"
    assert "exited with code 17" in assignment["failure_reason"]
    assert "worker exploded" in assignment["failure_reason"]


def test_v2_cli_run_role_service_tick_records_subprocess_invalid_json(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    worker_path = tmp_path / "worker.py"
    worker_path.write_text('print("not-json")', encoding="utf-8")
    db = V2Database(db_path)
    try:
        db.migrate()
        db.create_role_assignment(
            assignment_id="assignment-cli-subprocess-invalid-json",
            role_id="product-manager",
            source_ref="msg-cli-subprocess-invalid-json",
            title="CLI subprocess invalid JSON",
            summary="Record invalid subprocess worker output.",
            assignment_type="direct_conversation",
            visibility_scope="project",
            payload={},
        )
    finally:
        db.close()

    with pytest.raises(ValueError, match="invalid JSON"):
        main(
            [
                "--db",
                str(db_path),
                "run-role-service-tick",
                "--role-id",
                "product-manager",
                "--role-instance-id",
                "agentic-mesh-dev.product-manager.1",
                "--worker",
                "safe-output-subprocess",
                "--worker-command-json",
                json.dumps([sys.executable, str(worker_path)]),
            ]
        )

    db = V2Database(db_path)
    try:
        assignment = db.get_role_assignment("assignment-cli-subprocess-invalid-json")
    finally:
        db.close()

    assert assignment is not None
    assert assignment["status"] == "failed"
    assert "invalid JSON" in assignment["failure_reason"]


def test_v2_cli_run_role_service_tick_loads_worker_from_project_file(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    project_dir = tmp_path / "agentic-mesh"
    project_dir.mkdir()
    calls_path = project_dir / "calls.json"
    calls_path.write_text(
        """
        {
          "calls": [
            {
              "tool_name": "status.complete",
              "payload": {"message": "Project-configured worker complete."},
              "terminal": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    project_file = project_dir / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )
    db = V2Database(db_path)
    try:
        db.migrate()
        db.create_role_assignment(
            assignment_id="assignment-cli-project-worker",
            role_id="product-manager",
            source_ref="msg-cli-project-worker",
            title="CLI project worker",
            summary="Run a role-service tick using project worker config.",
            assignment_type="direct_conversation",
            visibility_scope="project",
            payload={},
        )
    finally:
        db.close()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "run-role-service-tick",
                "--role-id",
                "product-manager",
                "--role-instance-id",
                "agentic-mesh-dev.product-manager.1",
                "--project-file",
                str(project_file),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"processed_count": 1' in output
    db = V2Database(db_path)
    try:
        assignment = db.get_role_assignment("assignment-cli-project-worker")
        safe_outputs = db.list_safe_output_calls()
    finally:
        db.close()

    assert assignment is not None
    assert assignment["status"] == "completed"
    assert safe_outputs[0]["payload"]["message"] == "Project-configured worker complete."


def test_v2_cli_project_file_does_not_fake_unsupported_codex_adapter(tmp_path: Path) -> None:
    db_path = tmp_path / "v2.sqlite3"
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
roles:
  product-manager:
    worker:
      adapter: codex-cli
      model: codex
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported worker adapter `codex-cli`"):
        main(
            [
                "--db",
                str(db_path),
                "run-role-service-tick",
                "--role-id",
                "product-manager",
                "--role-instance-id",
                "agentic-mesh-dev.product-manager.1",
                "--project-file",
                str(project_file),
            ]
        )


def test_v2_cli_runs_project_role_services_once_for_configured_instances(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    project_dir = tmp_path / "agentic-mesh"
    project_dir.mkdir()
    calls_path = project_dir / "calls.json"
    calls_path.write_text(
        """
        {
          "calls": [
            {
              "tool_name": "status.complete",
              "payload": {"message": "Project role service complete."},
              "terminal": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    project_file = project_dir / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
roles:
  engineering:
    instances: 2
    worker:
      adapter: safe-output-file
      path: calls.json
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )
    db = V2Database(db_path)
    try:
        db.migrate()
        for role_id in ("product-manager", "engineering"):
            db.create_role_assignment(
                assignment_id=f"assignment-project-runner-{role_id}",
                role_id=role_id,
                source_ref=f"msg-project-runner-{role_id}",
                title=f"Project runner {role_id}",
                summary="Run configured project role services once.",
                assignment_type="work_item_handoff",
                visibility_scope="project",
                payload={},
            )
    finally:
        db.close()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "run-project-role-services-once",
                "--project-file",
                str(project_file),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"processed_count": 2' in output
    assert '"role_instance_id": "test-project.engineering.1"' in output
    assert '"role_instance_id": "test-project.engineering.2"' in output
    assert '"role_instance_id": "test-project.product-manager.1"' in output
    db = V2Database(db_path)
    try:
        assignments = db.list_role_assignments()
        instances = db.status_snapshot()["role_instance_statuses"]
    finally:
        db.close()

    assert {assignment["status"] for assignment in assignments} == {"completed"}
    assert {instance["role_instance_id"] for instance in instances} == {
        "test-project.engineering.1",
        "test-project.engineering.2",
        "test-project.product-manager.1",
    }


def test_v2_cli_project_role_services_can_skip_unsupported_adapters(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "v2.sqlite3"
    project_dir = tmp_path / "agentic-mesh"
    project_dir.mkdir()
    calls_path = project_dir / "calls.json"
    calls_path.write_text(
        """
        {
          "calls": [
            {
              "tool_name": "status.complete",
              "payload": {"message": "Supported worker complete."},
              "terminal": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    project_file = project_dir / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
roles:
  engineering:
    worker:
      adapter: codex-cli
      model: codex
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )
    db = V2Database(db_path)
    try:
        db.migrate()
        db.create_role_assignment(
            assignment_id="assignment-project-runner-product",
            role_id="product-manager",
            source_ref="msg-project-runner-product",
            title="Project runner supported role",
            summary="Run supported role and skip unsupported role.",
            assignment_type="work_item_handoff",
            visibility_scope="project",
            payload={},
        )
    finally:
        db.close()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "run-project-role-services-once",
                "--project-file",
                str(project_file),
                "--skip-unsupported",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"processed_count": 1' in output
    assert '"skipped_count": 1' in output
    assert '"status": "skipped"' in output
    assert "unsupported worker adapter `codex-cli`" in output


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
