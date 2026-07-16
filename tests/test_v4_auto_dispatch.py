from __future__ import annotations

from agentic_mesh_v4.auto_dispatch import WorkItemDispatchContext
from agentic_mesh_v4.auto_dispatch import looks_actionable_next_action
from agentic_mesh_v4.auto_dispatch import resolve_auto_dispatch
from agentic_mesh_v4.auto_dispatch import without_self_dispatch_targets


def test_without_self_dispatch_targets_excludes_self_owner_handoff() -> None:
    resolution = resolve_auto_dispatch(
        payload={},
        work_item=WorkItemDispatchContext(
            work_item_id="work-self",
            state="implementation",
            owner_role="project-manager",
            next_action="review current status",
        ),
    )

    filtered = without_self_dispatch_targets(resolution=resolution, source_role="project-manager")

    assert not filtered.should_dispatch
    assert filtered.status == "excluded"
    assert filtered.source == "self_dispatch_exclusion"


def test_any_meaningful_nonterminal_next_action_requires_continuation() -> None:
    assert looks_actionable_next_action("Promotion remains blocked pending credentials.")
    assert looks_actionable_next_action("Await the separately authorized next stage.")
    assert not looks_actionable_next_action("No further action required.")
