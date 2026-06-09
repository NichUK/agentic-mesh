import os
import sys
import json
import time
from dataclasses import replace
from pathlib import Path

from agentic_mesh.cli import build_runtime
from agentic_mesh.cli import project_workspace_root
from agentic_mesh.config import load_mesh_config
from agentic_mesh.messaging import MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED
from agentic_mesh.models import DocumentLibraryConfig
from agentic_mesh.models import Message
from agentic_mesh.storage import FileMessageStore
from agentic_mesh import workers
from agentic_mesh.workers import ConfiguredWorkerAdapter
from agentic_mesh.safe_outputs import append_safe_output_record
from agentic_mesh.safe_outputs import load_safe_output_records
from agentic_mesh.safe_outputs import result_from_safe_output_records
from agentic_mesh.workers import summarize_worker_failure


def test_safe_outputs_accept_document_update_and_completion(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    flow_state = mesh_config.project.flow.states["business_analysis"]
    message = Message.create(
        role_id="business-analyst",
        message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
        payload={"work_item_id": "work-safe", "work_item_type": "slice"},
        source="test",
    )
    output_file = tmp_path / "safe-outputs.jsonl"
    append_safe_output_record(
        output_file=output_file,
        tool="document.propose_update",
        payload={
            "path": "documents/analysis/business-analyst.md",
            "content": "# Business Analyst Worklist\n\nActual analysis.",
        },
    )
    append_safe_output_record(
        output_file=output_file,
        tool="status.report_completion",
        payload={"message": "Analysed the project."},
    )

    result = result_from_safe_output_records(
        records=load_safe_output_records(output_file),
        message=message,
        flow_state=flow_state,
    )

    assert result.status == "completed"
    assert result.document_updates[0].path == "documents/analysis/business-analyst.md"


def test_safe_outputs_accept_optional_document_index_metadata(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    flow_state = mesh_config.project.flow.states["business_analysis"]
    message = Message.create(
        role_id="business-analyst",
        message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
        payload={"work_item_id": "work-safe", "work_item_type": "slice"},
        source="test",
    )
    output_file = tmp_path / "safe-outputs.jsonl"
    append_safe_output_record(
        output_file=output_file,
        tool="document.propose_update",
        payload={
            "path": "work-items/work-safe/10-business-brief.md",
            "content": "# Business Analyst Worklist",
            "purpose": "Business framing.",
            "review_status": "approved",
            "index_summary": "Index metadata summary.",
            "maintain_work_item_index": True,
        },
    )
    append_safe_output_record(
        output_file=output_file,
        tool="status.report_completion",
        payload={"message": "Analysed the project."},
    )

    result = result_from_safe_output_records(
        records=load_safe_output_records(output_file),
        message=message,
        flow_state=flow_state,
    )

    update = result.document_updates[0]
    assert update.purpose == "Business framing."
    assert update.review_status == "approved"
    assert update.index_summary == "Index metadata summary."
    assert update.maintain_work_item_index is True


def test_safe_outputs_accept_first_class_routes(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    flow_state = mesh_config.project.flow.states["quality_review"]
    message = Message.create(
        role_id="qa-engineer",
        message_type="sdlc.quality_review",
        payload={"work_item_id": "work-correction", "work_item_type": "slice"},
        source="test",
    )
    output_file = tmp_path / "safe-outputs.jsonl"
    append_safe_output_record(
        output_file=output_file,
        tool="route.consult",
        payload={
            "target_role": "engineering",
            "message_type": "sdlc.consult.implementation",
            "title": "Correction",
            "summary": "Fix DEF-QA-LIFE-001.",
            "lifecycle_state": "implementation",
            "review_status": "changes_requested",
            "defect_id": "DEF-QA-LIFE-001",
            "required_change": "Route through the configured consult.",
            "evidence_required": "Attach implementation evidence.",
        },
    )
    append_safe_output_record(
        output_file=output_file,
        tool="status.report_completion",
        payload={"message": "QA correction requested."},
    )

    result = result_from_safe_output_records(
        records=load_safe_output_records(output_file),
        message=message,
        flow_state=flow_state,
    )

    assert result.routes[0].target_role == "engineering"
    assert result.routes[0].payload["defect_id"] == "DEF-QA-LIFE-001"


def test_configured_worker_blocks_when_codex_secret_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(workers.shutil, "which", lambda command: "codex")
    mesh_config = load_mesh_config(Path.cwd())
    worker = ConfiguredWorkerAdapter(
        project=mesh_config.project,
        auth_methods=mesh_config.auth_methods,
        workspace_root=tmp_path / "workspace",
        state_root=tmp_path / "state",
    )
    flow_state = mesh_config.project.flow.states["product_definition"]
    message = Message.create(
        role_id="product-manager",
        message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
        payload={
            "title": "Adopt this project",
            "summary": "Analyse the repo.",
            "work_item_id": "work-adoption",
            "work_item_type": "directive",
        },
        source="test",
    )

    result = worker.run(
        mesh_config.instances["agentic-mesh-dev.product-manager.1"],
        message,
        flow_state,
    )

    assert result.problem_status is not None
    assert result.problem_status.status == "needs_runtime_recovery"
    assert result.problem_status.problem_kind == "worker_failed"
    assert result.problem_status.failure_class == "auth_missing"
    assert result.problem_status.recovery_action == "repair_auth"
    assert result.problem_status.artifact_paths == ()


def test_configured_worker_points_missing_oauth_to_auth_ui(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(workers.shutil, "which", lambda command: "codex")
    monkeypatch.setenv(
        "AGENTIC_MESH_AUTH_ADMIN_URL",
        "https://mesh.example/auth/credentials",
    )
    mesh_config = load_mesh_config(Path.cwd(), project_file=str(project_file))
    worker = ConfiguredWorkerAdapter(
        project=mesh_config.project,
        auth_methods=mesh_config.auth_methods,
        workspace_root=tmp_path / "workspace",
        state_root=tmp_path / "state",
    )
    flow_state = mesh_config.project.flow.states["product_definition"]
    message = Message.create(
        role_id="product-manager",
        message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
        payload={"title": "Do work", "summary": "Needs OAuth"},
        source="test",
    )

    result = worker.run(
        mesh_config.instances["oauth-example.product-manager.1"],
        message,
        flow_state,
    )

    assert result.problem_status is not None
    assert result.problem_status.status == "needs_runtime_recovery"
    assert result.problem_status.failure_class == "auth_missing"
    assert "sign in with OpenAI" in result.problem_status.reason
    assert "codex-product-oauth" not in result.problem_status.reason


def test_configured_worker_uses_current_codex_exec_flags(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(workers.shutil, "which", lambda command: "codex")
    mesh_config = load_mesh_config(Path.cwd())
    instance = mesh_config.instances["agentic-mesh-dev.product-manager.1"]
    mount_ref = instance.override.worker.auth.mount_ref
    assert mount_ref is not None
    (tmp_path / "state" / "worker_mounts" / mount_ref).mkdir(parents=True)
    (tmp_path / "workspace").mkdir()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        captured["prompt"] = kwargs["input_text"]
        append_safe_output_record(
            output_file=Path(kwargs["env"]["AGENTIC_MESH_SAFE_OUTPUT_FILE"]),
            tool="document.propose_update",
            payload={
                "path": "documents/analysis/product-manager.md",
                "content": "# Smoke passed",
            },
        )
        append_safe_output_record(
            output_file=Path(kwargs["env"]["AGENTIC_MESH_SAFE_OUTPUT_FILE"]),
            tool="status.report_completion",
            payload={"message": "Smoke passed."},
        )

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""
            timed_out = False
            progress_observed_at = "2026-06-05T00:00:00+00:00"

        return Completed()

    monkeypatch.setattr(workers, "run_progress_aware_command", fake_run)
    project = replace(
        mesh_config.project,
        document_library=DocumentLibraryConfig(root=str(tmp_path / "docs")),
    )
    worker = ConfiguredWorkerAdapter(
        mesh_config=mesh_config,
        project=project,
        auth_methods=mesh_config.auth_methods,
        workspace_root=tmp_path / "workspace",
        state_root=tmp_path / "state",
    )
    message = Message.create(
        role_id="product-manager",
        message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
        payload={
            "title": "Smoke",
            "summary": "Check command flags.",
            "work_item_id": "work-smoke",
            "work_item_type": "slice",
            "lifecycle_state": "product_definition",
        },
        source="test",
    )

    result = worker.run(
        instance,
        message,
        mesh_config.project.flow.states["product_definition"],
    )

    command = captured["command"]
    assert result.status == "completed"
    assert "--ask-for-approval" not in command
    assert "--sandbox" in command
    assert "danger-full-access" in command
    assert "-c" in command
    assert "model_reasoning_effort=high" in command
    assert "--output-schema" not in command
    assert "-o" not in command
    assert captured["env"]["CODEX_HOME"] == str(
        tmp_path / "state" / "worker_mounts" / mount_ref
    )
    assert captured["env"]["AGENTIC_MESH_SAFE_OUTPUT_FILE"]
    assert captured["env"]["AGENTIC_MESH_WORK_ITEM_ID"] == "work-smoke"
    prompt = str(captured["prompt"])
    assert "<system>" in prompt
    assert "<safe-outputs>" in prompt
    assert "python -m agentic_mesh.cli safe-output <tool-name> ." in prompt
    assert "Do not return legacy final JSON" in prompt
    assert "Durable claim discipline" in prompt
    assert "unless you emitted the corresponding safe-output call" in prompt
    assert "Never say you created, restarted, promoted, updated" in prompt
    assert "<goal>" in prompt
    assert "Build Agentic Mesh into an open-core" in prompt
    assert "pluggable connectors" in prompt
    assert "current_focus" not in prompt
    assert "<workspace>" in prompt
    assert '"workspace_root": "examples/projects/agentic-mesh-dev"' in prompt
    assert '"default_repository": "agentic-mesh"' in prompt
    assert '"path": "../../.."' in prompt
    assert "<document-library-and-memory>" in prompt
    assert "<accountability>" in prompt
    assert "Role profile:" in prompt
    assert "product accountability owner" in prompt
    assert "decision_rights" in prompt
    assert "core_workflows" in prompt
    assert "<capabilities>" in prompt
    assert "availability has not been validated" in prompt
    assert "document-library.read" in prompt
    assert "Use safe-output tools for every durable effect" in prompt
    prompt_files = list(
        (tmp_path / "docs" / "work-items" / "work-smoke" / "debug" / "prompts").glob(
            "**/*.prompt.txt"
        )
    )
    metadata_files = list(
        (tmp_path / "docs" / "work-items" / "work-smoke" / "debug" / "prompts").glob(
            "**/*.metadata.json"
        )
    )
    assert len(prompt_files) == 1
    assert len(metadata_files) == 1
    assert prompt_files[0].read_text(encoding="utf-8").strip() == prompt.strip()
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["audit_kind"] == "work_item_prompt"
    assert metadata["role_instance_id"] == instance.instance_id
    assert metadata["message_id"] == message.message_id
    assert metadata["prompt_capture"] == "exact_stdin_sent_to_worker_adapter"


def test_codex_timeout_returns_worker_recovery_problem(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(workers.shutil, "which", lambda command: "codex")
    mesh_config = load_mesh_config(Path.cwd())
    instance = mesh_config.instances["agentic-mesh-dev.product-manager.1"]
    mount_ref = instance.override.worker.auth.mount_ref
    assert mount_ref is not None
    (tmp_path / "state" / "worker_mounts" / mount_ref).mkdir(parents=True)
    (tmp_path / "workspace").mkdir()

    def fake_run(command, **kwargs):
        class Completed:
            returncode = -9
            stdout = "raw provider output with /tmp/secret/path"
            stderr = "secret_ref=should-not-leak"
            timed_out = True
            started_at = 0.0
            progress_observed_at = "2026-06-05T00:00:00+00:00"

        return Completed()

    monkeypatch.setattr(workers, "run_progress_aware_command", fake_run)
    worker = ConfiguredWorkerAdapter(
        project=mesh_config.project,
        auth_methods=mesh_config.auth_methods,
        workspace_root=tmp_path / "workspace",
        state_root=tmp_path / "state",
    )
    message = Message.create(
        role_id="product-manager",
        message_type=MESSAGE_TYPE_SPONSOR_DIRECTIVE_REQUESTED,
        payload={"title": "Timeout", "summary": "No valid result."},
        source="test",
    )

    outcome = worker.run(
        instance,
        message,
        mesh_config.project.flow.states["product_definition"],
    )

    assert outcome.problem_status is not None
    assert outcome.problem_status.status == "needs_runtime_recovery"
    assert outcome.problem_status.problem_kind == "worker_failed"
    assert outcome.problem_status.failure_class == "timeout"
    assert outcome.problem_status.retryable is True
    assert "secret_ref" not in outcome.problem_status.to_dict()


def test_progress_aware_command_allows_progress_past_soft_timeout(tmp_path: Path) -> None:
    completed = workers.run_progress_aware_command(
        [
            sys.executable,
            "-c",
            (
                "import sys,time\n"
                "for i in range(5):\n"
                " print(f'tick {i}', flush=True)\n"
                " time.sleep(0.2)\n"
            ),
        ],
        input_text="",
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_seconds=None,
        progress_window_seconds=1,
        progress_paths=[],
    )

    assert completed.timed_out is False
    assert completed.returncode == 0
    assert "tick 4" in completed.stdout


def test_progress_aware_command_times_out_when_quiet(tmp_path: Path) -> None:
    completed = workers.run_progress_aware_command(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        input_text="",
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_seconds=None,
        progress_window_seconds=0.2,
        progress_paths=[],
    )

    assert completed.timed_out is True
    assert completed.returncode == -9


def test_progress_aware_command_uses_completion_probe(tmp_path: Path) -> None:
    completed = workers.run_progress_aware_command(
        [
            sys.executable,
            "-c",
            (
                "import time\n"
                "while True:\n"
                " print('still running', flush=True)\n"
                " time.sleep(0.1)\n"
            ),
        ],
        input_text="",
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_seconds=None,
        progress_window_seconds=5,
        progress_paths=[],
        completion_probe=lambda: '{"status":"completed","message":"done","document_updates":[],"handoffs":[],"routes":[]}',
    )

    assert completed.completed_from_probe is True
    assert completed.timed_out is False
    assert completed.returncode == 0
    assert '"message":"done"' in completed.stdout


def test_codex_session_completed_result_reads_task_complete(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "06" / "06"
    session_dir.mkdir(parents=True)
    session_path = session_dir / "rollout-2026-06-06T16-38-55.jsonl"
    result = {
        "status": "completed",
        "message": "Recovered from session log.",
        "document_updates": [],
        "handoffs": [],
        "routes": [],
    }
    session_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": json.dumps(result),
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    recovered = workers.codex_session_completed_result(
        codex_home,
        started_at=time.time() - 1,
        required_markers=["Recovered from session log."],
    )

    assert recovered == json.dumps(result)
    assert (
        workers.codex_session_completed_result(
            codex_home,
            started_at=time.time() - 1,
            required_markers=["work-other"],
        )
        is None
    )


def test_summarize_worker_failure_keeps_error_tail() -> None:
    detail = "banner\n" + ("prompt\n" * 500) + "ERROR: invalid_json_schema"

    summary = summarize_worker_failure(detail, max_length=80)

    assert summary.startswith("...")
    assert "ERROR: invalid_json_schema" in summary
    assert "banner" not in summary


def test_build_runtime_uses_configured_worker_adapter(tmp_path: Path) -> None:
    mesh_config, _, message_store, _, _, runtime = build_runtime(
        Path.cwd(),
        "examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
        tmp_path / "workspace",
        tmp_path / "state",
    )

    assert isinstance(runtime.worker, ConfiguredWorkerAdapter)
    assert isinstance(message_store, FileMessageStore)
    assert project_workspace_root(tmp_path / "workspace", mesh_config) == (
        tmp_path / "workspace" / "examples" / "projects" / "agentic-mesh-dev"
    ).resolve()
    assert runtime.artifact_store.workspace_root == (
        tmp_path / "workspace" / "examples" / "projects" / "agentic-mesh-dev"
    ).resolve()
