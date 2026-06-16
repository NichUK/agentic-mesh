from agentic_mesh_v3.governance import GovernanceChecklist
from agentic_mesh_v3.reporting import AgentStatus
from agentic_mesh_v3.reporting import ArtifactStatus
from agentic_mesh_v3.reporting import BacklogItemStatus
from agentic_mesh_v3.reporting import GovernanceRecordStatus
from agentic_mesh_v3.reporting import ReleaseStatus
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
                "2026-06-15 10:03:00",
            ),
        ),
    )

    status_html = render_status_page(snapshot)
    agents_html = render_agents_page(snapshot)
    work_html = render_work_item_page(snapshot, "work-1")

    assert "Backlog / Queue" in status_html
    assert "Attention Needed" in status_html
    assert "Blocked Work" in status_html
    assert "Stale Work" in status_html
    assert "Governance Waits" in status_html
    assert "Active Work" in status_html
    assert '<a href="/work-item/work-1">work-1</a>' in status_html
    assert '<a href="/work-item/work-blocked">work-blocked</a>' in status_html
    assert "Fix deployment" in status_html
    assert "Resolve deployment credentials." in status_html
    assert '<a href="/work-item/work-stale">work-stale</a>' in status_html
    assert "work item has not changed for 7200 seconds" in status_html
    assert "Missing consultation evidence for `qa-engineer`." in status_html
    assert "agentic-mesh-dev.engineering.1" in agents_html
    assert "Dead Letters" in agents_html
    assert "Last Activity" in agents_html
    assert "2026-06-15 10:03:00" in agents_html
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


def test_status_page_shows_empty_blocked_work_state() -> None:
    html = render_status_page(ReportingSnapshot(project_id="agentic-mesh-dev"))

    assert "Blocked Work" in html
    assert "No blocked work recorded." in html


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


def test_work_item_detail_page_shows_explicit_raci_context() -> None:
    detail = WorkItemDetail(
        work_item_id="work-1",
        title="Governed release",
        description="Needs visible governance.",
        state="active",
        owner_role="release-manager",
        current_phase="deployment",
        next_action="Deploy configured target.",
        governance={
            "phase": "deployment",
            "accountable_role": "release-manager",
            "responsible_roles": ["platform-engineer", "engineering"],
            "consulted_roles": ["qa-engineer", "security-architect"],
            "informed_roles": ["project-manager", "product-manager"],
            "sponsor_decision_points": ["release-approval"],
            "required_evidence": ["release-record.md", "smoke-test.md"],
        },
    )

    html = render_work_item_detail_page(detail, "work-1")

    assert "<h2>RACI</h2>" in html
    assert "Accountable" in html
    assert "release-manager" in html
    assert "Responsible" in html
    assert "platform-engineer" in html
    assert "Consulted" in html
    assert "qa-engineer" in html
    assert "Informed" in html
    assert "project-manager" in html
    assert "Sponsor decision points" in html
    assert "release-approval" in html
    assert "Required evidence before handoff" in html
    assert "smoke-test.md" in html


def test_work_item_detail_page_shows_consultations_and_informed_updates() -> None:
    detail = WorkItemDetail(
        work_item_id="work-1",
        title="Governed work",
        description="Needs consultation evidence.",
        state="active",
        owner_role="solution-architect",
        current_phase="system-design",
        next_action="Resolve consultation.",
        governance={},
        governance_records=(
            GovernanceRecordStatus(
                record_id="governance-1",
                record_type="consult.request",
                role_instance_id="agentic-mesh-dev.solution-architect.1",
                target_ref="security-architect",
                summary="Please review the security architecture assumptions.",
                status="requested",
            ),
            GovernanceRecordStatus(
                record_id="governance-2",
                record_type="informed.update",
                role_instance_id="agentic-mesh-dev.solution-architect.1",
                target_ref="project-manager",
                summary="System design moved to security review.",
                status="sent",
            ),
        ),
    )

    html = render_work_item_detail_page(detail, "work-1")

    assert "<h2>Consultations</h2>" in html
    assert "security-architect" in html
    assert "Please review the security architecture assumptions." in html
    assert "<h2>Informed Updates</h2>" in html
    assert "project-manager" in html
    assert "System design moved to security review." in html


def test_work_item_detail_page_shows_release_metadata() -> None:
    detail = WorkItemDetail(
        work_item_id="work-1",
        title="Release runtime",
        description="Needs release evidence.",
        state="released",
        owner_role="release-manager",
        current_phase="deployment",
        next_action="Close release.",
        governance={},
        releases=(
            ReleaseStatus(
                release_id="release-1",
                status="deployed",
                scope="Runtime release",
                deployment_result="deployment ok",
                rollback_plan="Restore previous image.",
                residual_risks="Regression suite still pending.",
                version_ref="commit:abc123",
                approval_ref="approval-release-1",
                smoke_evidence="GET /healthz passed.",
                closure_state="release_disposition_recorded",
            ),
        ),
    )

    html = render_work_item_detail_page(detail, "work-1")

    assert "Version" in html
    assert "commit:abc123" in html
    assert "Approval" in html
    assert "approval-release-1" in html
    assert "Smoke: GET /healthz passed." in html
    assert "release_disposition_recorded" in html


def test_work_item_detail_page_shows_document_framework_artifact_paths() -> None:
    detail = WorkItemDetail(
        work_item_id="work-1",
        title="Documented work",
        description="Needs artifact visibility.",
        state="active",
        owner_role="product-manager",
        current_phase="shaping",
        next_action="Review product definition.",
        governance={},
        artifacts=(
            ArtifactStatus(
                filename="020-product-definition.md",
                title="Product definition",
                relative_path="work-items/work-1/020-product-definition.md",
                document_type="product_definition",
                status="published",
                created_by_role="product-manager",
            ),
            ArtifactStatus(
                filename="sketch.png",
                title="Sketch",
                relative_path="work-items/work-1/sketch.png",
                document_type="artifact",
                status="published",
                created_by_role="ux-designer",
                url="https://example.test/documents/work-items/work-1/sketch.png",
            ),
        ),
    )

    html = render_work_item_detail_page(detail, "work-1", document_framework_id="togaf-sdlc-v1")

    assert "Document framework" in html
    assert "togaf-sdlc-v1" in html
    assert "Framework Path" in html
    assert "work-items/work-1/020-product-definition.md" in html
    assert "/artifact-viewer/work-1/020-product-definition.md" in html
    assert "https://example.test/documents/work-items/work-1/sketch.png" in html
    assert "Flexible supporting artifact" in html
