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
        "handoff.require",
        "stakeholder.ask_question",
        "approval.request",
        "release.deploy",
        "release.close",
        "decision.record",
        "risk.register",
        "blocker.raise",
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
    assert "explicitly record that this assignment is terminal/end-of-flow" in instructions
    assert "create the next handoff through safe-output tools before finishing" in instructions
    assert "Project Manager for governance/progression" in normalized_instructions
    assert "Delivery Manager for delivery coordination" in normalized_instructions
    assert "A reply alone is not enough for non-terminal work" in normalized_instructions
    assert "one of the safe-output calls must establish who owns the next step" in normalized_safe_outputs


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
