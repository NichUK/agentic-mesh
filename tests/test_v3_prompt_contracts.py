from pathlib import Path


def test_v3_safe_output_prompt_names_current_tools() -> None:
    prompt = Path("config/prompts/worker/safe-outputs.xml").read_text(encoding="utf-8")

    stale_tool_names = (
        "queue.propose_item",
        "handoff.request",
        "sponsor.ask_question",
        "human_response.request",
        "release.record_decision",
        "document.propose_update",
        "product.mark_sponsor_ready",
        "report.blocked",
        "release.request_approval",
    )
    for tool_name in stale_tool_names:
        assert tool_name not in prompt

    for tool_name in (
        "backlog.upsert",
        "work_item.upsert",
        "agent.delegate",
        "handoff.require",
        "stakeholder.ask_question",
        "approval.request",
        "release.deploy",
        "release.close",
        "decision.record",
        "risk.register",
        "blocker.raise",
        "runtime.status.inspect",
        "runtime.broker.inspect",
        "runtime.broker.retry_dead_letter",
        "runtime.message_journal.inspect",
        "runtime.lifecycle.request",
        "status.reply",
    ):
        assert tool_name in prompt
    assert "text_markdown" in prompt
    assert "reason" in prompt
    assert "summary" in prompt


def test_v3_worker_instructions_require_forward_route_or_terminal_closure() -> None:
    instructions = Path("config/prompts/worker/instructions.xml").read_text(encoding="utf-8")
    safe_outputs = Path("config/prompts/worker/safe-outputs.xml").read_text(encoding="utf-8")
    normalized_instructions = " ".join(instructions.split())
    normalized_safe_outputs = " ".join(safe_outputs.split())

    assert "Do not leave work silently parked with yourself" in instructions
    assert "All agents share these standing operating instructions" in instructions
    assert "common baseline safe-output tool set" in normalized_instructions
    assert "follow the shared instructions" in normalized_instructions
    assert "explicitly record that this assignment is terminal/end-of-flow" in instructions
    assert "create the next handoff through safe-output tools before finishing" in instructions
    assert "Project Manager for governance/progression" in normalized_instructions
    assert "Delivery Manager for delivery coordination" in normalized_instructions
    assert "agent.delegate" in normalized_instructions
    assert "A reply alone is not enough for non-terminal work" in normalized_instructions
    assert "one of the safe-output calls must establish who owns the next step" in normalized_safe_outputs


def test_v3_safe_output_prompt_has_operational_debug_playbook() -> None:
    safe_outputs = Path("config/prompts/worker/safe-outputs.xml").read_text(encoding="utf-8")
    contract = Path("config/prompts/worker/codex-tool-contract.md").read_text(encoding="utf-8")
    normalized_safe_outputs = " ".join(safe_outputs.split())
    normalized_contract = " ".join(contract.split())

    assert "Operational/status/debug requests have a required safe-output playbook" in safe_outputs
    assert "runtime.status.inspect as the DO safe-output" in normalized_safe_outputs
    assert "runtime.message_journal.inspect when a message id" in normalized_safe_outputs
    assert "runtime.broker.inspect when inbox" in normalized_safe_outputs
    assert "runtime.broker.retry_dead_letter" in normalized_safe_outputs
    assert "concrete retry reason" in normalized_safe_outputs
    assert "do not create tracked work" in normalized_safe_outputs
    assert "If inspection cannot be performed, call report.incomplete" in normalized_safe_outputs
    assert "Finish with status.reply using `text_markdown`" in normalized_safe_outputs
    assert "Operational/status/debug requests still require tools" in contract
    assert "Non-JSON stdout is invalid" in contract
    assert "report.incomplete` is both the recorded DO outcome and the REPLY" in normalized_safe_outputs
    assert "Human-facing REPLY tools are terminal" in normalized_contract
    assert "Agent-to-agent tools" in normalized_safe_outputs


def test_v3_system_prompt_restricts_broad_filesystem_searches() -> None:
    system_prompt = Path("config/prompts/worker/system-security.xml").read_text(encoding="utf-8")
    normalized = " ".join(system_prompt.split())

    assert "search only the assigned source repository" in normalized
    assert "Do not recursively search `/mesh`" in normalized
    assert "runtime state folders" in normalized
    assert "mounted credential folders" in normalized


def test_v3_role_templates_do_not_reference_removed_safe_output_tools() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in Path("config/roles").glob("*.yaml"))

    assert "release.record_no_deployment" not in combined
    assert "governance.record-exception" not in combined
    assert "release.deploy" in combined
    assert "governance.record_exception" in combined
