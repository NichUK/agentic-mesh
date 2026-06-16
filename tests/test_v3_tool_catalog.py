from agentic_mesh_v3.tool_catalog import tool_catalog_for_role


def test_tool_catalog_marks_role_scoped_permissions_and_terminal_tools() -> None:
    product_tools = {entry.tool_name: entry for entry in tool_catalog_for_role("product-manager")}

    assert product_tools["status.reply"].allowed is True
    assert product_tools["status.reply"].terminal is True
    assert product_tools["backlog.upsert"].allowed is True
    assert product_tools["release.deploy"].allowed is False
    assert product_tools["release.deploy"].description


def test_tool_catalog_allows_release_manager_release_tools() -> None:
    release_tools = {entry.tool_name: entry for entry in tool_catalog_for_role("release-manager")}

    assert release_tools["release.deploy"].allowed is True
    assert release_tools["release.close"].allowed is True
