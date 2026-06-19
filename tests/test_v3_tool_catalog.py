from agentic_mesh_v3.tool_catalog import tool_catalog_for_role


STARTER_ROLE_IDS = (
    "business-analyst",
    "delivery-manager",
    "engineering",
    "enterprise-architect",
    "platform-engineer",
    "product-manager",
    "project-manager",
    "prompt-engineer",
    "qa-engineer",
    "release-manager",
    "research-analyst",
    "security-architect",
    "solution-architect",
    "technical-writer",
    "ux-designer",
)


def _allowed_tool_names(role_id: str) -> set[str]:
    return {entry.tool_name for entry in tool_catalog_for_role(role_id) if entry.allowed}


def test_starter_roles_share_same_baseline_tools_except_release_manager_extras() -> None:
    product_baseline = _allowed_tool_names("product-manager")
    release_extras = {"release.close", "release.deploy", "release.record"}

    for role_id in STARTER_ROLE_IDS:
        allowed = _allowed_tool_names(role_id)
        if role_id == "release-manager":
            assert allowed == product_baseline | release_extras
        else:
            assert allowed == product_baseline


def test_tool_catalog_marks_role_scoped_permissions_and_terminal_tools() -> None:
    product_tools = {entry.tool_name: entry for entry in tool_catalog_for_role("product-manager")}

    assert product_tools["status.reply"].allowed is True
    assert product_tools["status.reply"].terminal is True
    assert product_tools["status.reply"].do_tool is False
    assert product_tools["status.reply"].reply_tool is True
    assert product_tools["status.reply"].required_fields == ("text_markdown",)
    assert product_tools["noop"].terminal is True
    assert product_tools["noop"].do_tool is True
    assert product_tools["noop"].reply_tool is False
    assert product_tools["noop"].required_fields == ("reason",)
    assert product_tools["status.complete"].terminal is True
    assert product_tools["status.complete"].do_tool is False
    assert product_tools["status.complete"].reply_tool is True
    assert product_tools["status.complete"].required_fields == ("summary",)
    assert product_tools["agent.delegate"].allowed is True
    assert product_tools["agent.delegate"].do_tool is True
    assert product_tools["agent.delegate"].required_fields == ("target_role", "task", "reason", "expected_output")
    assert product_tools["approval.request"].terminal is True
    assert product_tools["approval.request"].do_tool is True
    assert product_tools["approval.request"].reply_tool is True
    assert product_tools["report.incomplete"].terminal is True
    assert product_tools["report.incomplete"].do_tool is True
    assert product_tools["report.incomplete"].reply_tool is True
    assert product_tools["report.incomplete"].required_fields == ("reason",)
    assert product_tools["artifact.link"].allowed is True
    assert product_tools["artifact.link"].do_tool is True
    assert product_tools["artifact.link"].description
    assert product_tools["blocker.raise"].allowed is True
    assert product_tools["blocker.raise"].terminal is True
    assert product_tools["blocker.raise"].do_tool is True
    assert product_tools["blocker.raise"].reply_tool is True
    assert product_tools["blocker.raise"].required_fields == ("work_item_id", "summary", "next_action")
    assert product_tools["consult.request"].required_fields == ("work_item_id", "target_role", "question")
    assert product_tools["conversation.compact_context"].allowed is True
    assert product_tools["conversation.compact_context"].do_tool is True
    assert product_tools["conversation.compact_context"].required_fields == (
        "conversation_ref",
        "summary",
        "source_message_ids",
        "visibility",
    )
    assert product_tools["decision.record"].allowed is True
    assert product_tools["decision.record"].required_fields == ("work_item_id", "summary")
    assert product_tools["risk.register"].allowed is True
    assert product_tools["risk.register"].required_fields == ("work_item_id", "summary")
    assert product_tools["runtime.broker.inspect"].allowed is True
    assert product_tools["runtime.broker.inspect"].do_tool is True
    assert product_tools["runtime.broker.inspect"].required_fields == ("reason",)
    assert product_tools["runtime.lifecycle.request"].allowed is True
    assert product_tools["runtime.lifecycle.request"].do_tool is True
    assert product_tools["runtime.lifecycle.request"].required_fields == ("reason",)
    assert product_tools["runtime.message_journal.inspect"].allowed is True
    assert product_tools["runtime.message_journal.inspect"].do_tool is True
    assert product_tools["runtime.message_journal.inspect"].required_fields == ("reason",)
    assert product_tools["runtime.status.inspect"].allowed is True
    assert product_tools["runtime.status.inspect"].do_tool is True
    assert product_tools["runtime.status.inspect"].required_fields == ("reason",)
    assert product_tools["runtime.sweep.request"].allowed is True
    assert product_tools["runtime.sweep.request"].do_tool is True
    assert product_tools["runtime.sweep.request"].required_fields == ("reason",)
    assert "required_fields" in product_tools["consult.request"].to_dict()
    assert product_tools["handoff.require"].required_fields == (
        "work_item_id",
        "target_role",
        "phase",
        "accountable_role",
        "required_next_action",
        "acceptance_criteria",
        "evidence_requirements",
        "artifact_links",
        "open_decisions",
        "open_risks",
        "consulted_roles",
        "informed_roles",
        "stakeholder_follow_up",
    )
    assert product_tools["backlog.upsert"].allowed is True
    assert product_tools["work_item.reopen"].allowed is True
    assert product_tools["work_item.reopen"].required_fields == ("work_item_id", "state", "reason")
    assert product_tools["release.deploy"].allowed is False
    assert product_tools["release.deploy"].do_tool is True
    assert product_tools["release.deploy"].description
    catalog_entry = product_tools["status.reply"].to_dict()
    assert catalog_entry["do_tool"] is False
    assert catalog_entry["reply_tool"] is True


def test_tool_catalog_allows_release_manager_release_tools() -> None:
    release_tools = {entry.tool_name: entry for entry in tool_catalog_for_role("release-manager")}

    assert release_tools["release.deploy"].allowed is True
    assert release_tools["release.deploy"].required_fields == (
        "work_item_id",
        "target_id",
        "version_ref",
        "approval_ref",
    )
    assert release_tools["release.record"].required_fields == (
        "work_item_id",
        "scope",
        "version_ref",
        "approval_ref",
        "deployment_result",
        "smoke_evidence",
        "rollback_plan",
    )
    assert release_tools["release.close"].allowed is True
    assert release_tools["work_item.reopen"].allowed is True
