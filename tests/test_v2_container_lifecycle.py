from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v2.container_lifecycle import ComposeRoleLifecycleConfig
from agentic_mesh_v2.container_lifecycle import ContainerCommandResult
from agentic_mesh_v2.container_lifecycle import ContainerLifecycleExecutor
from agentic_mesh_v2.container_lifecycle import plan_compose_lifecycle_action
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.project_config import load_role_container_lifecycle_config
from agentic_mesh_v2.server import V2StatusHandler


def _render(snapshot: dict[str, object]) -> str:
    handler = object.__new__(V2StatusHandler)
    handler._snapshot = lambda: snapshot  # type: ignore[method-assign]
    return handler._render_status()


def test_container_lifecycle_config_loads_project_defaults_and_role_overrides(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
container_lifecycle:
  adapter: docker-compose
  compose_files:
    - docker-compose.yml
  service_name_template: "{project_id}-{role_id}-{index}"
  working_directory: deploy
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
    container_lifecycle:
      service_name_template: "pm-{index}"
""",
        encoding="utf-8",
    )

    config = load_role_container_lifecycle_config(project_file, role_id="product-manager")

    assert config["adapter"] == "docker-compose"
    assert config["compose_files"] == [str(tmp_path / "docker-compose.yml")]
    assert config["service_name_template"] == "pm-{index}"
    assert config["working_directory"] == str(tmp_path / "deploy")


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        ({}, "adapter must be `docker-compose`"),
        ({"adapter": "docker"}, "adapter must be `docker-compose`"),
        ({"adapter": "docker-compose"}, "requires compose_files"),
        (
            {"adapter": "docker-compose", "compose_files": [123]},
            "compose_files item 0 must be a non-empty string",
        ),
        (
            {"adapter": "docker-compose", "compose_files": ["compose.yml"]},
            "requires service_name_template",
        ),
        (
            {
                "adapter": "docker-compose",
                "compose_files": ["compose.yml"],
                "service_name_template": "{unknown}",
            },
            "unsupported service_name_template field",
        ),
        (
            {
                "adapter": "docker-compose",
                "compose_files": ["compose.yml"],
                "service_name_template": "{project_id[0]}",
            },
            "unsupported service_name_template field",
        ),
        (
            {
                "adapter": "docker-compose",
                "compose_files": ["compose.yml"],
                "service_name_template": "{project_id.__class__}",
            },
            "unsupported service_name_template field",
        ),
    ],
)
def test_compose_role_lifecycle_config_rejects_invalid_shapes(
    mapping: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ComposeRoleLifecycleConfig.from_mapping(mapping)


def test_container_lifecycle_config_rejects_non_string_compose_files(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
container_lifecycle:
  adapter: docker-compose
  compose_files:
    - 123
  service_name_template: "{project_id}-{role_id}-{index}"
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: calls.json
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="compose_files item 0 must be a non-empty string"):
        load_role_container_lifecycle_config(project_file, role_id="product-manager")


def test_compose_role_lifecycle_plans_stop_and_start_commands(tmp_path: Path) -> None:
    config = ComposeRoleLifecycleConfig.from_mapping(
        {
            "adapter": "docker-compose",
            "compose_files": [str(tmp_path / "compose.yml")],
            "service_name_template": "{project_id}-{role_id}-{index}",
            "working_directory": str(tmp_path),
        }
    )

    stop = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )
    start = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hydrating",
        reason="Queued work arrived.",
    )
    idle = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="idle",
        reason="No lifecycle action.",
    )

    assert stop is not None
    assert stop.action == "stop"
    assert stop.service_name == "test-project-product-manager-1"
    assert stop.command == (
        "docker",
        "compose",
        "-f",
        str(tmp_path / "compose.yml"),
        "stop",
        "test-project-product-manager-1",
    )
    assert start is not None
    assert start.action == "start"
    assert start.command[-3:] == ("up", "-d", "test-project-product-manager-1")
    assert idle is None


def test_compose_role_lifecycle_requires_numeric_role_instance_index(tmp_path: Path) -> None:
    config = ComposeRoleLifecycleConfig.from_mapping(
        {
            "adapter": "docker-compose",
            "compose_files": [str(tmp_path / "compose.yml")],
            "service_name_template": "{role_id}-{index}",
        }
    )

    with pytest.raises(ValueError, match="numeric instance index"):
        plan_compose_lifecycle_action(
            config=config,
            project_id="test-project",
            role_id="product-manager",
            role_instance_id="product-manager-main",
            status="hibernated",
            reason="Idle grace elapsed.",
        )


def test_container_lifecycle_executor_records_success_and_hydrates_started_instance(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.update_role_instance_hibernation(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hydrating",
        reason="Queued work arrived.",
    )
    calls: list[tuple[list[str], Path | None, int]] = []

    def runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ContainerCommandResult:
        calls.append((command, cwd, timeout_seconds))
        return ContainerCommandResult(exit_code=0, stdout="started")

    config = ComposeRoleLifecycleConfig.from_mapping(
        {
            "adapter": "docker-compose",
            "compose_files": [str(tmp_path / "compose.yml")],
            "service_name_template": "{project_id}-{role_id}-{index}",
            "working_directory": str(tmp_path),
        }
    )
    action = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hydrating",
        reason="Queued work arrived.",
    )
    assert action is not None

    action_id, result = ContainerLifecycleExecutor(db, runner=runner, timeout_seconds=42).execute(action)

    snapshot = db.status_snapshot()
    assert result.exit_code == 0
    assert calls == [(list(action.command), tmp_path, 42)]
    assert snapshot["role_container_lifecycle_actions"][0]["action_id"] == action_id
    assert snapshot["role_container_lifecycle_actions"][0]["status"] == "succeeded"
    assert snapshot["role_container_lifecycle_actions"][0]["stdout"] == "started"
    assert snapshot["role_instance_statuses"][0]["status"] == "idle"
    assert snapshot["role_instance_statuses"][0]["detail"] == "Role instance hydrated after container lifecycle start."
    assert snapshot["runtime_attention_items"] == []
    html = _render(snapshot)
    assert "Role Container Lifecycle Actions" in html
    assert "test-project-product-manager-1" in html


def test_container_lifecycle_executor_records_failed_stop_without_changing_instance_state(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.update_role_instance_hibernation(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )

    def runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ContainerCommandResult:
        return ContainerCommandResult(exit_code=17, stdout="stopping", stderr="compose failed")

    config = ComposeRoleLifecycleConfig.from_mapping(
        {
            "adapter": "docker-compose",
            "compose_files": [str(tmp_path / "compose.yml")],
            "service_name_template": "{project_id}-{role_id}-{index}",
        }
    )
    action = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )
    assert action is not None

    _, result = ContainerLifecycleExecutor(db, runner=runner).execute(action)

    snapshot = db.status_snapshot()
    assert result.exit_code == 17
    assert snapshot["role_container_lifecycle_actions"][0]["status"] == "failed"
    assert snapshot["role_container_lifecycle_actions"][0]["stderr"] == "compose failed"
    assert snapshot["role_instance_statuses"][0]["status"] == "hibernated"
    assert snapshot["counts"]["runtime_attention_items"] == 1
    attention = snapshot["runtime_attention_items"][0]
    assert attention["source_type"] == "role_container_lifecycle"
    assert attention["source_ref"] == snapshot["role_container_lifecycle_actions"][0]["action_id"]
    assert attention["owner"] == "platform-engineer"
    assert attention["reason_class"] == "container_lifecycle_failed"
    assert attention["retryable"] is True
    assert "retry the lifecycle action" in attention["next_action"]
    html = _render(snapshot)
    assert "Runtime Attention" in html
    assert "container_lifecycle_failed" in html


def test_container_lifecycle_executor_preserves_repeated_attempt_evidence(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.update_role_instance_hibernation(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )

    def runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ContainerCommandResult:
        return ContainerCommandResult(exit_code=17, stderr="compose failed")

    config = ComposeRoleLifecycleConfig.from_mapping(
        {
            "adapter": "docker-compose",
            "compose_files": [str(tmp_path / "compose.yml")],
            "service_name_template": "{project_id}-{role_id}-{index}",
        }
    )
    action = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )
    assert action is not None
    executor = ContainerLifecycleExecutor(db, runner=runner)

    first_id, _ = executor.execute(action)
    second_id = executor.record_plan(action)
    third_id, _ = executor.execute(action)

    rows = db.status_snapshot()["role_container_lifecycle_actions"]
    assert first_id != second_id != third_id
    assert len(rows) == 3
    assert {row["action_fingerprint"] for row in rows} == {rows[0]["action_fingerprint"]}
    statuses = [row["status"] for row in rows]
    assert statuses.count("failed") == 2
    assert statuses.count("planned") == 1
    assert any(row["action_id"] == first_id and row["stderr"] == "compose failed" for row in rows)
    attention_items = db.status_snapshot()["runtime_attention_items"]
    assert len(attention_items) == 2
    assert {item["source_ref"] for item in attention_items} == {first_id, third_id}


def test_successful_retry_closes_matching_lifecycle_attention(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.update_role_instance_hibernation(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )

    attempts = 0

    def runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ContainerCommandResult:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return ContainerCommandResult(exit_code=17, stderr="compose failed")
        return ContainerCommandResult(exit_code=0, stdout="stopped")

    config = ComposeRoleLifecycleConfig.from_mapping(
        {
            "adapter": "docker-compose",
            "compose_files": [str(tmp_path / "compose.yml")],
            "service_name_template": "{project_id}-{role_id}-{index}",
        }
    )
    action = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )
    assert action is not None
    unrelated_action = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Different maintenance reason.",
    )
    assert unrelated_action is not None
    executor = ContainerLifecycleExecutor(db, runner=runner)

    first_id, first_result = executor.execute(action)
    unrelated_id, unrelated_result = executor.execute(unrelated_action)
    second_id, second_result = executor.execute(action)

    snapshot = db.status_snapshot()
    assert first_result.exit_code == 17
    assert unrelated_result.exit_code == 17
    assert second_result.exit_code == 0
    rows = snapshot["role_container_lifecycle_actions"]
    assert {row["action_id"]: row["status"] for row in rows} == {
        first_id: "failed",
        unrelated_id: "failed",
        second_id: "succeeded",
    }
    attention_by_source = {item["source_ref"]: item for item in snapshot["runtime_attention_items"]}
    assert attention_by_source[first_id]["status"] == "closed"
    assert second_id in attention_by_source[first_id]["next_action"]
    assert attention_by_source[unrelated_id]["status"] == "open"
    assert any(event["event_type"] == "runtime.attention_closed" for event in snapshot["recent_events"])


def test_execute_retry_reuses_failed_action_details(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.update_role_instance_hibernation(
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )
    calls: list[list[str]] = []

    def runner(command: list[str], *, cwd: Path | None, timeout_seconds: int) -> ContainerCommandResult:
        calls.append(command)
        if len(calls) == 1:
            return ContainerCommandResult(exit_code=17, stderr="compose failed")
        return ContainerCommandResult(exit_code=0, stdout="stopped")

    config = ComposeRoleLifecycleConfig.from_mapping(
        {
            "adapter": "docker-compose",
            "compose_files": [str(tmp_path / "compose.yml")],
            "service_name_template": "{project_id}-{role_id}-{index}",
            "working_directory": str(tmp_path / "deploy"),
        }
    )
    action = plan_compose_lifecycle_action(
        config=config,
        project_id="test-project",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        status="hibernated",
        reason="Idle grace elapsed.",
    )
    assert action is not None
    executor = ContainerLifecycleExecutor(db, runner=runner, timeout_seconds=12)

    first_id, first_result = executor.execute(action)
    retry_id, retry_action, retry_result = executor.execute_retry(first_id)

    assert first_result.exit_code == 17
    assert retry_result.exit_code == 0
    assert retry_action == action
    assert calls == [list(action.command), list(action.command)]
    rows = db.status_snapshot()["role_container_lifecycle_actions"]
    row_by_id = {row["action_id"]: row for row in rows}
    assert row_by_id[first_id]["status"] == "failed"
    assert row_by_id[retry_id]["status"] == "succeeded"
    assert row_by_id[first_id]["action_fingerprint"] == row_by_id[retry_id]["action_fingerprint"]
    assert db.status_snapshot()["runtime_attention_items"][0]["status"] == "closed"


def test_retry_rejects_unknown_or_non_failed_lifecycle_action(tmp_path: Path) -> None:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.record_role_container_lifecycle_action(
        action_id="role-container-action-success",
        action_fingerprint="fingerprint-success",
        role_id="product-manager",
        role_instance_id="test-project.product-manager.1",
        action="stop",
        service_name="test-project-product-manager-1",
        command=["docker", "compose", "stop", "test-project-product-manager-1"],
        working_directory=None,
        status="succeeded",
        reason="Idle grace elapsed.",
        exit_code=0,
        stdout="stopped",
    )
    executor = ContainerLifecycleExecutor(db, runner=lambda command, cwd, timeout_seconds: ContainerCommandResult(0))

    with pytest.raises(ValueError, match="unknown role container lifecycle action"):
        executor.record_retry_plan("role-container-action-missing")
    with pytest.raises(ValueError, match="only failed role container lifecycle actions can be retried"):
        executor.record_retry_plan("role-container-action-success")
