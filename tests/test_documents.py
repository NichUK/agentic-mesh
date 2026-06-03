from pathlib import Path

from agentic_mesh.config import load_mesh_config
from agentic_mesh.documents import DocumentLifecyclePlanner


def test_document_contribution_requests_owner_review_for_other_roles() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    planner = DocumentLifecyclePlanner(mesh_config.project.document_accountabilities)

    events = planner.contribution_events(
        "docs/product/stories.md",
        contributor_role="ux-designer",
    )

    assert [event.event_type for event in events] == [
        "document.contribution_added",
        "document.owner_review_requested",
    ]
    assert events[1].owner_role == "product-manager"


def test_document_owner_contribution_does_not_request_self_review() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    planner = DocumentLifecyclePlanner(mesh_config.project.document_accountabilities)

    events = planner.contribution_events(
        "docs/product/stories.md",
        contributor_role="product-manager",
    )

    assert [event.event_type for event in events] == ["document.contribution_added"]
