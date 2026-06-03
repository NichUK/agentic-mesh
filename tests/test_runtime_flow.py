from pathlib import Path

from agentic_mesh.artifacts import ArtifactStore
from agentic_mesh.config import load_mesh_config
from agentic_mesh.journal import EventJournal
from agentic_mesh.models import Message
from agentic_mesh.storage import FileConnectorOutbox
from agentic_mesh.runtime import AgentRuntime
from agentic_mesh.storage import FileMessageStore
from agentic_mesh.workers import StubCodexWorkerAdapter


def test_project_configured_sdlc_flow_reaches_engineering(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        StubCodexWorkerAdapter(),
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.intake",
            payload={
                "title": "Local Runtime Skeleton",
                "summary": "Prove the local spine.",
                "work_item_id": "slice-local-runtime",
                "work_item_type": "slice",
                "lifecycle_state": "business_analysis",
            },
            source="test",
        )
    )

    for role_id in [
        "business-analyst",
        "product-manager",
        "ux-designer",
        "enterprise-architect",
        "solution-architect",
        "security-architect",
        "platform-engineer",
        "engineering",
    ]:
        instance_id = (
            "agentic-mesh-dev.engineering.1"
            if role_id == "engineering"
            else f"agentic-mesh-dev.{role_id}.1"
        )
        assert runtime.run_once(instance_id, mesh_config.instances[instance_id])

    assert message_store.pending_count("engineering") == 0
    assert message_store.pending_count("qa-engineer") == 1

    business_briefs = tmp_path / "workspace" / "docs" / "business" / "briefs.md"
    stories = tmp_path / "workspace" / "docs" / "product" / "stories.md"
    ux_notes = tmp_path / "workspace" / "docs" / "ux" / "design-notes.md"
    enterprise_notes = tmp_path / "workspace" / "docs" / "architecture" / "enterprise-notes.md"
    solution_notes = tmp_path / "workspace" / "docs" / "architecture" / "solution-notes.md"
    security_notes = tmp_path / "workspace" / "docs" / "security" / "security-decisions.md"
    platform_notes = tmp_path / "workspace" / "docs" / "platform" / "operations.md"
    engineering_log = tmp_path / "workspace" / "docs" / "engineering" / "implementation-log.md"
    assert "business_analysis" in business_briefs.read_text(encoding="utf-8")
    assert "Local Runtime Skeleton" in stories.read_text(encoding="utf-8")
    assert "experience_design" in ux_notes.read_text(encoding="utf-8")
    assert "enterprise_alignment" in enterprise_notes.read_text(encoding="utf-8")
    assert "solution_design" in solution_notes.read_text(encoding="utf-8")
    assert "security_review" in security_notes.read_text(encoding="utf-8")
    assert "platform_readiness" in platform_notes.read_text(encoding="utf-8")
    assert "agentic-mesh-dev.engineering.1" in engineering_log.read_text(encoding="utf-8")

    event_types = [event["event_type"] for event in journal.read_all()]
    assert "message_accepted" in event_types
    assert "work_claimed" in event_types
    assert "agent_run_started" in event_types
    assert "documentation_updated" in event_types
    assert "handoff_emitted" in event_types
    assert "message_routed" in event_types
    assert "work_completed" in event_types
    assert "local_trace_span" in event_types


def test_parallel_work_items_keep_independent_lifecycle_context(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(tmp_path / "state", mesh_config.project.project_id, journal)
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        StubCodexWorkerAdapter(),
    )

    for work_item_id, work_item_type in [
        ("slice-runtime-loop", "slice"),
        ("spike-otel-provider", "spike"),
    ]:
        message_store.enqueue(
            Message.create(
                role_id="business-analyst",
                message_type="sdlc.intake",
                payload={
                    "title": work_item_id,
                    "summary": f"Parallel {work_item_type} work.",
                    "work_item_id": work_item_id,
                    "work_item_type": work_item_type,
                    "lifecycle_state": "business_analysis",
                },
                source="test",
            )
        )

    assert runtime.run_once(
        "agentic-mesh-dev.business-analyst.1",
        mesh_config.instances["agentic-mesh-dev.business-analyst.1"],
    )
    assert runtime.run_once(
        "agentic-mesh-dev.business-analyst.1",
        mesh_config.instances["agentic-mesh-dev.business-analyst.1"],
    )

    assert message_store.pending_count("product-manager") == 2
    handoff_events = [
        event
        for event in journal.read_all()
        if event["event_type"] == "handoff_emitted"
    ]
    assert {event["work_item_id"] for event in handoff_events} == {
        "slice-runtime-loop",
        "spike-otel-provider",
    }
    assert all(
        event["target_lifecycle_state"] == "product_definition"
        for event in handoff_events
    )


