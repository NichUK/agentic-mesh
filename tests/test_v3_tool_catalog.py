from agentic_mesh_v3.tool_catalog import tool_catalog_for_role


def test_tool_catalog_marks_role_scoped_permissions_and_terminal_tools() -> None:
    product_tools = {entry.tool_name: entry for entry in tool_catalog_for_role("product-manager")}

    assert product_tools["status.reply"].allowed is True
    assert product_tools["status.reply"].terminal is True
    assert product_tools["status.reply"].required_fields == ("text_markdown",)
    assert product_tools["noop"].terminal is True
    assert product_tools["noop"].required_fields == ("reason",)
    assert product_tools["status.complete"].terminal is True
    assert product_tools["status.complete"].required_fields == ("summary",)
    assert product_tools["report.incomplete"].terminal is True
    assert product_tools["report.incomplete"].required_fields == ("reason",)
    assert product_tools["artifact.link"].allowed is True
    assert product_tools["artifact.link"].description
    assert product_tools["blocker.raise"].allowed is True
    assert product_tools["blocker.raise"].required_fields == ("work_item_id", "summary", "next_action")
    assert product_tools["consult.request"].required_fields == ("work_item_id", "target_role", "question")
    assert product_tools["decision.record"].allowed is True
    assert product_tools["decision.record"].required_fields == ("work_item_id", "summary")
    assert product_tools["risk.register"].allowed is True
    assert product_tools["risk.register"].required_fields == ("work_item_id", "summary")
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
    assert product_tools["release.deploy"].allowed is False
    assert product_tools["release.deploy"].description


def test_tool_catalog_allows_release_manager_release_tools() -> None:
    release_tools = {entry.tool_name: entry for entry in tool_catalog_for_role("release-manager")}

    assert release_tools["release.deploy"].allowed is True
    assert release_tools["release.close"].allowed is True
