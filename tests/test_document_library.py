from pathlib import Path
from unittest.mock import patch

from agentic_mesh.config import load_mesh_config
from agentic_mesh.document_library import DocumentRecord
from agentic_mesh.document_library import GitFilesystemDocumentLibraryAdapter
from agentic_mesh.document_library import ValidationFinding
from agentic_mesh.document_library import build_document_manifest
from agentic_mesh.document_library import classify_backend_url
from agentic_mesh.document_library import migration_dry_run
from agentic_mesh.document_library import redact_source_provenance
from agentic_mesh.document_library import render_flow_markdown_export
from agentic_mesh.document_library import render_flow_mermaid
from agentic_mesh.document_library import render_migration_dry_run
from agentic_mesh.document_library import render_validation_findings
from agentic_mesh.document_library import resolve_document_library_root
from agentic_mesh.document_library import validate_document_library
from agentic_mesh.document_library import write_document_manifest
from agentic_mesh.models import FlowGate
from agentic_mesh.models import FlowHandoff
from agentic_mesh.models import FlowState
from agentic_mesh.models import SdlcFlow


def test_document_library_root_resolves_independently_from_workspace() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    workspace_root = Path.cwd() / "examples" / "projects" / "agentic-mesh-dev"

    assert resolve_document_library_root(
        workspace_root,
        mesh_config.project.document_library,
    ) == Path.cwd()


def test_build_document_manifest_includes_plans_meshes_and_memory() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    manifest = build_document_manifest(mesh_config.project)
    documents = {doc["path"]: doc for doc in manifest["documents"]}

    assert manifest["document_library"]["structure_policy"] == "togaf-sdlc-v1"
    assert manifest["role_memory"]["provenance_required"] is True
    assert sorted(manifest["meshes"]) == ["governance", "sdlc"]
    implementation_plan = documents[
        "work-items/{work_item_id}/80-implementation-plan.md"
    ]
    assert implementation_plan["document_kind"] == "work_item_template"
    assert "implementation_planning" in implementation_plan["lifecycle_states"]
    assert "evidence" in implementation_plan["required_sections"]
    assert implementation_plan["review_status"] == "configured_only"
    assert (
        implementation_plan["review_status_source"]["authority"]
        == "not_gate_authoritative"
    )
    assert implementation_plan["migration"]["target_path"].endswith(
        "/800-implementation-plan.md"
    )

    quality_plan = documents["work-items/{work_item_id}/90-quality-plan.md"]
    assert quality_plan["document_kind"] == "work_item_template"
    assert "quality_planning" in quality_plan["lifecycle_states"]
    assert manifest["schema_version"] == "document-library-manifest-v1"


def test_write_document_manifest_uses_configured_index_path(tmp_path) -> None:
    mesh_config = load_mesh_config(
        Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
    )

    manifest_path = write_document_manifest(tmp_path, mesh_config.project)

    assert manifest_path == tmp_path / "docs" / "00-index" / (
        "document-library-manifest.json"
    )
    assert manifest_path.exists()


def test_document_record_redacts_unclassified_backend_url() -> None:
    record = DocumentRecord(
        path="docs/example.md",
        document_kind="durable",
        owner_role="engineering",
        accountability="owner",
        backend_url="https://tenant.example.com/private/doc?sig=secret",
    )

    manifest = {"documents": [record]}
    rendered = render_validation_findings(
        [
            ValidationFinding(
                severity="error",
                reason_code="not-a-real-code",
                required_action="Do not echo https://tenant.example.com/private/doc?sig=secret",
                detail="credential_ref=live-secret",
            )
        ],
        output_format="json",
    )

    assert classify_backend_url(record.backend_url) == "support_only"
    assert "not-a-real-code" not in rendered
    assert "unknown" in rendered
    assert "tenant.example.com" not in rendered
    assert "credential_ref" not in rendered
    assert manifest["documents"][0].backend_url is not None


def test_git_filesystem_adapter_confines_paths_and_versions(tmp_path) -> None:
    adapter = GitFilesystemDocumentLibraryAdapter(tmp_path)

    written = adapter.write_text("docs/example.md", "hello")

    assert written == tmp_path / "docs" / "example.md"
    assert adapter.read_text("docs/example.md") == "hello\n"
    assert adapter.stat("docs/example.md")["exists"] is True
    assert adapter.current_version("docs/example.md").startswith("sha256:")
    assert adapter.resolve_backend_url("docs/example.md") is None