def test_human_response_gate_queues_connector_message(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(
        tmp_path / "state",
        mesh_config.project.project_id,
        journal,
    )
    connector_outbox = FileConnectorOutbox(
        tmp_path / "state",
        mesh_config.project.project_id,
        journal,
    )
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        StubCodexWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )

    message_store.enqueue(
        Message.create(
            role_id="release-manager",
            message_type="sdlc.release_review",
            payload={
                "title": "Release local runtime",
                "summary": "Confirm release readiness.",
                "work_item_id": "slice-release",
                "work_item_type": "slice",
                "lifecycle_state": "release_review",
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    assert connector_outbox.pending_count("approvals") == 1
    request = connector_outbox.claim_next("approvals", "local-connector")
    assert request is not None
    assert request.type == "human_response.requested"
    assert request.payload["gate_id"] == "release_decision_response"
    assert request.payload["response_type"] == "approve_not_approve"
    assert request.payload["response_template"]["value_type"] == "string"

    events = journal.read_all()
    assert "human_response_requested" in [
        event["event_type"]
        for event in events
    ]
    completed = [
        event
        for event in events
        if event["event_type"] == "work_completed"
    ][0]
    assert completed["status"] == "waiting_for_human_response"


def test_sdlc_handoff_queues_connector_message(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(
        tmp_path / "state",
        mesh_config.project.project_id,
        journal,
    )
    connector_outbox = FileConnectorOutbox(
        tmp_path / "state",
        mesh_config.project.project_id,
        journal,
    )
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        StubCodexWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )

    message_store.enqueue(
        Message.create(
            role_id="business-analyst",
            message_type="sdlc.intake",
            payload={
                "title": "Teams lifecycle smoke",
                "summary": "Prove handoff messaging reaches Teams.",
                "work_item_id": "slice-teams-smoke",
                "work_item_type": "slice",
                "lifecycle_state": "business_analysis",
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.business-analyst.1",
        mesh_config.instances["agentic-mesh-dev.business-analyst.1"],
    )

    assert connector_outbox.pending_count("product") == 1
    handoff = connector_outbox.claim_next("product", "test-connector")
    assert handoff is not None
    assert handoff.type == "sdlc.handoff"
    assert handoff.payload["source_role"] == "business-analyst"
    assert handoff.payload["target_role"] == "product-manager"
    assert handoff.payload["target_lifecycle_state"] == "product_definition"


def test_human_response_received_completes_release_review(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    journal = EventJournal(tmp_path / "state", mesh_config.project.project_id)
    message_store = FileMessageStore(
        tmp_path / "state",
        mesh_config.project.project_id,
        journal,
    )
    connector_outbox = FileConnectorOutbox(
        tmp_path / "state",
        mesh_config.project.project_id,
        journal,
    )
    artifacts = ArtifactStore(tmp_path / "workspace", mesh_config.project.project_id, journal)
    runtime = AgentRuntime(
        message_store,
        artifacts,
        journal,
        mesh_config.project,
        StubCodexWorkerAdapter(),
        connector_outbox=connector_outbox,
        response_types=mesh_config.response_types,
    )

    message_store.enqueue(
        Message.create(
            role_id="release-manager",
            message_type="human_response.received",
            payload={
                "title": "Human response received",
                "summary": "Release sponsor approved.",
                "work_item_id": "slice-release",
                "work_item_type": "slice",
                "lifecycle_state": "release_review",
                "gate_id": "release_decision_response",
                "response_request_id": "human-response-1",
                "response_value": "approved",
            },
            source="test",
        )
    )

    assert runtime.run_once(
        "agentic-mesh-dev.release-manager.1",
        mesh_config.instances["agentic-mesh-dev.release-manager.1"],
    )

    assert connector_outbox.pending_count("approvals") == 0
    event_types = [event["event_type"] for event in journal.read_all()]
    assert "human_response_recorded" in event_types
    completed = [
        event
        for event in journal.read_all()
        if event["event_type"] == "work_completed"
    ][0]
    assert completed["status"] == "completed_after_human_response"
