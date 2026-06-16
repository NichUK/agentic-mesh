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


def test_v3_role_templates_do_not_reference_removed_safe_output_tools() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in Path("config/roles").glob("*.yaml"))

    assert "release.record_no_deployment" not in combined
    assert "governance.record-exception" not in combined
    assert "release.deploy" in combined
    assert "governance.record_exception" in combined
