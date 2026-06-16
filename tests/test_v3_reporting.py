from agentic_mesh_v3.governance import GovernanceChecklist
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.reporting import BacklogItemStatus
from agentic_mesh_v3.reporting import ReportingSnapshot
from agentic_mesh_v3.reporting import WorkItemStatus
from agentic_mesh_v3.reporting import artifact_viewer_path
from agentic_mesh_v3.reporting import render_agents_page
from agentic_mesh_v3.reporting import render_status_page
from agentic_mesh_v3.reporting import render_work_item_detail_page
from agentic_mesh_v3.reporting import render_work_item_page
from agentic_mesh_v3.reporting import work_item_url
from agentic_mesh_v3.reporting import WorkItemDetail


def test_reporting_pages_include_required_status_data() -> None:
    snapshot = ReportingSnapshot(
        project_id="agentic-mesh-dev",
        backlog=(BacklogItemStatus("queue-1", "Add status page", "queued", "project-manager", "work-1"),),
        work_items=(
            WorkItemStatus("work-1", "Add status page", "active", "engineering", "Implement", 2),
            WorkItemStatus(
                "work-blocked",
                "Fix deployment",
                "blocked",
                "release-manager",
                "Resolve deployment credentials.",
                0,
                "2026-06-15 10:00:00",
                "work item is in blocked",
            ),
            WorkItemStatus(
                "work-stale",
                "Review stale item",
                "active",
                "project-manager",
                "Chase owner.",
                0,
                "2026-06-15 09:00:00",
                "work item has not changed for 7200 seconds",
            ),
        ),
        agents=(
            AgentStatus(
                "agentic-mesh-dev.engineering.1",
                "running",
                "2026-06-15T10:00:00Z",
                "work-1",
                3,
                1,
                ("Missing consultation evidence for `qa-engineer`.",),
                2,
                "2026-06-15 10:01:00",
                None,
                None,
                None,
                "wake",
                "pending inbox messages",
                "agentic-mesh-dev-engineering-1",
                0,
                True,
                "2026-06-15 10:02:00",
            ),
        ),
    )

    status_html = render_status_page(snapshot)
    agents_html = render_agents_page(snapshot)
    work_html = render_work_item_page(snapshot, "work-1")

    assert "Backlog / Queue" in status_html
    assert "Attention Needed" in status_html
    assert "Stale Work" in status_html
    assert "Governance Waits" in status_html
    assert "Active Work" in status_html
    assert '<a href="/work-item/work-1">work-1</a>' in status_html
    assert '<a href="/work-item/work-blocked">work-blocked</a>' in status_html
    assert "Resolve deployment credentials." in status_html
    assert '<a href="/work-item/work-stale">work-stale</a>' in status_html
    assert "work item has not changed for 7200 seconds" in status_html
    assert "Missing consultation evidence for `qa-engineer`." in status_html
    assert "agentic-mesh-dev.engineering.1" in agents_html
    assert "Dead Letters" in agents_html
    assert "Lifecycle" in agents_html
    assert "<strong>wake</strong>" in agents_html
    assert "pending inbox messages" in agents_html
    assert "Memory" in agents_html
    assert "2 entries" in agents_html
    assert "2026-06-15 10:01:00" in agents_html
    assert '<a href="/work-item/work-1">work-1</a>' in agents_html
    assert "<li>Missing consultation evidence for `qa-engineer`.</li>" in agents_html
    assert "<td>1</td>" in agents_html
    assert "<strong>Work item:</strong> work-1" in work_html


def test_artifact_viewer_path_is_work_item_scoped() -> None:
    assert artifact_viewer_path("work-1", "020-product-definition.md") == "work-items/work-1/020-product-definition.md"


def test_work_item_url_escapes_work_item_id() -> None:
    assert work_item_url("work/1") == "/work-item/work%2F1"


def test_work_item_detail_page_shows_missing_required_evidence() -> None:
    detail = WorkItemDetail(
        work_item_id="work-1",
        title="Governed work",
        description="Needs evidence.",
        state="active",
        owner_role="engineering",
        current_phase="development",
        next_action="Link evidence.",
        governance={},
        governance_checklist=GovernanceChecklist(missing_required_evidence=("100-implementation-log.md",)),
    )

    html = render_work_item_detail_page(detail, "work-1")

    assert "Required evidence" in html
    assert "100-implementation-log.md" in html