def test_git_filesystem_adapter_rejects_unsafe_paths_before_write(
    tmp_path: Path,
) -> None:
    adapter = GitFilesystemDocumentLibraryAdapter(tmp_path / "library")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "work-items").mkdir()
    (tmp_path / "library" / "work-items" / "work-link").symlink_to(outside)

    for bad_path in [
        "",
        "/tmp/example.md",
        "../example.md",
        "docs/../../example.md",
        "C:/tmp/example.md",
        r"C:\tmp\example.md",
        "//server/share/example.md",
        r"\\server\share\example.md",
        "work-items/work-link/example.md",
    ]:
        try:
            adapter.write_text(bad_path, "unsafe")
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe path {bad_path}")

    assert not (outside / "example.md").exists()


def test_write_document_manifest_is_atomic_on_replace_failure(tmp_path) -> None:
    mesh_config = load_mesh_config(
        Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
    )
    first = write_document_manifest(tmp_path, mesh_config.project)
    previous = first.read_text(encoding="utf-8")

    with patch("agentic_mesh.document_library.os.replace", side_effect=OSError("boom")):
        try:
            write_document_manifest(tmp_path, mesh_config.project)
        except OSError:
            pass
        else:
            raise AssertionError("replace failure was not propagated")

    assert first.read_text(encoding="utf-8") == previous


