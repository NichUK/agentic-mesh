from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.reporting import BacklogItemStatus
from agentic_mesh_v3.reporting import ReportingSnapshot
from agentic_mesh_v3.reporting import WorkItemStatus
from agentic_mesh_v3.reporting import artifact_viewer_path
from agentic_mesh_v3.reporting import render_agents_page
from agentic_mesh_v3.reporting import render_status_page
from agentic_mesh_v3.reporting import render_work_item_page
from agentic_mesh_v3.reporting import work_item_url


def test_reporting_pages_include_required_status_data() -> None:
    snapshot = ReportingSnapshot(
        project_id="agentic-mesh-dev",
        backlog=(BacklogItemStatus("queue-1", "Add status page", "queued", "project-manager", "work-1"),),
        work_items=(WorkItemStatus("work-1", "Add status page", "active", "engineering", "Implement", 2),),
        agents=(AgentStatus("agentic-mesh-dev.engineering.1", "running", "2026-06-15T10:00:00Z", "work-1", 3, 1),),
    )

    status_html = render_status_page(snapshot)
    agents_html = render_agents_page(snapshot)
    work_html = render_work_item_page(snapshot, "work-1")

    assert "Backlog / Queue" in status_html
    assert "Active Work" in status_html
    assert '<a href="/work-item/work-1">work-1</a>' in status_html
    assert "agentic-mesh-dev.engineering.1" in agents_html
    assert "Dead Letters" in agents_html
    assert "<td>1</td>" in agents_html
    assert "<strong>Work item:</strong> work-1" in work_html


def test_artifact_viewer_path_is_work_item_scoped() -> None:
    assert artifact_viewer_path("work-1", "020-product-definition.md") == "work-items/work-1/020-product-definition.md"


def test_work_item_url_escapes_work_item_id() -> None:
    assert work_item_url("work/1") == "/work-item/work%2F1"
