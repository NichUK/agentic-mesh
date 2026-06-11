import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_mesh.safe_outputs import SafeOutputRecord
from agentic_mesh.safe_outputs import append_safe_output_record
from agentic_mesh.safe_outputs import load_safe_output_records
from agentic_mesh.safe_outputs import result_from_safe_output_records
from agentic_mesh.safe_outputs import safe_output_tools_prompt
from agentic_mesh.models import FlowState
from agentic_mesh.models import Message


def test_safe_output_tools_prompt_preserves_mandatory_finish_contract() -> None:
    prompt = safe_output_tools_prompt()

    assert "You MUST call at least one safe-output tool during every agent run." in prompt
    assert "You MUST call at least one terminal safe-output tool before finishing." in prompt
    assert "The terminal safe-output call is the only valid way to finish a run." in prompt
    assert "status.report_progress` is non-terminal" in prompt
    assert "Do NOT use placeholder, speculative, or fake safe-output calls." in prompt
    assert prompt.index("Terminal tools:") < prompt.index("Available tools:")
    assert "status.reply" in prompt


def test_direct_conversation_safe_output_prompt_hides_forbidden_tools() -> None:
    prompt = safe_output_tools_prompt(lifecycle_state="direct_conversation")

    terminal_section = prompt.split("Terminal tools:", 1)[1].split(
        "Available tools:", 1
    )[0]
    available_section = prompt.split("Available tools:", 1)[1].split("Examples:", 1)[0]
    assert "Direct conversation context:" in prompt
    assert "status.reply" in terminal_section
    assert "sponsor.ask_question" in terminal_section
    assert "work_item.handoff" in terminal_section
    assert "status.report_completion" not in terminal_section
    assert "handoff.propose" not in terminal_section
    assert "status.report_completion" not in available_section
    assert "handoff.propose" not in available_section


def test_safe_output_cli_records_valid_payload(tmp_path: Path) -> None:
    output_file = tmp_path / "safe-outputs.jsonl"
    payload = {"message": "No durable work needed."}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_mesh.cli",
            "safe-output",
            "noop",
            ".",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={
            **__import__("os").environ,
            "AGENTIC_MESH_SAFE_OUTPUT_FILE": str(output_file),
            "AGENTIC_MESH_WORK_ITEM_ID": "work-cli",
        },
    )

    assert completed.returncode == 0, completed.stderr
    records = load_safe_output_records(output_file)
    assert records[0].tool == "noop"
    assert records[0].payload == payload


def test_direct_conversation_rejects_flow_handoff_tool(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="work_item.handoff, not handoff.propose"):
        append_safe_output_record(
            output_file=tmp_path / "safe-outputs.jsonl",
            tool="handoff.propose",
            payload={
                "target_role": "ux-designer",
                "message_type": "sdlc.experience_design",
            },
            context={"AGENTIC_MESH_LIFECYCLE_STATE": "direct_conversation"},
        )


def test_direct_conversation_rejects_report_completion_terminal_tool(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="do not use status.report_completion"):
        append_safe_output_record(
            output_file=tmp_path / "safe-outputs.jsonl",
            tool="status.report_completion",
            payload={"message": "Done."},
            context={"AGENTIC_MESH_LIFECYCLE_STATE": "direct_conversation"},
        )


def test_direct_conversation_document_update_must_be_slice_scoped(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="work-items/work-product/"):
        append_safe_output_record(
            output_file=tmp_path / "safe-outputs.jsonl",
            tool="document.propose_update",
            payload={
                "path": "documents/analysis/product-manager.md",
                "content": "Wrong place.",
            },
            context={
                "AGENTIC_MESH_LIFECYCLE_STATE": "direct_conversation",
                "AGENTIC_MESH_WORK_ITEM_ID": "work-product",
            },
        )


def test_status_reply_is_terminal_human_facing_markdown() -> None:
    record = SafeOutputRecord(
        tool="status.reply",
        payload={"message": "## Status\n\n- **Ready:** yes"},
        recorded_at="2026-06-10T10:00:00+00:00",
        context={},
        validation={"status": "valid"},
    )
    result = result_from_safe_output_records(
        records=[record],
        message=Message.create(
            role_id="product-manager",
            message_type="conversation.direct",
            payload={"title": "Status"},
            source="test",
        ),
        flow_state=FlowState(
            state_id="direct_conversation",
            owner_role="product-manager",
            purpose="Reply in role.",
            artifact_path="",
            handoffs={},
        ),
    )

    assert result.status == "completed"
    assert result.message == "## Status\n\n- **Ready:** yes"
    assert result.document_updates == []
    assert result.routes == []
    assert result.handoffs == []
    assert result.queue_proposals == []
    assert result.terminal_tool == "status.reply"


def test_queue_propose_item_becomes_structured_queue_proposal() -> None:
    records = [
        SafeOutputRecord(
            tool="queue.propose_item",
            payload={
                "title": "Build gateway bot",
                "summary": "Create the dedicated Agentic Mesh gateway bot.",
                "owner_role": "delivery-manager",
                "recommended_work_item_type": "slice",
            },
            recorded_at="2026-06-10T10:00:00+00:00",
            context={},
            validation={"status": "valid"},
        ),
        SafeOutputRecord(
            tool="status.reply",
            payload={"message": "I've proposed this as a tracked slice."},
            recorded_at="2026-06-10T10:00:01+00:00",
            context={},
            validation={"status": "valid"},
        ),
    ]

    result = result_from_safe_output_records(
        records=records,
        message=Message.create(
            role_id="product-manager",
            message_type="conversation.direct",
            payload={"title": "Gateway bot"},
            source="test",
        ),
        flow_state=FlowState(
            state_id="direct_conversation",
            owner_role="product-manager",
            purpose="Reply in role.",
            artifact_path="",
            handoffs={},
        ),
    )

    assert result.status == "completed"
    assert result.message == "I've proposed this as a tracked slice."
    assert len(result.queue_proposals) == 1
    assert result.queue_proposals[0].title == "Build gateway bot"
    assert result.queue_proposals[0].owner_role == "delivery-manager"
    assert result.terminal_tool == "status.reply"