def test_validation_reports_stable_reason_codes_and_redacts_detail(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work-items" / "work-123"
    work_dir.mkdir(parents=True)
    (work_dir / "10-business-brief.md").write_text(
        "Review status: `approved`\n\n## Review Log\n\napproved\n",
        encoding="utf-8",
    )
    (work_dir / "00-index.md").write_text(
        "# Work Item Index: fixture\n\nsecret_ref=live-value\n",
        encoding="utf-8",
    )

    findings = validate_document_library(tmp_path, work_item_id="work-123")
    rendered_json = render_validation_findings(findings, output_format="json")
    rendered_text = render_validation_findings(findings)

    assert {finding.reason_code for finding in findings} >= {
        "redaction_violation",
        "review_status_ambiguous",
    }
    assert "secret_ref" not in rendered_json
    assert "live-value" not in rendered_text


def test_migration_dry_run_reports_without_renaming(tmp_path: Path) -> None:
    work_dir = tmp_path / "work-items" / "work-123"
    work_dir.mkdir(parents=True)
    original = work_dir / "10-business-brief.md"
    original.write_text("# Brief\n", encoding="utf-8")
    (work_dir / "00-index.md").write_text("# Work Item Index\n", encoding="utf-8")

    report = migration_dry_run(tmp_path, work_item_id="work-123")
    rendered = render_migration_dry_run(report)

    assert report["dry_run"] is True
    assert report["renamed"] == 0
    assert report["records"][0]["current_path"] == (
        "work-items/work-123/10-business-brief.md"
    )
    assert all("00-index.md" not in record["current_path"] for record in report["records"])
    assert report["records"][0]["target_path"] == (
        "work-items/work-123/100-business-brief.md"
    )
    assert original.exists()
    assert not (work_dir / "100-business-brief.md").exists()
    assert "Filesystem changes: none" in rendered


def test_migration_dry_run_fails_closed_on_conflict(tmp_path: Path) -> None:
    work_dir = tmp_path / "work-items" / "work-123"
    work_dir.mkdir(parents=True)
    (work_dir / "10-business-brief.md").write_text("# Brief\n", encoding="utf-8")
    (work_dir / "100-business-brief.md").write_text("# Existing\n", encoding="utf-8")

    report = migration_dry_run(tmp_path, work_item_id="work-123")

    assert report["records"][0]["conflict_status"] == "conflict"
    assert report["findings"][0]["reason_code"] == "migration_conflict"


def test_source_provenance_redaction_allowlist() -> None:
    redacted = redact_source_provenance(
        {
            "work_item_id": "work-123",
            "work_item_type": "slice",
            "queue_item_id": "queue-1",
            "source_anchor_ref": "source:abc",
            "connector_type": "teams",
            "connector_instance_id": "teams-bot-listener",
            "source_scope": "all-agents",
            "display_label": "Nicholas in all-agents",
            "received_at": "2026-06-05T00:00:00+00:00",
            "raw_payload_path": "/mesh/state/raw/live.json",
            "tenant_id": "tenant-live",
            "secret_ref": "secret_ref=live",
        }
    )

    assert redacted == {
        "connector_instance_id": "teams-bot-listener",
        "connector_type": "teams",
        "display_label": "Nicholas in all-agents",
        "queue_item_id": "queue-1",
        "received_at": "2026-06-05T00:00:00+00:00",
        "source_anchor_ref": "source:abc",
        "source_scope": "all-agents",
        "work_item_id": "work-123",
        "work_item_type": "slice",
    }


def test_render_flow_mermaid_includes_planning_and_review_loops() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    mermaid = render_flow_mermaid(mesh_config.project.flow)

    assert "flowchart LR" in mermaid
    assert "implementation planning" in mermaid
    assert "quality planning" in mermaid
    assert "plan review" in mermaid


def test_render_flow_markdown_export_covers_active_sdlc_contract() -> None:
    mesh_config = load_mesh_config(Path.cwd())

    markdown = render_flow_markdown_export(
        mesh_config.project.flow,
        project_id=mesh_config.project.project_id,
        project_file="examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
        flow_source="config/flows/sdlc.yaml",
        work_item_id="work-28575311c3b44dfda88038387df705ce",
        queue_item_id="queue-b24e7893f5d2469c972e7fa8f0c187f4",
        generated_at="2026-06-05T00:00:00+00:00",
        command="python -m agentic_mesh.cli flow-export --format markdown",
    )

    assert markdown.startswith("# Lifecycle Flow\n")
    assert "- Project: `agentic-mesh-dev`" in markdown
    assert "- Flow: `agentic-mesh-sdlc-v0`" in markdown
    assert "Consult routes are omitted from the V0 default export" in markdown
    assert "| State | Owner role | Gates | Forward handoff | Terminal |" in markdown
    assert "```mermaid\nflowchart TD" in markdown
    for state_id, state in mesh_config.project.flow.states.items():
        assert state_id.replace("_", " ").replace("-", " ") in markdown
        assert state.owner_role in markdown
        for gate in state.gates:
            assert gate.gate_id.replace("_", " ") in markdown
            assert gate.type.replace("_", " ") in markdown
        for handoff in state.handoffs.values():
            assert (
                f"{handoff.status} -&gt; {handoff.target_state.replace('_', ' ')}"
                in markdown
            )
    assert "release review (terminal)" in markdown
    assert r"| release review \(terminal\) | release-manager |" in markdown
    assert "quality_context" not in markdown
    assert "product_scope" not in markdown
    assert "security_context" not in markdown


def test_render_flow_markdown_export_uses_data_driven_terminal_detection() -> None:
    flow = SdlcFlow(
        flow_id="custom-flow",
        entry_state="start_here",
        work_item_types=["slice"],
        states={
            "start_here": FlowState(
                state_id="start_here",
                owner_role="role-a",
                purpose="Start.",
                artifact_path="work-items/{work_item_id}/start.md",
                handoffs={
                    "approved": FlowHandoff(
                        status="approved",
                        target_state="done_elsewhere",
                        target_role="role-b",
                        message_type="custom.done",
                    )
                },
                gates=[
                    FlowGate(
                        gate_id="start_gate",
                        type="document_owner_review",
                    )
                ],
            ),
            "done_elsewhere": FlowState(
                state_id="done_elsewhere",
                owner_role="role-b",
                purpose="Finish.",
                artifact_path="work-items/{work_item_id}/done.md",
                handoffs={},
            ),
        },
    )

    markdown = render_flow_markdown_export(
        flow,
        project_id="custom-project",
        project_file="project.yaml",
        flow_source="custom-flow.yaml",
        work_item_id="work-custom",
        queue_item_id=None,
        generated_at="2026-06-05T00:00:00+00:00",
        command="python -m agentic_mesh.cli flow-export --format markdown",
    )

    assert "done elsewhere (terminal)" in markdown
    assert "release review" not in markdown
    assert "approved -&gt; done elsewhere" in markdown


def test_render_flow_markdown_export_escapes_hostile_labels() -> None:
    hostile = 'bad|state<script onclick="x">`[link](javascript:alert(1))'
    flow = SdlcFlow(
        flow_id="hostile<script>",
        entry_state=hostile,
        work_item_types=["slice"],
        states={
            hostile: FlowState(
                state_id=hostile,
                owner_role='role|owner<img src=x onerror="x">',
                purpose="Hostile.",
                artifact_path="work-items/{work_item_id}/hostile.md",
                handoffs={},
                gates=[
                    FlowGate(
                        gate_id='gate|id\n<script>alert(1)</script>',
                        type='review"];\nmalicious-->x',
                    )
                ],
            )
        },
    )

    markdown = render_flow_markdown_export(
        flow,
        project_id="custom-project",
        project_file="project.yaml",
        flow_source="custom-flow.yaml",
        work_item_id="work-custom",
        queue_item_id=None,
        generated_at="2026-06-05T00:00:00+00:00",
        command="python -m agentic_mesh.cli flow-export --format markdown",
    )

    assert "<script" not in markdown
    assert "onclick=" not in markdown
    assert "onerror=" not in markdown
    assert "javascript:alert" in markdown
    assert r"bad\|state" in markdown
    assert "&lt;script" in markdown
    assert "&#96;" in markdown
    assert "malicious-->" not in markdown
