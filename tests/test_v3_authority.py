from pathlib import Path

from agentic_mesh_v3.authority import ToolAuthorityPolicy
from agentic_mesh_v3.authority import role_from_instance
from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.deployment import NoDeploymentDisposition
from agentic_mesh_v3.tools import V3ToolService


def test_role_from_instance_extracts_role_id() -> None:
    assert role_from_instance("agentic-mesh-dev.release-manager.1") == "release-manager"


def test_authority_allows_release_manager_to_deploy(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        db.upsert_work_item(
            work_item_id="work-1",
            title="Release",
            description="Release.",
            state="release_review",
            owner_role="release-manager",
        )
        V3ToolService(
            db,
            deployment_targets={
                "planning-only": NoDeploymentDisposition(target_id="planning-only", reason="No deployment.")
            },
        ).call(
            role_instance_id="agentic-mesh-dev.release-manager.1",
            tool_name="release.deploy",
            payload={"work_item_id": "work-1", "target_id": "planning-only", "scope": "No deployment."},
        )
        calls = db.list_tool_calls()
    finally:
        db.close()

    assert calls[0]["tool_name"] == "release.deploy"


def test_authority_rejects_product_manager_release_deploy(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        try:
            V3ToolService(db).call(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                tool_name="release.deploy",
                payload={"work_item_id": "work-1", "target_id": "local-smoke", "scope": "Nope"},
            )
        except PermissionError as exc:
            assert "product-manager" in str(exc)
            assert "release.deploy" in str(exc)
        else:
            raise AssertionError("product-manager should not deploy releases")
    finally:
        db.close()


def test_authority_allows_all_roles_to_consult_and_handoff() -> None:
    policy = ToolAuthorityPolicy.default()

    assert "artifact.link" in policy.allowed_tools_for_role("business-analyst")
    assert "consult.request" in policy.allowed_tools_for_role("business-analyst")
    assert "handoff.require" in policy.allowed_tools_for_role("ux-designer")