def test_work_item_action_records_become_structured_runtime_actions() -> None:
    records = [
        SafeOutputRecord(
            tool="work_item.close",
            payload={
                "work_item_id": "work-release",
                "reason": "Sponsor approved closure as superseded.",
                "disposition": "superseded",
            },
            recorded_at="2026-06-10T10:00:00+00:00",
            context={},
            validation={"status": "valid"},
        ),
        SafeOutputRecord(
            tool="work_item.reopen_flow",
            payload={
                "work_item_id": "work-recheck",
                "target_role": "qa-engineer",
                "lifecycle_state": "quality_review",
                "reason": "QA must rerun checks after a runtime fix.",
            },
            recorded_at="2026-06-10T10:00:01+00:00",
            context={},
            validation={"status": "valid"},
        ),
        SafeOutputRecord(
            tool="status.reply",
            payload={"message": "Closed one work item and reopened QA for another."},
            recorded_at="2026-06-10T10:00:02+00:00",
            context={},
            validation={"status": "valid"},
        ),
    ]

    result = result_from_safe_output_records(
        records=records,
        message=Message.create(
            role_id="release-manager",
            message_type="conversation.direct",
            payload={"title": "Release reconciliation"},
            source="test",
        ),
        flow_state=FlowState(
            state_id="direct_conversation",
            owner_role="release-manager",
            purpose="Resolve release state.",
            artifact_path="",
            handoffs={},
        ),
    )

    assert result.status == "completed"
    assert [action.action for action in result.work_item_actions] == [
        "close",
        "reopen_flow",
    ]
    assert result.work_item_actions[0].disposition == "superseded"
    assert result.work_item_actions[1].target_role == "qa-engineer"
    assert result.work_item_actions[1].message_type == "sdlc.quality_review"


def test_work_item_handoff_record_becomes_structured_runtime_action() -> None:
    records = [
        SafeOutputRecord(
            tool="work_item.handoff",
            payload={
                "work_item_id": "work-product",
                "source_lifecycle_state": "product_definition",
                "target_role": "ux-designer",
                "lifecycle_state": "experience_design",
                "reason": "Product definition is ready for experience design.",
                "summary": "Continue dashboard presentation refinement.",
            },
            recorded_at="2026-06-10T10:00:00+00:00",
            context={},
            validation={"status": "valid"},
        ),
        SafeOutputRecord(
            tool="status.reply",
            payload={"message": "I handed this to UX for experience design."},
            recorded_at="2026-06-10T10:00:01+00:00",
            context={},
            validation={"status": "valid"},
        ),
    ]

    result = result_from_safe_output_records(
        records=records,
        message=Message.create(
            role_id="product-manager",
            message_type="conversation.direct",
            payload={"title": "Continue existing slice", "work_item_type": "slice"},
            source="test",
        ),
        flow_state=FlowState(
            state_id="direct_conversation",
            owner_role="product-manager",
            purpose="Reply in role.",
            artifact_path="",
            handoffs={},
        ),
    )

    assert result.status == "completed"
    assert result.message == "I handed this to UX for experience design."
    assert len(result.work_item_actions) == 1
    action = result.work_item_actions[0]
    assert action.action == "handoff"
    assert action.work_item_id == "work-product"
    assert action.source_lifecycle_state == "product_definition"
    assert action.target_role == "ux-designer"
    assert action.lifecycle_state == "experience_design"
    assert action.message_type == "sdlc.experience_design"
    assert action.summary == "Continue dashboard presentation refinement."


def test_work_item_handoff_can_be_terminal() -> None:
    record = SafeOutputRecord(
        tool="work_item.handoff",
        payload={
            "work_item_id": "work-product",
            "source_lifecycle_state": "product_definition",
            "target_role": "ux-designer",
            "lifecycle_state": "experience_design",
            "reason": "Product definition is ready for experience design.",
            "summary": "Continue dashboard presentation refinement.",
        },
        recorded_at="2026-06-10T10:00:00+00:00",
        context={},
        validation={"status": "valid"},
    )

    result = result_from_safe_output_records(
        records=[record],
        message=Message.create(
            role_id="product-manager",
            message_type="conversation.direct",
            payload={"title": "Continue existing slice", "work_item_type": "slice"},
            source="test",
        ),
        flow_state=FlowState(
            state_id="direct_conversation",
            owner_role="product-manager",
            purpose="Reply in role.",
            artifact_path="",
            handoffs={},
        ),
    )

    assert result.status == "completed"
    assert result.message == "Continue dashboard presentation refinement."
    assert result.terminal_tool == "work_item.handoff"


def test_safe_outputs_mcp_records_tool_call(tmp_path: Path) -> None:
    output_file = tmp_path / "safe-outputs.jsonl"
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "status.report_completion",
            "arguments": {"message": "Completed through MCP."},
        },
    }
    completed = subprocess.run(
        [sys.executable, "-m", "agentic_mesh.safe_outputs_mcp"],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env={
            **__import__("os").environ,
            "AGENTIC_MESH_SAFE_OUTPUT_FILE": str(output_file),
            "AGENTIC_MESH_WORK_ITEM_ID": "work-mcp",
        },
    )

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout.splitlines()[0])
    assert response["result"]["isError"] is False
    records = load_safe_output_records(output_file)
    assert records[0].tool == "status.report_completion"
    assert records[0].payload["message"] == "Completed through MCP."
